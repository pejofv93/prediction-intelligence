"""
services/sports-agent/analyzers/basket_preview_live.py
Fichas de baloncesto de Tendencias (bp-v1, de momento solo Euroliga) y su graduación. Mismo esquema
que las fichas de fútbol (match_preview_store) y de Europa (match_preview_uefa_live): se recalculan si
cambia el contenido, se congelan 2 h antes del inicio y se gradúa la versión congelada.

Lista de próximos partidos: el feed de la Euroliga (incrowdsports, sin clave, ~4 páginas por pasada;
collectors.basketball_collector._fetch_eur_season_games), los no terminados de los próximos
WINDOW_DAYS días. Historial: eur_games (lo carga la tarea diaria sports-eur-tennis-daily, 06:30 UTC),
temporada en curso y anterior, solo partidos de días anteriores a hoy.

Líneas y etiquetas: las de match_preview_metrics/bp-v1-EUR_backtest (scripts/ops/basket_backtest.py
con confirmar=ESCRIBIR). Sin ese documento no se escriben fichas (las líneas salen de él). Como en las
ligas de fútbol (mp-v1), un mercado con «ventaja medida» en el backtest (IC95 entero bajo 0 frente a
la media de la liga y a la del equipo) lleva highlight y da píldoras ⭐ en la web y en Telegram, sin
esperar a la graduación en directo (eso es solo de copas, Europa y femenino).

Fichas → match_previews_basket/EUR_{temporada}_{gamecode}; grupo "basket", div = liga ("EUR").
Graduación (run_basket_grade) contra eur_games → match_preview_grades_basket y
match_preview_metrics/bp-v1-EUR_live (métricas en directo frente a las dos medias; informativas).
dry_run=True por defecto: calcula y cuenta, no escribe. Resúmenes del log: solo recuentos.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from analyzers import basket_preview as bp
from analyzers.match_preview_store import (
    GRADE_AFTER_H, GRADE_LOOKBACK_DAYS, METRICS, NO_RESULT_DAYS, _get_doc, _get_many, _parse, _set_docs,
    content_hash, decide, freeze_at, validity,
)

logger = logging.getLogger(__name__)

PREVIEWS_B = "match_previews_basket"
GROUP = "basket"
LEAGUE = "EUR"
LEAGUE_NAMES = {"EUR": "Euroliga"}
WINDOW_DAYS = 3
BACKTEST_DOC = bp.metrics_doc_id(LEAGUE, "backtest")
GRADES_B = "match_preview_grades_basket"
LIVE_DOC = bp.metrics_doc_id(LEAGUE, "live")


def _kickoff(raw) -> Optional[datetime]:
    """Fecha del feed ('2026-11-26T19:00:00.000Z') → datetime UTC; sin zona se toma como UTC."""
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fixtures_from_feed(games: list[dict], now: datetime, days: int = WINDOW_DAYS,
                       league: str = LEAGUE) -> list[dict]:
    """Partidos del feed sin terminar que empiezan entre ahora y now + days."""
    hi = now + timedelta(days=days)
    out = []
    for g in games or []:
        if str(g.get("status") or "").lower() == "result":
            continue
        ko = _kickoff(g.get("date"))
        home, away = g.get("home") or {}, g.get("away") or {}
        if ko is None or not now <= ko <= hi or not home.get("code") or not away.get("code"):
            continue
        season = (g.get("season") or {}).get("code") or str(g.get("identifier", "_")).split("_")[0]
        try:
            code = int(g.get("code"))
        except (TypeError, ValueError):
            continue
        out.append({"match_id": f"{league}_{season}_{code}", "div": league, "league": league,
                    "league_name": LEAGUE_NAMES.get(league, league), "season": season, "gamecode": code,
                    "date": ko.date().isoformat(), "kickoff_utc": ko.isoformat(),
                    "phase": (g.get("phaseType") or {}).get("code"), "round": (g.get("round") or {}).get("round"),
                    "home_code": home["code"], "away_code": away["code"],
                    "home_team": home.get("name") or home["code"], "away_team": away.get("name") or away["code"],
                    "source": "euroleague_feed"})
    return sorted(out, key=lambda f: (f["kickoff_utc"], f["match_id"]))


def _fetch_feed(season: str) -> list[dict]:
    from collectors.basketball_collector import _fetch_eur_season_games
    return _fetch_eur_season_games(season)


def _read_games(seasons: list[str]) -> list[dict]:
    from shared.firestore_client import iter_all
    out = []
    for s in seasons:
        out += [d.to_dict() or {} for d in iter_all("eur_games", where=("season", "==", s))]
    return out


def preview_content(fx: dict, pred: dict, labels_source: str) -> dict:
    return {**fx, **pred, "group": GROUP, "labels_source": labels_source,
            "label_policy": "ventaja medida en el backtest (IC95 bajo 0 frente a la media de la liga y del equipo)"}


async def run_basket_previews(dry_run: bool = True, now: Optional[datetime] = None, days: int = WINDOW_DAYS,
                              feed: Optional[list[dict]] = None, docs: Optional[list[dict]] = None,
                              backtest: Optional[dict] = None, existing: Optional[dict] = None) -> dict:
    """Fichas de Euroliga de los próximos `days` días. Los parámetros opcionales son para tests
    (None = leer del feed / Firestore)."""
    from collectors.basketball_collector import _eur_season_code

    now = now or datetime.now(timezone.utc)
    summary: dict = {"ok": True, "dry_run": dry_run, "now": now.isoformat(), "league": LEAGUE, "counts": {},
                     "errors": []}
    if backtest is None:
        backtest = _get_doc(METRICS, BACKTEST_DOC)
    if not backtest or not backtest.get("lines"):
        # No es un fallo: hasta guardar el backtest (basket-backtest con confirmar=ESCRIBIR) no hay fichas.
        summary["aviso"] = f"sin {METRICS}/{BACKTEST_DOC}: no se escriben fichas de baloncesto"
        return summary
    season = _eur_season_code(now)
    if feed is None:
        try:
            feed = await asyncio.get_running_loop().run_in_executor(None, _fetch_feed, season)
        except Exception:
            logger.error("basket_previews: no se pudo leer el feed de la Euroliga", exc_info=True)
            feed = None
    if not feed:
        summary.update(ok=False, errors=["feed de la Euroliga vacío o caído: sin calendario"])
        return summary
    fxs = fixtures_from_feed(feed, now, days)
    summary["fixtures_window"] = len(fxs)
    counts: dict[str, int] = defaultdict(int)
    to_write: dict[str, dict] = {}
    if fxs:
        if docs is None:
            docs = await asyncio.to_thread(_read_games, [bp.prev_season(season), season])
        today = now.date().isoformat()
        games = [g for g in bp.games_from_docs(docs, LEAGUE) if g["date"] < today]
        summary["history_games"] = len(games)
        hist = bp.History(games)
        resid = await asyncio.to_thread(bp.residuals_until, hist, LEAGUE, season)
        lines = {mk: tuple(v) for mk, v in (backtest.get("lines") or {}).items()}
        labels = dict(backtest.get("labels") or {})
        summary["edge_markets"] = sorted(mk for mk, lab in labels.items() if lab == bp.LABEL_EDGE)
        if existing is None:
            existing = _get_many(PREVIEWS_B, [fx["match_id"] for fx in fxs])
        for fx in fxs:
            pred = await asyncio.to_thread(bp.predict_game, hist, resid, LEAGUE, fx["season"], fx["date"],
                                           fx["home_code"], fx["away_code"], lines, labels)
            if pred is None:
                counts["sin historial de liga"] += 1
                continue
            pred["teams"]["home"]["team"], pred["teams"]["away"]["team"] = fx["home_team"], fx["away_team"]
            content = preview_content(fx, pred, BACKTEST_DOC)
            action = decide(content, existing.get(fx["match_id"]), now)
            counts[action] += 1
            if pred["low_sample"]:
                counts["muestra insuficiente"] += 1
            if action in ("nueva", "cambia", "tarde"):
                prev = existing.get(fx["match_id"]) or {}
                to_write[fx["match_id"]] = {**content, "hash": content_hash(content),
                                            "computed_at": now.isoformat(),
                                            "created_at": prev.get("created_at") or now.isoformat(),
                                            "freeze_at": freeze_at(fx["kickoff_utc"]).isoformat(),
                                            "written_after_freeze": action == "tarde",
                                            "n_writes": prev.get("n_writes", 0) + 1}
        summary["previews"] = [fx["match_id"] for fx in fxs]          # ids, sin equipos
    summary["counts"] = dict(counts)
    summary["would_write" if dry_run else "written"] = len(to_write)
    if not dry_run and to_write:
        try:
            _set_docs(PREVIEWS_B, to_write)
        except Exception:
            logger.error("basket_previews: error escribiendo", exc_info=True)
            summary["errors"].append("escritura fallida (ver logs del servicio)")
            summary["ok"] = False
    logger.info("basket_previews: %s; escribe %d", dict(counts), len(to_write))
    return summary


# ── Graduación ─────────────────────────────────────────────────────────────────
# Contra eur_games/{temporada}_{gamecode} (lo baja la tarea diaria de las 06:30 UTC). Mismas reglas que
# mp-v1: solo la versión congelada, anulada si se escribió tras el inicio o con datos del día, «sin
# resultado» a los NO_RESULT_DAYS días. Solo las líneas fijas (las extra no se gradúan). Puntos con
# prórroga, como gradúan las casas.

def result_from_game(doc: Optional[dict], preview: dict) -> Optional[dict]:
    """eur_games → {"h": {stat}, "a": {stat}, "date"} para grade_preview; None si falta o si los
    equipos no cuadran con la ficha."""
    if not doc or doc.get("home_code") != preview.get("home_code") or doc.get("away_code") != preview.get("away_code"):
        return None
    sides = {k: {s: (doc.get(src) or {}).get(s) for s in bp.STATS} for k, src in (("h", "home"), ("a", "away"))}
    if any(v is None for side in sides.values() for v in side.values()):
        return None
    return {**sides, "date": doc.get("date")}


def grade_basket_preview(preview: dict, result: dict) -> dict:
    """grade_preview de mp-v1 más p_team en cada línea (la segunda referencia del backtest)."""
    from analyzers.match_preview_store import grade_preview
    out = grade_preview(preview, result)
    for ln in out["lines"]:
        blk = preview["stats"][ln["stat"]]["lines"][ln["side"]]["lines"].get(bp.line_key(ln["line"])) or {}
        ln["p_team"] = blk.get("p_team")
    return out


def rows_from_grades(grades, ref: str = "p_league") -> list[tuple]:
    """Filas con el formato del backtest (season, liga, mercado, línea, y, p_ref, p_modelo, partido)."""
    rows = []
    for g in grades:
        if g.get("status") != "graduada" or g.get("low_sample"):
            continue
        for ln in g.get("lines") or []:
            if ln.get(ref) is not None:
                rows.append((g.get("season"), g["div"], ln["market"], ln["line"], ln["y"], ln[ref], ln["p_model"],
                             g["match_id"]))
    return rows


def live_doc(grades: list[dict], now: datetime) -> dict:
    """match_preview_metrics/bp-v1-EUR_live: métricas en directo frente a las dos medias (solo
    agregados). Informativo: las etiquetas de las fichas siguen saliendo del backtest."""
    from analyzers.match_preview import market_metrics
    met = {}
    for key, ref in (("vs_liga", "p_league"), ("vs_equipo", "p_team")):
        rows = rows_from_grades(grades, ref)
        met[key] = {v["market"]: {f: v[f] for f in ("n_matches", "n_lines", "brier_league", "brier_model", "delta",
                                                     "ci_lo", "ci_hi", "ci_label")}
                    for v in (market_metrics(rows) if rows else {}).values() if v["div"] == "ALL"}
    graded = [g for g in grades if g.get("status") == "graduada" and not g.get("low_sample")]
    return {"model_version": bp.MODEL_VERSION, "kind": "live", "league": LEAGUE, "group": GROUP,
            "n_graduated": len(graded), "labels": bp.labels_from_backtest(
                {f"{mk}|ALL": {**v, "market": mk} for mk, v in met["vs_liga"].items()},
                {f"{mk}|ALL": {**v, "market": mk} for mk, v in met["vs_equipo"].items()}),
            "metrics": met, "computed_at": now.isoformat()}


def _read_previews_to_grade(now: datetime) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    lo = (now - timedelta(days=GRADE_LOOKBACK_DAYS)).isoformat()
    hi = (now - timedelta(hours=GRADE_AFTER_H)).isoformat()
    q = (col(PREVIEWS_B).where(filter=FieldFilter("kickoff_utc", ">=", lo))
         .where(filter=FieldFilter("kickoff_utc", "<=", hi)))
    return [s.to_dict() for s in q.stream()]


def _read_all_grades() -> list[dict]:
    from shared.firestore_client import col
    return [s.to_dict() for s in col(GRADES_B).stream()]


async def run_basket_grade(dry_run: bool = True, now: Optional[datetime] = None,
                           previews: Optional[list[dict]] = None, results: Optional[dict] = None,
                           graded: Optional[dict] = None, all_grades: Optional[list[dict]] = None,
                           write=None) -> dict:
    """Gradúa las fichas de baloncesto congeladas con eur_games → match_preview_grades_basket y
    match_preview_metrics/bp-v1-EUR_live. Los parámetros opcionales son para tests. Resumen: solo
    recuentos."""
    now = now or datetime.now(timezone.utc)
    write = write or _set_docs
    summary: dict = {"ok": True, "dry_run": dry_run, "counts": {}, "errors": []}
    if previews is None:
        previews = await asyncio.to_thread(_read_previews_to_grade, now)
    else:
        lo, hi = now - timedelta(days=GRADE_LOOKBACK_DAYS), now - timedelta(hours=GRADE_AFTER_H)
        previews = [p for p in previews if lo <= _parse(p["kickoff_utc"]) <= hi]
    if graded is None:
        graded = _get_many(GRADES_B, [p["match_id"] for p in previews])
    pending = [p for p in previews if p["match_id"] not in graded]
    if results is None:
        results = _get_many("eur_games", [f"{p['season']}_{p['gamecode']}" for p in pending])
    docs: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    for p in pending:
        base = {"match_id": p["match_id"], "div": p["div"], "league": p.get("league"), "season": p.get("season"),
                "gamecode": p.get("gamecode"), "date": p["date"], "kickoff_utc": p["kickoff_utc"],
                "home_team": p["home_team"], "away_team": p["away_team"], "model_version": p.get("model_version"),
                "group": GROUP, "low_sample": bool(p.get("low_sample")), "preview_hash": p.get("hash"),
                "preview_computed_at": p.get("computed_at"), "graded_at": now.isoformat()}
        why = validity(p)
        raw = results.get(f"{p['season']}_{p['gamecode']}")
        res = result_from_game(raw, p)
        if why:
            doc = {**base, "status": "anulada", "reason": why}
        elif res is not None:
            doc = {**base, "status": "graduada", **grade_basket_preview(p, res)}
        elif raw is not None:
            doc = {**base, "status": "anulada", "reason": "el partido guardado no cuadra con la ficha"}
        elif now - _parse(p["kickoff_utc"]) >= timedelta(days=NO_RESULT_DAYS):
            doc = {**base, "status": "sin resultado"}
        else:
            counts["esperando eur_games"] += 1
            continue
        counts[doc["status"]] += 1
        docs[p["match_id"]] = doc
    summary["counts"] = dict(counts)
    if all_grades is None:
        all_grades = await asyncio.to_thread(_read_all_grades)
    merged = {g["match_id"]: g for g in all_grades if g.get("match_id")}
    merged.update(docs)
    ldoc = live_doc(list(merged.values()), now)
    summary.update(n_graduated=ldoc["n_graduated"],
                   live_edge_markets=sorted(mk for mk, lab in ldoc["labels"].items() if lab == bp.LABEL_EDGE))
    summary["would_write" if dry_run else "written"] = len(docs)
    if not dry_run:
        try:
            if docs:
                write(GRADES_B, docs)
            if ldoc["n_graduated"]:
                write(METRICS, {LIVE_DOC: ldoc})
        except Exception:
            logger.error("basket_grade: error escribiendo", exc_info=True)
            summary["errors"].append("escritura fallida (ver logs del servicio)")
            summary["ok"] = False
    logger.info("basket_grade: %s; graduados en total %d", dict(counts), ldoc["n_graduated"])
    return summary
