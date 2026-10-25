"""
scripts/clean_summer_friendlies.py

Quita de team_stats (equipos de las 8 ligas) los partidos de verano que allsportsapi2 no
marca como amistosos (853), en dos grupos que se aplican y revierten por separado:

  A  con torneo guardado: la regla exacta de rebuild_elo.is_friendly (torneo no oficial
     entre junio y agosto, con un equipo de las 8 ligas). Como Cup, Emirates Cup…
  B  sin torneo guardado (solo ids OTHER_SF_*, partidos antiguos): entre junio y agosto,
     rival que no es de su liga, o fecha anterior al inicio de su liga (primer partido
     de football-data de la temporada).
Los ids numéricos de football-data no se tocan nunca. Tras quitar, recalcula last_10,
forma, racha, rendimiento en casa/fuera y xg_per_game (mismas funciones que la siembra).

Cada grupo aplicado deja en team_stats_cleanup_log/<stamp>_<grupo> los partidos quitados
tal cual estaban; --revert los vuelve a meter (sin restaurar la copia entera, para no
pisar lo que haya escrito la siembra después). Ese registro no se borra.

Uso (copia antes: python scripts/clean_team_stats.py --backup --stamp YYYYMMDD):
    python scripts/clean_summer_friendlies.py                                  # dry-run A y B
    python scripts/clean_summer_friendlies.py --confirm --group A              # aplicar A
    python scripts/clean_summer_friendlies.py --revert --group A --stamp 20261025 [--confirm]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "services", "sports-agent"))
sys.path.insert(0, os.path.join(_HERE, ".."))
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "prediction-intelligence")
os.environ.setdefault("FIRESTORE_COLLECTION_PREFIX", "prod")

from _fsrest import decode, encode, get_db  # noqa: E402
from clean_team_stats import read_raw, write_raw  # noqa: E402
from collectors.stats_processor import (  # noqa: E402
    build_results_list, calculate_form_score, calculate_home_away_split,
    calculate_xg_proxy, detect_streak,
)
from rebuild_elo import _SEED_LEAGUE_CODES, is_football, is_friendly, ocho_ligas_ids, typed  # noqa: E402

LOG = "team_stats_cleanup_log"
WINDOW_MONTHS = ("06", "07", "08")


def league_starts(docs: list[dict]) -> dict[tuple[str, str], str]:
    """{(liga, año): inicio}: primer partido de football-data desde el 1-jul de ese año."""
    starts: dict[tuple[str, str], list[str]] = defaultdict(list)
    for d in docs:
        if d.get("league") not in _SEED_LEAGUE_CODES:
            continue
        for m in d.get("raw_matches") or []:
            day = str(m.get("date"))[:10]
            if str(m.get("match_id", "")).isdigit() and day[5:7] >= "07":
                starts[(d["league"], day[:4])].append(day)
    return {k: min(v) for k, v in starts.items()}


def group_of(d: dict, m: dict, ocho: set, byid: dict, starts: dict) -> tuple[str, str] | None:
    """('A'|'B', motivo) si el partido se quita; None si se queda."""
    mid = str(m.get("match_id", ""))
    if not mid.startswith("OTHER_SF_"):
        return None
    h, a = str(m.get("home_team_id")), str(m.get("away_team_id"))
    if m.get("tournament"):
        if is_friendly(m, ocho, (h, a)):
            return "A", f"torneo no oficial «{m['tournament']}»"
        return None
    day = str(m.get("date"))[:10]
    if day[5:7] not in WINDOW_MONTHS:
        return None
    rival_league = byid.get(a if m.get("was_home") else h, {}).get("league")
    if rival_league != d.get("league"):
        return "B", f"rival {('de ' + rival_league) if rival_league else 'sin liga'}, no es de su liga"
    start = starts.get((d["league"], day[:4]))
    if start and day < start:
        return "B", f"antes del inicio de su liga ({start})"
    return None


def recompute(doc: dict, raw: list[dict]) -> dict:
    raw = sorted(raw, key=lambda x: str(x.get("date", "")), reverse=True)
    tid = doc.get("team_id") if doc.get("team_id") is not None else doc["_id"]
    t = typed(str(tid))
    last_10 = build_results_list(raw, t)[:10]
    home_stats, away_stats = calculate_home_away_split(raw, t)
    xg_matches = [
        {"goals_scored": (x["goals_home"] if x.get("was_home") else x["goals_away"]),
         "goals_conceded": (x["goals_away"] if x.get("was_home") else x["goals_home"])}
        for x in raw
    ]
    now = datetime.now(timezone.utc)
    return {"raw_matches": raw, "last_10": last_10, "form_score": calculate_form_score(last_10),
            "streak": detect_streak(last_10), "home_stats": home_stats, "away_stats": away_stats,
            "xg_per_game": calculate_xg_proxy(xg_matches), "updated_at": now, "cleaned_at": now}


def _log_exists(db, doc_id: str) -> bool:
    try:
        db._call(f"/{db.prefix}{LOG}/{doc_id}")
        return True
    except Exception as e:
        if "404" in str(e):
            return False
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=os.environ["GOOGLE_CLOUD_PROJECT"])
    ap.add_argument("--prefix", default=os.environ["FIRESTORE_COLLECTION_PREFIX"])
    ap.add_argument("--account", default=os.environ.get("GCLOUD_ACCOUNT"))
    ap.add_argument("--group", choices=["A", "B"])
    ap.add_argument("--confirm", action="store_true", help="escribir (aplicar o revertir)")
    ap.add_argument("--revert", action="store_true", help="volver a meter los partidos del grupo")
    ap.add_argument("--stamp", default=datetime.now(timezone.utc).strftime("%Y%m%d"))
    args = ap.parse_args()

    db = get_db("rest", args.project, args.prefix, args.account)
    raw_docs = read_raw(db, "team_stats")
    docs = [{**{k: decode(v) for k, v in f.items()}, "_id": i} for i, f in raw_docs.items()]
    byid = {d["_id"]: d for d in docs}
    football = [d for d in docs if is_football(d) and not str(d["_id"]).startswith("bball_")]
    log_id = f"{args.stamp}_{args.group}"

    if args.revert:
        if not args.group:
            print("--revert necesita --group")
            return 2
        entries = decode(db._call(f"/{db.prefix}{LOG}/{log_id}")["fields"]["docs"])
        changes = {}
        for did, removed in entries.items():
            d = byid[did]
            ids = {str(x.get("match_id")) for x in d.get("raw_matches") or []}
            raw = list(d.get("raw_matches") or []) + [m for m in removed if str(m.get("match_id")) not in ids]
            changes[did] = {**raw_docs[did], **{k: encode(v) for k, v in recompute(d, raw).items()}}
            print(f"  {d.get('team_name')}: {len(d.get('raw_matches') or [])} → {len(raw)}")
        if not args.confirm:
            print(f"revert {log_id}: DRY-RUN, nada escrito ({len(changes)} docs)")
            return 0
        print(f"revert {log_id}: escritos {write_raw(db, 'team_stats', changes)} (el registro se conserva)")
        return 0

    ocho = ocho_ligas_ids(football)
    starts = league_starts(football)
    plan: dict[str, dict[str, list]] = {"A": defaultdict(list), "B": defaultdict(list)}
    for d in football:
        if d.get("league") not in _SEED_LEAGUE_CODES:
            continue
        for m in d.get("raw_matches") or []:
            g = group_of(d, m, ocho, byid, starts)
            if g:
                plan[g[0]][d["_id"]].append((m, g[1]))

    print(f"team_stats: {len(docs)} documentos · inicio de liga 26/27: {starts}")
    for g in ("A", "B"):
        p = plan[g]
        n_par = len({m["match_id"] for v in p.values() for m, _ in v})
        print(f"\n== GRUPO {g}: {n_par} partidos · {sum(len(v) for v in p.values())} apariciones · "
              f"{len(p)} equipos ==")
        for did in sorted(p, key=lambda i: (byid[i]["league"], byid[i]["team_name"])):
            d = byid[did]
            n = len(d.get("raw_matches") or [])
            print(f"  {d['league']:4} {d['team_name'][:28]:28} {n:2} → {n - len(p[did]):2}")
            for m, why in sorted(p[did], key=lambda x: str(x[0]["date"])):
                o = str(m["away_team_id"] if m["was_home"] else m["home_team_id"])
                print(f"        {str(m['date'])[:10]} {'vs' if m['was_home'] else '@ '} "
                      f"{byid.get(o, {}).get('team_name', o)[:26]:26} {m['goals_home']}-{m['goals_away']} "
                      f"[{m['match_id']}] — {why}")
        low = [byid[i]["team_name"] for i in p if len(byid[i]["raw_matches"]) - len(p[i]) < 10]
        print(f"  equipos que bajan de 10 partidos: {low or 'ninguno'}")

    if not args.confirm:
        print("\nDRY-RUN: nada escrito. Aplica un grupo con --confirm --group A|B.")
        return 0
    if not args.group:
        print("--confirm necesita --group A o B")
        return 2
    if _log_exists(db, log_id):
        print(f"ABORTADO: {LOG}/{log_id} ya existe (grupo ya aplicado); no se pisa")
        return 2
    changes, log = {}, {}
    for did, items in plan[args.group].items():
        d = byid[did]
        quit_ids = {id(m) for m, _ in items}
        kept = [m for m in d["raw_matches"] if id(m) not in quit_ids]
        changes[did] = {**raw_docs[did], **{k: encode(v) for k, v in recompute(d, kept).items()}}
        log[did] = [m for m, _ in items]
    db._call(f"/{db.prefix}{LOG}/{log_id}", "PATCH",
             {"fields": {"docs": encode(log), "group": encode(args.group),
                         "created_at": encode(datetime.now(timezone.utc))}})
    print(f"\nregistro de lo quitado → {args.prefix}{LOG}/{log_id} ({len(log)} docs)")
    print(f"escritos {write_raw(db, 'team_stats', changes)} documentos de team_stats")
    return 0


if __name__ == "__main__":
    sys.exit(main())
