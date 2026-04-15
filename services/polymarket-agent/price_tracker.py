"""
Price tracker — snapshots historicos, momentum y volume spike.
"""
import logging

logger = logging.getLogger(__name__)


async def save_price_snapshot(
    market_id: str, price_yes: float, price_no: float, volume_24h: float
) -> None:
    """Anade a Firestore poly_price_history."""
    # TODO: implementar en Sesion 5
    raise NotImplementedError


async def price_momentum(market_id: str) -> str:
    """
    Lee snapshots ultimas 6h.
    Sube > 3% → "RISING" | Baja > 3% → "FALLING" | Else → "STABLE".
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError


async def volume_spike(market_id: str) -> bool:
    """True si vol_24h_actual > 3 x media_7_dias."""
    # TODO: implementar en Sesion 5
    raise NotImplementedError


async def smart_money_detection(market_id: str) -> dict:
    """
    Detecta smart money usando heuristica de velocidad del spike.
    NO compara contra noticias en Firestore (no existe esa coleccion).
    Heuristica: si el volumen sube > 5x la media en < 1 hora → probable smart money.
    Devuelve {"is_smart_money": bool, "hours_before_news": None}.
    hours_before_news siempre None — no tenemos timestamps de noticias.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
