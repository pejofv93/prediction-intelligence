"""
Procesado y enriquecimiento de datos crudos de estadisticas.
xG proxy calculado desde datos de football-data.org (understat descartado).
"""
import logging

logger = logging.getLogger(__name__)


def calculate_form_score(results: list[str]) -> float:
    """
    results: lista de "W","D","L" mas reciente primero.
    Ponderacion decreciente: posicion 0 vale 1.0, posicion N vale 1/(N+1).
    W=3pts, D=1pt, L=0pts. Normaliza a 0-100.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


def calculate_home_away_split(
    matches: list[dict], team_id: int
) -> tuple[dict, dict]:
    """Separa stats de local vs visitante. Devuelve (home_stats, away_stats)."""
    # TODO: implementar en Sesion 2
    raise NotImplementedError


def detect_streak(results: list[str]) -> dict:
    """
    results: mas reciente primero.
    Devuelve {"type": "win"|"loss"|"draw", "count": N}
    donde N es la longitud de la racha actual desde el partido mas reciente.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


def calculate_h2h_advantage(h2h_matches: list[dict], team_id: int) -> float:
    """
    Retorna float en [-1.0, 1.0].
    1.0 = equipo gano todos. -1.0 = equipo perdio todos. 0.0 = equilibrio.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError


def calculate_xg_proxy(team_matches: list[dict]) -> float:
    """
    xG aproximado desde datos de football-data.org:
    xg_proxy = shots_on_target / shots_total x goals_scored  (si shots disponibles)
    xg_proxy = goals_scored / matches_played                 (si no hay shots)
    Precision ~70% vs xG real — suficiente para el modelo Poisson.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError
