"""
Repara los partidos CL/EL/ECL duplicados en upcoming_matches y la identidad de club
partida en team_stats / team_elo.

Contexto (2026-09-06): la fase de liga de CL entra por DOS vías —football-data.org
(match_id numérico) y allsportsapi2 (match_id `CL_SF_*`)— y la regla de propiedad no
las cruzaba porque cada fuente resuelve el club a un id distinto ("Como" sf_2704 vs
"Como 1907" 7397). Resultado: el mismo partido dos veces y señales de lados contrarios
(Como @1.95 / RB Leipzig @3.40).

Qué hace (todo dry-run salvo --apply):
  1. upcoming_matches: por cada doc `*_SF_*` (source=allsports_uefa) SCHEDULED/TIMED que
     tenga un gemelo de otra fuente (misma fecha + mismos equipos normalizados), BORRA el
     `*_SF_*` y sus predictions huérfanas. Gana football-data.
  2. team_stats / team_elo: detecta pares nombre-normalizado con id `sf_*` + id numérico.
     - Si el id numérico tiene datos reales (league != '' y raw_matches >= 10) → borra el
       doc `sf_*` (team_stats + team_elo).
     - Si el id numérico es un placeholder `Team_NNNN` sin datos → borra ESE placeholder
       (el `sf_*` es el que tiene el histórico sembrado).
  3. Recuerda lanzar después:  rebuild_elo.py --seed-only   (reconcilia el ELO).

Uso:
  python scripts/dedupe_uefa_duplicates.py                 # dry-run
  python scripts/dedupe_uefa_duplicates.py --apply         # ejecuta
  python scripts/dedupe_uefa_duplicates.py --apply --skip-team-stats

Auth: token de `gcloud auth print-access-token` (REST — gRPC está roto tras el proxy TLS
de Norton en el puesto de trabajo).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "prediction-intelligence")
PREFIX = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "prod")
ACCOUNT = os.environ.get("GCLOUD_ACCOUNT", "pejocanal@gmail.com")
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"

_GENERIC = {
    "fc", "cf", "ac", "as", "sc", "sv", "ss", "ssc", "us", "afc", "rc", "rcd", "cd", "ud",
    "fk", "sk", "if", "bk", "kv", "nk", "hnk", "gnk", "ik", "aik", "sad", "kf", "cs", "ks",
    "club", "de", "the", "team", "calcio", "futbol", "football", "atletico", "athletic",
}


def _norm(name: str) -> str:
    n = unicodedata.normalize("NFD", (name or "").lower().strip()).encode("ascii", "ignore").decode()
    n = "".join(c if c.isalnum() else " " for c in n)
    return " ".join(w for w in n.split() if w and not w.isdigit() and w not in _GENERIC)


def _token() -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        sys.exit("gcloud no está en el PATH")
    out = subprocess.run([exe, "auth", "print-access-token", "--account", ACCOUNT],
                         capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"gcloud auth print-access-token falló: {out.stderr[:200]}")
    return out.stdout.strip()


TOK = _token()


def _decode(v: dict):
    k, raw = next(iter(v.items()))
    if k == "integerValue":
        return int(raw)
    if k == "doubleValue":
        return float(raw)
    if k == "booleanValue":
        return bool(raw)
    if k == "nullValue":
        return None
    if k == "arrayValue":
        return [_decode(x) for x in raw.get("values", [])]
    if k == "mapValue":
        return {kk: _decode(x) for kk, x in raw.get("fields", {}).items()}
    return raw


def _run_query(coll: str, where: dict | None = None) -> list[dict]:
    sq: dict = {"from": [{"collectionId": f"{PREFIX}{coll}"}]}
    if where:
        sq["where"] = where
    req = urllib.request.Request(BASE + ":runQuery", data=json.dumps({"structuredQuery": sq}).encode(),
                                 method="POST")
    req.add_header("Authorization", f"Bearer {TOK}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as r:
        rows = json.loads(r.read().decode())
    out = []
    for row in rows:
        d = row.get("document")
        if not d:
            continue
        rec = {kk: _decode(vv) for kk, vv in d.get("fields", {}).items()}
        rec["_id"] = d["name"].split("/")[-1]
        out.append(rec)
    return out


def _eq(field: str, value: str) -> dict:
    return {"fieldFilter": {"field": {"fieldPath": field}, "op": "EQUAL",
                            "value": {"stringValue": value}}}


def _delete(coll: str, doc_id: str) -> None:
    name = f"projects/{PROJECT}/databases/(default)/documents/{PREFIX}{coll}/{urllib.parse.quote(doc_id, safe='')}"
    req = urllib.request.Request(f"https://firestore.googleapis.com/v1/{name}", method="DELETE")
    req.add_header("Authorization", f"Bearer {TOK}")
    urllib.request.urlopen(req, timeout=60).read()


def _day(d: dict) -> str:
    return str(d.get("match_date") or d.get("date") or "")[:10].replace("/", "-")


def _same_team(x: str, y: str) -> bool:
    return bool(x) and bool(y) and (x == y or (len(x) >= 4 and x in y) or (len(y) >= 4 and y in x))


def _same_fixture(a: dict, b: dict) -> bool:
    if _day(a) != _day(b) or not _day(a):
        return False
    ah, aa = _norm(a.get("home_team", "")), _norm(a.get("away_team", ""))
    bh, ba = _norm(b.get("home_team", "")), _norm(b.get("away_team", ""))
    if not (ah and aa and bh and ba):
        return False
    return _same_team(ah, bh) and _same_team(aa, ba)


def _prefix_preds(base: str) -> list[dict]:
    return _run_query("predictions", {"compositeFilter": {"op": "AND", "filters": [
        {"fieldFilter": {"field": {"fieldPath": "match_id"}, "op": "GREATER_THAN_OR_EQUAL",
                         "value": {"stringValue": base}}},
        {"fieldFilter": {"field": {"fieldPath": "match_id"}, "op": "LESS_THAN",
                         "value": {"stringValue": base + ""}}},
    ]}})


def dedupe_upcoming(apply: bool) -> None:
    docs: list[dict] = []
    for st in ("SCHEDULED", "TIMED"):
        docs += _run_query("upcoming_matches", _eq("status", st))
    foot = [d for d in docs if (d.get("sport") or "football").lower() == "football"]
    sf_docs = [d for d in foot if d.get("source") == "allsports_uefa"]
    other_docs = [d for d in foot if d.get("source") != "allsports_uefa"]

    total_docs = total_preds = 0
    for od in sorted(other_docs, key=lambda d: (_day(d), d.get("_id", ""))):
        twins = [s for s in sf_docs if _same_fixture(od, s)]
        if not twins:
            continue
        keep = od["_id"]
        print(f"\n  {_day(od)}  {_norm(od.get('home_team',''))} vs {_norm(od.get('away_team',''))}")
        print(f"    CONSERVA {keep}  ({od.get('source')})  "
              f"{od.get('home_team')} vs {od.get('away_team')}")
        for d in twins:
            preds = _prefix_preds(d["_id"])
            print(f"    BORRA    {d['_id']}  (allsports_uefa)  + {len(preds)} predictions"
                  + (f"  [{', '.join(p['_id'] + ('*' if p.get('alerted') else '') for p in preds)}]" if preds else ""))
            total_docs += 1
            total_preds += len(preds)
            if apply:
                for p in preds:
                    _delete("predictions", p["_id"])
                _delete("upcoming_matches", d["_id"])

    verb = "borrados" if apply else "se borrarían"
    print(f"\n  upcoming_matches: {verb} {total_docs} docs *_SF_* + {total_preds} predictions "
          "(el * marca alerted=True)")


def prune_stale_uefa(apply: bool, horizon_days: int = 30) -> None:
    """
    Borra docs allsports_uefa SCHEDULED/TIMED cuya fecha REAL (según el feed en vivo de
    allsportsapi2) esté fuera de la ventana `horizon_days` o que ya no aparezcan en el
    feed. Es la limpieza puntual del pile de "48 partidos CL el 08-sep" (jornadas 5-8 con
    la fecha vieja de la J1); a partir del deploy lo hace _collect_uefa en cada pasada.
    """
    import datetime as _dt
    key = _rapid_key()
    tours = {"CL": (7, 96518), "EL": (679, 96522), "ECL": (17015, 96529)}
    feed: dict[str, str] = {}   # match_id -> fecha real ISO (o "" si desaparecido)
    for lg, (tid, sid) in tours.items():
        for page in range(8):
            d = _rapid(f"/api/tournament/{tid}/season/{sid}/matches/next/{page}", key)
            for e in d.get("events", []):
                ts = e.get("startTimestamp")
                feed[f"{lg}_SF_{e['id']}"] = (
                    _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat() if ts else ""
                )
            if not d.get("hasNextPage"):
                break

    horizon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=horizon_days)
    docs: list[dict] = []
    for st in ("SCHEDULED", "TIMED"):
        docs += _run_query("upcoming_matches", _eq("status", st))
    uefa = [d for d in docs if d.get("source") == "allsports_uefa"]

    borrar = 0
    for d in uefa:
        real = feed.get(d["_id"])
        motivo = None
        if real is None:
            motivo = "no está en el feed 'next' (jugado/movido)"
        elif real == "":
            motivo = "sin fecha en el feed"
        else:
            try:
                rd = _dt.datetime.fromisoformat(real)
                if rd > horizon:
                    motivo = f"jornada lejana (real {real[:10]}, fuera de {horizon_days}d)"
            except ValueError:
                pass
        if motivo:
            borrar += 1
            print(f"    BORRA {d['_id']:22s} {d.get('league')} {d.get('home_team')} vs {d.get('away_team')}"
                  f"  (guardado {str(d.get('match_date') or d.get('date'))[:10]}) — {motivo}")
            if apply:
                for p in _prefix_preds(d["_id"]):
                    _delete("predictions", p["_id"])
                _delete("upcoming_matches", d["_id"])
    verb = "borrados" if apply else "se borrarían"
    print(f"\n  stale UEFA: {verb} {borrar} docs (de {len(uefa)} allsports_uefa pendientes)")


def _rapid_key() -> str:
    k = os.environ.get("FOOTBALL_RAPID_API_KEY", "")
    if k:
        return k
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    out = subprocess.run(
        [exe, "run", "services", "describe", "sports-agent", "--project", PROJECT,
         "--region", os.environ.get("CLOUD_RUN_REGION", "europe-west1"),
         "--format", "json(spec.template.spec.containers[0].env)"],
        capture_output=True, text=True,
    )
    if out.returncode:
        sys.exit(f"no se pudo leer FOOTBALL_RAPID_API_KEY de Cloud Run: {out.stderr[:200]}")
    env = json.loads(out.stdout)["spec"]["template"]["spec"]["containers"][0]["env"]
    for e in env:
        if e["name"] == "FOOTBALL_RAPID_API_KEY":
            return e["value"]
    sys.exit("FOOTBALL_RAPID_API_KEY no está entre las variables del servicio")


def _rapid(path: str, key: str) -> dict:
    req = urllib.request.Request(f"https://allsportsapi2.p.rapidapi.com{path}")
    req.add_header("x-rapidapi-key", key)
    req.add_header("x-rapidapi-host", "allsportsapi2.p.rapidapi.com")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"    (rapid {path} → HTTP {e.code})")
        return {}


def dedupe_team_identity(apply: bool) -> None:
    ts = [t for t in _run_query("team_stats") if (t.get("sport") or "football").lower() == "football"]
    by_name: dict[str, list[dict]] = defaultdict(list)
    for t in ts:
        nm = t.get("team_name") or ""
        key = _norm(nm)
        if not nm or not key or nm.startswith("Team_"):
            continue   # nombre vacío o todo-genérico ("Athletic Club" → "") → no agrupar
        by_name[key].append(t)

    borrar_sf = borrar_placeholder = 0
    for name, group in sorted(by_name.items()):
        ids = {str(t["_id"]) for t in group}
        if len(ids) < 2:
            continue
        sf_docs = [t for t in group if str(t["_id"]).startswith("sf_")]
        num_docs = [t for t in group if str(t["_id"]).isdigit()]
        if not sf_docs or not num_docs:
            continue
        num = max(num_docs, key=lambda t: len(t.get("raw_matches", []) or []))
        sf = max(sf_docs, key=lambda t: len(t.get("raw_matches", []) or []))
        # El id numérico gana en cuanto tenga una liga real (football-data lo sigue
        # alimentando en cada collect y `rebuild_elo --seed-only` le vuelca el histórico
        # europeo que ahora está en el sf_). Quedarse con el sf_ solo re-parte la
        # identidad en la siguiente pasada del colector.
        num_real = bool(num.get("league")) and len(num.get("raw_matches", []) or []) >= 1
        num_placeholder = str(num.get("team_name", "")).startswith("Team_") or \
            len(num.get("raw_matches", []) or []) == 0

        print(f"\n  '{name}'  sf={sf['_id']}(raw={len(sf.get('raw_matches',[]) or [])},"
              f"league={sf.get('league')!r})  num={num['_id']}(raw={len(num.get('raw_matches',[]) or [])},"
              f"league={num.get('league')!r},name={num.get('team_name')!r})")
        if num_real:
            print(f"    → BORRA sf_ ({sf['_id']}): team_stats + team_elo. Canónico = {num['_id']}")
            borrar_sf += 1
            if apply:
                _delete("team_stats", sf["_id"])
                try:
                    _delete("team_elo", sf["_id"])
                except urllib.error.HTTPError:
                    pass
        elif num_placeholder:
            print(f"    → BORRA placeholder numérico ({num['_id']}). Canónico = {sf['_id']}")
            borrar_placeholder += 1
            if apply:
                _delete("team_stats", num["_id"])
                try:
                    _delete("team_elo", num["_id"])
                except urllib.error.HTTPError:
                    pass
        else:
            print("    → AMBIGUO (ninguno claramente mejor) — revisar a mano, no se toca")

    verb = "borrados" if apply else "se borrarían"
    print(f"\n  team_stats: {verb} {borrar_sf} docs sf_ + {borrar_placeholder} placeholders Team_NNNN")
    if apply:
        print("  ⚠ Lanza ahora:  FIRESTORE_COLLECTION_PREFIX=prod python scripts/rebuild_elo.py --seed-only")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="ejecuta los borrados (por defecto: dry-run)")
    ap.add_argument("--skip-team-stats", action="store_true", help="no tocar team_stats/team_elo")
    ap.add_argument("--stale", action="store_true",
                    help="además, borra docs allsports_uefa con fecha real fuera de ventana (gasta ~15 req RapidAPI)")
    args = ap.parse_args()

    print(f"=== dedupe_uefa_duplicates ({'APPLY' if args.apply else 'DRY-RUN'}) "
          f"project={PROJECT} prefix={PREFIX} ===")
    print("\n## 1. upcoming_matches — partidos duplicados")
    dedupe_upcoming(args.apply)
    if args.stale:
        print("\n## 1b. upcoming_matches — docs allsports_uefa con fecha stale / jornada lejana")
        prune_stale_uefa(args.apply)
    if not args.skip_team_stats:
        print("\n## 2. team_stats / team_elo — identidad de club partida")
        dedupe_team_identity(args.apply)
    print("\nHecho." + ("" if args.apply else "  (nada modificado — usa --apply)"))
