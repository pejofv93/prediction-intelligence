"""
Volcado de solo lectura de Firestore (default) a JSONL, con verificación.

Todas las lecturas van fijadas al mismo instante (readTime = arranque - 60 s), así que
el volcado es una foto coherente aunque los servicios sigan escribiendo. Firestore sin
PITR solo guarda versiones de la última hora: el volcado y la verificación deben
terminar en menos de ~55 minutos (159k docs tardan unos minutos).

Salida en OUT_DIR/<readTime>/:
  <coleccion>.jsonl   una línea por documento: {"id","name","createTime","updateTime","fields"}
                      con los valores en el formato tipado de la API REST (sin pérdida de tipos)
  manifest.json       readTime, y por colección: recuento por agregación, líneas, sha256 del
                      fichero, hash combinado de documentos, censo de tipos, subcolecciones
                      detectadas en la muestra y resultado de la relectura aleatoria

Verificaciones (si alguna falla, el manifest lo marca y el script sale con código 1):
  1. líneas del JSONL == count() agregado en el mismo readTime
  2. sin ids duplicados
  3. relectura aleatoria de SAMPLE docs por colección con GET en el mismo readTime: idénticos
  4. búsqueda de subcolecciones en SUB_SAMPLE docs por colección (avisa si hay; la copia
     oficial `gcloud firestore export` sí incluye cualquier subcolección)
  5. detector de claves sobre TODOS los documentos (nombres de campo y valores de texto).
     Si encuentra algo, el manifest lo marca y el script sale con código 2: el workflow no
     sube nada. Solo imprime colección, patrón, ruta del campo y recuento, NUNCA el valor
     (los logs de Actions de este repo son públicos).

Uso (solo lectura, no sube nada):
  local (Python 3.11 por Norton):
    set SSL_CERT_FILE=C:\\Users\\Usuario\\norton_ca.pem
    set FS_DUMP_ACCOUNT=pejocanal@gmail.com
    C:\\Python311\\python.exe -I scripts\\migration\\firestore_dump.py --out C:\\Users\\Usuario\\backups\\firestore
  GitHub Actions (.github/workflows/firestore-backup.yml): sin FS_DUMP_ACCOUNT, usa la
  cuenta de servicio que activa google-github-actions/auth.
"""
import argparse, datetime as dt, hashlib, json, os, random, re, shutil, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

PROJECT = "prediction-intelligence"
DB = f"projects/{PROJECT}/databases/(default)"
API = "https://firestore.googleapis.com/v1"
ACCOUNT = os.environ.get("FS_DUMP_ACCOUNT")  # None -> cuenta activa de gcloud (la de servicio en Actions)
PAGE = 300

_token = {"v": None, "t": 0.0}


def token():
    if time.time() - _token["t"] > 1500:
        cmd = [shutil.which("gcloud"), "auth", "print-access-token"] + (["--account", ACCOUNT] if ACCOUNT else [])
        _token["v"] = subprocess.check_output(cmd, text=True).strip()
        _token["t"] = time.time()
    return _token["v"]


def call(url, body=None, tries=5):
    for i in range(tries):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET",
            headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503, 504) and i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise RuntimeError(f"HTTP {e.code} {url}: {e.read()[:300]!r}")
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise


def list_collection_ids(parent, rt):
    ids, tok = [], None
    while True:
        body = {"pageSize": 500, "readTime": rt}
        if tok:
            body["pageToken"] = tok
        d = call(f"{API}/{parent}:listCollectionIds", body)
        ids += d.get("collectionIds", [])
        tok = d.get("nextPageToken")
        if not tok:
            return ids


def agg_count(cid, rt):
    q = {"structuredAggregationQuery": {"structuredQuery": {"from": [{"collectionId": cid}]},
                                        "aggregations": [{"alias": "n", "count": {}}]}, "readTime": rt}
    d = call(f"{API}/{DB}/documents:runAggregationQuery", q)
    return int(d[0]["result"]["aggregateFields"]["n"]["integerValue"])


