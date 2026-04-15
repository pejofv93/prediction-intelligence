"""
Analizador Groq para mercados Polymarket enriched.
Recibe enriched_market → prob real + edge + reasoning.
"""
import logging

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE
from shared.groq_client import GROQ_CALL_DELAY

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Eres un analista experto en mercados de prediccion. "
    "Se te proporciona un mercado de Polymarket con datos estadisticos completos: "
    "historial de precios, order book, smart money, correlaciones y sentiment de noticias. "
    "Integra TODOS los datos. Responde SOLO en JSON: "
    '{"real_prob": float, "edge": float, "confidence": float, '
    '"trend": "RISING|FALLING|STABLE", "recommendation": "BUY_YES|BUY_NO|PASS|WATCH", '
    '"key_factors": list[str], "reasoning": string}'
)


async def analyze_market(enriched_market: dict) -> dict | None:
    """
    Solo analiza si: volume_24h > 5000 AND days_to_close > 2.
    NO usa web_search (news_sentiment ya viene del enricher).
    Al llamar en batch: await asyncio.sleep(GROQ_CALL_DELAY) entre cada mercado.
    Al guardar en poly_predictions copiar desde enriched_market:
      poly_prediction["volume_spike"] = enriched_market["volume_spike"]
      poly_prediction["smart_money_detected"] = enriched_market["smart_money"]["is_smart_money"]
    Guarda resultado en Firestore poly_predictions.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError


async def run_maintenance() -> None:
    """
    Ejecutar al final de cada /run-analyze.
    1. Borrar poly_price_history donde timestamp < now - 30 dias (batch delete)
    2. Borrar enriched_markets donde enriched_at < now - 7 dias
    Usar batch writes de Firestore (max 500 ops/batch) para no exceder limites.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
