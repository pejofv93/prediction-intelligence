"""
services/sports-agent/analyzers/match_preview_cups_live.py
Fichas de copa (paso 2 del plan de copas, 19-nov-2026): modelo de clubes mp-v1 sin factor de copa,
con la fuerza de cada equipo en su liga (CSV de football-data.co.uk, el mismo History de mp-v1).

Lista de próximos cruces (Highlightly): por fecha, /matches?leagueId={copa}&date={día}. Cada
(copa, día) se lista UNA vez, cuando entra en la ventana de WINDOW_DAYS días, y se guarda en
match_previews_state/cups (los listados de temporada de algunas copas, como la Coupe de France,
tienen miles de partidos de rondas amateur). Tope propio al día: HL_CUP_FIXTURES_DAILY (30 con el
plan PRO, hasta HL_PRO_UNTIL; 12 después). 9 copas × 1 día nuevo al día ≈ 9 peticiones al día.

Qué cruces llevan ficha (lo mismo que se midió en el backtest): los dos equipos en una división
principal de su país esta temporada y en la MISMA división; sin supercopas. Equipos de Highlightly
→ nombre de football-data: primero por id, con el mapa del backtest (votos sobre los partidos de
liga de Highlightly, hl_matches), guardado en el estado y recalculado cada ID_MAP_DAYS días; si el
id no está, por parecido con los equipos de esas divisiones (único y ≥ MIN_SIM).

Etiquetas: las del documento match_preview_metrics/mp-v1-cups_backtest ("medido en backtest" en
los cinco mercados por equipo; "solo referencia" en el resto). "Ventaja medida" (estrella) solo
cuando el mercado lleva STARS_AFTER_LIVE (50) cruces graduados en directo y su IC95 en directo
queda entero bajo 0 (regla de liga; mc.final_labels con match_preview_metrics/mp-v1-cups_live).
Graduación (run_cup_grade): la lanza el mantenimiento diario de Highlightly (job maint), que lista
los (copa, día) de las fichas de los días que mantiene y pide /statistics de esos cruces; se
gradúa por mercado, equipo y línea como mp-v1 (sin la línea "extra") →
match_preview_grades_cups/{match_id} y match_preview_metrics/mp-v1-cups_live (métricas, cruces
graduados por mercado y etiquetas).
Fichas → match_previews_cups/{país}_{fecha}_{id Highlightly}; grupo "cups". dry_run=True por
defecto: no escribe fichas, pero sí guarda el listado nuevo (ya está pagado).
Datos de Highlightly: SOLO la lista de partidos. Resúmenes del log: solo recuentos.
"""
import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from analyzers import match_preview as mp
from analyzers import match_preview_cups as mc
from analyzers.match_preview_store import (
    GRADE_AFTER_H, GRADE_LOOKBACK_DAYS, METRICS, NO_RESULT_DAYS, _get_doc, _get_many, _parse, _set_docs,
    content_hash, decide, freeze_at, grade_preview, load_matches, validity,
)

logger = logging.getLogger(__name__)

PREVIEWS_C = "match_previews_cups"
STATE_DOC = ("match_previews_state", "cups")
BACKTEST_DOC = "mp-v1-cups_backtest"
LIVE_DOC = "mp-v1-cups_live"
GRADES_C = "match_preview_grades_cups"
MODEL_VERSION = "mp-v1-cups"
HL_CUP_FIXTURES_API = "highlightly_cup_fixtures"     # contador propio
HL_CUP_FIXTURES_DAILY = 12
HL_CUP_FIXTURES_DAILY_PRO = 30
WINDOW_DAYS = 3
MIN_SIM = 0.80
ID_MAP_DAYS = 7
CUP_NAMES = {122477: "Copa del Rey", 39079: "FA Cup", 41632: "League Cup", 117371: "Coppa Italia",
             69715: "DFB-Pokal", 56950: "Coupe de France", 82480: "Taça de Portugal",
             83331: "Taça da Liga", 77374: "KNVB Beker"}
