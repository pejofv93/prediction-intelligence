"""
Binance price tracker — REST polling (no WebSocket, para evitar dependencia de min-instances=1).
Guarda snapshots BTC en Firestore binance_snapshots cada 5 minutos.
Detecta divergencias con mercados Polymarket crypto.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"


async def get_btc_price() -> dict:
    """
    GET https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT
    Devuelve {price: float, change_24h_pct: float, high_24h: float, low_24h: float, volume: float}
    Si falla: devuelve {"price": 0, "error": str(e)}
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(BINANCE_TICKER_URL)
        resp.raise_for_status()
        d = resp.json()
        return {
            "price": float(d.get("lastPrice", 0)),
            "change_24h_pct": float(d.get("priceChangePercent", 0)),
            "high_24h": float(d.get("highPrice", 0)),
            "low_24h": float(d.get("lowPrice", 0)),
            "volume": float(d.get("volume", 0)),
        }
    except Exception as e:
        logger.error("get_btc_price: error — %s", e)
        return {"price": 0, "error": str(e)}


async def save_btc_snapshot() -> dict:
    """
    Llama get_btc_price(), guarda en col("binance_snapshots") con:
    {symbol: "BTCUSDT", price, change_24h_pct, high_24h, low_24h, volume, recorded_at: datetime UTC}
    Devuelve el snapshot guardado.
    """
    from shared.firestore_client import col

    data = await get_btc_price()
    if data.get("price", 0) == 0:
        logger.warning("save_btc_snapshot: precio 0 — no guardando snapshot")
        return data

    snapshot = {
        "symbol": "BTCUSDT",
        "price": data["price"],
        "change_24h_pct": data["change_24h_pct"],
        "high_24h": data["high_24h"],
        "low_24h": data["low_24h"],
        "volume": data["volume"],
        "recorded_at": datetime.now(timezone.utc),
    }

    try:
        col("binance_snapshots").add(snapshot)
        logger.info(
            "save_btc_snapshot: BTC=%.2f (%.2f%%) guardado en Firestore",
            snapshot["price"],
            snapshot["change_24h_pct"],
        )
    except Exception as e:
        logger.error("save_btc_snapshot: error guardando en Firestore — %s", e)

    return snapshot


async def get_latest_btc() -> dict:
    """Lee el snapshot más reciente de Firestore binance_snapshots."""
    from shared.firestore_client import col

    docs = list(
        col("binance_snapshots")
        .where("symbol", "==", "BTCUSDT")
        .order_by("recorded_at", direction="DESCENDING")
        .limit(1)
        .stream()
    )
    if docs:
        d = docs[0].to_dict()
        recorded_at = d.get("recorded_at")
        if recorded_at is not None and hasattr(recorded_at, "replace"):
            # Verificar que el snapshot no tenga más de 10 minutos
            if hasattr(recorded_at, "tzinfo") and recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - recorded_at
            if age < timedelta(minutes=10):
                return d
    # Si no hay snapshot reciente (<10min), hacer fetch directo
    return await get_btc_price()


def detect_crypto_divergence(btc_data: dict, poly_market: dict) -> dict | None:
    """
    Detecta divergencia entre precio BTC y mercados crypto Polymarket.
    btc_data: {price, change_24h_pct}
    poly_market: {question, market_price_yes, edge}

    Lógica:
    - Si change_24h_pct > 2.0% y question contiene "above/over/hit" y market_price_yes < 0.40:
      → divergencia alcista: BTC sube pero mercado no lo refleja
    - Si change_24h_pct < -2.0% y question contiene "above/over/hit" y market_price_yes > 0.60:
      → divergencia bajista: BTC cae pero mercado sigue alto

    Devuelve {type: "bullish"|"bearish", btc_change: float, market_prob: float,
              suggested_edge: float} o None si no hay divergencia.
    """
    btc_change = float(btc_data.get("change_24h_pct", 0))
    question = str(poly_market.get("question", "")).lower()
    market_prob = float(poly_market.get("market_price_yes", 0.5))

    upward_keywords = ["above", "over", "hit", "reach", "exceed"]
    is_upward_question = any(kw in question for kw in upward_keywords)

    if btc_change > 2.0 and is_upward_question and market_prob < 0.40:
        # BTC sube pero el mercado no lo refleja → comprar YES es ineficiencia alcista
        suggested_edge = round(min(btc_change / 100 * 1.5, 0.20), 4)
        return {
            "type": "bullish",
            "btc_change": btc_change,
            "market_prob": market_prob,
            "suggested_edge": suggested_edge,
        }

    if btc_change < -2.0 and is_upward_question and market_prob > 0.60:
        # BTC cae pero el mercado sigue alto → comprar NO es ineficiencia bajista
        suggested_edge = round(min(abs(btc_change) / 100 * 1.5, 0.20), 4)
        return {
            "type": "bearish",
            "btc_change": btc_change,
            "market_prob": market_prob,
            "suggested_edge": suggested_edge,
        }

    return None
