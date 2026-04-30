"""
Analizador del libro de ordenes de Polymarket.
Usa condition_id (no market_id) para llamar al CLOB.
"""
import logging

from shared.firestore_client import col

logger = logging.getLogger(__name__)

_NEUTRAL = {
    "buy_pressure": 0.5,
    "spread": 0.0,
    "depth": 0.0,
    "imbalance_signal": "NEUTRAL",
}


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
    try:
        doc = col("poly_markets").document(market_id).get()
        if not doc.exists:
            logger.debug("analyze_orderbook(%s): mercado no encontrado en Firestore", market_id)
            return dict(_NEUTRAL)

        condition_id = doc.to_dict().get("condition_id", "")
        if not condition_id:
            logger.debug("analyze_orderbook(%s): sin condition_id", market_id)
            return dict(_NEUTRAL)

        from scanner import fetch_market_orderbook
        try:
            ob = await fetch_market_orderbook(condition_id)
        except Exception as e:
            logger.warning("analyze_orderbook(%s): CLOB fetch falló — %s", market_id, e)
            return dict(_NEUTRAL)

        buy_pressure = float(ob.get("buy_ratio", 0.5))
        spread = float(ob.get("spread", 0.0))

        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        depth = sum(float(b.get("size", 0)) for b in bids) + sum(float(a.get("size", 0)) for a in asks)

        if buy_pressure > 0.65:
            imbalance_signal = "BULLISH"
        elif buy_pressure < 0.35:
            imbalance_signal = "BEARISH"
        else:
            imbalance_signal = "NEUTRAL"

        return {
            "buy_pressure": round(buy_pressure, 4),
            "spread": round(spread, 4),
            "depth": round(depth, 2),
            "imbalance_signal": imbalance_signal,
        }

    except Exception:
        logger.error("analyze_orderbook(%s): error", market_id, exc_info=True)
        return dict(_NEUTRAL)
