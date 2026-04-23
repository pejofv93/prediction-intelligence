"""
Standalone lineup and injury fetcher for API-Football.
Endpoints used:
  GET /injuries?fixture={id}
  GET /fixtures/lineups?fixture={id}
"""
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://v3.football.api-sports.io"
_TIMEOUT = 10.0


async def fetch_injuries(fixture_id: int, api_key: str) -> list[dict]:
    """
    GET /injuries?fixture={id}.
    Devuelve lista de {player_name, team_name, reason, type}.
    Si falla: devuelve [].
    """
    if not api_key or not fixture_id:
        return []
    url = f"{_API_BASE}/injuries"
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": "v3.football.api-sports.io",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params={"fixture": fixture_id})
            resp.raise_for_status()
            data = resp.json()
            results: list[dict] = data.get("response", [])
            simplified: list[dict] = []
            for item in results:
                player_info = item.get("player") or {}
                team_info = item.get("team") or {}
                simplified.append({
                    "player_name": player_info.get("name", ""),
                    "team_name": team_info.get("name", ""),
                    "reason": item.get("reason", ""),
                    "type": item.get("type", ""),
                })
            logger.debug("lineup_checker.fetch_injuries: fixture=%d → %d lesiones", fixture_id, len(simplified))
            return simplified
    except Exception as e:
        logger.warning("lineup_checker.fetch_injuries: fixture=%d error — %s", fixture_id, e)
        return []


async def fetch_lineups(fixture_id: int, api_key: str) -> dict:
    """
    GET /fixtures/lineups?fixture={id}.
    Devuelve {home_team, away_team, home_xi, away_xi} con nombres de titulares.
    Si falla: devuelve {}.
    """
    if not api_key or not fixture_id:
        return {}
    url = f"{_API_BASE}/fixtures/lineups"
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": "v3.football.api-sports.io",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params={"fixture": fixture_id})
            resp.raise_for_status()
            data = resp.json()
            results: list[dict] = data.get("response", [])
            if len(results) < 1:
                return {}

            def _extract_xi(team_data: dict) -> list[str]:
                names: list[str] = []
                for row in team_data.get("startXI") or []:
                    p = row.get("player") or {}
                    name = p.get("name") or p.get("fullName", "")
                    if name:
                        names.append(name)
                return names

            home_data = results[0] if results else {}
            away_data = results[1] if len(results) > 1 else {}

            home_team_info = home_data.get("team") or {}
            away_team_info = away_data.get("team") or {}

            result = {
                "home_team": home_team_info.get("name", ""),
                "away_team": away_team_info.get("name", ""),
                "home_xi": _extract_xi(home_data),
                "away_xi": _extract_xi(away_data),
            }
            logger.debug(
                "lineup_checker.fetch_lineups: fixture=%d → home=%d xi, away=%d xi",
                fixture_id, len(result["home_xi"]), len(result["away_xi"]),
            )
            return result
    except Exception as e:
        logger.warning("lineup_checker.fetch_lineups: fixture=%d error — %s", fixture_id, e)
        return {}
