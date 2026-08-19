"""
Reconstrucción del ELO desde cero + siembra del histórico de clubes UEFA (C2b + C3).

Por qué: `update_all_elos` no llevaba registro de lo ya aplicado y re-aplicaba los mismos
resultados en cada ciclo (K=32). Medido sobre la base real: 7,9x de repetición mínima,
amplitud de 1.045 puntos donde una sola pasada da 261 y Spearman 0,656 contra el orden que
justifican los datos. El ledger (elo_applied) evita que vuelva a pasar, pero los valores
guardados siguen corruptos: hay que recomputarlos.

Y se aprovecha la misma pasada para sembrar los clubes de CL/EL/ECL desde allsportsapi2
(/api/team/{id}/matches/previous), que da 30 partidos por club — liga doméstica, copa y
previas europeas. Sin ese histórico el recomputo se queda en ~7 partidos por equipo y sale
un ELO plano; con él, tanto el ELO como el Poisson tienen material.

Fases:
  0. Copia de seguridad de team_elo → team_elo_backup_YYYYMMDD
  1. Censo de clubes UEFA + histórico por club → team_stats (schema de save_team_stats)
  2. Universo de partidos: team_stats.raw_matches + match_results + histórico UEFA,
     deduplicado por huella (fecha, local, visitante) con ids canónicos
  3. Recomputo del ELO en UNA pasada cronológica
  4. Escritura de team_elo (+ elo_history real)
  5. Ledger elo_applied con todas las huellas aplicadas
  6. Marcador model_weights/elo_rebuild + reset de pesos a DEFAULT_WEIGHTS

Uso:
    python scripts/rebuild_elo.py                          # dry-run: no escribe nada
    python scripts/rebuild_elo.py --confirm                # ejecuta
    python scripts/rebuild_elo.py --confirm --max-uefa-teams 80
    python scripts/rebuild_elo.py --no-uefa --confirm      # solo recomputo, sin siembra

Cuota allsportsapi2: 100 requests/día. El censo gasta ~8 y cada club 1.
Local: Python 3.11 con SSL_CERT_FILE al bundle de Norton (gRPC está bloqueado → --transport rest).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "services", "sports-agent"))

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "prediction-intelligence")
os.environ.setdefault("FIRESTORE_COLLECTION_PREFIX", "prod")

from _fsrest import get_db                                              # noqa: E402
from probe_uefa import _api_key, _get as _uefa_get, _season_label       # noqa: E402
from collectors.allsports_uefa import UEFA_TOURNAMENTS, parse_event     # noqa: E402
from collectors.team_identity import (                                  # noqa: E402
    build_identity_map, match_fingerprint, resolve,
)
from collectors.stats_processor import (                                # noqa: E402
    build_results_list, calculate_form_score, calculate_home_away_split,
    calculate_xg_proxy, detect_streak,
)

K_FACTOR, HOME_ADVANTAGE, DEFAULT_ELO = 32, 100, 1500.0
_RAW_MATCHES_KEEP = 20        # igual que firestore_writer
_ELO_HISTORY_KEEP = 10        # igual que elo_rating

# team_stats mezcla deportes: hay 26 docs de baloncesto (NBA/ACB/Euroliga) conviviendo con
# los de fútbol en el MISMO espacio de ids. El ELO guardado incluía por eso partidos de la
# NBA (los Cavaliers estaban entre los "equipos" con más ELO). Se excluyen del recomputo y
# del mapa de identidad: un ELO de fútbol construido con partidos de baloncesto no significa
# nada, y emparejar un club europeo contra "Real Madrid (Euroliga)" sería peor todavía.
_NON_FOOTBALL_LEAGUES = {"NBA", "ACB", "EUROLEAGUE", "EUROCUP", "NCAA"}


def is_football(doc: dict) -> bool:
    sport = (doc.get("sport") or "football").lower()
    return sport == "football" and str(doc.get("league", "")).upper() not in _NON_FOOTBALL_LEAGUES


def expected(a: float, b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((b - a) / 400.0))


def typed(team_id: str):
    """Ids numéricos como int (football-data) y acuñados como str ('sf_2817')."""
    return int(team_id) if str(team_id).isdigit() else str(team_id)


# ── Fase 1: siembra UEFA ─────────────────────────────────────────────────────

def discover_season(league: str, key: str) -> int | None:
    tid = UEFA_TOURNAMENTS[league]
    data, _ = _uefa_get(f"/api/tournament/{tid}/seasons", key)
    seasons = data.get("seasons", [])
    label = _season_label()
    chosen = next((s for s in seasons if str(s.get("year")) == label), None) or (
        seasons[0] if seasons else None)
    if not chosen:
        return None
    print(f"  {league}: temporada {chosen.get('year')} → season_id={chosen.get('id')}")
    return int(chosen["id"])


def fetch_uefa_matches(key: str, leagues: list[str]) -> tuple[list[dict], dict[int, str]]:
    """Fixtures y resultados de las competiciones + censo de clubes."""
    matches: list[dict] = []
    clubs: dict[int, str] = {}
    for lg in leagues:
        sid = discover_season(lg, key)
        if not sid:
            print(f"  {lg}: sin temporada — omitida")
            continue
        tid = UEFA_TOURNAMENTS[lg]
        for direction in ("next", "last"):
            for page in range(2):
                data, _ = _uefa_get(
                    f"/api/tournament/{tid}/season/{sid}/matches/{direction}/{page}", key)
                events = data.get("events", [])
                for e in events:
                    m = parse_event(e, lg)
                    if not m:
                        continue
                    matches.append(m)
                    clubs[m["home_source_id"]] = m["home_team"]
                    clubs[m["away_source_id"]] = m["away_team"]
                if not data.get("hasNextPage") or len(events) < 30:
                    break
    print(f"  censo: {len(clubs)} clubes distintos, {len(matches)} partidos de competición")
    return matches, clubs


def fetch_club_histories(key: str, clubs: dict[int, str], limit: int) -> list[dict]:
    """1 request por club: 30 partidos con marcador (todas las competiciones)."""
    out: list[dict] = []
    for i, (sf_id, name) in enumerate(sorted(clubs.items())):
        if i >= limit:
            print(f"  tope de {limit} clubes alcanzado — quedan {len(clubs) - limit} "
                  f"para la siguiente tanda (cuota diaria)")
            break
        try:
            data, headers = _uefa_get(f"/api/team/{sf_id}/matches/previous/0", key)
        except Exception as e:
            print(f"  {name}: ERROR {type(e).__name__} {e}")
            continue
        events = data.get("events", [])
        for e in events:
            m = parse_event(e, "OTHER")
            if m:
                out.append(m)
        rem = headers.get("X-RateLimit-Requests-Remaining", "?")
        print(f"  [{i + 1}/{min(limit, len(clubs))}] {name}: {len(events)} partidos "
              f"(cuota {rem})")
        if str(rem).isdigit() and int(rem) <= 2:
            print("  cuota diaria casi agotada — se corta la siembra aquí")
            break
    return out


# ── Fase 2: universo de partidos ─────────────────────────────────────────────

def norm_uefa_match(m: dict, imap: dict) -> dict | None:
    """Partido de allsportsapi2 → entrada de universo con ids canónicos."""
    if m.get("goals_home") is None or m.get("goals_away") is None:
        return None
    h = resolve(m["home_team"], m["home_source_id"], imap)
    a = resolve(m["away_team"], m["away_source_id"], imap)
    return {"date": m.get("date", ""), "h": h, "a": a,
            "gh": m["goals_home"], "ga": m["goals_away"],
            "home_name": m["home_team"], "away_name": m["away_team"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--confirm", action="store_true", help="escribir en Firestore")
    ap.add_argument("--transport", choices=["rest", "grpc"], default="rest")
    ap.add_argument("--project", default=os.environ["GOOGLE_CLOUD_PROJECT"])
    ap.add_argument("--prefix", default=os.environ["FIRESTORE_COLLECTION_PREFIX"])
    ap.add_argument("--account", default=os.environ.get("GCLOUD_ACCOUNT"))
    ap.add_argument("--no-uefa", action="store_true", help="solo recomputo, sin siembra UEFA")
    ap.add_argument("--leagues", default="CL,EL,ECL")
    ap.add_argument("--max-uefa-teams", type=int, default=80,
                    help="tope de clubes por ejecución (cuota 100/día)")
    ap.add_argument("--skip-backup", action="store_true")
    ap.add_argument("--no-reset-weights", action="store_true",
                    help="no resetear model_weights a DEFAULT_WEIGHTS")
    args = ap.parse_args()

    db = get_db(args.transport, args.project, args.prefix, args.account)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    now = datetime.now(timezone.utc)
    modo = "EJECUTANDO" if args.confirm else "DRY-RUN (no escribe nada)"
    print(f"=== rebuild_elo — {modo} ===\n")

    # ── 0. Copia de seguridad ────────────────────────────────────────────────
    old_elo = db.read_collection("team_elo")
    print(f"0. team_elo actual: {len(old_elo)} documentos")
    backup_col = f"team_elo_backup_{stamp}"
    if not args.skip_backup:
        snap = {d["_id"]: {k: v for k, v in d.items() if k != "_id"} for d in old_elo}
        if args.confirm:
            n = db.write_docs(backup_col, snap)
            print(f"   copia de seguridad → {args.prefix}{backup_col}: {n} docs")
        else:
            print(f"   [dry-run] copiaría {len(snap)} docs a {args.prefix}{backup_col}")

    # ── 1. Datos base ────────────────────────────────────────────────────────
    team_stats_all = db.read_collection("team_stats")
    team_stats = [t for t in team_stats_all if is_football(t)]
    otros_deportes = len(team_stats_all) - len(team_stats)
    results = db.read_collection("match_results")
    imap = build_identity_map(team_stats)
    names = {str(t["team_id"]): t.get("team_name", "") for t in team_stats if t.get("team_id") is not None}
    print(f"1. team_stats: {len(team_stats)} de fútbol ({otros_deportes} de otros deportes "
          f"excluidos) | match_results: {len(results)} | mapa de identidad: {len(imap)} nombres")

    uefa_matches: list[dict] = []
    club_hist: list[dict] = []
    clubs: dict[int, str] = {}
    if not args.no_uefa:
        key = _api_key()
        leagues = [x.strip().upper() for x in args.leagues.split(",") if x.strip()]
        print("\n   siembra UEFA:")
        uefa_matches, clubs = fetch_uefa_matches(key, leagues)
        club_hist = fetch_club_histories(key, clubs, args.max_uefa_teams)
        print(f"   histórico de clubes: {len(club_hist)} partidos brutos")

    # ── 2. Universo de partidos, deduplicado por huella ──────────────────────
    universe: dict[str, dict] = {}
    src_count = {"raw_matches": 0, "match_results": 0, "uefa": 0}

    for t in team_stats:
        for m in t.get("raw_matches") or []:
            if m.get("goals_home") is None or m.get("goals_away") is None:
                continue
            h, a = str(m.get("home_team_id")), str(m.get("away_team_id"))
            if not h or not a or h == "None" or a == "None":
                continue
            fp = match_fingerprint(m.get("date", ""), h, a)
            if fp not in universe:
                universe[fp] = {"date": m.get("date", ""), "h": h, "a": a,
                                "gh": m["goals_home"], "ga": m["goals_away"]}
                src_count["raw_matches"] += 1

    name2id = {}
    for t in team_stats:
        if t.get("team_id") is not None and t.get("team_name"):
            name2id.setdefault(t["team_name"], str(t["team_id"]))
    for r in results:
        h = resolve(r.get("home_team", ""), f"res_{r.get('_id')}", imap)
        a = resolve(r.get("away_team", ""), f"res_{r.get('_id')}", imap)
        if h.startswith("res_") or a.startswith("res_"):
            continue                      # equipo desconocido: sin id canónico, se omite
        if r.get("goals_home") is None or r.get("goals_away") is None:
            continue
        fp = match_fingerprint(r.get("match_date", ""), h, a)
        if fp not in universe:
            universe[fp] = {"date": r.get("match_date", ""), "h": h, "a": a,
                            "gh": r["goals_home"], "ga": r["goals_away"]}
            src_count["match_results"] += 1

    for m in uefa_matches + club_hist:
        norm = norm_uefa_match(m, imap)
        if not norm:
            continue
        fp = match_fingerprint(norm["date"], norm["h"], norm["a"])
        if fp not in universe:
            universe[fp] = norm
            src_count["uefa"] += 1
        names.setdefault(norm["h"], norm["home_name"])
        names.setdefault(norm["a"], norm["away_name"])

    print(f"\n2. universo: {len(universe)} partidos únicos "
          f"(raw_matches {src_count['raw_matches']}, match_results {src_count['match_results']}, "
          f"UEFA {src_count['uefa']})")

    # ── 3. Recomputo en una pasada ───────────────────────────────────────────
    elo: dict[str, float] = {}
    history: dict[str, list[dict]] = {}
    applied: dict[str, dict] = {}
    orden = sorted(universe.items(), key=lambda kv: str(kv[1]["date"]))
    for fp, m in orden:
        h, a = m["h"], m["a"]
        eh, ea = elo.get(h, DEFAULT_ELO), elo.get(a, DEFAULT_ELO)
        score = 1.0 if m["gh"] > m["ga"] else 0.0 if m["ga"] > m["gh"] else 0.5
        p = expected(eh + HOME_ADVANTAGE, ea)
        elo[h] = (eh + HOME_ADVANTAGE) + K_FACTOR * (score - p) - HOME_ADVANTAGE
        elo[a] = ea + K_FACTOR * ((1.0 - score) - (1.0 - p))
        res = "HOME_WIN" if score == 1.0 else "AWAY_WIN" if score == 0.0 else "DRAW"
        for tid, opp in ((h, a), (a, h)):
            history.setdefault(tid, []).append(
                {"date": m["date"], "elo": round(elo[tid], 1), "opponent_id": typed(opp),
                 "result": res})
        applied[fp] = {"home_team_id": h, "away_team_id": a,
                       "date": str(m["date"]), "result": res}

    partidos_por_equipo = sorted((len(v) for v in history.values()), reverse=True)
    mediana = partidos_por_equipo[len(partidos_por_equipo) // 2] if partidos_por_equipo else 0
    print(f"3. ELO recomputado para {len(elo)} equipos "
          f"(mediana {mediana} partidos/equipo, máx {partidos_por_equipo[0] if partidos_por_equipo else 0})")

    top = sorted(elo.items(), key=lambda kv: -kv[1])[:10]
    bot = sorted(elo.items(), key=lambda kv: kv[1])[:5]
    print("\n   top 10:")
    for tid, v in top:
        prev = next((float(d.get("elo", 0)) for d in old_elo if str(d.get("team_id")) == tid), None)
        delta = f"{v - prev:+.0f}" if prev is not None else "nuevo"
        print(f"     {names.get(tid, tid)[:28]:<30}{v:>8.0f}   (antes "
              f"{prev if prev is None else round(prev)}, {delta})")
    print("   cola:")
    for tid, v in bot:
        print(f"     {names.get(tid, tid)[:28]:<30}{v:>8.0f}")

    # ── 4-6. Escrituras ──────────────────────────────────────────────────────
    elo_docs = {
        tid: {"team_id": typed(tid), "team_name": names.get(tid, f"Team_{tid}"),
              "elo": round(v, 1), "elo_history": history.get(tid, [])[-_ELO_HISTORY_KEEP:],
              "updated_at": now, "rebuilt_at": now}
        for tid, v in elo.items()
    }
    huerfanos = [d["_id"] for d in old_elo if d["_id"] not in elo_docs]

    # team_stats de los clubes UEFA sembrados
    stats_docs = build_uefa_team_stats(club_hist, clubs, team_stats, imap, names)

    print(f"\n4. team_elo: {len(elo_docs)} docs a escribir, {len(huerfanos)} huérfanos "
          f"(equipos sin partidos en el universo) a borrar")
    print(f"5. elo_applied: {len(applied)} huellas")
    print(f"6. team_stats sembrados desde UEFA: {len(stats_docs)}")

    if not args.confirm:
        print("\n[dry-run] nada escrito. Repetir con --confirm para ejecutar.")
        return

    db.write_docs("team_elo", elo_docs)
    if huerfanos:
        db.delete_docs("team_elo", huerfanos)
    db.write_docs("elo_applied", {fp: {**meta, "source": "rebuild", "applied_at": now}
                                  for fp, meta in applied.items()})
    if stats_docs:
        db.write_docs("team_stats", stats_docs)

    # Marcador de época: learning_engine lo usa para separar las señales emitidas con el
    # ELO corrupto de las posteriores.
    db.write_docs("model_weights", {"elo_rebuild": {
        "rebuilt_at": now,
        "teams": len(elo_docs),
        "matches_applied": len(applied),
        "uefa_clubs_seeded": len(stats_docs),
        "backup_collection": backup_col,
        "reason": "re-aplicacion de resultados en update_all_elos (sin ledger) hasta 2026-08-19",
    }})

    if not args.no_reset_weights:
        reset_weights(db, now)

    # La semana en curso queda a caballo entre las dos épocas: lo ya acumulado en su fila
    # de accuracy_log salió del ELO corrupto y lo que venga después, no. Se marca para que
    # el corte sea legible al leer el histórico.
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    actual_log = {d["_id"]: d for d in db.read_collection("accuracy_log")}
    if week in actual_log:
        fila = {k: v for k, v in actual_log[week].items() if k != "_id"}
        fila["elo_rebuild_boundary"] = True
        fila["elo_rebuild_at"] = now
        db.write_docs("accuracy_log", {week: fila})
        print(f"   accuracy_log/{week} marcado como frontera entre épocas")

    print("\nHecho. Comprobar con: python scripts/diagnose_elo_drift.py "
          f"(y contra la copia: --elo-collection {backup_col})")


def build_uefa_team_stats(club_hist: list[dict], clubs: dict[int, str],
                          team_stats: list[dict], imap: dict, names: dict) -> dict[str, dict]:
    """
    Docs de team_stats para los clubes sembrados, con el mismo schema que save_team_stats
    (raw_matches + derivadas), fusionando con el histórico que ya hubiera guardado.
    """
    existing = {str(t.get("team_id")): t for t in team_stats if t.get("team_id") is not None}
    por_equipo: dict[str, list[dict]] = {}

    for m in club_hist:
        if m.get("goals_home") is None or m.get("goals_away") is None:
            continue
        h = resolve(m["home_team"], m["home_source_id"], imap)
        a = resolve(m["away_team"], m["away_source_id"], imap)
        for tid in (h, a):
            por_equipo.setdefault(tid, []).append({
                "match_id": m["match_id"],
                "date": m.get("date", ""),
                "home_team_id": typed(h),
                "away_team_id": typed(a),
                "goals_home": m["goals_home"],
                "goals_away": m["goals_away"],
                "was_home": typed(h) == typed(tid),
            })

    out: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    for tid, fresh in por_equipo.items():
        prev = existing.get(tid, {})
        merged = {str(x.get("match_id")): x for x in (prev.get("raw_matches") or [])}
        for x in fresh:
            merged[str(x["match_id"])] = x
        raw = sorted(merged.values(), key=lambda x: str(x.get("date", "")),
                     reverse=True)[:_RAW_MATCHES_KEEP]

        results = build_results_list(raw, typed(tid))
        last_10 = results[:10]
        home_stats, away_stats = calculate_home_away_split(raw, typed(tid))
        xg_matches = [
            {"goals_scored": (x["goals_home"] if x.get("was_home") else x["goals_away"]),
             "goals_conceded": (x["goals_away"] if x.get("was_home") else x["goals_home"])}
            for x in raw
        ]
        out[tid] = {
            "team_id": typed(tid),
            "team_name": prev.get("team_name") or names.get(tid, f"Team_{tid}"),
            "league": prev.get("league", ""),
            "last_10": last_10,
            "form_score": calculate_form_score(last_10),
            "home_stats": home_stats,
            "away_stats": away_stats,
            "streak": detect_streak(last_10),
            "xg_per_game": calculate_xg_proxy(xg_matches),
            "raw_matches": raw,
            "updated_at": now,
            "seeded_from": "allsportsapi2",
        }
    return out


def reset_weights(db, now: datetime) -> None:
    """
    Resetea model_weights/current a DEFAULT_WEIGHTS: los pesos aprendidos se ajustaron con
    el ELO corrupto como feature, así que arrastrarlos propaga la distorsión. Los contadores
    acumulados también se ponen a cero para que la accuracy no mezcle épocas.
    """
    from shared.config import DEFAULT_WEIGHTS
    actual = {d["_id"]: d for d in db.read_collection("model_weights")}
    cur = actual.get("current", {})
    version = int(cur.get("version", 0)) + 1
    db.write_docs("model_weights", {"current": {
        "version": version,
        "updated": now,
        "weights": dict(DEFAULT_WEIGHTS),
        "accuracy_by_league": {},
        "accuracy_by_market": {},
        "accuracy_by_confidence": {},
        "blacklisted_leagues": [],
        "min_edge_threshold": cur.get("min_edge_threshold", 0.08),
        "min_confidence": cur.get("min_confidence", 0.65),
        "total_predictions": 0,
        "correct_predictions": 0,
        "groq_predictions_count": 0,
        "weights_before_reset": cur.get("weights", {}),
        "reset_reason": "rebuild de ELO — pesos aprendidos sobre un feature corrupto",
    }})
    print(f"   model_weights/current reseteado a DEFAULT_WEIGHTS (version {version}); "
          f"pesos anteriores guardados en weights_before_reset: "
          f"{json.dumps(cur.get('weights', {}), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
