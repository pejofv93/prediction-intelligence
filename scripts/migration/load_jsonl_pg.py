"""
Carga un volcado de scripts/migration/firestore_dump.py (JSONL + manifest.json) en la tabla
fs_docs de scripts/migration/pg_store.py y verifica que no se ha perdido ni cambiado nada.

Verificaciones (sale con código 1 si alguna falla):
  1. sha256 de cada .jsonl == manifest (el fichero llegó intacto)
  2. líneas del .jsonl == manifest
  3. filas en Postgres por colección == líneas del manifest
  4. hash por documento: valor REST del JSONL -> Python -> forma canónica, frente a la fila de
     Postgres -> Python -> forma canónica. Detecta cualquier diferencia de tipo (int/float,
     timestamp, null frente a ausente, bytes...).

Solo imprime nombres de colección, recuentos y hashes; nunca contenido (logs públicos).

Uso:
  PG_DSN=postgresql://... python scripts/migration/load_jsonl_pg.py --dump <carpeta con manifest.json>
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_store as ps  # noqa: E402

UTC = dt.timezone.utc


def parse_ts(s: str) -> dt.datetime:
    """RFC 3339 de la API REST (0-9 decimales) -> datetime UTC con microsegundos."""
    s = s.rstrip("Z")
    if "." in s:
        base, frac = s.split(".", 1)
        s = f"{base}.{(frac + '000000')[:6]}"
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def rest_to_py(v: dict):
    (k, x), = v.items()
    if k == "nullValue":
        return None
    if k == "booleanValue":
        return bool(x)
    if k == "integerValue":
        return int(x)
    if k == "doubleValue":
        return float(x)  # admite "NaN", "Infinity", "-Infinity"
    if k == "timestampValue":
        return parse_ts(x)
    if k == "stringValue":
        return x
    if k == "bytesValue":
        return base64.b64decode(x)
    if k == "referenceValue":
        return ps.Reference(x.split("/documents/", 1)[-1])
    if k == "geoPointValue":
        return ps.GeoPoint(x.get("latitude", 0.0), x.get("longitude", 0.0))
    if k == "arrayValue":
        return [rest_to_py(i) for i in x.get("values", [])]
    if k == "mapValue":
        return {kk: rest_to_py(vv) for kk, vv in x.get("fields", {}).items()}
    raise ValueError(f"tipo REST desconocido: {k}")


def canon(py_doc: dict) -> str:
    return json.dumps(ps.encode(py_doc), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True, help="carpeta con manifest.json y los .jsonl")
    ap.add_argument("--only", nargs="*", help="limitar a estas colecciones")
    a = ap.parse_args()

    manifest = json.load(open(os.path.join(a.dump, "manifest.json"), encoding="utf-8"))
    if not manifest.get("ok"):
        print("El manifest del volcado no está OK: no se carga.")
        sys.exit(1)
    pool = ps.get_pool(os.environ["PG_DSN"])
    all_ok = True
    total = 0
    for cid, meta in sorted(manifest["collections"].items()):
        if a.only and cid not in a.only:
            continue
        path = os.path.join(a.dump, meta["file"])
        file_ok = sha256_file(path) == meta["sha256_file"]

        expected = {}
        lines = 0
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM fs_docs WHERE collection=%s", (cid,))
                with conn.cursor() as cur:
                    with cur.copy("COPY fs_docs (collection, id, data, create_time, update_time) FROM STDIN") as cp:
                        with open(path, encoding="utf-8") as f:
                            for line in f:
                                rec = json.loads(line)
                                doc = {k: rest_to_py(v) for k, v in rec.get("fields", {}).items()}
                                expected[rec["id"]] = hashlib.sha256(canon(doc).encode()).hexdigest()
                                cp.write_row((cid, rec["id"], ps._dumps(ps.encode(doc)),
                                              parse_ts(rec["createTime"]), parse_ts(rec["updateTime"])))
                                lines += 1

        got = {}
        with pool.connection() as conn:
            with conn.cursor(name=f"read_{abs(hash(cid))}") as cur:
                cur.execute("SELECT id, data FROM fs_docs WHERE collection=%s", (cid,))
                for doc_id, data in cur:
                    got[doc_id] = hashlib.sha256(canon(ps.decode(data)).encode()).hexdigest()

        mismatched = sum(1 for k in expected if got.get(k) != expected[k])
        missing = len(set(expected) - set(got))
        extra = len(set(got) - set(expected))
        ok = file_ok and lines == meta["lines"] and len(got) == meta["lines"] and not (mismatched or missing or extra)
        all_ok &= ok
        total += len(got)
        dh = hashlib.sha256("".join(f"{k}:{got[k]}\n" for k in sorted(got)).encode()).hexdigest()[:16]
        print(f"{'OK ' if ok else 'ERR'} {cid}: fichero {'íntegro' if file_ok else 'ALTERADO'} / "
              f"líneas {lines} (manifest {meta['lines']}) / filas {len(got)} / distintos {mismatched} / "
              f"faltan {missing} / sobran {extra} / hash {dh}", flush=True)

    print(f"{'TODO OK' if all_ok else 'HAY FALLOS'}: {total} documentos en Postgres", flush=True)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
