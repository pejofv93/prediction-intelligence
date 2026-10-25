"""
services/sports-agent/analyzers/trend_calibration.py

Calibra el umbral de "patrón fuerte" por mercado del feed de tendencias a
partir de su propio historial en trend_accuracy_log — aislado del motor de
valor: solo lee trend_accuracy_log y escribe trend_market_calibration, nunca
toca predictions/shadow_trades/accuracy_log/model_weights.

v1 SOLO APRIETA, nunca afloja el umbral fijo de shared/config.py: con lo que
se emite hoy solo hay señales graduadas de la franja que YA pasó el umbral
fijo — bajar el umbral exigiría datos de la franja que nunca se generó (sesgo
de selección: no sabemos cómo acertaría algo que nunca se emitió). Aflojar
con sondas controladas queda para una fase futura con más historial.

Algoritmo por mercado (>= TREND_CALIBRATION_MIN_SAMPLE graduadas, ventana de
las TREND_CALIBRATION_WINDOW más recientes, "void" excluido igual que hace el
dashboard):
  1. hit_rate_fixed = aciertos / n sobre TODO lo admitido por el umbral fijo.
  2. Si hit_rate_fixed >= el objetivo propio del mercado (TREND_MARKET_TARGET)
     → el umbral fijo ya cumple el objetivo, no hace falta apretar ("at_fixed").
  3. Si no, se busca el corte MÁS LAXO (para no sacrificar más volumen del
     necesario) dentro de lo observado: se ordena por rate_or_ratio
     descendente y se prueban prefijos de mayor a menor tamaño (mínimo
     TREND_CALIBRATION_MIN_TAIL_SAMPLE señales) hasta encontrar el primero
     cuyo hit-rate acumulado alcance el objetivo → ese es el umbral calibrado
     ("calibrated").
  4. Si ni el prefijo más exigente (las mejores TREND_CALIBRATION_MIN_TAIL_SAMPLE
     señales) llega al objetivo, el mercado no calibra — se mantiene el fijo,
     marcado como aviso ("below_target").
Por debajo de TREND_CALIBRATION_MIN_SAMPLE el mercado se queda sin cambios
("fixed") — puede quedarse así indefinidamente, no es un fallo.

Mercados de RACHA (SERIES_MARKETS, desde 2026-10-25) — _calibrate_series + histéresis:
  - Solo se prueban umbrales COMPLETOS (todo lo >= a un valor). Las rachas son
    décimas (0.7/0.8/0.9/1.0) con muchos empates, y el corte por prefijo de
    _calibrate_one partía un empate: anotaba "0.9 al 70% en 20" cuando el feed
    admite las 21 señales >= 0.9 (66.7%).
  - Sin umbral que llegue al objetivo → umbral fijo y "below_target" (la salida
    para ese mercado es una variante en sombra, no apretar sin llegar).
  - Histéresis: el umbral vigente solo se sustituye si el nuevo mejora su acierto
    en >= TREND_CALIBRATION_HYSTERESIS (ambos tramos con >= MIN_TAIL señales), o si
    el vigente ya no cumple el objetivo. Y nunca antes de TREND_CALIBRATION_MIN_DAYS
    desde el último cambio. Sin esto Ambos marcan cambió 6 veces en 10 días.
Todos los mercados guardan en el doc el historial de cambios de umbral.
"""
import logging
from datetime import datetime, timezone

from shared.config import (
    TREND_CALIBRATION_HYSTERESIS,
    TREND_CALIBRATION_MIN_DAYS,
    TREND_CALIBRATION_MIN_SAMPLE,
    TREND_CALIBRATION_MIN_TAIL_SAMPLE,
    TREND_CALIBRATION_WINDOW,
    TREND_MARKET_FIXED_THRESHOLD,
    TREND_MARKET_TARGET,
)

logger = logging.getLogger(__name__)

# Mercados de racha (pattern_type "series"): hit-rate en décimas, con empates.
SERIES_MARKETS = frozenset({"btts", "team_goals_over", "team_goals_over_3", "handicap"})


def _tail(rows: list[dict], threshold: float) -> tuple[int, float | None]:
    """(n, acierto) de TODO lo que admite el umbral — lo que el feed emitiría."""
    tail = [r for r in rows if (r.get("rate_or_ratio") or 0) >= threshold - 1e-9]
    if not tail:
        return 0, None
    return len(tail), sum(1 for r in tail if r.get("result") == "hit") / len(tail)