def iter_docs(cid, rt):
    tok = None
    while True:
        qs = {"pageSize": PAGE, "readTime": rt}
        if tok:
            qs["pageToken"] = tok
        d = call(f"{API}/{DB}/documents/{urllib.parse.quote(cid)}?{urllib.parse.urlencode(qs)}")
        yield from d.get("documents", [])
        tok = d.get("nextPageToken")
        if not tok:
            return


def canon(doc):
    return json.dumps({"fields": doc.get("fields", {}), "updateTime": doc.get("updateTime")},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def type_census(value, acc):
    """Cuenta tipos REST (stringValue, integerValue, timestampValue...) recursivamente."""
    for k, v in value.items():
        acc[k] = acc.get(k, 0) + 1
        if k == "mapValue":
            for sub in v.get("fields", {}).values():
                type_census(sub, acc)
        elif k == "arrayValue":
            for sub in v.get("values", []):
                type_census(sub, acc)


# --- detector de claves -------------------------------------------------------------
SECRET_VALUE_PATTERNS = {
    "groq": r"gsk_[A-Za-z0-9]{20,}",
    "anthropic": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai": r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    "google_api_key": r"AIza[0-9A-Za-z_-]{35}",
    "google_oauth_token": r"ya29\.[0-9A-Za-z_-]{20,}",
    "telegram_bot_token": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}",
    "tavily": r"tvly-[A-Za-z0-9_-]{20,}",
    "private_key_block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "service_account_json": r"\"private_key_id\"|\"client_x509_cert_url\"",
    "aws_access_key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack_token": r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "key_in_url": r"[?&](?:api[_-]?key|apikey|access_token|token|key|secret)=[A-Za-z0-9_\-.]{16,}",
}
SECRET_VALUE_RE = {k: re.compile(v, re.IGNORECASE if k == "key_in_url" else 0)
                   for k, v in SECRET_VALUE_PATTERNS.items()}
# Nombres de campo sospechosos: solo cuentan si el valor es texto no trivial (>= 8 caracteres, sin espacios)
SECRET_FIELD_RE = re.compile(
    r"^(?:api_?key|apikey|secret|client_secret|password|passwd|private_key|access_token|refresh_token|"
    r"bearer|authorization|x-cloud-token|cloud_token|bot_token|.*_api_key|.*_secret|.*_password)$", re.IGNORECASE)


def scan_value(value, path, hits):
    """Recorre un valor REST tipado. `hits` acumula {(patrón, ruta): recuento}; nunca guarda el valor."""
    for k, v in value.items():
        if k == "mapValue":
            for name, sub in v.get("fields", {}).items():
                scan_field(name, sub, f"{path}.{name}", hits)
        elif k == "arrayValue":
            for sub in v.get("values", []):
                scan_value(sub, f"{path}[]", hits)
        elif k == "stringValue":
            for pname, rx in SECRET_VALUE_RE.items():
                if rx.search(v):
                    hits[(pname, path)] = hits.get((pname, path), 0) + 1


