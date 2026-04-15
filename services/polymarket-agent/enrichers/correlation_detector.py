"""
Detector de correlaciones entre mercados Polymarket.
Detecta mercados mutuamente excluyentes e ineficiencias de arbitraje.
"""
import logging

logger = logging.getLogger(__name__)


async def find_correlated_markets(
    market_id: str, all_markets: list[dict]
) -> list[dict]:
    """
    Detecta mercados correlacionados por keywords.
    Ej: "Biden gana" <-> "Trump gana" son mutuamente excluyentes.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError


def detect_arbitrage(market: dict, correlated: list[dict]) -> dict:
    """
    Probabilidad = market["price_yes"] y correlated[i]["price_yes"].
    Si sum(price_yes de mercados mutuamente excluyentes) != 1.0 → ineficiencia.
    inefficiency = abs(1.0 - total_prob).
    direction = "OVERPRICED" si total > 1, "UNDERPRICED" si total < 1.
    Devuelve {"detected": bool, "inefficiency": float, "direction": str}.
    Clave "detected" (no "arbitrage_detected") — coincidir con enriched_markets.arbitrage schema.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
