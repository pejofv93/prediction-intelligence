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
  2. Si hit_rate_fixed >= TREND_TARGET_HIT_RATE → el umbral fijo ya cumple el
     objetivo, no hace falta apretar ("at_fixed").
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
"""
import logging
from datetime import datetime, timezone

from shared.config import (
    TREND_CALIBRATION_MIN_SAMPLE,
    TREND_CALIBRATION_MIN_TAIL_SAMPLE,
    TREND_CALIBRATION_WINDOW,
    TREND_MARKET_FIXED_THRESHOLD,
    TREND_TARGET_HIT_RATE,
)

logger = logging.getLogger(__name__)


def _calibrate_one(rows: list[dict], fixed_threshold: float) -> dict:
    """rows: graduadas (hit/miss, sin void) de UN mercado, ya recortadas a la
    ventana de recencia. No asume ningún orden de entrada."""
    n = len(rows)
    if n < TREND_CALIBRATION_MIN_SAMPLE:
        return {"sample_n": n, "hit_rate_fixed": None,
                "threshold_effective": fixed_threshold, "status": "fixed"}

    hits = sum(1 for r in rows if r.get("result") == "hit")
    hit_rate_fixed = round(hits / n, 4)

    if hit_rate_fixed >= TREND_TARGET_HIT_RATE:
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
        if prefix_hit_rate >= TREND_TARGET_HIT_RATE:
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

    by_market: dict[str, list[dict]] = {}
    try:
        for d in col("trend_accuracy_log").stream():
            row = d.to_dict() or {}
            market = row.get("market")
            if not market or row.get("result") not in ("hit", "miss"):
                continue  # "void" (DNB anulado por empate) fuera, igual que el dashboard
            by_market.setdefault(market, []).append(row)
    except Exception:
        logger.error("trend_calibration: error leyendo trend_accuracy_log", exc_info=True)
        return {"markets_updated": 0, "error": "read_failed"}

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    for market, fixed_threshold in TREND_MARKET_FIXED_THRESHOLD.items():
        rows = by_market.get(market, [])
        rows.sort(key=lambda r: r.get("graded_at") or "", reverse=True)
        rows = rows[:TREND_CALIBRATION_WINDOW]

        result = _calibrate_one(rows, fixed_threshold)
        doc = {
            "market": market,
            "pattern_type": rows[0].get("pattern_type") if rows else None,
            "threshold_fixed": fixed_threshold,
            "target_hit_rate": TREND_TARGET_HIT_RATE,
            "updated_at": now_iso,
            **result,
        }
        try:
            col("trend_market_calibration").document(market).set(doc)
            updated += 1
        except Exception:
            logger.error("trend_calibration: error guardando %s", market, exc_info=True)

    logger.info("trend_calibration: %d mercados actualizados", updated)
    return {"markets_updated": updated}
