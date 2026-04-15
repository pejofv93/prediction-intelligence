"""
Collector: football-data.org
Deportes: futbol europeo (PL, PD, BL1, SA)
Modelo: Poisson bivariado + ELO
"""
import asyncio
import logging

import httpx

from shared.config import FOOTBALL_API_KEY, SUPPORTED_FOOTBALL_LEAGUES

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"
RATE_LIMIT_DELAY = 6.5  # segundos entre requests (10 req/min = 1/6s, con margen)

# NO definir HEADERS a nivel de modulo — FOOTBALL_API_KEY puede ser None en el momento
# en que se importa el modulo. Construir el header dentro de cada funcion async.


async def get_upcoming_matches(days: int = 7) -> list[dict]:
    """GET /matches?dateFrom=today&dateTo=today+days. Filtra por SUPPORTED_LEAGUES."""
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def get_team_stats(team_id: int, last_n: int = 10) -> dict:
    """GET /teams/{team_id}/matches?status=FINISHED&limit={last_n}"""
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def get_h2h(team1_id: int, team2_id: int) -> list[dict]:
    """
    GET /teams/{team1_id}/matches?status=FINISHED&limit=10. Filtra vs team2_id.
    Free tier: max 10 partidos.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def get_standings(league_id: int) -> list[dict]:
    """GET /competitions/{league_id}/standings"""
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def get_match_result(match_id: str) -> dict | None:
    """GET /matches/{match_id}. Devuelve resultado si FINISHED, None si no."""
    # TODO: implementar en Sesion 2
    raise NotImplementedError
