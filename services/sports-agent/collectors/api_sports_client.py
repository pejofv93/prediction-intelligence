"""
Collector: API-Sports multi-deporte via RapidAPI.
Usa FOOTBALL_RAPID_API_KEY — la misma key que football_api.py.
Deportes: NBA, NFL, MLB, NHL, MMA.
Rate limit: 100 req/DIA total compartido con cuotas de futbol.
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx

from shared.config import FOOTBALL_RAPID_API_KEY, SUPPORTED_SPORTS_APISPORTS

logger = logging.getLogger(__name__)

# Base URLs por deporte (todas requieren X-RapidAPI-Key y X-RapidAPI-Host)
API_SPORTS_HOSTS = {
    "basketball": "api-basketball.p.rapidapi.com",  # corregido: basketball-arbitrage era 404
    "american-football": "api-american-football.p.rapidapi.com",
    "baseball": "api-baseball.p.rapidapi.com",
    "hockey": "api-hockey.p.rapidapi.com",
    # "mma": "api-mma.p.rapidapi.com",  # desactivado: API eliminada → 404
}

# Deportes desactivados por endpoint muerto — get_games_today retorna [] sin HTTP
_DISABLED_SPORTS = {"mma", "ufc"}  # api-mma.p.rapidapi.com/fights → 404

API_SPORTS_DELAY = 2.0  # segundos entre requests — conservador dado el limite diario

# Mapeo deporte abreviado → sport_type key (para buscar en API_SPORTS_HOSTS)
_LEAGUE_TO_SPORT_TYPE = {v: k for k, v in SUPPORTED_SPORTS_APISPORTS.items()}
# {"nba": "basketball", "nfl": "american-football", "mlb": "baseball", "nhl": "hockey", "ufc": "mma"}
# Euroleague usa el mismo host que NBA (api-basketball.p.rapidapi.com)
_LEAGUE_TO_SPORT_TYPE["euroleague"] = "basketball"

# Mapeo sport abreviado → nombre de liga en Firestore
_SPORT_TO_LEAGUE = {
    "nba": "NBA",
    "nfl": "NFL",
    "mlb": "MLB",
    "nhl": "NHL",
    "mma": "MMA",
    "ufc": "MMA",
    "euroleague": "EUROLEAGUE",
}


def _get_host(sport: str) -> str:
    """Devuelve el host RapidAPI para un deporte dado (ej: 'nba' → 'api-basketball...')."""
    sport_type = _LEAGUE_TO_SPORT_TYPE.get(sport)
    if not sport_type:
        raise ValueError(f"Deporte no soportado: {sport}")
    host = API_SPORTS_HOSTS.get(sport_type)
    if not host:
        raise ValueError(f"Host no encontrado para sport_type: {sport_type}")
    return host


async def _request(host: str, path: str, params: dict | None = None) -> dict | None:
    """
    Llamada autenticada a API-Sports via RapidAPI con rate limiting y manejo de errores.
    Devuelve JSON parseado o None si falla.
    """
    if not FOOTBALL_RAPID_API_KEY:
        raise RuntimeError("FOOTBALL_RAPID_API_KEY no configurada para este servicio")

    await asyncio.sleep(API_SPORTS_DELAY)

    url = f"https://{host}{path}"
    headers = {
        "X-RapidAPI-Key": FOOTBALL_RAPID_API_KEY,
        "X-RapidAPI-Host": host,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers, params=params)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                logger.warning("Rate limit API-Sports — esperando %ds", wait)
                await asyncio.sleep(wait)
                resp = await client.get(url, headers=headers, params=params)

            if resp.status_code >= 400:
                logger.error(
                    "API-Sports %s%s → HTTP %d: %.200s",
                    host, path, resp.status_code, resp.text,
                )
                return None

            return resp.json()

    except httpx.TimeoutException:
        logger.error("Timeout en API-Sports %s%s", host, path, exc_info=True)
        return None
    except Exception:
        logger.error("Error en API-Sports %s%s", host, path, exc_info=True)
        return None


def _parse_game(item: dict, sport: str) -> dict:
    """
    Normaliza un objeto game de API-Sports al formato interno estándar.
    La estructura varía ligeramente por deporte; intentamos cubrir los casos principales.
    """
    teams = item.get("teams", {})
    # API-Basketball usa "visitors", otros usan "away"
    home = teams.get("home", {})
    away = teams.get("visitors", teams.get("away", {}))

    scores = item.get("scores", item.get("score", {}))
    home_score_obj = scores.get("home", {})
    away_score_obj = scores.get("visitors", scores.get("away", {}))

    # Puntos/goles según deporte
    home_pts = (
        home_score_obj.get("points")
        or home_score_obj.get("score")
        or home_score_obj.get("total")
    )
    away_pts = (
        away_score_obj.get("points")
        or away_score_obj.get("score")
        or away_score_obj.get("total")
    )

    status_obj = item.get("status", {})
    status_str = status_obj.get("long", status_obj.get("short", "SCHEDULED"))

    return {
        "match_id": str(item.get("id", "")),
        "date": item.get("date", item.get("date_start", "")),
        "home_team_id": home.get("id"),
        "away_team_id": away.get("id"),
        "home_team_name": home.get("name", ""),
        "away_team_name": away.get("name", ""),
        "goals_home": home_pts,
        "goals_away": away_pts,
        "league": _SPORT_TO_LEAGUE.get(sport, sport.upper()),
        "status": status_str,
        "sport": sport,
        # Shots no disponibles en API-Sports de forma estándar
        "shots_home": None,
        "shots_away": None,
        "shots_on_target_home": None,
        "shots_on_target_away": None,
    }


async def get_games_today(sport: str) -> list[dict]:
    """
    sport: "nba" | "nfl" | "mlb" | "nhl".
    Devuelve partidos del dia con scores si disponibles.
    """
    if sport in _DISABLED_SPORTS:
        logger.info("api_sports_client: colector %s desactivado — endpoint muerto", sport)
        return []
    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_games_today: %s", e)
        return []

    today = datetime.now(timezone.utc).date().isoformat()

    # API-MMA/UFC usa endpoint diferente
    if sport in ("mma", "ufc"):
        data = await _request(host, "/fights", {"date": today})
    else:
        data = await _request(host, "/games", {"date": today})

    if not data:
        return []

    games = []
    for item in data.get("response", []):
        try:
            games.append(_parse_game(item, sport))
        except Exception:
            logger.error("Error parseando game de API-Sports (%s)", sport, exc_info=True)

    logger.info("get_games_today(%s): %d partidos hoy", sport, len(games))
    return games


async def get_team_stats_bdl(
    sport: str, team_id: int, last_n: int = 10
) -> list[dict]:
    """
    Ultimos N partidos del equipo para calcular forma.
    Devuelve lista de partidos normalizados (mismo formato que football_api).
    """
    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_team_stats_bdl: %s", e)
        return []

    # Algunos endpoints de API-Sports usan "last", otros usan paginacion
    if sport in ("mma", "ufc"):
        # MMA/UFC no tiene endpoint de team stats estándar
        logger.info("get_team_stats_bdl: MMA no tiene stats de equipo — omitiendo")
        return []

    data = await _request(host, "/games", {"team": team_id, "last": last_n})
    if not data:
        return []

    matches = []
    for item in data.get("response", []):
        try:
            parsed = _parse_game(item, sport)
            # Solo incluir partidos terminados con resultado
            if parsed["goals_home"] is not None and parsed["goals_away"] is not None:
                matches.append(parsed)
        except Exception:
            logger.error("Error parseando team stats (%s, %d)", sport, team_id, exc_info=True)

    logger.info("get_team_stats_bdl(%s, %d): %d partidos", sport, team_id, len(matches))
    return matches


async def get_injuries(sport: str) -> list[dict]:
    """
    Solo NBA y NFL tienen endpoint de lesiones en API-Sports.
    Devuelve lista de jugadores lesionados actualmente.
    """
    if sport not in ("nba", "nfl"):
        return []

    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_injuries: %s", e)
        return []

    data = await _request(host, "/injuries")
    if not data:
        return []

    injuries = []
    for item in data.get("response", []):
        try:
            player = item.get("player", {})
            team = item.get("team", {})
            injuries.append({
                "player_id": player.get("id"),
                "player_name": player.get("name", ""),
                "team_id": team.get("id"),
                "team_name": team.get("name", ""),
                "status": item.get("status", ""),
                "reason": item.get("reason", ""),
                "sport": sport,
            })
        except Exception:
            logger.error("Error parseando injury (%s)", sport, exc_info=True)

    logger.info("get_injuries(%s): %d lesionados", sport, len(injuries))
    return injuries


async def get_odds_bdl(sport: str, game_id: int) -> dict | None:
    """
    Cuotas en tiempo real si disponibles (API-Sports, solo algunas ligas).
    Devuelve {moneyline_home, moneyline_away, spread, total} o None.
    """
    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_odds_bdl: %s", e)
        return None

    data = await _request(host, "/odds", {"game": game_id})
    if not data:
        return None

    response = data.get("response", [])
    if not response:
        return None

    try:
        bookmaker = response[0].get("bookmakers", [{}])[0]
        bets = bookmaker.get("bets", [])

        result: dict = {}
        for bet in bets:
            bet_name = bet.get("name", "").lower()
            values = {v["value"]: v.get("odd") for v in bet.get("values", [])}

            if "money line" in bet_name or "moneyline" in bet_name:
                result["moneyline_home"] = values.get("Home")
                result["moneyline_away"] = values.get("Away")
            elif "spread" in bet_name or "handicap" in bet_name:
                result["spread"] = values
            elif "total" in bet_name or "over/under" in bet_name:
                result["total"] = values

        return result if result else None

    except (IndexError, KeyError, TypeError):
        logger.warning("get_odds_bdl(%s, %d): formato de odds inesperado", sport, game_id)
        return None
