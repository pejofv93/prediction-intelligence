"""
Sistema ELO dinamico adaptado al futbol.
Lee y escribe en Firestore coleccion team_elo.
"""
import logging

logger = logging.getLogger(__name__)

K_FACTOR = 32        # sensibilidad del sistema ELO a resultados
HOME_ADVANTAGE = 100  # puntos ELO extra para equipo local
DEFAULT_ELO = 1500


def expected_score(elo_a: float, elo_b: float) -> float:
    """Probabilidad esperada de victoria de A contra B segun ELO."""
    # TODO: implementar en Sesion 3
    raise NotImplementedError


def update_elo(
    elo_winner: float, elo_loser: float, score: float
) -> tuple[float, float]:
    """
    score: 1.0=victoria, 0.5=empate, 0.0=derrota del 'winner'.
    Devuelve (nuevo_elo_a, nuevo_elo_b).
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError


def get_team_elo(team_id: int) -> float:
    """Lee ELO actual de Firestore coleccion team_elo. Si no existe, devuelve DEFAULT_ELO."""
    # TODO: implementar en Sesion 3
    raise NotImplementedError


async def update_all_elos(finished_matches: list[dict]) -> None:
    """
    Procesa partidos terminados en orden cronologico.
    Actualiza Firestore team_elo por cada equipo.
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError


def elo_win_probability(home_id: int, away_id: int) -> float:
    """
    Devuelve prob de victoria local incluyendo HOME_ADVANTAGE.
    Resultado en [0.0, 1.0].
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError
