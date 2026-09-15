"""
services/sports-agent/analyzers/trend_grader.py

Gradúa las señales de trend_signals (feed de tendencias, sin cuotas) en su
PROPIA colección trend_accuracy_log — nunca toca accuracy_log/predictions/
shadow_trades ni pesos del modelo.

Dos rutas de graduación, una por tipo de evidencia (separadas para poder
comparar hit-rate real de series vs rolling sin mezclarlas):

  series  — contra el marcador real: col('match_results'), ya escrito por
            update_finished_matches (goals_home/goals_away por match_id).
  rolling — contra el CSV de football-data.co.uk (HC/AC/HY/AR), buscando la
            fila del partido concreto por equipos+fecha. Mismo CSV gratis que
            ya descarga fdco_collector — sin llamada ni coste nuevo.
"""
import logging
from datetime import date, datetime, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

from collectors.fdco_collector import FDCO_LEAGUES, _slugify, fetch_league_csv

logger = logging.getLogger(__name__)

# No perseguir partidos demasiado viejos si el resultado/CSV nunca llega
# (equipo desciende de categoría, liga deja de tener stats, etc.) — se
# abandonan para no acumular pendientes para siempre.
_GRADE_MAX_AGE_DAYS = 21


def _parse_iso_date(raw) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)[:10]).date()
    except ValueError:
        return None


def _names_match(a: str, b: str) -> bool:
    na, nb = _slugify(a or ""), _slugify(b or "")
    return bool(na and nb) and (na == nb or na in nb or nb in na)


# ── Serie: goles/BTTS/hándicap contra match_results ─────────────────────────────

def _grade_series_doc(sig: dict, result: dict) -> bool | None:
    gh, ga = result.get("goals_home"), result.get("goals_away")
    if gh is None or ga is None:
        return None
    gf, ga_team = (gh, ga) if sig.get("side") == "home" else (ga, gh)
    market = sig.get("market")
    threshold = sig.get("threshold")

    if market == "team_goals_over":
        return gf >= threshold
    if market == "btts":
        return gf > 0 and ga_team > 0
    if market == "handicap":
        return (gf - ga_team) >= threshold
    return None


async def grade_series_signals() -> dict:
    from shared.firestore_client import col

    pending = list(
        col("trend_signals")
        .where(filter=FieldFilter("pattern_type", "==", "series"))
        .where(filter=FieldFilter("graded", "==", False))
        .stream()
    )
    graded, skipped = 0, 0
    today = datetime.now(timezone.utc).date()

    for d in pending:
        sig = d.to_dict() or {}
        match_id = sig.get("match_id", "")
        if not match_id:
            continue
        try:
            snap = col("match_results").document(match_id).get()
        except Exception:
            logger.error("trend_grader: error leyendo match_results %s", match_id, exc_info=True)
            continue

        if not snap.exists:
            match_date = _parse_iso_date(sig.get("match_date"))
            if match_date and (today - match_date).days > _GRADE_MAX_AGE_DAYS:
                skipped += 1
            continue

        hit = _grade_series_doc(sig, snap.to_dict() or {})
        if hit is None:
            continue
        _write_grade(d.id, sig, hit, "match_results")
        graded += 1

    logger.info("trend_grader(series): %d graduadas, %d abandonadas por antigüedad", graded, skipped)
    return {"graded": graded, "skipped": skipped}


# ── Rolling: córners/tarjetas contra el CSV football-data.co.uk ────────────────

def _season_year_for_date(d: date) -> int:
    """football-data.co.uk codifica temporada por año de FIN (2024/25 → 2025)."""
    return d.year + 1 if d.month >= 7 else d.year


def _find_fdco_row(rows: list[dict], home_team: str, away_team: str, target: date) -> dict | None:
    for row in rows:
        row_date = None
        raw_date = (row.get("Date") or "").strip()
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            try:
                row_date = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        if not row_date or abs((row_date - target).days) > 1:
            continue
        if _names_match(row.get("HomeTeam", ""), home_team) and _names_match(row.get("AwayTeam", ""), away_team):
            return row
    return None


async def grade_rolling_signals() -> dict:
    from shared.firestore_client import col

    pending = list(
        col("trend_signals")
        .where(filter=FieldFilter("pattern_type", "==", "rolling"))
        .where(filter=FieldFilter("graded", "==", False))
        .stream()
    )
    if not pending:
        return {"graded": 0, "skipped": 0}

    today = datetime.now(timezone.utc).date()
    # Cachear el CSV por (liga, season_year) dentro de esta pasada — varias
    # señales de la misma jornada comparten la misma descarga.
    csv_cache: dict[tuple[str, int], list[dict]] = {}
    graded, skipped = 0, 0

    for d in pending:
        sig = d.to_dict() or {}
        league = sig.get("league", "")
        if league not in FDCO_LEAGUES:
            continue
        match_date = _parse_iso_date(sig.get("match_date"))
        if not match_date or match_date >= today:
            continue  # sin fecha válida o el partido aún no se ha jugado
        if (today - match_date).days > _GRADE_MAX_AGE_DAYS:
            skipped += 1
            continue

        cache_key = (league, _season_year_for_date(match_date))
        if cache_key not in csv_cache:
            csv_cache[cache_key] = await fetch_league_csv(cache_key[0], cache_key[1])
        rows = csv_cache[cache_key]
        if not rows:
            continue

        row = _find_fdco_row(rows, sig.get("home_team", ""), sig.get("away_team", ""), match_date)
        if not row:
            continue

        is_home = sig.get("side") == "home"
        market = sig.get("market")
        threshold = sig.get("threshold") or 0
        try:
            if market == "corners":
                actual = float(row.get("HC") or 0) if is_home else float(row.get("AC") or 0)
            elif market == "cards":
                yellows = float(row.get("HY") or 0) if is_home else float(row.get("AY") or 0)
                reds = float(row.get("HR") or 0) if is_home else float(row.get("AR") or 0)
                actual = yellows + reds
            else:
                continue
        except ValueError:
            continue

        _write_grade(d.id, sig, actual >= threshold, "fdco_csv", extra={"actual": actual})
        graded += 1

    logger.info("trend_grader(rolling): %d graduadas, %d abandonadas por antigüedad", graded, skipped)
    return {"graded": graded, "skipped": skipped}


def _write_grade(doc_id: str, sig: dict, hit: bool, source: str, extra: dict | None = None) -> None:
    from shared.firestore_client import col

    now_iso = datetime.now(timezone.utc).isoformat()
    result = "hit" if hit else "miss"
    try:
        col("trend_signals").document(doc_id).update({
            "graded": True, "result": result, "graded_at": now_iso, "grade_source": source,
        })
    except Exception:
        logger.error("trend_grader: error actualizando trend_signals %s", doc_id, exc_info=True)
        return

    log_doc = {
        "signal_id": doc_id, "pattern_type": sig.get("pattern_type"), "market": sig.get("market"),
        "team": sig.get("team"), "league": sig.get("league"), "match_id": sig.get("match_id"),
        "sample_size": sig.get("sample_size"), "rate_or_ratio": sig.get("rate_or_ratio"),
        "result": result, "graded_at": now_iso, "grade_source": source,
        **(extra or {}),
    }
    try:
        col("trend_accuracy_log").document(doc_id).set(log_doc)
    except Exception:
        logger.error("trend_grader: error guardando trend_accuracy_log %s", doc_id, exc_info=True)


async def run_trend_grader() -> dict:
    """Punto de entrada del job periódico (/run-trend-grade)."""
    series_result = await grade_series_signals()
    rolling_result = await grade_rolling_signals()
    logger.info("trend_grader: series=%s rolling=%s", series_result, rolling_result)
    return {"series": series_result, "rolling": rolling_result}
