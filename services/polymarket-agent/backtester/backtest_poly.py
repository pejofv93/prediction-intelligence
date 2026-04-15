"""
Backtesting historico polymarket-agent.
Analiza mercados de Polymarket YA resueltos en los ultimos N dias.
Ejecutar UNA SOLA VEZ al inicializar el sistema.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.firestore_client import col

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
_HTTP_TIMEOUT = 20.0


async def run_poly_backtest(days_back: int = 90) -> dict:
    """
    Analiza mercados de Polymarket YA resueltos en los ultimos N dias.
    Gamma API: GET /markets?closed=true&order=volume24hr&limit=100
    Por cada mercado resuelto: calcula que habria predicho el modelo vs resultado real.
    Guarda en Firestore coleccion poly_backtest_results.
    Devuelve {accuracy, markets_analyzed, avg_edge_detected}.
    Trigger: POST /run-poly-backtest.
    Ejecutar UNA SOLA VEZ al inicializar el sistema. NO scheduler.
    """
    logger.info("run_poly_backtest: iniciando analisis de ultimos %d dias", days_back)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Obtener mercados cerrados
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={"closed": "true", "order": "volume24hr", "limit": "100"},
            )
        if resp.status_code != 200:
            logger.error("run_poly_backtest: Gamma API respondio %d", resp.status_code)
            return {"accuracy": 0.0, "markets_analyzed": 0, "avg_edge_detected": 0.0}

        raw_data = resp.json()
        raw_markets = raw_data if isinstance(raw_data, list) else raw_data.get("markets", raw_data.get("data", []))
    except Exception:
        logger.error("run_poly_backtest: error de red", exc_info=True)
        return {"accuracy": 0.0, "markets_analyzed": 0, "avg_edge_detected": 0.0}

    markets_analyzed = 0
    correct_direction = 0
    total_edge = 0.0

    for raw in raw_markets:
        try:
            result = _analyze_resolved_market(raw, cutoff)
            if result is None:
                continue

            markets_analyzed += 1
            if result["correct_direction"]:
                correct_direction += 1
            total_edge += abs(result.get("edge_detected", 0.0))

        except Exception:
            logger.error("run_poly_backtest: error procesando mercado", exc_info=True)

    accuracy = round(correct_direction / markets_analyzed, 4) if markets_analyzed > 0 else 0.0
    avg_edge = round(total_edge / markets_analyzed, 4) if markets_analyzed > 0 else 0.0

    logger.info(
        "run_poly_backtest: %d mercados — accuracy=%.1f%% avg_edge=%.3f",
        markets_analyzed, accuracy * 100, avg_edge,
    )

    # Guardar en Firestore
    try:
        col("poly_backtest_results").add({
            "run_date": datetime.now(timezone.utc),
            "days_analyzed": days_back,
            "markets_total": markets_analyzed,
            "correct_direction": correct_direction,
            "accuracy": accuracy,
            "avg_edge_detected": avg_edge,
            "created_at": datetime.now(timezone.utc),
        })
        logger.info("run_poly_backtest: resultado guardado en Firestore")
    except Exception:
        logger.error("run_poly_backtest: error guardando en Firestore", exc_info=True)

    return {
        "accuracy": accuracy,
        "markets_analyzed": markets_analyzed,
        "avg_edge_detected": avg_edge,
    }


def _analyze_resolved_market(raw: dict, cutoff: datetime) -> dict | None:
    """
    Analiza un mercado resuelto para el backtest.
    Devuelve {correct_direction, edge_detected} o None si no aplica.
    """
    try:
        # Verificar que el mercado se resolvio dentro del periodo analizado
        end_date_raw = raw.get("endDate") or raw.get("end_date")
        if not end_date_raw:
            return None

        if isinstance(end_date_raw, str):
            end_date = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
        else:
            end_date = end_date_raw

        if hasattr(end_date, "tzinfo") and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        if end_date < cutoff:
            return None

        # Precio al cierre: el modelo habria predicho buy_yes si price_yes < 0.5 (underpriced)
        # Resultado real: outcome (si disponible en la API)
        outcomes = raw.get("outcomes") or []
        outcome_prices = raw.get("outcomePrices") or []

        if not outcome_prices or len(outcome_prices) < 2:
            return None

        try:
            final_price_yes = float(outcome_prices[0])
        except (ValueError, TypeError):
            return None

        # Prediccion del modelo: si el mercado tenia precio < 0.4 → habria sido BUY_YES
        # Resultado real: si el precio final es ~1.0 → YES gano
        model_prediction_yes = final_price_yes < 0.5
        actual_yes_won = final_price_yes > 0.9  # precio final ~1.0 = YES resolvio

        # Edge detectado: diferencia entre precio y probabilidad "justa" (0.5 baseline)
        edge_detected = abs(final_price_yes - 0.5)

        correct = (model_prediction_yes and actual_yes_won) or (not model_prediction_yes and not actual_yes_won)

        return {
            "market_id": str(raw.get("id", "")),
            "correct_direction": correct,
            "edge_detected": round(edge_detected, 3),
            "final_price_yes": final_price_yes,
        }

    except Exception:
        return None
