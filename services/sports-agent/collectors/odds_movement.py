"""
Movimiento de cuotas desde odds_cache (sin llamadas API adicionales).
Lee directamente del documento odds_cache:
  odds_cache.opening_home_odds → cuota de apertura (NO se sobreescribe)
  odds_cache.home_odds         → cuota actual (actualizada en cada refresh)
  movement = (home_odds - opening_home_odds) / opening_home_odds
Si opening_home_odds == home_odds (primer fetch) → movement = 0.0
CERO llamadas adicionales a la API.
"""
import logging

logger = logging.getLogger(__name__)


async def get_odds_movement(fixture_id: str) -> float:
    """
    Lee odds_cache de Firestore para fixture_id.
    Devuelve variacion porcentual de la cuota home desde apertura.
    Devuelve 0.0 si no hay datos o es el primer fetch.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError
