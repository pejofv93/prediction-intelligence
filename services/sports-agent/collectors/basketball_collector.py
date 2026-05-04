"""
Collector de baloncesto — wrapper sobre api_sports_client.py.
Añade H2H y enriquecimiento de stats para el basketball_analyzer.

Ligas soportadas (api-basketball.p.rapidapi.com):
  NBA        — league_id=12   (Playoffs mayo 2026)
  ACB        — league_id=116  (Liga Endesa, España)
  EUROLEAGUE — league_id=120  (Turkish Airlines EuroLeague, Final Four mayo)
  NCAA       — league_id=51   (Nov-Marzo — offseason)

Nota: api-basketball requiere SIEMPRE date + league + season.
  Sin season devuelve []. /leagues?search= → 403 en plan free.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from collectors.api_sports_client import (
    get_games_by_league,
    get_nba_games_espn,
    get_nba_team_stats_espn,
    get_team_stats_bdl,
)
from collectors.stats_processor import (
    build_results_list,
    calculate_form_score,
    detect_streak,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Ligas via api-basketball.p.rapidapi.com (requiere suscripción activa en RapidAPI)
# NBA se recopila via ESPN (gratuito) — no necesita suscripción
# ACB/EUROLEAGUE requieren suscripción activa a api-basketball
_LEAGUES_BY_ID: dict[str, int] = {
    "ACB":        116,   # Liga Endesa — España
    "EUROLEAGUE": 120,   # Turkish Airlines EuroLeague Final Four (mayo)
    # "NCAA":       51,  # Nov-Marzo — offseason en mayo
}


async def collect_basketball_games(days: int = 1) -> list[dict]:
    """
    Recopila partidos de baloncesto de hoy.

    NBA: ESPN public scoreboard API (gratuito, sin clave, siempre disponible).
    ACB/EUROLEAGUE: api-basketball.p.rapidapi.com (requiere suscripción activa).
      → Si devuelve 403, se omiten silenciosamente.
    """
    all_games: list[dict] = []

    # --- NBA via ESPN (sin key, sin suscripción) ---
    try:
        nba_games = await get_nba_games_espn()
        if nba_games:
            logger.info("basketball NBA (ESPN): %d partidos obtenidos", len(nba_games))
        else:
            logger.info("basketball NBA (ESPN): sin partidos hoy (offseason o no programados)")
        all_games.extend(nba_games)
    except Exception:
        logger.error("basketball_collector: error colectando NBA via ESPN", exc_info=True)

    # --- ACB y EUROLEAGUE via api-basketball (suscripción requerida) ---
    if os.environ.get("FOOTBALL_RAPID_API_KEY"):
        for league_name, league_id in _LEAGUES_BY_ID.items():
            try:
                games = await get_games_by_league(league_id)
                for g in games:
                    g.setdefault("sport", "basketball")
                    g.setdefault("league", league_name)
                if games:
                    logger.info("basketball %s (id=%d): %d partidos obtenidos",
                                league_name, league_id, len(games))
                else:
                    logger.info("basketball %s (id=%d): sin partidos (offseason o 403 sin suscripción)",
                                league_name, league_id)
                all_games.extend(games)
            except Exception:
                logger.error("basketball_collector: error colectando %s (id=%d)",
                             league_name, league_id, exc_info=True)

    logger.info("basketball_collector: %d partidos totales de baloncesto", len(all_games))
    return all_games


async def collect_basketball_team_stats(games: list[dict]) -> None:
    """
    Para cada equipo en la lista de partidos, recopila sus últimos partidos
    y guarda team_stats enriquecido en Firestore.

    - Partidos ESPN (source='espn'): usa get_nba_team_stats_espn() — gratuito, sin clave.
    - Partidos api-basketball: usa get_team_stats_bdl() — requiere suscripción activa.
    """
    teams_seen: set[int] = set()

    for game in games:
        source = game.get("source", "")
        for team_id_key in ("home_team_id", "away_team_id"):
            team_id = game.get(team_id_key)
            if not team_id or team_id in teams_seen:
                continue

            teams_seen.add(team_id)
            try:
                if source == "espn":
                    # ESPN schedule → raw_matches ya en formato correcto
                    raw_matches_fmt = await get_nba_team_stats_espn(team_id)
                    if not raw_matches_fmt:
                        logger.debug("basketball_collector: ESPN sin partidos completados para team %d", team_id)
                        continue

                    # Form score desde raw_matches ESPN (was_home ya poblado)
                    results = [
                        {"result": "win" if (m["goals_home"] > m["goals_away"] and m["was_home"])
                                        or (m["goals_away"] > m["goals_home"] and not m["was_home"])
                                  else "loss"}
                        for m in raw_matches_fmt
                    ]
                    form_score = calculate_form_score(results[:10])
                    streak = detect_streak(results[:10])

                    # Nombre del equipo desde el game actual
                    if team_id == game.get("home_team_id"):
                        team_name = game.get("home_team_name", f"Team_{team_id}")
                    else:
                        team_name = game.get("away_team_name", f"Team_{team_id}")

                    doc = {
                        "team_id": team_id,
                        "team_name": team_name,
                        "league": game.get("league", "NBA"),
                        "sport": "nba",
                        "last_10": results[:10],
                        "form_score": form_score,
                        "streak": streak,
                        "raw_matches": raw_matches_fmt[:10],
                        "xg_per_game": 0.0,
                        "source": "espn",
                        "updated_at": datetime.now(timezone.utc),
                    }
                else:
                    sport = game.get("sport", "nba")
                    raw = await get_team_stats_bdl(sport, team_id, last_n=10)
                    if not raw:
                        logger.debug("basketball_collector: sin stats para team %d", team_id)
                        continue

                    results = build_results_list(raw, team_id)
                    form_score = calculate_form_score(results[:10])
                    streak = detect_streak(results[:10])

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
                            "goals_home": m.get("goals_home") or 0,
                            "goals_away": m.get("goals_away") or 0,
                            "home_team_id": m["home_team_id"],
                            "was_home": m["home_team_id"] == team_id,
                            "match_date": m.get("date", ""),
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
                        "xg_per_game": 0.0,
                        "updated_at": datetime.now(timezone.utc),
                    }

                col("team_stats").document(f"bball_{team_id}").set(doc)
                logger.info(
                    "basketball_collector: team_stats(%d) %s form=%.1f src=%s partidos=%d",
                    team_id, doc["team_name"], form_score, source or "api", len(raw_matches_fmt),
                )

            except Exception:
                logger.error("basketball_collector: error stats team %d", team_id, exc_info=True)
