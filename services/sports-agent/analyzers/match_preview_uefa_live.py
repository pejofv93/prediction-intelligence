"""
services/sports-agent/analyzers/match_preview_uefa_live.py
Fichas de Europa (mp-e1: Champions, Europa League, Conference League) y su graduación. Mismo
esquema que las copas (match_preview_cups_live), con el modelo de match_preview_uefa.

Lista de próximos partidos (Highlightly): por fecha, /matches?leagueId={id}&date={día}, con los ids de
la temporada en curso de UEFA_SEASONS (highlightly_loader). Cada (id, día) se lista UNA vez al entrar
en la ventana de WINDOW_DAYS días y se guarda en match_previews_state/uefa. Tope propio al día
(contador highlightly_uefa_fixtures): HL_UEFA_FIXTURES_DAILY_PRO hasta HL_PRO_UNTIL y
HL_UEFA_FIXTURES_DAILY después. Hasta HL_PRO_UNTIL, las tres competiciones; después SOLO Champions y
Europa League (la Conference no se puede graduar con la cuota gratuita: el mantenimiento solo pide el
detalle de CL y EL con ficha).

Modelo: el historial de liga de las 8 ligas, las segundas y B1/T1/G1/SC0 (CSV de football-data.co.uk) y
los partidos europeos ya cargados (hl_team_stats grupo uefa, esta temporada y la anterior); cada ficha
solo con lo jugado antes de su fecha. Equipo de Highlightly → nombre de football-data: por id (mapa de
votos sobre hl_matches, guardado en el estado y recalculado cada ID_MAP_DAYS días, como copas) y, si no,
por parecido único. Un equipo sin datos de liga sale con su nombre de Highlightly y la ficha en
"muestra insuficiente".

Etiquetas: las de match_preview_metrics/mp-e1_backtest. Desde el 27-nov (decisión del usuario) con la
regla de la Euroliga más una condición: "ventaja medida" (⭐, la única que da píldoras en Telegram y en
la web) si el backtest la mide en todo el periodo y en las temporadas limpias, y en 2025/26 + 2026/27
juntas el modelo no sale peor que la referencia (mu.star_labels); si no, "solo referencia". mc.final_labels con match_preview_metrics/mp-e1_live solo puede añadir ⭐ a un mercado
"medido en backtest" (documentos antiguos), nunca quitarla. Sin el documento del backtest no se
escriben fichas.
Graduación (run_uefa_grade): la lanza el mantenimiento diario de Highlightly (job maint), que lista los
(id, día) de las fichas y pide /statistics solo de esos partidos → match_preview_grades_uefa y
match_preview_metrics/mp-e1_live.
Fichas → match_previews_uefa/{comp}_{fecha}_{id Highlightly}; grupo "uefa", div = comp. dry_run=True
por defecto: no escribe fichas, pero sí guarda el listado nuevo (ya está pagado).
Resúmenes del log: solo recuentos.
"""
import asyncio
import logging
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from analyzers import match_preview as mp
from analyzers import match_preview_cups as mc
from analyzers import match_preview_cups_live as ml
from analyzers import match_preview_uefa as mu
from analyzers.match_preview_store import (
    GRADE_AFTER_H, GRADE_LOOKBACK_DAYS, METRICS, NO_RESULT_DAYS, _get_doc, _get_many, _parse, _set_docs,
    content_hash, decide, freeze_at, grade_preview, validity,
)

logger = logging.getLogger(__name__)

PREVIEWS_U = "match_previews_uefa"
STATE_DOC = ("match_previews_state", "uefa")
BACKTEST_DOC = "mp-e1_backtest"
LIVE_DOC = "mp-e1_live"
GRADES_U = "match_preview_grades_uefa"
MODEL_VERSION = mu.MODEL_VERSION
HL_UEFA_FIXTURES_API = "highlightly_uefa_fixtures"   # contador propio
HL_UEFA_FIXTURES_DAILY = 4          # tras el PRO: CL y EL, un día nuevo cada una ≈ 2 al día
HL_UEFA_FIXTURES_DAILY_PRO = 15
WINDOW_DAYS = 3
POST_PRO_COMPS = ("CL", "EL")


def live_comps(today: date) -> tuple[str, ...]:
    """Competiciones con ficha: las tres con el plan PRO; después, solo CL y EL."""
    from collectors.highlightly_players import HL_PRO_UNTIL
    return mu.COMPS if today <= HL_PRO_UNTIL else POST_PRO_COMPS


