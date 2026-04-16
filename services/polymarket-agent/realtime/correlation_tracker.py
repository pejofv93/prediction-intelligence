"""
Tracker de correlacion entre mercados Polymarket y activos crypto (CoinGecko).
"""
import logging

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3"
CRYPTO_KEYWORDS = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
}


import time

import httpx

# Cache en memoria: {coin_id: (price, timestamp)}
_price_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 60  # segundos


async def get_crypto_price(coin_id: str) -> float | None:
    """
    GET /simple/price?ids={coin_id}&vs_currencies=usd
    Cache en memoria 60s para no spammear CoinGecko.
    """
    from shared.config import COINGECKO_API_KEY

    now = time.time()
    if coin_id in _price_cache:
        price, ts = _price_cache[coin_id]
        if now - ts < _CACHE_TTL:
            return price

    try:
        params = {"ids": coin_id, "vs_currencies": "usd"}
        headers = {}
        if COINGECKO_API_KEY:
            headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{COINGECKO_URL}/simple/price",
                params=params,
                headers=headers,
            )

        if resp.status_code != 200:
            logger.debug("get_crypto_price(%s): respondio %d", coin_id, resp.status_code)
            return None

        data = resp.json()
        price = float(data.get(coin_id, {}).get("usd", 0))
        if price > 0:
            _price_cache[coin_id] = (price, now)
            return price
        return None

    except Exception:
        logger.error("get_crypto_price(%s): error", coin_id, exc_info=True)
        return None


def detect_market_leads_asset(
    market_price_now: float,
    market_price_1h_ago: float,
    asset_price_now: float,
    asset_price_1h_ago: float,
) -> dict:
    """
    Si el mercado sube y el activo NO ha subido todavia → "market_leads" → posible smart money.
    Devuelve {divergence: bool, direction: "market_leads"|"asset_leads"|"aligned", magnitude: float}
    """
    try:
        market_change = (market_price_now - market_price_1h_ago) / market_price_1h_ago if market_price_1h_ago != 0 else 0.0
        asset_change = (asset_price_now - asset_price_1h_ago) / asset_price_1h_ago if asset_price_1h_ago != 0 else 0.0

        magnitude = round(abs(market_change - asset_change), 4)

        # Divergencia significativa (> 3%)
        if magnitude < 0.03:
            return {"divergence": False, "direction": "aligned", "magnitude": magnitude}

        if market_change > 0.03 and asset_change < 0.01:
            direction = "market_leads"
        elif asset_change > 0.03 and market_change < 0.01:
            direction = "asset_leads"
        else:
            direction = "aligned"

        divergence = direction in ("market_leads", "asset_leads")
        return {"divergence": divergence, "direction": direction, "magnitude": magnitude}

    except Exception:
        logger.error("detect_market_leads_asset: error", exc_info=True)
        return {"divergence": False, "direction": "aligned", "magnitude": 0.0}


async def enrich_with_correlation(poly_prediction: dict) -> dict:
    """
    Si el mercado tiene keyword crypto → obtiene precio de CoinGecko y calcula divergencia.
    Anade "asset_correlation" al poly_prediction.
    """
    try:
        question = poly_prediction.get("question", "").lower()
        coin_id = None
        for keyword, cid in CRYPTO_KEYWORDS.items():
            if keyword in question:
                coin_id = cid
                break

        if coin_id is None:
            poly_prediction["asset_correlation"] = None
            return poly_prediction

        price_now = await get_crypto_price(coin_id)
        if price_now is None:
            poly_prediction["asset_correlation"] = None
            return poly_prediction

        # Para la divergencia necesitariamos precio hace 1h — si no lo tenemos usamos 0.5 de cambio neutral
        # En este contexto simplificado solo guardamos el precio actual
        poly_prediction["asset_correlation"] = {
            "coin_id": coin_id,
            "price_usd": price_now,
            "divergence": None,  # requiere historial de precio del mercado hace 1h
        }
        return poly_prediction

    except Exception:
        logger.error("enrich_with_correlation: error", exc_info=True)
        poly_prediction["asset_correlation"] = None
        return poly_prediction