def _calibrate_series(rows: list[dict], fixed_threshold: float, target: float) -> dict:
    """Como _calibrate_one pero probando solo umbrales completos (sin partir empates)."""
    n = len(rows)
    if n < TREND_CALIBRATION_MIN_SAMPLE:
        return {"sample_n": n, "hit_rate_fixed": None,
                "threshold_effective": fixed_threshold, "status": "fixed"}
    _, hr_fixed = _tail(rows, fixed_threshold)
    hit_rate_fixed = round(hr_fixed, 4)
    if hit_rate_fixed >= target:
        return {"sample_n": n, "hit_rate_fixed": hit_rate_fixed,
                "threshold_effective": fixed_threshold, "status": "at_fixed"}
    values = sorted({r.get("rate_or_ratio") or 0 for r in rows
                     if (r.get("rate_or_ratio") or 0) > fixed_threshold + 1e-9})
    for value in values:  # el más laxo que cumple, para no perder más volumen del necesario
        k, hr = _tail(rows, value)
        if k >= TREND_CALIBRATION_MIN_TAIL_SAMPLE and hr >= target:
            return {"sample_n": n, "hit_rate_fixed": hit_rate_fixed,
                    "threshold_effective": round(value, 4), "status": "calibrated",
                    "calibrated_n": k, "calibrated_hit_rate": round(hr, 4)}
    return {"sample_n": n, "hit_rate_fixed": hit_rate_fixed,
            "threshold_effective": fixed_threshold, "status": "below_target"}


def _with_hysteresis(result: dict, rows: list[dict], prev: dict, fixed_threshold: float,
                     target: float, now: datetime) -> dict:
    """Decide si el umbral candidato sustituye al vigente (prev.threshold_effective).
    Si no, devuelve el vigente con su acierto real de hoy y held_by con el motivo."""
    current = prev.get("threshold_effective")
    candidate = result["threshold_effective"]
    if current is None or abs(current - candidate) < 1e-9:
        return result

    cur_n, cur_hr = _tail(rows, current)
    cand_n, cand_hr = _tail(rows, candidate)
    cur_meets = (cur_n >= TREND_CALIBRATION_MIN_TAIL_SAMPLE and cur_hr is not None
                 and cur_hr >= target)
    last = prev.get("last_change_at")
    days = (now - datetime.fromisoformat(last)).days if last else None

    held_by = None
    if days is not None and days < TREND_CALIBRATION_MIN_DAYS:
        held_by = "min_days"
    elif cur_meets and not (cand_n >= TREND_CALIBRATION_MIN_TAIL_SAMPLE and cand_hr is not None
                            and cand_hr >= cur_hr + TREND_CALIBRATION_HYSTERESIS):
        held_by = "hysteresis"
    if held_by is None:
        return result

    held = {k: v for k, v in result.items() if k not in ("calibrated_n", "calibrated_hit_rate")}
    held.update({"threshold_effective": current, "held_by": held_by,
                 "candidate_threshold": candidate})
    if abs(current - fixed_threshold) < 1e-9:
        held["status"] = "at_fixed" if (cur_hr or 0) >= target else "below_target"
    else:
        held["status"] = "calibrated" if cur_meets else "below_target"
        held.update({"calibrated_n": cur_n,
                     "calibrated_hit_rate": round(cur_hr, 4) if cur_hr is not None else None})
    return held


def _history(prev: dict, doc: dict, now_iso: str) -> tuple[list[dict], str | None]:
    """Historial de cambios de umbral (todos los mercados) y fecha del último cambio."""
    history = list(prev.get("threshold_history") or [])
    last_change = prev.get("last_change_at")
    before = prev.get("threshold_effective")
    after = doc["threshold_effective"]
    if before is None or abs(before - after) > 1e-9:
        history.append({
            "at": now_iso, "threshold_from": before, "threshold_to": after,
            "status": doc.get("status"), "sample_n": doc.get("sample_n"),
            "hit_rate": doc.get("calibrated_hit_rate") if doc.get("status") == "calibrated"
            else doc.get("hit_rate_fixed"),
        })
        last_change = now_iso if before is not None else last_change
    return history, last_change