def live_league_ids(now: datetime, seasons: Optional[dict] = None) -> list[tuple[int, str]]:
    """[(id de Highlightly, comp)] de la temporada en curso de las competiciones con ficha."""
    from collectors import highlightly_loader as L
    seasons = L.UEFA_SEASONS if seasons is None else seasons
    cur = ml.current_hl_season(now)
    comps = live_comps(now.date())
    return sorted({(lid, c) for c in comps for lid, y in seasons.get(c, []) if y == cur},
                  key=lambda t: (mu.COMPS.index(t[1]), t[0]))


def fixtures_cap(today: Optional[date] = None) -> int:
    from collectors.highlightly_players import HL_PRO_UNTIL
    today = today or datetime.now(timezone.utc).date()
    return HL_UEFA_FIXTURES_DAILY_PRO if today <= HL_PRO_UNTIL else HL_UEFA_FIXTURES_DAILY


def pending_lists(state: dict, now: datetime, lids: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """(id, comp, día) de la ventana que aún no se han listado."""
    done = state.get("listed") or {}
    return [(lid, comp, d) for d in ml.window_days(now, WINDOW_DAYS) for lid, comp in lids
            if f"{lid}|{d}" not in done]


def fixtures_from_day(listing: list[dict], league_id: int, comp: str, hl_season: int) -> list[dict]:
    from collectors.highlightly_loader import listing_meta
    out = []
    for raw in listing or []:
        m = listing_meta(raw, "uefa", league_id, hl_season)
        if m["finished"] or not m["kickoff_utc"] or m["home_team_id"] is None or m["away_team_id"] is None:
            continue
        try:
            ko = _parse(m["kickoff_utc"])
        except (TypeError, ValueError):
            continue
        out.append({"hl_match_id": m["match_id"], "league_id": league_id, "comp": comp,
                    "date": ko.date().isoformat(), "kickoff_utc": ko.isoformat(),
                    "hl_home": m["home_team"], "hl_away": m["away_team"],
                    "hl_home_id": m["home_team_id"], "hl_away_id": m["away_team_id"]})
    return out


async def list_uefa_fixtures(now: datetime, api_key: Optional[str], quota, state: dict, client=None,
                             lids: Optional[list[tuple[int, str]]] = None) -> dict:
    """Lista los (id, día) pendientes de la ventana mientras quede tope. No escribe nada."""
    import httpx
    from collectors import highlightly_players as hp
    from collectors.highlightly_loader import _Budget, _list_day

    lids = live_league_ids(now) if lids is None else lids
    st = ml.prune_state(state, now)
    out = {"hl_requests": 0, "listed": 0, "pending": 0, "errors": []}
    if not lids:
        out["errors"].append("sin ids de Europa de la temporada en curso en UEFA_SEASONS: no se lista nada")
    todo = pending_lists(st, now, lids)
    if todo and api_key and quota is not None:
        from shared.api_quota_manager import daily_period
        mine = quota._get_doc(f"{HL_UEFA_FIXTURES_API}_{daily_period(HL_UEFA_FIXTURES_API)}")
        total = quota._get_doc(f"{hp.HL_QUOTA_API}_{daily_period(hp.HL_QUOTA_API)}")
        left = fixtures_cap(now.date()) - int(mine.get("used", 0) or 0)
        if total.get("remaining_reported") is not None:
            left = min(left, int(total["remaining_reported"]))
        budget = _Budget(max(0, left))
        season = ml.current_hl_season(now)
        own = client is None
        client = client or httpx.AsyncClient(timeout=30.0)
        try:
            for lid, comp, day in todo:
                before = budget.used
                try:
                    listing = await _list_day(client, api_key, quota, budget, lid, day)
                except hp.HighlightlyBudgetExhausted:
                    listing = None
                    out["errors"].append("cuota de Highlightly agotada")
                for _ in range(budget.used - before):
                    quota.track_call(HL_UEFA_FIXTURES_API)
                if listing is None:
                    continue
                st["listed"][f"{lid}|{day}"] = now.isoformat()
                st["fixtures"] = [f for f in st["fixtures"] if not (f["league_id"] == lid and f["date"] == day)]
                st["fixtures"] += fixtures_from_day(listing, lid, comp, season)
                out["listed"] += 1
        finally:
            if own:
                await client.aclose()
        out["hl_requests"] = budget.used
    elif todo and not api_key:
        out["errors"].append("sin HIGHLIGHTLY_API_KEY: se usa solo lo guardado")
    out["pending"] = len(pending_lists(st, now, lids))
    out["state"] = st
    return out


# ── Historial y motor ────────────────────────────────────────────────────────────

async def load_history(season: str, client=None) -> tuple[list[dict], list[str]]:
    """Partidos de liga de `season` y de la anterior en todas las ligas con CSV (ALL_DIVS)."""
    import httpx
    from collectors.fdco_team_matches import _to_team_rows, fetch_csv_rows
    rows, failed = [], []
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        for div in mu.ALL_DIVS:
            for s in (season, mp.prev_season(season)):
                raw = await fetch_csv_rows(client, div, s)
                if raw is None:
                    failed.append(f"{div}_{s}")
                else:
                    rows.extend(_to_team_rows(raw, div, s))
                await asyncio.sleep(0.3)        # cortesía con football-data.co.uk
    finally:
        if own:
            await client.aclose()
    return mp.matches_from_team_rows(rows), failed


def _read_uefa_docs(seasons: set) -> list[dict]:
    from shared.firestore_client import iter_all
    docs = [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "uefa"))]
    return [d for d in docs if d.get("date") and mc.season_of(d["date"][:10]) in seasons]


