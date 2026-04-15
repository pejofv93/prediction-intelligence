"""
Escritor Firestore para collectors.
OBLIGATORIO: save_team_stats debe guardar raw_matches para que Poisson funcione.
"""
import logging

logger = logging.getLogger(__name__)


async def save_upcoming_matches(matches: list[dict]) -> None:
    """Guarda lista de upcoming_matches en Firestore. Doc ID = match_id."""
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def save_team_stats(team_id: int, raw_api_matches: list[dict]) -> None:
    """
    Procesa raw_api_matches y guarda en team_stats.
    Calcula: last_10, form_score, home_stats, away_stats, streak, xg_per_game.
    IMPRESCINDIBLE: raw_matches = [{match_id, date, home_team_id, away_team_id,
                                    goals_home, goals_away, was_home}]
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


async def save_h2h(
    team1_id: int, team2_id: int, h2h_matches: list[dict]
) -> None:
    """
    Guarda h2h_data. pair_key = f"{min(t1,t2)}_{max(t1,t2)}".
    h2h_advantage desde perspectiva del equipo con menor ID (= team1 canonico).
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError
