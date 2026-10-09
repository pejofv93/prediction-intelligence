"""
scripts/clean_team_stats.py

Limpieza de team_stats de fútbol contaminado por la siembra UEFA (desde el 19-ago-2026):
partidos repetidos en raw_matches (el mismo partido con el id de football-data y con
OTHER_SF_*, o con el rival bajo dos ids).

Pasos, en este orden y por separado:
  --backup            copia team_stats → team_stats_backup_YYYYMMDD y verifica que está completa
  (por defecto)       DRY-RUN: por equipo, qué partidos se quitarían y por qué. No escribe nada.
  --confirm           aplica la deduplicación y recalcula last_10, forma, racha, rendimiento en
                      casa/fuera y xg_per_game (mismas funciones que la siembra)

Solo deduplica (exacto, sin cuota de API). Los amistosos guardados sin nombre de torneo NO
se quitan aquí: se marcan como "sospechosos" en el informe y se resuelven volviendo a pedir
el historial con el torneo (rebuild_elo.py --seed-only con el progreso reiniciado).
No toca el ELO ni ninguna otra colección.

Uso:
    python scripts/clean_team_stats.py --backup            # 1. copia de seguridad
    python scripts/clean_team_stats.py                     # 2. dry-run (informe)
    python scripts/clean_team_stats.py --confirm           # 3. aplicar
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "services", "sports-agent"))
sys.path.insert(0, os.path.join(_HERE, ".."))
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "prediction-intelligence")
os.environ.setdefault("FIRESTORE_COLLECTION_PREFIX", "prod")

from _fsrest import decode, encode, get_db  # noqa: E402
from collectors.stats_processor import (  # noqa: E402
    build_results_list, calculate_form_score, calculate_home_away_split,
    calculate_xg_proxy, detect_streak,
)
from rebuild_elo import _SEED_LEAGUE_CODES, _match_key, dedupe_raw, is_football, typed  # noqa: E402


def read_raw(db, name: str) -> dict[str, dict]:
    """{doc_id: fields en formato Value de la API}. Sin decodificar: _fsrest devuelve los
    timestamps como texto y reescribirlos así cambiaría su tipo (fecha → cadena)."""
    out, token = {}, ""
    while True:
        q = "?pageSize=300" + (f"&pageToken={token}" if token else "")
        page = db._call(f"/{db.prefix}{name}{q}")
        for d in page.get("documents", []):
            out[d["name"].split("/")[-1]] = d.get("fields", {})
        token = page.get("nextPageToken", "")
        if not token:
            return out


def write_raw(db, name: str, docs: dict[str, dict]) -> int:
    """Escribe docs ya en formato Value (sobrescribe el doc entero)."""
    items, written = list(docs.items()), 0
    for i in range(0, len(items), 200):
        db._call(":batchWrite", "POST", {"writes": [
            {"update": {"name": db._doc_name(name, did), "fields": fields}}
            for did, fields in items[i:i + 200]]})
        written += len(items[i:i + 200])
    return written


def _is_preseason_other(m: dict) -> bool:
    """OTHER_SF_* entre el 1-jul y el 14-ago: amistoso probable (no se borra aquí)."""
    day = str(m.get("date") or "")[:10]
    return str(m.get("match_id", "")).startswith("OTHER_") and "07-01" <= day[5:] <= "08-14"


def clean_doc(doc: dict) -> tuple[dict | None, list[dict]]:
    """(doc limpio o None si no cambia, partidos quitados con su motivo)."""
    raw = doc.get("raw_matches") or []
    tid = doc.get("team_id") if doc.get("team_id") is not None else doc.get("_id")
    kept = dedupe_raw(raw, tid)
    kept_ids = {id(m) for m in kept}
    removed = []
    for m in raw:
        if id(m) in kept_ids:
            continue
        twin = next(k for k in kept if _match_key(k, tid) == _match_key(m, tid))
        removed.append({**m, "_motivo": f"duplicado de {twin.get('match_id')}"})
    if not removed:
        return None, []
    kept = sorted(kept, key=lambda x: str(x.get("date", "")), reverse=True)
    t = typed(str(tid))
    results = build_results_list(kept, t)
    last_10 = results[:10]
    home_stats, away_stats = calculate_home_away_split(kept, t)
    xg_matches = [
        {"goals_scored": (x["goals_home"] if x.get("was_home") else x["goals_away"]),
         "goals_conceded": (x["goals_away"] if x.get("was_home") else x["goals_home"])}
        for x in kept
    ]
    new = {}
    new.update({
        "raw_matches": kept, "last_10": last_10, "form_score": calculate_form_score(last_10),
        "streak": detect_streak(last_10), "home_stats": home_stats, "away_stats": away_stats,
        "xg_per_game": calculate_xg_proxy(xg_matches),
        "updated_at": datetime.now(timezone.utc), "cleaned_at": datetime.now(timezone.utc),
        "cleaned_removed": len(removed),
    })
    return new, removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["rest"], default="rest",
                    help="solo REST: lee y escribe los valores en bruto para no cambiar tipos")
    ap.add_argument("--project", default=os.environ["GOOGLE_CLOUD_PROJECT"])
    ap.add_argument("--prefix", default=os.environ["FIRESTORE_COLLECTION_PREFIX"])
    ap.add_argument("--account", default=os.environ.get("GCLOUD_ACCOUNT"))
    ap.add_argument("--backup", action="store_true", help="solo copia de seguridad + verificación")
    ap.add_argument("--confirm", action="store_true", help="aplicar la limpieza")
    ap.add_argument("--stamp", default=datetime.now(timezone.utc).strftime("%Y%m%d"))
    args = ap.parse_args()

    db = get_db(args.transport, args.project, args.prefix, args.account)
    raw_docs = read_raw(db, "team_stats")
    docs = [{**{k: decode(v) for k, v in f.items()}, "_id": i} for i, f in raw_docs.items()]
    print(f"team_stats: {len(docs)} documentos")

    if args.backup:
        col = f"team_stats_backup_{args.stamp}"
        if read_raw(db, col):
            print(f"ABORTADO: {args.prefix}{col} ya existe y no se pisa")
            return 2
        n = write_raw(db, col, raw_docs)
        back = read_raw(db, col)
        missing = [i for i in raw_docs if i not in back]
        diff = [i for i in raw_docs if i in back and back[i] != raw_docs[i]]
        print(f"copia → {args.prefix}{col}: {n} escritos, {len(back)} leídos de vuelta, "
              f"{len(missing)} faltan, {len(diff)} distintos del original (campo a campo)")
        return 0 if not missing and not diff else 1

    football = [d for d in docs if is_football(d) and not str(d["_id"]).startswith("bball_")]
    changes: dict[str, dict] = {}
    report = []
    for d in football:
        new, removed = clean_doc(d)
        raw = d.get("raw_matches") or []
        after = new["raw_matches"] if new else raw
        report.append({
            "id": d["_id"], "name": d.get("team_name", ""), "league": d.get("league") or "",
            "before": len(raw), "after": len(after), "removed": removed,
            "suspicious": [m for m in after if _is_preseason_other(m)],
        })
        if new:
            # Campos que no se recalculan: tal cual estaban (mismo tipo); los nuevos, codificados.
            changes[d["_id"]] = {**raw_docs[d["_id"]], **{k: encode(v) for k, v in new.items()}}

    seeded = [r for r in report if r["league"] in _SEED_LEAGUE_CODES]
    print(f"\nfútbol: {len(football)} equipos · con duplicados: {len(changes)} · partidos a quitar: "
          f"{sum(len(r['removed']) for r in report)}")
    print(f"8 ligas: {len(seeded)} equipos · con duplicados: {sum(1 for r in seeded if r['removed'])} · "
          f"a quitar: {sum(len(r['removed']) for r in seeded)} · amistosos sospechosos que quedan: "
          f"{sum(len(r['suspicious']) for r in seeded)}")
    print("\n== 8 ligas, por equipo (antes → después · quitados · sospechosos) ==")
    for r in sorted(seeded, key=lambda r: (r["league"], r["name"])):
        print(f"  {r['league']:4} {r['name'][:30]:30} {r['before']:2} → {r['after']:2} · "
              f"−{len(r['removed'])} · sospechosos {len(r['suspicious'])}")
        for m in r["removed"]:
            print(f"        quita {str(m.get('date'))[:10]} {m.get('home_team_id')} "
                  f"{m.get('goals_home')}-{m.get('goals_away')} {m.get('away_team_id')} "
                  f"[{m.get('match_id')}] — {m['_motivo']}")
    low = [r for r in report if r["after"] < 5]
    print(f"\nequipos de fútbol con menos de 5 partidos tras limpiar: {len(low)} "
          f"(de las 8 ligas: {sum(1 for r in low if r['league'] in _SEED_LEAGUE_CODES)})")
    for r in low:
        if r["league"] in _SEED_LEAGUE_CODES:
            print(f"  {r['league']} {r['name']}: {r['after']}")
    print(f"distribución de partidos tras limpiar (8 ligas): "
          f"{dict(sorted(Counter(r['after'] for r in seeded).items()))}")

    if not args.confirm:
        print("\nDRY-RUN: nada escrito. Revisa el informe y repite con --confirm.")
        return 0
    n = write_raw(db, "team_stats", changes)
    print(f"\nescritos {n} documentos de team_stats limpios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
