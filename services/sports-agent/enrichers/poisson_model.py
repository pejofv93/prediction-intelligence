"""
Modelo Poisson bivariado con correccion Dixon-Coles.
Usado exclusivamente para futbol (PL, PD, BL1, SA).
"""
import logging

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson

from shared.config import MIN_MATCHES_TO_FIT

logger = logging.getLogger(__name__)

# Parametros de arranque en frio (cold start)
# Se sobreescriben con datos reales cuando hay suficientes partidos.
COLD_START_PARAMS = {
    "home_advantage": 0.25,    # ventaja media de jugar en casa
    "default_attack": 1.2,     # goles esperados de ataque medio
    "default_defense": 1.0,    # goles esperados contra defensa media
}


def fit_attack_defense(matches: list[dict]) -> dict:
    """
    Ajusta parametros de ataque y defensa por equipo usando maxima verosimilitud.
    matches: ultimos partidos disponibles (max 10 con free tier) con goals_home, goals_away.
    Si un equipo tiene < MIN_MATCHES_TO_FIT partidos → usa COLD_START_PARAMS.
    Devuelve {team_id: {"attack": float, "defense": float}} + home_advantage global.
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError


def dixon_coles_correction(
    lambda_home: float, mu_away: float, rho: float = -0.13
) -> np.ndarray:
    """
    Correccion Dixon-Coles para scores bajos (0-0, 1-0, 0-1, 1-1).
    rho=-0.13 es el valor estandar empirico.
    Devuelve matriz de correccion 2x2.
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError


def predict_match_probs(
    home_id: int, away_id: int, team_params: dict
) -> dict:
    """
    Calcula distribucion bivariada de marcadores hasta 8 goles por equipo.
    Aplica correccion Dixon-Coles.
    Devuelve:
    {
      "home_win": float,
      "draw": float,
      "away_win": float,
      "home_xg": float,
      "away_xg": float,
      "score_matrix": list  # matriz 9x9 de probabilidades por marcador
    }
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError
