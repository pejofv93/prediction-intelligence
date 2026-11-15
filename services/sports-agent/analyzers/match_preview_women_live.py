"""
services/sports-agent/analyzers/match_preview_women_live.py

Fútbol femenino (mp-w1) — Paso 4: lista de partidos, fichas y graduación. Mismo patrón que
match_preview_store (mp-v1): dry_run=True por defecto, mismo hash y congelado (2 h antes).

  Lista de partidos: un listado de temporada por liga en Highlightly, como mucho una vez cada
      RELIST_H horas y con un tope propio al día (contador highlightly_women_fixtures): 30 mientras
      dura el plan PRO (hasta HL_PRO_UNTIL incluido) y 12 después. Se guarda en
      match_previews_state/women; entre listados se usa lo guardado. El dry-run también guarda
      el listado (única escritura del dry-run, además de los contadores de cuota), para que la
      escritura real no repita las peticiones.
  Fichas → match_previews_women/{div}_{fecha}_{id Highlightly}: SIEMPRE "solo referencia" (no se
      usan las etiquetas del backtest) hasta medirlas en directo. Líneas y dispersión: las
      guardadas en match_preview_metrics/mp-w1_backtest (sin ese documento no hay fichas).
  Graduación → match_preview_grades_women + match_preview_metrics/mp-w1_live: con las
      estadísticas que baja el mantenimiento diario de Highlightly (hl_team_stats/{id}).

Colecciones propias: la graduación y las métricas en directo de mp-v1 no ven nada de aquí. La web
de Tendencias las junta en la exportación, en el grupo "Femenino".
Datos de Highlightly: SOLO estadísticas, sin cuotas; el motor de apuestas no lee nada de aquí.
Los resúmenes (log público del workflow) llevan solo recuentos: nunca equipos ni partidos.
"""
import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from analyzers import match_preview as mp
from analyzers import match_preview_women as mw
from analyzers.match_preview_store import (
    GRADE_AFTER_H, GRADE_LOOKBACK_DAYS, METRICS, NO_RESULT_DAYS, _get_doc, _get_many, _parse, _set_docs,
    content_hash, decide, freeze_at, grade_preview, rows_from_grades, validity,
)
from analyzers.match_preview_women_store import (
    backtest_doc_id, params_from_backtest, read_backtest_doc, read_women_docs,
)

logger = logging.getLogger(__name__)

PREVIEWS_W, GRADES_W = "match_previews_women", "match_preview_grades_women"
STATE_DOC = ("match_previews_state", "women")
HL_WOMEN_FIXTURES_API = "highlightly_women_fixtures"    # contador propio
HL_WOMEN_FIXTURES_DAILY = 12      # tope al día con el plan gratuito: un listado por liga (≈ 2-3 páginas)
HL_WOMEN_FIXTURES_DAILY_PRO = 30  # tope al día mientras dura el plan PRO
RELIST_H = 20                     # una liga listada hace menos no se vuelve a listar
LIST_AHEAD_DAYS = 7               # partidos que se guardan del listado
LABEL_POLICY = "solo referencia hasta medirlo en directo"


def live_doc_id() -> str:
    return f"{mw.MODEL_VERSION}_live"


def current_hl_season(now: datetime) -> int:
    """Temporada de Highlightly en curso (las ligas femeninas van de agosto a junio)."""
    return now.year if now.month >= 7 else now.year - 1


# ── Lista de partidos ──────────────────────────────────────────────────────────

def fixtures_from_listing(listing: list[dict], league_id: int, hl_season: int, now: datetime,
                          ahead_days: int = LIST_AHEAD_DAYS) -> list[dict]:
    """Partidos sin terminar que empiezan entre ahora y `ahead_days` días."""
    from collectors.highlightly_loader import listing_meta

    div = mw.WOMEN_DIVS[league_id]
    out = []
    for raw in listing or []:
        m = listing_meta(raw, "women", league_id, hl_season)
        if m["finished"] or not m["kickoff_utc"] or m["home_team_id"] is None or m["away_team_id"] is None:
            continue
        try:
            ko = _parse(m["kickoff_utc"])
        except (TypeError, ValueError):
            continue
        if not now <= ko <= now + timedelta(days=ahead_days):
            continue
        out.append({"match_id": f"{div}_{ko.date().isoformat()}_{m['match_id']}", "div": div,
                    "league": mw.DIV_NAMES[div], "date": ko.date().isoformat(), "kickoff_utc": ko.isoformat(),
                    "home_team": m["home_team"], "away_team": m["away_team"],
                    "home_id": str(m["home_team_id"]), "away_id": str(m["away_team_id"]),
                    "hl_match_id": m["match_id"], "hl_season": hl_season, "source": "highlightly"})
    return out


def women_fixtures_cap(today=None) -> int:
    """30 hasta el fin del plan PRO (HL_PRO_UNTIL, incluido); 12 después."""
    from collectors.highlightly_players import HL_PRO_UNTIL
    today = today or datetime.now(timezone.utc).date()
    return HL_WOMEN_FIXTURES_DAILY_PRO if today <= HL_PRO_UNTIL else HL_WOMEN_FIXTURES_DAILY


def women_fixtures_budget(used_today: int, remaining_reported: Optional[int],
                          cap: Optional[int] = None) -> int:
    left = (women_fixtures_cap() if cap is None else cap) - int(used_today or 0)
    if remaining_reported is not None:
        left = min(left, int(remaining_reported))
    return max(0, left)


def _fresh(entry: Optional[dict], now: datetime) -> bool:
    try:
        return bool(entry and entry.get("listed_at")
                    and (now - _parse(entry["listed_at"])).total_seconds() < RELIST_H * 3600)
    except (TypeError, ValueError):
        return False


async def list_women_fixtures(now: datetime, api_key: Optional[str], quota, state: dict,
                              client=None) -> dict:
    """Lista de las 5 ligas: lo listado ahora (si toca y queda tope) y, si no, lo guardado.
    No escribe nada: devuelve el estado nuevo en by_div_state."""
    import httpx
    from collectors import highlightly_players as hp
    from collectors.highlightly_loader import _Budget, _list_all

    by_div = dict(state.get("by_div") or {})
    out = {"fixtures": [], "listed": [], "from_state": [], "not_listed": [], "hl_requests": 0, "errors": []}
    due = []
    for lid, div in mw.WOMEN_DIVS.items():
        if _fresh(by_div.get(div), now):
            out["from_state"].append(div)
        else:
            due.append(lid)
    if due and api_key and quota is not None:
        from shared.api_quota_manager import daily_period
        mine = quota._get_doc(f"{HL_WOMEN_FIXTURES_API}_{daily_period(HL_WOMEN_FIXTURES_API)}")
        total = quota._get_doc(f"{hp.HL_QUOTA_API}_{daily_period(hp.HL_QUOTA_API)}")
        budget = _Budget(women_fixtures_budget(mine.get("used", 0), total.get("remaining_reported")))
        season = current_hl_season(now)
        own = client is None
        client = client or httpx.AsyncClient(timeout=30.0)
        try:
            for lid in due:
                div = mw.WOMEN_DIVS[lid]
                before = budget.used
                try:
                    listing = await _list_all(client, api_key, quota, budget, lid, season)
                except hp.HighlightlyBudgetExhausted:
                    listing = None
                    out["errors"].append("cuota de Highlightly agotada")
                for _ in range(budget.used - before):
                    quota.track_call(HL_WOMEN_FIXTURES_API)
                if listing is None:
                    out["not_listed"].append(div)
                    continue
                by_div[div] = {"listed_at": now.isoformat(),
                               "fixtures": fixtures_from_listing(listing, lid, season, now)}
                out["listed"].append(div)
        finally:
            if own:
                await client.aclose()
        out["hl_requests"] = budget.used
    else:
        out["not_listed"] += [mw.WOMEN_DIVS[lid] for lid in due]
        if due and not api_key:
            out["errors"].append("sin HIGHLIGHTLY_API_KEY: se usa solo lo guardado")
    for v in by_div.values():
        out["fixtures"] += [f for f in v.get("fixtures") or [] if _parse(f["kickoff_utc"]) >= now]
    out["by_div_state"] = by_div
    return out


# ── Fichas ─────────────────────────────────────────────────────────────────────

def women_preview_content(f: dict, pred: dict) -> dict:
    """Lo que se guarda: partido + ficha, con la versión mp-w1, los nombres de los equipos (el
    modelo trabaja con sus ids de Highlightly) y la política de etiqueta."""
    keep = ("match_id", "div", "league", "date", "kickoff_utc", "home_team", "away_team", "home_id",
            "away_id", "hl_match_id", "source")
    teams = {side: {**t, "team": f["home_team" if side == "home" else "away_team"]}
             for side, t in pred["teams"].items()}
    return {**{k: f[k] for k in keep if f.get(k) is not None}, **pred, "teams": teams,
            "model_version": mw.MODEL_VERSION, "group": "women", "labels_source": None,
            "label_policy": LABEL_POLICY}


async def run_women_previews(dry_run: bool = True, now: Optional[datetime] = None, days: int = 3,
                             highlightly: bool = True, fixtures: Optional[list[dict]] = None,
                             docs: Optional[list[dict]] = None, backtest: Optional[dict] = None,
                             existing: Optional[dict] = None, state: Optional[dict] = None) -> dict:
    """Fichas de los próximos `days` días. fixtures/docs/backtest/existing/state: para tests.
    En dry-run no escribe fichas, pero sí guarda el listado nuevo de Highlightly (si lo hubo)."""
    now = now or datetime.now(timezone.utc)
    summary: dict = {"ok": True, "dry_run": dry_run, "now": now.isoformat(), "counts": {}, "by_div": {},
                     "errors": []}
    if backtest is None:
        backtest = read_backtest_doc()
    params = params_from_backtest(backtest)
    if params is None:
        summary.update(ok=False, errors=[f"sin {METRICS}/{backtest_doc_id()}: no se escriben fichas"])
        return summary
    lines, disp = params

    new_state = None
    if fixtures is None:
        if state is None:
            state = _get_doc(*STATE_DOC) or {}
        api_key = os.environ.get("HIGHLIGHTLY_API_KEY") if highlightly else None
        quota = None
        if api_key:
            # También en dry-run: cada petición se apunta en los contadores.
            from shared.api_quota_manager import QuotaManager
            quota = QuotaManager()
        lst = await list_women_fixtures(now, api_key, quota, state)
        fixtures = lst["fixtures"]
        summary.update(hl_requests=lst["hl_requests"], listed=lst["listed"], from_state=lst["from_state"],
                       not_listed=lst["not_listed"])
        summary["errors"] += lst["errors"]
        new_state = {"by_div": lst["by_div_state"], "last_run": now.isoformat()}

    if docs is None:
        docs = await asyncio.to_thread(read_women_docs)
    hist = mw.history(mw.matches_from_hl(docs))
    season = mw.season_code(current_hl_season(now))

    todo = [f for f in fixtures if now <= _parse(f["kickoff_utc"]) <= now + timedelta(days=days)]
    if existing is None:
        existing = _get_many(PREVIEWS_W, [f["match_id"] for f in todo])
    to_write: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    by_div: dict = defaultdict(lambda: defaultdict(int))
    for f in todo:
        pred = mp.predict_match(hist, f["div"], season, f["date"], f["home_id"], f["away_id"],
                                labels={}, lines=lines, dispersion=disp)
        content = women_preview_content(f, pred)
        action = decide(content, existing.get(f["match_id"]), now)
        counts[action] += 1
        by_div[f["div"]][action] += 1
        if pred["low_sample"]:
            by_div[f["div"]]["muestra insuficiente"] += 1
        if action in ("nueva", "cambia", "tarde"):
            prev = existing.get(f["match_id"]) or {}
            to_write[f["match_id"]] = {**content, "season": season, "hash": content_hash(content),
                                       "computed_at": now.isoformat(),
                                       "created_at": prev.get("created_at") or now.isoformat(),
                                       "freeze_at": freeze_at(f["kickoff_utc"]).isoformat(),
                                       "written_after_freeze": action == "tarde",
                                       "n_writes": prev.get("n_writes", 0) + 1}
    summary["counts"] = dict(counts)
    summary["by_div"] = {mw.DIV_NAMES[d]: dict(v) for d, v in sorted(by_div.items())}
    summary["fixtures_window"] = len(todo)
    summary["would_write" if dry_run else "written"] = len(to_write)
    # El listado se guarda también en dry-run si se pidió algo a Highlightly: ya está pagado y
    # así la escritura real no lo repite (y no se queda sin tope, como pasó el 15-nov).
    save_state = new_state is not None and (not dry_run or summary.get("listed"))
    try:
        if not dry_run:
            _set_docs(PREVIEWS_W, to_write)
        if save_state:
            _set_docs(STATE_DOC[0], {STATE_DOC[1]: new_state})
    except Exception:
        logger.error("women_previews: error escribiendo", exc_info=True)
        summary["errors"].append("escritura fallida (ver logs del servicio)")
        summary["ok"] = False
    summary["state_saved"] = bool(save_state) and summary["ok"]
    logger.info("women_previews: %s; escribe %d", dict(counts), len(to_write))
    return summary