def build_engine(hist: mp.History, resolve, uefa_docs: list[dict], today: str) -> tuple[mu.Engine, Counter]:
    """Motor con los partidos europeos anteriores a `today` (sin filas de backtest) y β ajustada."""
    counts = Counter()
    prior = [m for m in mu.uefa_matches(uefa_docs, resolve, counts) if m["date"] < today]
    engine = mu.Engine(hist, mu.team_divs_all(hist))
    mu.backtest_rows(engine, prior, Counter(), seasons=())
    engine.refit()
    counts["europeos_previos"] = len(prior)
    return engine, counts


def uefa_fixture(f: dict, season: str, resolve) -> dict:
    """Partido de Highlightly → fixture (nombres de football-data si se emparejan; si no, el de
    Highlightly y la marca *_resolved = False)."""
    home = resolve(f["hl_home_id"], f["hl_home"], season)
    away = resolve(f["hl_away_id"], f["hl_away"], season)
    return {"match_id": f"{f['comp']}_{f['date']}_{f['hl_match_id']}", "div": f["comp"], "comp": f["comp"],
            "comp_name": mu.COMP_NAMES[f["comp"]], "league_id": f["league_id"], "date": f["date"],
            "kickoff_utc": f["kickoff_utc"], "home_team": home or f["hl_home"], "away_team": away or f["hl_away"],
            "home_resolved": home is not None, "away_resolved": away is not None,
            "hl_match_id": f["hl_match_id"], "source": "highlightly"}


def labels_for(backtest: Optional[dict], live: Optional[dict] = None) -> dict[str, str]:
    """Etiquetas del backtest, con la estrella si el directo ya la habilita (regla de copas)."""
    return mc.final_labels((backtest or {}).get("labels"), (live or {}).get("metrics"))


def uefa_preview_content(fx: dict, pred: dict, labels_source: Optional[str]) -> dict:
    return {**fx, **pred, "model_version": MODEL_VERSION, "group": "uefa", "labels_source": labels_source,
            "label_policy": "ventaja medida en backtest: todo el periodo y temporadas limpias; 2025/26 + 2026/27 no peor"}


async def run_uefa_previews(dry_run: bool = True, now: Optional[datetime] = None, highlightly: bool = True,
                            state: Optional[dict] = None, matches: Optional[list[dict]] = None,
                            uefa_docs: Optional[list[dict]] = None, backtest: Optional[dict] = None,
                            live: Optional[dict] = None, existing: Optional[dict] = None, client=None,
                            read_hl_matches=None, lids: Optional[list] = None,
                            collect: Optional[dict] = None) -> dict:
    """Fichas de Europa de los próximos WINDOW_DAYS días. Los parámetros opcionales son para tests
    (None = leer de Firestore / football-data). collect: si se pasa, recibe {match_id: ficha} de
    todas las fichas calculadas (la prueba sin escribir, scripts/ops/uefa_preview_show.py)."""
    from collectors.highlightly_players import team_similarity

    now = now or datetime.now(timezone.utc)
    summary: dict = {"ok": True, "dry_run": dry_run, "now": now.isoformat(), "counts": {}, "by_comp": {},
                     "errors": []}
    if backtest is None:
        backtest = _get_doc(METRICS, BACKTEST_DOC)
        if live is None:
            try:
                live = _get_doc(METRICS, LIVE_DOC)
            except Exception:
                logger.error("uefa_previews: no se pudo leer %s (sin estrellas)", LIVE_DOC, exc_info=True)
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
    lst = await list_uefa_fixtures(now, api_key, quota, state, client=client, lids=lids)
    summary.update(hl_requests=lst["hl_requests"], listed=lst["listed"], pending_lists=lst["pending"])
    summary["errors"] += [e for e in lst["errors"] if not e.startswith("sin ids")]
    if any(e.startswith("sin ids") for e in lst["errors"]):
        summary["aviso"] = lst["errors"][0]
    new_state = lst["state"]
    allowed = set(live_comps(now.date()))
    todo = [f for f in new_state["fixtures"] if now <= _parse(f["kickoff_utc"]) and f["comp"] in allowed]

    season = mc.season_of(now.date().isoformat())
    counts: dict[str, int] = defaultdict(int)
    by_comp: dict = defaultdict(lambda: defaultdict(int))
    to_write: dict[str, dict] = {}
    rebuilt = False
    if todo:
        if matches is None:
            matches, failed = await load_history(season)
            if failed:
                summary["errors"].append(f"aviso: faltan CSV de historial: {failed}")
        hist = mp.History(matches, prev_divs=mu.ALL_DIVS)
        ids, rebuilt = await asyncio.to_thread(ml.id_map, state, now, hist, read_hl_matches or ml._read_hl_matches,
                                               team_similarity)
        if rebuilt:
            new_state["id_names"] = {"updated_at": now.isoformat(), "map": ids}
        summary["id_map"] = {"ids": len(ids), "recalculado": rebuilt}
        tdiv = mu.team_divs_all(hist)
        resolve = mu.make_resolver(ids, tdiv, team_similarity)
        if uefa_docs is None:
            uefa_docs = await asyncio.to_thread(_read_uefa_docs, {season, mp.prev_season(season)})
        engine, ecounts = build_engine(hist, resolve, uefa_docs, now.date().isoformat())
        summary["engine"] = {"europeos_previos": ecounts["europeos_previos"], "n_fit": engine.n_fit,
                             "con_factor": bool(engine.beta)}
        fxs = [uefa_fixture(f, season, resolve) for f in todo]
        if existing is None:
            existing = _get_many(PREVIEWS_U, [fx["match_id"] for fx in fxs]) if fxs else {}
        labels = labels_for(backtest, live)
        for fx in fxs:
            pred = mu.predict_uefa_match(engine, fx["comp"], season, fx["date"],
                                         fx["home_team"] if fx["home_resolved"] else None,
                                         fx["away_team"] if fx["away_resolved"] else None, labels=labels)
            for side in ("home", "away"):        # nombre de pantalla aunque no tenga datos de liga
                pred["teams"][side]["team"] = fx[f"{side}_team"]
            content = uefa_preview_content(fx, pred, BACKTEST_DOC)
            if collect is not None:
                collect[fx["match_id"]] = content
            action = decide(content, existing.get(fx["match_id"]), now)
            counts[action] += 1
            by_comp[fx["comp"]][action] += 1
            if pred["low_sample"]:
                by_comp[fx["comp"]]["muestra insuficiente"] += 1
            by_comp[fx["comp"]]["equipos sin emparejar"] += (not fx["home_resolved"]) + (not fx["away_resolved"])
            if action in ("nueva", "cambia", "tarde"):
                prev = existing.get(fx["match_id"]) or {}
                to_write[fx["match_id"]] = {**content, "season": season, "hash": content_hash(content),
                                            "computed_at": now.isoformat(),
                                            "created_at": prev.get("created_at") or now.isoformat(),
                                            "freeze_at": freeze_at(fx["kickoff_utc"]).isoformat(),
                                            "written_after_freeze": action == "tarde",
                                            "n_writes": prev.get("n_writes", 0) + 1}
        summary["previews"] = [{"match_id": fx["match_id"], "comp": fx["comp"]} for fx in fxs]   # sin equipos
    summary["counts"] = dict(counts)
    summary["by_comp"] = {k: dict(v) for k, v in sorted(by_comp.items())}
    summary["fixtures_window"] = len(todo)
    summary["would_write" if dry_run else "written"] = len(to_write)
    save_state = not dry_run or lst["listed"] > 0 or rebuilt
    try:
        if not dry_run and to_write:
            _set_docs(PREVIEWS_U, to_write)
        if save_state:
            _set_docs(STATE_DOC[0], {STATE_DOC[1]: {**new_state, "last_run": now.isoformat()}})
    except Exception:
        logger.error("uefa_previews: error escribiendo", exc_info=True)
        summary["errors"].append("escritura fallida (ver logs del servicio)")
        summary["ok"] = False
    summary["state_saved"] = bool(save_state) and summary["ok"]
    logger.info("uefa_previews: %s; escribe %d", dict(counts), len(to_write))
    return summary


# ── Graduación (la lanza el mantenimiento diario de Highlightly) ──────────────────

