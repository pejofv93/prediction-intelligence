"""
Movimiento de cuotas desde odds_cache (sin llamadas API adicionales).
Lee directamente del documento odds_cache en Firestore:
  odds_cache.opening_home_odds → cuota de apertura (NO se sobreescribe nunca)
  odds_cache.home_odds         → cuota actual (actualizada en cada refresh)
  movement = (home_odds - opening_home_odds) / opening_home_odds
Si opening_home_odds == home_odds (primer fetch) → movement = 0.0
CERO llamadas adicionales a la API.
"""
import logging

from shared.firestore_client import col

logger = logging.getLogger(__name__)


async def get_odds_movement(fixture_id: str) -> float:
    """
    Lee odds_cache de Firestore para fixture_id.
    Devuelve variacion porcentual de la cuota home desde apertura.
    Devuelve 0.0 si no hay datos o es el primer fetch (sin movimiento).
    """
    try:
        doc = col("odds_cache").document(fixture_id).get()
        if not doc.exists:
            return 0.0

        data = doc.to_dict()
        home_odds = data.get("home_odds")
        opening_home_odds = data.get("opening_home_odds")

        if not home_odds or not opening_home_odds or opening_home_odds == 0:
            return 0.0

        movement = (home_odds - opening_home_odds) / opening_home_odds
        return round(movement, 4)

    except Exception:
        logger.error(
            "get_odds_movement(%s): error leyendo odds_cache", fixture_id, exc_info=True
        )
        return 0.0
