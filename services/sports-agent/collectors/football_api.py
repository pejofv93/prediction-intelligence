"""
Collector: football-data.org
Deportes: futbol europeo (PL, PD, BL1, SA)
Rate limit: 10 req/min → RATE_LIMIT_DELAY = 6.5s entre requests.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import FOOTBALL_API_KEY, SUPPORTED_FOOTBALL_LEAGUES

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"
RATE_LIMIT_DELAY = 6.5  # segundos entre requests (10 req/min = 1/6s, con margen)

# NO definir HEADERS a nivel de modulo — FOOTBALL_API_KEY puede ser None al importar.
# Construir el header dentro de cada funcion async.


async def _request(path: str) -> dict | None:
    """
    Llamada autenticada a football-data.org con rate limiting y manejo de errores.
    Devuelve JSON parseado o None si falla.
    """
    if not FOOTBALL_API_KEY:
        raise RuntimeError("FOOTBALL_API_KEY no configurada para este servicio")

    await asyncio.sleep(RATE_LIMIT_DELAY)

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    url = f"{BASE_URL}{path}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                logger.warning("Rate limit football-data.org — esperando %ds", wait)
                await asyncio.sleep(wait)
                resp = await client.get(url, headers=headers)

            if resp.status_code >= 400:
                logger.error(
                    "football-data.org %s → HTTP %d: %.200s",
                    path, resp.status_code, resp.text,
                )
                return None

            return resp.json()

    except httpx.TimeoutException:
        logger.error("Timeout en football-data.org %s", path, exc_info=True)
        return None
    except Exception:
        logger.error("Error en football-data.org %s", path, exc_info=True)
        return None


def _parse_match(m: dict) -> dict | None:
    """
    Normaliza un objeto match de football-data.org al formato interno estándar.
    Devuelve None si faltan campos obligatorios.
    """
    try:
        score = m.get("score", {}).get("fullTime", {})
        return {
            "match_id": str(m["id"]),
            "date": m["utcDate"],
            "home_team_id": m["homeTeam"]["id"],
            "away_team_id": m["awayTeam"]["id"],
            "home_team_name": m["homeTeam"].get("name", ""),
            "away_team_name": m["awayTeam"].get("name", ""),
            "goals_home": score.get("home"),    # None si partido no terminado
            "goals_away": score.get("away"),
            "league": m.get("competition", {}).get("code", ""),
            "status": m.get("status", "SCHEDULED"),
            # Shots — disponibles según plan; None en free tier
            "shots_home": None,
            "shots_away": None,
            "shots_on_target_home": None,
            "shots_on_target_away": None,
        }
    except KeyError as e:
        logger.warning("Campo faltante en match de football-data.org: %s", e)
        return None


async def get_upcoming_matches(days: int = 7) -> list[dict]:
    """GET /matches?dateFrom=today&dateTo=today+days. Filtra por SUPPORTED_LEAGUES."""
    today = datetime.now(timezone.utc).date()
    date_to = today + timedelta(days=days)
    leagues = ",".join(SUPPORTED_FOOTBALL_LEAGUES.keys())

    data = await _request(
        f"/matches?dateFrom={today}&dateTo={date_to}&competitions={leagues}"
    )
    if not data:
        return []

    matches = []
    for m in data.get("matches", []):
        parsed = _parse_match(m)
        if parsed:
            matches.append(parsed)

    logger.info("get_upcoming_matches: %d partidos en los proximos %d dias", len(matches), days)
    return matches


async def get_team_stats(team_id: int, last_n: int = 10) -> list[dict]:
    """GET /teams/{team_id}/matches?status=FINISHED&limit={last_n}"""
    data = await _request(
        f"/teams/{team_id}/matches?status=FINISHED&limit={last_n}"
    )
    if not data:
        return []

    matches = []
    for m in data.get("matches", []):
        parsed = _parse_match(m)
        # Solo incluir partidos con resultado completo
        if parsed and parsed["goals_home"] is not None and parsed["goals_away"] is not None:
            matches.append(parsed)

    logger.info("get_team_stats(%d): %d partidos terminados", team_id, len(matches))
    return matches


async def get_h2h(team1_id: int, team2_id: int) -> list[dict]:
    """
    GET /teams/{team1_id}/matches?status=FINISHED&limit=10. Filtra vs team2_id.
    Free tier: max 10 partidos. No hay endpoint H2H directo — filtrado manual.
    """
    data = await _request(
        f"/teams/{team1_id}/matches?status=FINISHED&limit=10"
    )
    if not data:
        return []

    h2h = []
    for m in data.get("matches", []):
        home_id = m.get("homeTeam", {}).get("id")
        away_id = m.get("awayTeam", {}).get("id")
        if team2_id in (home_id, away_id):
            parsed = _parse_match(m)
            if parsed and parsed["goals_home"] is not None:
                h2h.append(parsed)

    logger.info("get_h2h(%d, %d): %d partidos directos", team1_id, team2_id, len(h2h))
    return h2h


async def get_standings(league_id: int) -> list[dict]:
    """GET /competitions/{league_id}/standings"""
    data = await _request(f"/competitions/{league_id}/standings")
    if not data:
        return []

    standings = []
    for group in data.get("standings", []):
        if group.get("type") != "TOTAL":
            continue
        for entry in group.get("table", []):
            try:
                standings.append({
                    "team_id": entry["team"]["id"],
                    "team_name": entry["team"]["name"],
                    "position": entry["position"],
                    "played": entry["playedGames"],
                    "won": entry["won"],
                    "draw": entry["draw"],
                    "lost": entry["lost"],
                    "points": entry["points"],
                    "goals_for": entry["goalsFor"],
                    "goals_against": entry["goalsAgainst"],
                })
            except KeyError as e:
                logger.warning("Campo faltante en standings: %s", e)

    logger.info("get_standings(%d): %d equipos", league_id, len(standings))
    return standings


async def get_finished_matches(days_back: int = 30) -> list[dict]:
    """
    GET /matches?status=FINISHED en ventanas de 10 días (límite free plan).
    Itera desde ayer hacia atrás en chunks ≤10 días hasta cubrir days_back.
    Devuelve partidos terminados de todas las ligas soportadas.
    """
    today = datetime.now(timezone.utc).date()
    leagues = ",".join(SUPPORTED_FOOTBALL_LEAGUES.keys())

    all_matches: list[dict] = []
    seen_ids: set[str] = set()

    # Iterar en chunks de 10 días (límite API football-data.org free plan)
    chunk_size = 10
    cursor = today - timedelta(days=1)  # ayer
    remaining = days_back

    while remaining > 0:
        window = min(chunk_size, remaining)
        date_from = cursor - timedelta(days=window - 1)

        data = await _request(
            f"/matches?dateFrom={date_from}&dateTo={cursor}"
            f"&competitions={leagues}&status=FINISHED"
        )
        if data:
            for m in data.get("matches", []):
                parsed = _parse_match(m)
                if (
                    parsed
                    and parsed["goals_home"] is not None
                    and parsed["goals_away"] is not None
                    and parsed["match_id"] not in seen_ids
                ):
                    all_matches.append(parsed)
                    seen_ids.add(parsed["match_id"])

        cursor = date_from - timedelta(days=1)
        remaining -= window

    logger.info(
        "get_finished_matches: %d partidos terminados (últimos %d días)",
        len(all_matches), days_back,
    )
    return all_matches


async def get_match_result(match_id: str) -> dict | None:
    """
    GET /matches/{match_id}. Devuelve resultado si FINISHED, None si no.
    Usado por learning_engine para verificar resultados pendientes.
    """
    if not str(match_id).isdigit():
        logger.debug("get_match_result: ignorando match_id no entero (%s)", match_id)
        return None
    data = await _request(f"/matches/{match_id}")
    if not data:
        return None

    status = data.get("status")
    if status != "FINISHED":
        return None

    score = data.get("score", {}).get("fullTime", {})
    home_goals = score.get("home")
    away_goals = score.get("away")

    if home_goals is None or away_goals is None:
        logger.warning("get_match_result(%s): score nulo aunque status=FINISHED", match_id)
        return None

    if home_goals > away_goals:
        result = "HOME_WIN"
    elif away_goals > home_goals:
        result = "AWAY_WIN"
    else:
        result = "DRAW"

    return {
        "match_id": match_id,
        "status": status,
        "goals_home": home_goals,
        "goals_away": away_goals,
        "result": result,
    }