def live_doc(rows: list[tuple], live: dict, backtest: Optional[dict], now: datetime) -> dict:
    """match_preview_metrics/mp-e1_live: métricas en directo, partidos graduados (total y por mercado)
    y las etiquetas que resultan (con estrella si ya toca)."""
    labels = mc.final_labels((backtest or {}).get("labels"), live)
    return {"model_version": MODEL_VERSION, "kind": "live", "group": "uefa", "n_rows": len(rows),
            "n_graduated": len({r[7] for r in rows}), "n_by_market": mc.live_counts(live),
            "stars_after": mc.STARS_AFTER_LIVE, "labels": labels,
            "star_markets": sorted(mk for mk, lab in labels.items() if lab == mp.LABEL_EDGE),
            "rule": ("estrella: ventaja medida en el backtest (todo el periodo y temporadas limpias; 2025/26 + "
                     "2026/27 no peor que la referencia); con un "
                     f"backtest antiguo, medido en backtest + {mc.STARS_AFTER_LIVE} graduados con IC95 en "
                     "directo entero bajo 0"),
            "metrics": live, "computed_at": now.isoformat()}


def _read_uefa_previews_to_grade(now: datetime) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    lo = (now - timedelta(days=GRADE_LOOKBACK_DAYS)).isoformat()
    hi = (now - timedelta(hours=GRADE_AFTER_H)).isoformat()
    q = (col(PREVIEWS_U).where(filter=FieldFilter("kickoff_utc", ">=", lo))
         .where(filter=FieldFilter("kickoff_utc", "<=", hi)))
    return [s.to_dict() for s in q.stream()]


def _read_all_uefa_grades() -> list[dict]:
    from shared.firestore_client import col
    return [s.to_dict() for s in col(GRADES_U).stream()]


async def run_uefa_grade(dry_run: bool = True, now: Optional[datetime] = None,
                         previews: Optional[list[dict]] = None, results: Optional[dict] = None,
                         graded: Optional[dict] = None, all_grades: Optional[list[dict]] = None,
                         backtest: Optional[dict] = None, write=None) -> dict:
    """Gradúa las fichas de Europa congeladas con hl_team_stats/{hl_match_id} (las baja el
    mantenimiento diario). Mismas reglas que copas y mp-v1. Resumen: solo recuentos."""
    now = now or datetime.now(timezone.utc)
    write = write or _set_docs
    summary: dict = {"ok": True, "dry_run": dry_run, "counts": {}, "by_comp": {}, "errors": []}
    previews = _read_uefa_previews_to_grade(now) if previews is None else ml.previews_to_grade(previews, now)
    if graded is None:
        graded = _get_many(GRADES_U, [p["match_id"] for p in previews])
    pending = [p for p in previews if p["match_id"] not in graded]
    if results is None:
        results = _get_many("hl_team_stats", [str(p["hl_match_id"]) for p in pending if p.get("hl_match_id")])
    docs: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    by_comp: dict = defaultdict(lambda: defaultdict(int))
    for p in pending:
        # "country" = competición: así mc.rows_from_cup_grades y las métricas de copas valen tal cual.
        base = {"match_id": p["match_id"], "div": p["div"], "comp": p.get("comp"), "country": p.get("comp"),
                "season": p.get("season"), "date": p["date"], "kickoff_utc": p["kickoff_utc"],
                "home_team": p["home_team"], "away_team": p["away_team"], "hl_match_id": p.get("hl_match_id"),
                "model_version": p.get("model_version"), "group": "uefa", "low_sample": bool(p.get("low_sample")),
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
            by_comp[p.get("comp") or "?"]["esperando estadísticas"] += 1
            continue
        counts[doc["status"]] += 1
        by_comp[p.get("comp") or "?"][doc["status"]] += 1
        docs[p["match_id"]] = doc
    summary["counts"] = dict(counts)
    summary["by_comp"] = {k: dict(v) for k, v in sorted(by_comp.items())}

    if all_grades is None:
        all_grades = _read_all_uefa_grades()
    merged = {g["match_id"]: g for g in all_grades if g.get("match_id")}
    merged.update(docs)
    # Las estrellas se miden como en el backtest: solo partidos con los dos equipos con datos de liga.
    rows = mc.rows_from_cup_grades(g for g in merged.values() if not g.get("low_sample"))
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
                write(GRADES_U, docs)
            if rows:
                write(METRICS, {LIVE_DOC: ldoc})
        except Exception:
            logger.error("uefa_grade: error escribiendo", exc_info=True)
            summary["errors"].append("escritura fallida (ver logs del servicio)")
            summary["ok"] = False
    logger.info("uefa_grade: %s; partidos graduados en total %d", dict(counts), ldoc["n_graduated"])
    return summary