def _calibrate_one(rows: list[dict], fixed_threshold: float, target: float) -> dict:
    """rows: graduadas (hit/miss, sin void) de UN mercado, ya recortadas a la
    ventana de recencia. target: objetivo propio del mercado. No asume ningún
    orden de entrada."""
    n = len(rows)
    if n < TREND_CALIBRATION_MIN_SAMPLE:
        return {"sample_n": n, "hit_rate_fixed": None,
                "threshold_effective": fixed_threshold, "status": "fixed"}

    hits = sum(1 for r in rows if r.get("result") == "hit")
    hit_rate_fixed = round(hits / n, 4)

    if hit_rate_fixed >= target:
        return {"sample_n": n, "hit_rate_fixed": hit_rate_fixed,
                "threshold_effective": fixed_threshold, "status": "at_fixed"}

    # Prefijos de mayor a menor "fuerza" (rate_or_ratio desc) — prefix_hits[k]
    # = aciertos entre las k señales más fuertes, para poder mirar el hit-rate
    # de cada prefijo sin recalcular desde cero.
    ranked = sorted(rows, key=lambda r: r.get("rate_or_ratio") or 0, reverse=True)
    prefix_hits = [0] * (n + 1)
    for i, r in enumerate(ranked, start=1):
        prefix_hits[i] = prefix_hits[i - 1] + (1 if r.get("result") == "hit" else 0)

    for length in range(n - 1, TREND_CALIBRATION_MIN_TAIL_SAMPLE - 1, -1):
        prefix_hit_rate = prefix_hits[length] / length
        if prefix_hit_rate >= target:
            return {
                "sample_n": n, "hit_rate_fixed": hit_rate_fixed,
                "threshold_effective": round(ranked[length - 1]["rate_or_ratio"], 4),
                "status": "calibrated",
                "calibrated_n": length, "calibrated_hit_rate": round(prefix_hit_rate, 4),
            }

    return {"sample_n": n, "hit_rate_fixed": hit_rate_fixed,
            "threshold_effective": fixed_threshold, "status": "below_target"}


async def run_trend_calibration() -> dict:
    """Punto de entrada — se llama al final de trend_grader.run_trend_grader(),
    una vez al día tras graduar. Recorre TREND_MARKET_FIXED_THRESHOLD (los 14
    mercados del feed) y escribe/actualiza un doc por mercado."""
    from shared.firestore_client import col

    from analyzers.trend_variants import active_rules

    # Con una variante promovida, el mercado se calibra solo con las graduadas de esa regla.
    active = active_rules()
    by_market: dict[str, list[dict]] = {}
    try:
        for d in col("trend_accuracy_log").stream():
            row = d.to_dict() or {}
            market = row.get("market")
            if not market or row.get("result") not in ("hit", "miss"):
                continue  # "void" (DNB anulado por empate) fuera, igual que el dashboard
            if row.get("shadow"):
                continue  # variantes en sombra: se miden aparte (trend_variants)
            if market in active and row.get("rule_id") != f"{market}.{active[market]}":
                continue
            by_market.setdefault(market, []).append(row)
    except Exception:
        logger.error("trend_calibration: error leyendo trend_accuracy_log", exc_info=True)
        return {"markets_updated": 0, "error": "read_failed"}

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    try:
        prev_docs = {d.id: d.to_dict() or {} for d in col("trend_market_calibration").stream()}
    except Exception:
        logger.error("trend_calibration: error leyendo el estado anterior — sin histéresis", exc_info=True)
        prev_docs = {}
    updated = 0
    for market, fixed_threshold in TREND_MARKET_FIXED_THRESHOLD.items():
        rows = by_market.get(market, [])
        rows.sort(key=lambda r: r.get("graded_at") or "", reverse=True)
        rows = rows[:TREND_CALIBRATION_WINDOW]

        target = TREND_MARKET_TARGET[market]
        prev = prev_docs.get(market, {})
        if market in SERIES_MARKETS:
            result = _with_hysteresis(_calibrate_series(rows, fixed_threshold, target),
                                      rows, prev, fixed_threshold, target, now)
        else:
            result = _calibrate_one(rows, fixed_threshold, target)
        doc = {
            "market": market,
            "pattern_type": rows[0].get("pattern_type") if rows else None,
            "threshold_fixed": fixed_threshold,
            "target_hit_rate": target,
            "updated_at": now_iso,
            **result,
        }
        doc["threshold_history"], doc["last_change_at"] = _history(prev, doc, now_iso)
        if doc.get("held_by"):
            logger.info("trend_calibration(%s): se mantiene %s (candidato %s, motivo %s)",
                        market, doc["threshold_effective"], doc["candidate_threshold"], doc["held_by"])
        try:
            col("trend_market_calibration").document(market).set(doc)
            updated += 1
        except Exception:
            logger.error("trend_calibration: error guardando %s", market, exc_info=True)

    logger.info("trend_calibration: %d mercados actualizados", updated)
    return {"markets_updated": updated}
