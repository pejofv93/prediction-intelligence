"""
Analizador del libro de ordenes de Polymarket.
Usa condition_id (no market_id) para llamar al CLOB.
"""
import logging

logger = logging.getLogger(__name__)


async def analyze_orderbook(market_id: str) -> dict:
    """
    1. Obtener condition_id desde Firestore poly_markets donde market_id == market_id
       (NO usar market_id directamente en la URL del CLOB — son IDs distintos)
    2. Llamar scanner.fetch_market_orderbook(condition_id)
    3. Si condition_id no existe o el fetch falla → devolver buy_pressure=0.5, imbalance_signal="NEUTRAL"
    Devuelve:
      buy_pressure: ratio compradores YES (0.0-1.0)
      spread: diferencia bid/ask
      depth: volumen total en libro
      imbalance_signal: "BULLISH" si buy_ratio > 0.65, "BEARISH" si < 0.35, "NEUTRAL" si no
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
