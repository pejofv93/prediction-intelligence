"""
Collector: AllSportsApi via RapidAPI.
host: allsportsapi2.p.rapidapi.com
Cubre: Nations League, WC Qualifiers Europa, Liga Argentina, Copa Sudamericana, Copa América.
Escribe en Firestore: upcoming_matches (mismo schema que football_api.py).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import FOOTBALL_RAPID_API_KEY, ALLSPORTS_FOOTBALL_LEAGUES, ALLSPORTS_LEAGUE_NAMES

logger = logging.getLogger(__name__)

COLLECTOR_DISABLED = True  # endpoint muerto: /football/ → 404 "Endpoint does not exist"

_HOST = "allsportsapi2.p.rapidapi.com"
_BASE = f"https://{_HOST}"
_HTTP_TIMEOUT = 20.0
_DELAY = 2.0


async def _request(path: str, params: dict | None = None) -> dict | None:
    if not FOOTBALL_RAPID_API_KEY:
        logger.warning("allsports_client: FOOTBALL_RAPID_API_KEY no configurada")
        return None

    await asyncio.sleep(_DELAY)
    headers = {"X-RapidAPI-Key": FOOTBALL_RAPID_API_KEY, "X-RapidAPI-Host": _HOST}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_BASE}{path}", headers=headers, params=params)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            logger.warning("allsports_client: rate limit — esperando %ds", wait)
            await asyncio.sleep(wait)
            return None
        if resp.status_code >= 400:
            logger.error("allsports_client: %s → HTTP %d %.150s", path, resp.status_code, resp.text)
            return None
        return resp.json()
    except Exception:
        logger.error("allsports_client: error en %s", path, exc_info=True)
        return None


def _parse_fixture(raw: dict, league_code: str) -> dict | None:
    try:
        fixture = raw.get("fixture", raw)
        teams = raw.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})

        # AllSportsApi puede devolver IDs como enteros o strings
        home_id = home.get("id") or raw.get("homeTeamId") or raw.get("home_team_id")
        away_id = away.get("id") or raw.get("awayTeamId") or raw.get("away_team_id")
        home_name = home.get("name", raw.get("homeTeamName", ""))
        away_name = away.get("name", raw.get("awayTeamName", ""))

        match_id = str(fixture.get("id", raw.get("id", raw.get("fixture_id", ""))))
        if not match_id:
            return None

        date_raw = (fixture.get("date") or raw.get("date") or raw.get("event_date") or "")
        try:
            if date_raw:
                match_date = datetime.fromisoformat(str(date_raw).replace("Z", "+00:00"))
            else:
                match_date = datetime.now(timezone.utc) + timedelta(days=1)
        except Exception:
            match_date = datetime.now(timezone.utc) + timedelta(days=1)

        status = raw.get("fixture", {}).get("status", {}).get("short", raw.get("status", "NS"))
        if hasattr(status, "get"):
            status = status.get("short", "NS")

        score = raw.get("score", {}).get("fulltime", {})
        goals_home = score.get("home") if isinstance(score, dict) else None
        goals_away = score.get("away") if isinstance(score, dict) else None

        return {
            "match_id": f"allsports_{match_id}",
            "date": match_date.isoformat(),
            "home_team_id": int(home_id) if home_id else None,
            "away_team_id": int(away_id) if away_id else None,
            "home_team": home_name,
            "away_team": away_name,
            "home_team_name": home_name,
            "away_team_name": away_name,
            "goals_home": goals_home,
            "goals_away": goals_away,
            "league": ALLSPORTS_LEAGUE_NAMES.get(league_code, league_code),
            "status": str(status) if status else "SCHEDULED",
            "sport": "football",
            "source": "allsports",
        }
    except Exception:
        logger.error("allsports_client: error parseando fixture", exc_info=True)
        return None


async def get_upcoming_matches(days: int = 7) -> list[dict]:
    """
    Obtiene partidos próximos de todas las ligas en ALLSPORTS_FOOTBALL_LEAGUES.
    Devuelve lista normalizada compatible con firestore_writer.save_upcoming_matches.
    """
    if COLLECTOR_DISABLED:
        logger.info("allsports_client: colector desactivado — endpoint muerto")
        return []
    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + timedelta(days=days)).strftime("%Y-%m-%d")

    all_matches: list[dict] = []

    for league_code, league_id in ALLSPORTS_FOOTBALL_LEAGUES.items():
        logger.info("allsports_client: obteniendo %s (id=%d)", league_code, league_id)

        # Intentar endpoint estándar AllSportsApi
        data = await _request("/football/fixtures", params={
            "leagueId": league_id,
            "from": date_from,
            "to": date_to,
        })

        if data is None:
            # Fallback: endpoint alternativo
            data = await _request("/football/", params={
                "leagueId": league_id,
                "from": date_from,
                "to": date_to,
            })

        if data is None:
            logger.warning("allsports_client: sin datos para %s", league_code)
            continue

        # AllSportsApi puede devolver lista directa o objeto con key "response"
        fixtures = data if isinstance(data, list) else data.get("response", data.get("result", []))
        if not isinstance(fixtures, list):
            logger.warning("allsports_client: formato inesperado para %s: %s", league_code, type(fixtures))
            continue

        for raw in fixtures:
            parsed = _parse_fixture(raw, league_code)
            if parsed:
                all_matches.append(parsed)

        logger.info("allsports_client: %s → %d partidos", league_code, len(fixtures))

    logger.info("allsports_client: %d partidos totales en %d ligas", len(all_matches), len(ALLSPORTS_FOOTBALL_LEAGUES))
    return all_matches