LIVE_CUPS = tuple(lid for lid in mc.CUP_COUNTRY if lid not in mc.SUPERCUPS)


def current_hl_season(now: datetime) -> int:
    return now.year if now.month >= 7 else now.year - 1


def window_days(now: datetime, n: int = WINDOW_DAYS) -> list[str]:
    return [(now.date() + timedelta(days=i)).isoformat() for i in range(n)]


def cup_fixtures_cap(today=None) -> int:
    from collectors.highlightly_players import HL_PRO_UNTIL
    today = today or datetime.now(timezone.utc).date()
    return HL_CUP_FIXTURES_DAILY_PRO if today <= HL_PRO_UNTIL else HL_CUP_FIXTURES_DAILY


def pending_lists(state: dict, now: datetime) -> list[tuple[int, str]]:
    """(copa, día) de la ventana que aún no se han listado."""
    done = state.get("listed") or {}
    return [(lid, d) for d in window_days(now) for lid in LIVE_CUPS if f"{lid}|{d}" not in done]


def prune_state(state: dict, now: datetime) -> dict:
    """Quita los días ya pasados (listado y partidos)."""
    today = now.date().isoformat()
    listed = {k: v for k, v in (state.get("listed") or {}).items() if k.split("|")[1] >= today}
    fixtures = [f for f in state.get("fixtures") or [] if f["date"] >= today]
    return {"listed": listed, "fixtures": fixtures, **({"id_names": state["id_names"]} if state.get("id_names") else {})}


def fixtures_from_day(listing: list[dict], league_id: int, hl_season: int) -> list[dict]:
    from collectors.highlightly_loader import listing_meta
    out = []
    for raw in listing or []:
        m = listing_meta(raw, "cups", league_id, hl_season)
        if m["finished"] or not m["kickoff_utc"] or m["home_team_id"] is None or m["away_team_id"] is None:
            continue
        try:
            ko = _parse(m["kickoff_utc"])
        except (TypeError, ValueError):
            continue
        out.append({"hl_match_id": m["match_id"], "cup": league_id, "country": mc.CUP_COUNTRY[league_id],
                    "date": ko.date().isoformat(), "kickoff_utc": ko.isoformat(),
                    "hl_home": m["home_team"], "hl_away": m["away_team"],
                    "hl_home_id": m["home_team_id"], "hl_away_id": m["away_team_id"]})
    return out


async def list_cup_fixtures(now: datetime, api_key: Optional[str], quota, state: dict, client=None) -> dict:
    """Lista los (copa, día) pendientes de la ventana mientras quede tope. No escribe nada."""
    import httpx
    from collectors import highlightly_players as hp
    from collectors.highlightly_loader import _Budget, _list_day

    st = prune_state(state, now)
    out = {"hl_requests": 0, "listed": 0, "pending": 0, "errors": []}
    todo = pending_lists(st, now)
    if todo and api_key and quota is not None:
        from shared.api_quota_manager import daily_period
        mine = quota._get_doc(f"{HL_CUP_FIXTURES_API}_{daily_period(HL_CUP_FIXTURES_API)}")
        total = quota._get_doc(f"{hp.HL_QUOTA_API}_{daily_period(hp.HL_QUOTA_API)}")
        left = cup_fixtures_cap() - int(mine.get("used", 0) or 0)
        if total.get("remaining_reported") is not None:
            left = min(left, int(total["remaining_reported"]))
        budget = _Budget(max(0, left))
        season = current_hl_season(now)
        own = client is None
        client = client or httpx.AsyncClient(timeout=30.0)
        try:
            for lid, day in todo:
                before = budget.used
                try:
                    listing = await _list_day(client, api_key, quota, budget, lid, day)
                except hp.HighlightlyBudgetExhausted:
                    listing = None
                    out["errors"].append("cuota de Highlightly agotada")
                for _ in range(budget.used - before):
                    quota.track_call(HL_CUP_FIXTURES_API)
                if listing is None:
                    continue
                st["listed"][f"{lid}|{day}"] = now.isoformat()
                st["fixtures"] = [f for f in st["fixtures"] if not (f["cup"] == lid and f["date"] == day)]
                st["fixtures"] += fixtures_from_day(listing, lid, season)
                out["listed"] += 1
        finally:
            if own:
                await client.aclose()
        out["hl_requests"] = budget.used
    elif todo and not api_key:
        out["errors"].append("sin HIGHLIGHTLY_API_KEY: se usa solo lo guardado")
    out["pending"] = len(pending_lists(st, now))
    out["state"] = st
    return out


def map_team(hl_name: str, candidates: set[str], similarity) -> Optional[str]:
    """Nombre de football-data con el mayor parecido, si es único y ≥ MIN_SIM."""
    scored = sorted(((similarity(hl_name or "", c), c) for c in candidates), reverse=True)
    if not scored or scored[0][0] < MIN_SIM or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1]


def id_map(state: dict, now: datetime, hist: mp.History, read_hl_matches, similarity,
           version: Optional[str] = None) -> tuple[dict, bool]:
    """{id de Highlightly (str): nombre de football-data}; recalculado si tiene ID_MAP_DAYS o más, o si
    el guardado es de otra versión de fuentes (version; copas no la usa)."""
    cur = state.get("id_names") or {}
    try:
        fresh = (cur.get("map") and cur.get("version") == version
                 and now - _parse(cur["updated_at"]) < timedelta(days=ID_MAP_DAYS))
    except (KeyError, TypeError, ValueError):
        fresh = False
    if fresh:
        return cur["map"], False
    csv_by_div_day: dict = {}
    for m in hist.by_ds.values():
        for x in m:
            csv_by_div_day.setdefault((x["div"], x["date"]), []).append(x)
    ids = mc.hl_id_names(read_hl_matches(), csv_by_div_day, similarity)
    return {str(k): v for k, v in ids.items()}, True


def _read_hl_matches() -> list[dict]:
    from shared.firestore_client import iter_all
    return [d.to_dict() or {} for d in iter_all("hl_matches")]


def cup_fixture(f: dict, tdiv: dict[tuple[str, str], str], season: str, similarity,
                ids: Optional[dict] = None) -> tuple[Optional[dict], str]:
    """Cruce de Highlightly → fixture con nombres y división, o (None, motivo del descarte)."""
    divs = mc.COUNTRY_DIVS[f["country"]]
    cands = {t for (t, s), d in tdiv.items() if s == season and d in divs}
    ids = ids or {}

    def name(hl_id, hl_name):
        n = ids.get(str(hl_id))
        return n if n in cands else map_team(hl_name, cands, similarity)

    home, away = name(f["hl_home_id"], f["hl_home"]), name(f["hl_away_id"], f["hl_away"])
    if not home or not away:
        return None, "equipo sin emparejar o fuera de las ligas principales"
    dh, da = tdiv[(home, season)], tdiv[(away, season)]
    if dh != da:
        return None, "distinta división"
    return {"match_id": f"{f['country']}_{f['date']}_{f['hl_match_id']}", "div": dh,
            "cup": CUP_NAMES.get(f["cup"], str(f["cup"])), "cup_id": f["cup"], "country": f["country"],
            "date": f["date"], "kickoff_utc": f["kickoff_utc"], "home_team": home, "away_team": away,
            "hl_match_id": f["hl_match_id"], "source": "highlightly"}, ""


def labels_for(doc: Optional[dict], div: str, live: Optional[dict] = None) -> dict[tuple[str, str], str]:
    """Etiquetas del backtest, con la estrella si el directo ya la habilita (mc.final_labels)."""
    labels = mc.final_labels((doc or {}).get("labels"), (live or {}).get("metrics"))
    return {(mk, div): lab for mk, lab in labels.items()}


