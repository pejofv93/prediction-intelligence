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


async def get_crypto_price(coin_id: str) -> float | None:
    """
    GET /simple/price?ids={coin_id}&vs_currencies=usd
    Cache en memoria 60s para no spammear CoinGecko.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError


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
    # TODO: implementar en Sesion 5
    raise NotImplementedError


async def enrich_with_correlation(poly_prediction: dict) -> dict:
    """
    Si el mercado tiene keyword crypto → obtiene precio de CoinGecko y calcula divergencia.
    Anade "asset_correlation" al poly_prediction.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
