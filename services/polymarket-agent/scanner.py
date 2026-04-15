"""
Scanner Polymarket — fetch top 50 mercados activos por volumen.
Guarda en Firestore poly_markets.
"""
import logging

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


async def fetch_active_markets(
    limit: int = 50, min_volume: float = 10000
) -> list[dict]:
    """
    GET /markets?active=true&order=volume24hr&limit={limit}
    Filtra: volume_24h >= min_volume AND end_date > now + 2 days.
    IMPORTANTE: Guardar campo "conditionId" de la respuesta como "condition_id" en Firestore.
    Sin condition_id el orderbook_analyzer no puede funcionar.
    Guarda en Firestore poly_markets.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError


async def fetch_market_orderbook(condition_id: str) -> dict:
    """
    El orderbook NO esta en gamma-api.polymarket.com.
    URL correcta: https://clob.polymarket.com/order-book/{condition_id}
    condition_id viene del campo condition_id del mercado en Firestore poly_markets.
    Sin auth para lectura publica.
    Si falla → devuelve buy_ratio=0.5 (neutral, no crashear).
    Devuelve {bids, asks, spread, buy_ratio}.
    buy_ratio = sum(bid sizes) / (sum(bid sizes) + sum(ask sizes))
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