def cup_preview_content(fx: dict, pred: dict, labels_source: Optional[str]) -> dict:
    return {**fx, **pred, "model_version": MODEL_VERSION, "group": "cups", "labels_source": labels_source,
            "label_policy": "medido en backtest; estrella tras ~50 cruces graduados en directo"}


async def run_cup_previews(dry_run: bool = True, now: Optional[datetime] = None, highlightly: bool = True,
                           state: Optional[dict] = None, matches: Optional[list[dict]] = None,
                           backtest: Optional[dict] = None, existing: Optional[dict] = None,
                           client=None, read_hl_matches=None, live: Optional[dict] = None) -> dict:
    """Fichas de copa de los próximos WINDOW_DAYS días. state/matches/backtest/existing/client/live:
    tests (live solo se lee de Firestore si tampoco se pasa backtest)."""
    from collectors.highlightly_players import team_similarity

    now = now or datetime.now(timezone.utc)
    summary: dict = {"ok": True, "dry_run": dry_run, "now": now.isoformat(), "counts": {}, "by_cup": {},
                     "errors": []}
    if backtest is None:
        backtest = _get_doc(METRICS, BACKTEST_DOC)
        if live is None:
            try:
                live = _get_doc(METRICS, LIVE_DOC)
            except Exception:
                logger.error("cup_previews: no se pudo leer %s (sin estrellas)", LIVE_DOC, exc_info=True)
    if not backtest:
        summary.update(ok=False, errors=[f"sin {METRICS}/{BACKTEST_DOC}: no se escriben fichas"])
        return summary
    if state is None:
        state = _get_doc(*STATE_DOC) or {}
    api_key = os.environ.get("HIGHLIGHTLY_API_KEY") if highlightly else None
    quota = None
    if api_key:
        from shared.api_quota_manager import QuotaManager
        quota = QuotaManager()
    lst = await list_cup_fixtures(now, api_key, quota, state, client=client)
    summary.update(hl_requests=lst["hl_requests"], listed=lst["listed"], pending_lists=lst["pending"])
    summary["errors"] += lst["errors"]
    new_state = lst["state"]

    season = mc.season_of(now.date().isoformat())
    if matches is None:
        matches, failed = await load_matches(season)
        if failed:
            summary["errors"].append(f"aviso: faltan CSV de historial: {failed}")
    hist = mp.History(matches)
    tdiv = mc.team_divs(hist)
    todo_any = any(now <= _parse(f["kickoff_utc"]) for f in new_state["fixtures"])
    ids, rebuilt = ({}, False)
    if todo_any:
        ids, rebuilt = await asyncio.to_thread(id_map, state, now, hist, read_hl_matches or _read_hl_matches,
                                               team_similarity)
        if rebuilt:
            new_state["id_names"] = {"updated_at": now.isoformat(), "map": ids}
    summary["id_map"] = {"ids": len(ids), "recalculado": rebuilt}

    todo = [f for f in new_state["fixtures"] if now <= _parse(f["kickoff_utc"])]
    counts: dict[str, int] = defaultdict(int)
    by_cup: dict = defaultdict(lambda: defaultdict(int))
    fxs = []
    for f in todo:
        cup = CUP_NAMES.get(f["cup"], str(f["cup"]))
        fx, why = cup_fixture(f, tdiv, season, team_similarity, ids)
        if fx is None:
            counts["sin ficha: " + why] += 1
            by_cup[cup]["sin ficha"] += 1
            continue
        fxs.append(fx)
    if existing is None:
        existing = _get_many(PREVIEWS_C, [fx["match_id"] for fx in fxs]) if fxs else {}
    to_write: dict[str, dict] = {}
    for fx in fxs:
        pred = mp.predict_match(hist, fx["div"], season, fx["date"], fx["home_team"], fx["away_team"],
                                labels=labels_for(backtest, fx["div"], live))
        content = cup_preview_content(fx, pred, BACKTEST_DOC)
        action = decide(content, existing.get(fx["match_id"]), now)
        counts[action] += 1
        by_cup[fx["cup"]][action] += 1
        if pred["low_sample"]:
            by_cup[fx["cup"]]["muestra insuficiente"] += 1
        if action in ("nueva", "cambia", "tarde"):
            prev = existing.get(fx["match_id"]) or {}
            to_write[fx["match_id"]] = {**content, "season": season, "hash": content_hash(content),
                                        "computed_at": now.isoformat(),
                                        "created_at": prev.get("created_at") or now.isoformat(),
                                        "freeze_at": freeze_at(fx["kickoff_utc"]).isoformat(),
                                        "written_after_freeze": action == "tarde",
                                        "n_writes": prev.get("n_writes", 0) + 1}
    summary["counts"] = dict(counts)
    summary["by_cup"] = {k: dict(v) for k, v in sorted(by_cup.items())}
    summary["fixtures_window"] = len(todo)
    summary["would_write" if dry_run else "written"] = len(to_write)
    save_state = not dry_run or lst["listed"] > 0 or rebuilt
    try:
        if not dry_run and to_write:
            _set_docs(PREVIEWS_C, to_write)
        if save_state:
            _set_docs(STATE_DOC[0], {STATE_DOC[1]: {**new_state, "last_run": now.isoformat()}})
    except Exception:
        logger.error("cup_previews: error escribiendo", exc_info=True)
        summary["errors"].append("escritura fallida (ver logs del servicio)")
        summary["ok"] = False
    summary["state_saved"] = bool(save_state) and summary["ok"]
    summary["previews"] = [{"match_id": fx["match_id"], "cup": fx["cup"]} for fx in fxs]   # sin equipos
    logger.info("cup_previews: %s; escribe %d", dict(counts), len(to_write))
    return summary