# ── Graduación ─────────────────────────────────────────────────────────────────

def _read_women_previews_to_grade(now: datetime) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    lo = (now - timedelta(days=GRADE_LOOKBACK_DAYS)).isoformat()
    hi = (now - timedelta(hours=GRADE_AFTER_H)).isoformat()
    q = (col(PREVIEWS_W).where(filter=FieldFilter("kickoff_utc", ">=", lo))
         .where(filter=FieldFilter("kickoff_utc", "<=", hi)))
    return [s.to_dict() for s in q.stream()]


def _read_all_women_grades() -> list[dict]:
    from shared.firestore_client import col
    return [s.to_dict() for s in col(GRADES_W).stream()]


def result_from_hl(doc: Optional[dict]) -> Optional[dict]:
    """Doc de hl_team_stats → resultado en el formato de mp-v1, solo si ya tiene estadísticas."""
    if not doc or doc.get("status") != "done" or not doc.get("stats"):
        return None
    ms = mw.matches_from_hl([doc])
    return ms[0] if ms else None


async def run_women_grade(dry_run: bool = True, now: Optional[datetime] = None,
                          previews: Optional[list[dict]] = None, results: Optional[dict] = None,
                          graded: Optional[dict] = None, all_grades: Optional[list[dict]] = None) -> dict:
    """Mismas reglas que mp-v1: anulada si se escribió tras el inicio o usó datos del día del
    partido; "sin resultado" a los NO_RESULT_DAYS días sin estadísticas."""
    now = now or datetime.now(timezone.utc)
    summary: dict = {"ok": True, "dry_run": dry_run, "counts": {}, "by_div": {}, "errors": []}
    if previews is None:
        previews = _read_women_previews_to_grade(now)
    if graded is None:
        graded = _get_many(GRADES_W, [p["match_id"] for p in previews])
    pending = [p for p in previews if p["match_id"] not in graded]
    if results is None:
        results = _get_many("hl_team_stats", [str(p["hl_match_id"]) for p in pending if p.get("hl_match_id")])
    docs: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    by_div: dict = defaultdict(lambda: defaultdict(int))
    for p in pending:
        base = {"match_id": p["match_id"], "div": p["div"], "season": p.get("season"), "date": p["date"],
                "kickoff_utc": p["kickoff_utc"], "home_team": p["home_team"], "away_team": p["away_team"],
                "model_version": p.get("model_version"), "group": "women", "preview_hash": p.get("hash"),
                "preview_computed_at": p.get("computed_at"), "graded_at": now.isoformat()}
        why = validity(p)
        res = result_from_hl(results.get(str(p.get("hl_match_id"))))
        if why:
            doc = {**base, "status": "anulada", "reason": why}
        elif res is not None:
            doc = {**base, "status": "graduada", **grade_preview(p, res)}
        elif now - _parse(p["kickoff_utc"]) >= timedelta(days=NO_RESULT_DAYS):
            doc = {**base, "status": "sin resultado"}
        else:
            counts["esperando estadísticas"] += 1
            by_div[p["div"]]["esperando estadísticas"] += 1
            continue
        counts[doc["status"]] += 1
        by_div[p["div"]][doc["status"]] += 1
        docs[p["match_id"]] = doc
    summary["counts"] = dict(counts)
    summary["by_div"] = {mw.DIV_NAMES.get(d, d): dict(v) for d, v in sorted(by_div.items())}

    if all_grades is None:
        all_grades = _read_all_women_grades()
    merged = {g["match_id"]: g for g in all_grades}
    merged.update(docs)
    rows = rows_from_grades(list(merged.values()))
    live = mp.market_metrics(rows) if rows else {}
    summary["live_metrics"] = {k: {f: v[f] for f in ("n_matches", "delta", "ci_lo", "ci_hi", "label")}
                               for k, v in live.items() if k.endswith("|ALL")}
    summary["would_write" if dry_run else "written"] = len(docs)
    if not dry_run:
        try:
            _set_docs(GRADES_W, docs)
            if rows:
                _set_docs(METRICS, {live_doc_id(): {"model_version": mw.MODEL_VERSION, "kind": "live",
                                                    "group": "women", "n_rows": len(rows), "metrics": live,
                                                    "computed_at": now.isoformat()}})
        except Exception:
            logger.error("women_grade: error escribiendo", exc_info=True)
            summary["errors"].append("escritura fallida (ver logs del servicio)")
            summary["ok"] = False
    logger.info("women_grade: %s", dict(counts))
    return summary
