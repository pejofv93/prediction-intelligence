"""
Collector de baloncesto — wrapper sobre api_sports_client.py.
Añade H2H y enriquecimiento de stats para el basketball_analyzer.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from collectors.api_sports_client import get_games_today, get_team_stats_bdl
from collectors.stats_processor import (
    build_results_list, calculate_form_score, detect_streak,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)


async def collect_basketball_games(days: int = 1) -> list[dict]:
    """
    Recopila partidos NBA y Euroleague de hoy.
    Devuelve lista normalizada compatible con save_upcoming_matches.
    """
    if not os.environ.get("FOOTBALL_RAPID_API_KEY"):
        logger.warning("basketball_collector: FOOTBALL_RAPID_API_KEY no configurada — omitiendo baloncesto")
        return []

    sports = ["nba", "euroleague"]
    all_games: list[dict] = []

    for sport in sports:
        games = await get_games_today(sport)
        for g in games:
            g.setdefault("sport", sport)
            g.setdefault("league", sport.upper())
        all_games.extend(games)

    logger.info("basketball_collector: %d partidos de baloncesto", len(all_games))
    return all_games


async def collect_basketball_team_stats(games: list[dict]) -> None:
    """
    Para cada equipo en la lista de partidos, recopila sus últimos 10 partidos
    y guarda team_stats enriquecido en Firestore.
    """
    teams_seen: set[int] = set()

    for game in games:
        for team_id_key, sport in [
            ("home_team_id", game.get("sport", "nba")),
            ("away_team_id", game.get("sport", "nba")),
        ]:
            team_id = game.get(team_id_key)
            if not team_id or team_id in teams_seen:
                continue

            teams_seen.add(team_id)
            try:
                raw = await get_team_stats_bdl(sport, team_id, last_n=10)
                if not raw:
                    logger.debug("basketball_collector: sin stats para team %d", team_id)
                    continue

                results = build_results_list(raw, team_id)
                form_score = calculate_form_score(results[:10])
                streak = detect_streak(results[:10])

                # Extraer nombre del equipo
                team_name = ""
                for m in raw:
                    if m.get("home_team_id") == team_id:
                        team_name = m.get("home_team_name", "")
                        break
                    elif m.get("away_team_id") == team_id:
                        team_name = m.get("away_team_name", "")
                        break

                raw_matches_fmt = [
                    {
                        "match_id": m["match_id"],
                        "date": m["date"],
                        "home_team_id": m["home_team_id"],
                        "away_team_id": m["away_team_id"],
                        "goals_home": m.get("goals_home") or 0,
                        "goals_away": m.get("goals_away") or 0,
                        "was_home": m["home_team_id"] == team_id,
                    }
                    for m in raw
                    if m.get("goals_home") is not None and m.get("goals_away") is not None
                ]

                doc = {
                    "team_id": team_id,
                    "team_name": team_name or f"Team_{team_id}",
                    "league": game.get("league", "NBA"),
                    "sport": sport,
                    "last_10": results[:10],
                    "form_score": form_score,
                    "streak": streak,
                    "raw_matches": raw_matches_fmt,
                    "xg_per_game": 0.0,   # no aplica baloncesto
                    "updated_at": datetime.now(timezone.utc),
                }

                col("team_stats").document(str(team_id)).set(doc)
                logger.info("basketball_collector: team_stats(%d) %s form=%.1f",
                            team_id, team_name, form_score)

            except Exception:
                logger.error("basketball_collector: error stats team %d", team_id, exc_info=True)