# ── Graduación (la lanza el mantenimiento diario de Highlightly) ──────────────────

def previews_to_grade(previews: list[dict], now: datetime) -> list[dict]:
    """Fichas empezadas hace más de GRADE_AFTER_H horas y menos de GRADE_LOOKBACK_DAYS días."""
    lo, hi = now - timedelta(days=GRADE_LOOKBACK_DAYS), now - timedelta(hours=GRADE_AFTER_H)
    out = []
    for p in previews:
        try:
            if lo <= _parse(p["kickoff_utc"]) <= hi:
                out.append(p)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _read_cup_previews_to_grade(now: datetime) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    lo = (now - timedelta(days=GRADE_LOOKBACK_DAYS)).isoformat()
    hi = (now - timedelta(hours=GRADE_AFTER_H)).isoformat()
    q = (col(PREVIEWS_C).where(filter=FieldFilter("kickoff_utc", ">=", lo))
         .where(filter=FieldFilter("kickoff_utc", "<=", hi)))
    return [s.to_dict() for s in q.stream()]


def _read_all_cup_grades() -> list[dict]:
    from shared.firestore_client import col
    return [s.to_dict() for s in col(GRADES_C).stream()]


def live_doc(rows: list[tuple], live: dict, backtest: Optional[dict], now: datetime) -> dict:
    """match_preview_metrics/mp-v1-cups_live: métricas en directo, contador de cruces graduados
    (total y por mercado) y las etiquetas que resultan (con estrella si ya toca)."""
    labels = mc.final_labels((backtest or {}).get("labels"), live)
    return {"model_version": MODEL_VERSION, "kind": "live", "group": "cups", "n_rows": len(rows),
            "n_graduated": len({r[7] for r in rows}), "n_by_market": mc.live_counts(live),
            "stars_after": mc.STARS_AFTER_LIVE, "labels": labels,
            "star_markets": sorted(mk for mk, lab in labels.items() if lab == mp.LABEL_EDGE),
            "rule": (f"estrella: medido en backtest + al menos {mc.STARS_AFTER_LIVE} cruces graduados "
                     "+ IC95 en directo entero bajo 0 (todos los países)"),
            "metrics": live, "computed_at": now.isoformat()}


async def run_cup_grade(dry_run: bool = True, now: Optional[datetime] = None,
                        previews: Optional[list[dict]] = None, results: Optional[dict] = None,
                        graded: Optional[dict] = None, all_grades: Optional[list[dict]] = None,
                        backtest: Optional[dict] = None, write=None) -> dict:
    """Gradúa las fichas de copa congeladas con las estadísticas de hl_team_stats/{hl_match_id}
    (las baja el mantenimiento diario). Mismas reglas que mp-v1: anulada si se escribió tras el
    inicio o usó datos del día del partido; "sin resultado" a los NO_RESULT_DAYS días.
    previews/results/graded/all_grades/backtest/write: el mantenimiento los pasa con su almacén
    (None = leer/escribir en Firestore). Resumen: solo recuentos."""
    now = now or datetime.now(timezone.utc)
    write = write or _set_docs
    summary: dict = {"ok": True, "dry_run": dry_run, "counts": {}, "by_cup": {}, "errors": []}
    previews = _read_cup_previews_to_grade(now) if previews is None else previews_to_grade(previews, now)
    if graded is None:
        graded = _get_many(GRADES_C, [p["match_id"] for p in previews])
    pending = [p for p in previews if p["match_id"] not in graded]
    if results is None:
        results = _get_many("hl_team_stats", [str(p["hl_match_id"]) for p in pending if p.get("hl_match_id")])
    docs: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    by_cup: dict = defaultdict(lambda: defaultdict(int))
    for p in pending:
        base = {"match_id": p["match_id"], "div": p["div"], "country": p.get("country"), "cup": p.get("cup"),
                "cup_id": p.get("cup_id"), "season": p.get("season"), "date": p["date"],
                "kickoff_utc": p["kickoff_utc"], "home_team": p["home_team"], "away_team": p["away_team"],
                "hl_match_id": p.get("hl_match_id"), "model_version": p.get("model_version"), "group": "cups",
                "preview_hash": p.get("hash"), "preview_computed_at": p.get("computed_at"),
                "graded_at": now.isoformat()}
        why = validity(p)
        res = mc.cup_result(results.get(str(p.get("hl_match_id"))))
        if why:
            doc = {**base, "status": "anulada", "reason": why}
        elif res is not None:
            doc = {**base, "status": "graduada", **grade_preview(p, res)}
        elif now - _parse(p["kickoff_utc"]) >= timedelta(days=NO_RESULT_DAYS):
            doc = {**base, "status": "sin resultado"}
        else:
            counts["esperando estadísticas"] += 1
            by_cup[p.get("cup") or "?"]["esperando estadísticas"] += 1
            continue
        counts[doc["status"]] += 1
        by_cup[p.get("cup") or "?"][doc["status"]] += 1
        docs[p["match_id"]] = doc
    summary["counts"] = dict(counts)
    summary["by_cup"] = {k: dict(v) for k, v in sorted(by_cup.items())}

    if all_grades is None:
        all_grades = _read_all_cup_grades()
    merged = {g["match_id"]: g for g in all_grades if g.get("match_id")}
    merged.update(docs)
    rows = mc.rows_from_cup_grades(merged.values())
    live = mp.market_metrics(rows) if rows else {}
    if backtest is None:
        backtest = _get_doc(METRICS, BACKTEST_DOC)
    ldoc = live_doc(rows, live, backtest, now)
    summary.update(n_graduated=ldoc["n_graduated"], n_by_market=ldoc["n_by_market"],
                   stars_after=mc.STARS_AFTER_LIVE, star_markets=ldoc["star_markets"])
    summary["would_write" if dry_run else "written"] = len(docs)
    if not dry_run:
        try:
            if docs:
                write(GRADES_C, docs)
            if rows:
                write(METRICS, {LIVE_DOC: ldoc})
        except Exception:
            logger.error("cup_grade: error escribiendo", exc_info=True)
            summary["errors"].append("escritura fallida (ver logs del servicio)")
            summary["ok"] = False
    logger.info("cup_grade: %s; cruces graduados en total %d", dict(counts), ldoc["n_graduated"])
    return summary