def scan_field(name, value, path, hits):
    if SECRET_FIELD_RE.match(name):
        sv = value.get("stringValue")
        if isinstance(sv, str) and len(sv) >= 8 and " " not in sv:
            hits[("field_name", path)] = hits.get(("field_name", path), 0) + 1
    scan_value(value, path, hits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=50, help="docs releídos por colección")
    ap.add_argument("--sub-sample", type=int, default=5, help="docs revisados en busca de subcolecciones")
    ap.add_argument("--only", nargs="*", help="limitar a estas colecciones (prueba)")
    a = ap.parse_args()

    t0 = time.time()
    read_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)).replace(microsecond=0)
    rt = read_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = os.path.join(a.out, rt.replace(":", ""))
    os.makedirs(out, exist_ok=False)
    print(f"readTime={rt} -> {out}", flush=True)

    cols = list_collection_ids(f"{DB}/documents", rt)
    if a.only:
        cols = [c for c in cols if c in a.only]
    manifest = {"project": PROJECT, "database": "(default)", "readTime": rt, "collections": {}, "ok": True,
                "secret_scan": {"patterns": sorted(SECRET_VALUE_PATTERNS) + ["field_name"], "hits": {}}}
    rnd = random.Random(rt)

    for cid in cols:
        path = os.path.join(out, f"{cid}.jsonl")
        fh, ids, dh, census, n = hashlib.sha256(), set(), hashlib.sha256(), {}, 0
        dup = 0
        doc_hash = {}
        hits = {}
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for doc in iter_docs(cid, rt):
                did = doc["name"].rsplit("/", 1)[1]
                if did in ids:
                    dup += 1
                ids.add(did)
                line = json.dumps({"id": did, "name": doc["name"], "createTime": doc.get("createTime"),
                                   "updateTime": doc.get("updateTime"), "fields": doc.get("fields", {})},
                                  ensure_ascii=False, separators=(",", ":"))
                f.write(line + "\n")
                fh.update((line + "\n").encode())
                doc_hash[did] = hashlib.sha256(canon(doc).encode()).hexdigest()
                for fname, v in doc.get("fields", {}).items():
                    type_census(v, census)
                    scan_field(fname, v, fname, hits)
                n += 1
        for did in sorted(doc_hash):
            dh.update(f"{did}:{doc_hash[did]}\n".encode())

        expected = agg_count(cid, rt)

        # Relectura aleatoria en el mismo readTime
        mism = []
        pick = rnd.sample(sorted(doc_hash), min(a.sample, len(doc_hash)))
        if pick:
            res = call(f"{API}/{DB}/documents:batchGet",
                       {"documents": [f"{DB}/documents/{cid}/{d}" for d in pick], "readTime": rt})
            got = {r["found"]["name"].rsplit("/", 1)[1]: r["found"] for r in res if "found" in r}
            for did in pick:
                g = got.get(did)
                if g is None or hashlib.sha256(canon(g).encode()).hexdigest() != doc_hash[did]:
                    mism.append(did)

        # Subcolecciones en una muestra
        subs = set()
        for did in rnd.sample(sorted(doc_hash), min(a.sub_sample, len(doc_hash))):
            subs.update(list_collection_ids(f"{DB}/documents/{cid}/{did}", rt))

        if hits:
            manifest["secret_scan"]["hits"][cid] = [
                {"pattern": p, "field": f, "count": c} for (p, f), c in sorted(hits.items())]
            for (p, f), c in sorted(hits.items()):
                print(f"CLAVE? {cid}: patrón={p} campo={f} docs={c}", flush=True)
        ok = (n == expected) and dup == 0 and not mism
        manifest["collections"][cid] = {
            "file": f"{cid}.jsonl", "lines": n, "count_aggregate": expected, "duplicate_ids": dup,
            "sha256_file": fh.hexdigest(), "sha256_docs": dh.hexdigest(), "bytes": os.path.getsize(path),
            "type_census": dict(sorted(census.items())), "resample_checked": min(a.sample, n),
            "resample_mismatches": mism, "subcollections_in_sample": sorted(subs), "ok": ok}
        manifest["ok"] &= ok
        print(f"{'OK ' if ok else 'ERR'} {cid}: {n} líneas / count {expected} / dup {dup} / "
              f"relectura {len(mism)} fallos / subcol {sorted(subs) or '-'}", flush=True)
        if time.time() - t0 > 3000:
            manifest["ok"] = False
            manifest["error"] = "superados 50 min: el readTime está a punto de caducar"
            print(manifest["error"], flush=True)
            break

    manifest["elapsed_s"] = round(time.time() - t0)
    manifest["total_docs"] = sum(c["lines"] for c in manifest["collections"].values())
    n_hits = sum(len(v) for v in manifest["secret_scan"]["hits"].values())
    manifest["secret_scan"]["clean"] = n_hits == 0
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(a.out, "LAST_DUMP"), "w", encoding="utf-8") as f:
        f.write(os.path.basename(out))  # para que el workflow sepa qué carpeta subir
    print(f"{'TODO OK' if manifest['ok'] else 'HAY FALLOS'}: {manifest['total_docs']} docs, "
          f"{len(manifest['collections'])} colecciones, {manifest['elapsed_s']} s; "
          f"detector de claves: {'limpio' if n_hits == 0 else str(n_hits) + ' coincidencias'}", flush=True)
    if n_hits:
        sys.exit(2)  # posible clave: el workflow no sube nada
    sys.exit(0 if manifest["ok"] else 1)


if __name__ == "__main__":
    main()
