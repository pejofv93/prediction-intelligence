"""
Collector: API-Sports multi-deporte
Usa FOOTBALL_RAPID_API_KEY — la misma key que futbol.
Deportes: NBA, NFL, MLB, NHL, MMA
Rate limit: 100 req/dia total compartido con futbol.
"""
import logging

from shared.config import FOOTBALL_RAPID_API_KEY, SUPPORTED_SPORTS_APISPORTS

logger = logging.getLogger(__name__)

# URLs por deporte (todas requieren X-RapidAPI-Key y X-RapidAPI-Host)
API_SPORTS_HOSTS = {
    "basketball": "api-basketball.p.rapidapi.com",
    "american-football": "api-american-football.p.rapidapi.com",
    "baseball": "api-baseball.p.rapidapi.com",
    "hockey": "api-hockey.p.rapidapi.com",
    "mma": "api-mma.p.rapidapi.com",
}

API_SPORTS_DELAY = 2.0  # segundos entre requests — conservador dado el limite diario


async def get_games_today(sport: str) -> list[dict]:
    """
    sport: "nba" | "nfl" | "mlb" | "nhl" | "mma"
    Devuelve partidos del dia con scores si disponibles.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def get_team_stats_bdl(sport: str, team_id: int, last_n: int = 10) -> dict:
    """
    Ultimos N partidos del equipo: pts, reb, ast (NBA) o yds, td (NFL) etc.
    Calcula: form_score, home_away_split, streak.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def get_injuries(sport: str) -> list[dict]:
    """
    Solo NBA y NFL tienen endpoint de lesiones en API-Sports.
    Devuelve lista de jugadores lesionados actualmente.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def get_odds_bdl(sport: str, game_id: int) -> dict | None:
    """
    Cuotas en tiempo real si disponibles (API-Sports, solo algunas ligas).
    Si no disponible → fallback a API-Sports: GET https://api-sports.io/odds.
    Devuelve {moneyline_home, moneyline_away, spread, total} o None.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError
