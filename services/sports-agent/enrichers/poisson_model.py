"""
Modelo Poisson bivariado con correccion Dixon-Coles.
Usado exclusivamente para futbol (PL, PD, BL1, SA).

Parametrizacion:
  lambda_home = attack_home * defense_away * (1 + home_advantage)
  mu_away     = attack_away * defense_home

fit_attack_defense ajusta ataque y defensa por equipo via maxima verosimilitud.
predict_match_probs construye la matriz de marcadores 9x9 y calcula outcome probs.
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
    "home_advantage": 0.25,    # ventaja media de jugar en casa (multiplicador - 1)
    "default_attack": 1.2,     # goles esperados de ataque medio
    "default_defense": 1.0,    # goles esperados contra defensa media
}


def fit_attack_defense(matches: list[dict]) -> dict:
    """
    Ajusta parametros de ataque y defensa por equipo usando maxima verosimilitud.
    matches: lista de partidos con home_team_id, away_team_id, goals_home, goals_away.
    Si un equipo tiene < MIN_MATCHES_TO_FIT partidos → usa COLD_START_PARAMS.
    Devuelve {team_id: {"attack": float, "defense": float}, "home_advantage": float}.
    """
    if not matches:
        logger.warning("fit_attack_defense: lista de partidos vacia — usando cold start")
        return {"home_advantage": COLD_START_PARAMS["home_advantage"]}

    # Extraer equipos unicos y contar partidos por equipo
    team_match_count: dict[int, int] = {}
    for m in matches:
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        if home_id is not None:
            team_match_count[home_id] = team_match_count.get(home_id, 0) + 1
        if away_id is not None:
            team_match_count[away_id] = team_match_count.get(away_id, 0) + 1

    if not team_match_count:
        return {"home_advantage": COLD_START_PARAMS["home_advantage"]}

    teams = sorted(team_match_count.keys())
    n = len(teams)
    team_idx = {t: i for i, t in enumerate(teams)}

    # Partidos validos (con goles definidos)
    valid_matches = [
        m for m in matches
        if m.get("goals_home") is not None and m.get("goals_away") is not None
        and m.get("home_team_id") in team_idx and m.get("away_team_id") in team_idx
    ]

    if len(valid_matches) < 2:
        logger.warning(
            "fit_attack_defense: solo %d partidos validos — usando cold start",
            len(valid_matches),
        )
        result: dict = {"home_advantage": COLD_START_PARAMS["home_advantage"]}
        for t in teams:
            result[t] = {
                "attack": COLD_START_PARAMS["default_attack"],
                "defense": COLD_START_PARAMS["default_defense"],
            }
        return result

    # Parametrizacion en log-espacio para garantizar positividad:
    # params = [log(attack_0), ..., log(attack_n-1),
    #           log(defense_0), ..., log(defense_n-1),
    #           log(1 + home_advantage)]
    x0 = np.zeros(2 * n + 1)
    x0[:n] = np.log(COLD_START_PARAMS["default_attack"])
    x0[n:2*n] = np.log(COLD_START_PARAMS["default_defense"])
    x0[-1] = np.log(1.0 + COLD_START_PARAMS["home_advantage"])

    def neg_log_likelihood(params: np.ndarray) -> float:
        attacks = np.exp(params[:n])
        defenses = np.exp(params[n:2*n])
        home_mult = np.exp(params[-1])  # = 1 + home_advantage

        ll = 0.0
        for m in valid_matches:
            hi = team_idx[m["home_team_id"]]
            ai = team_idx[m["away_team_id"]]
            gh = int(m["goals_home"])
            ga = int(m["goals_away"])

            lambda_h = attacks[hi] * defenses[ai] * home_mult
            mu_a = attacks[ai] * defenses[hi]

            # Evitar log(0) en pmf
            lambda_h = max(lambda_h, 1e-6)
            mu_a = max(mu_a, 1e-6)

            ll += poisson.logpmf(gh, lambda_h) + poisson.logpmf(ga, mu_a)

        return -ll

    try:
        result_opt = minimize(
            neg_log_likelihood,
            x0,
            method="Nelder-Mead",
            options={"maxiter": 2000, "xatol": 1e-5, "fatol": 1e-5},
        )
        opt_params = result_opt.x
        attacks_fit = np.exp(opt_params[:n])
        defenses_fit = np.exp(opt_params[n:2*n])
        home_adv_fit = float(np.exp(opt_params[-1])) - 1.0  # convertir a forma aditiva

        logger.debug(
            "fit_attack_defense: optimizacion completada (exito=%s, iter=%d)",
            result_opt.success, result_opt.nit,
        )

    except Exception:
        logger.error("fit_attack_defense: error en optimizacion — usando cold start", exc_info=True)
        out: dict = {"home_advantage": COLD_START_PARAMS["home_advantage"]}
        for t in teams:
            out[t] = {
                "attack": COLD_START_PARAMS["default_attack"],
                "defense": COLD_START_PARAMS["default_defense"],
            }
        return out

    # Construir resultado — equipos con pocos partidos usan cold start
    fitted: dict = {"home_advantage": max(0.0, home_adv_fit)}
    for t in teams:
        i = team_idx[t]
        if team_match_count[t] < MIN_MATCHES_TO_FIT:
            fitted[t] = {
                "attack": COLD_START_PARAMS["default_attack"],
                "defense": COLD_START_PARAMS["default_defense"],
            }
        else:
            fitted[t] = {
                "attack": float(max(attacks_fit[i], 0.1)),
                "defense": float(max(defenses_fit[i], 0.1)),
            }

    return fitted


def dixon_coles_correction(
    lambda_home: float, mu_away: float, rho: float = -0.13
) -> np.ndarray:
    """
    Correccion Dixon-Coles para scores bajos (0-0, 1-0, 0-1, 1-1).
    rho=-0.13 es el valor estandar empirico (correlacion negativa entre goles).
    Devuelve matriz de correccion 2x2: correction[home_goals, away_goals].
    """
    correction = np.ones((2, 2))
    correction[0, 0] = 1.0 - lambda_home * mu_away * rho
    correction[1, 0] = 1.0 + mu_away * rho
    correction[0, 1] = 1.0 + lambda_home * rho
    correction[1, 1] = 1.0 - rho
    # Clampear a valores positivos (rho extremo podria hacer correcciones negativas)
    correction = np.maximum(correction, 0.0)
    return correction


def predict_match_probs(
    home_id: int, away_id: int, team_params: dict
) -> dict:
    """
    Calcula distribucion bivariada de marcadores hasta 8 goles por equipo.
    Aplica correccion Dixon-Coles a scores bajos (0-1 x 0-1).
    Devuelve:
    {
      "home_win": float,
      "draw": float,
      "away_win": float,
      "home_xg": float,    # goles esperados local (lambda)
      "away_xg": float,    # goles esperados visitante (mu)
      "score_matrix": list # matriz 9x9 de probabilidades por marcador
    }
    """
    MAX_GOALS = 8

    home_p = team_params.get(home_id, {})
    away_p = team_params.get(away_id, {})
    home_adv = team_params.get("home_advantage", COLD_START_PARAMS["home_advantage"])

    attack_home = home_p.get("attack", COLD_START_PARAMS["default_attack"])
    defense_home = home_p.get("defense", COLD_START_PARAMS["default_defense"])
    attack_away = away_p.get("attack", COLD_START_PARAMS["default_attack"])
    defense_away = away_p.get("defense", COLD_START_PARAMS["default_defense"])

    # Goles esperados — lambda para local, mu para visitante
    lambda_home = float(attack_home * defense_away * (1.0 + home_adv))
    mu_away = float(attack_away * defense_home)

    lambda_home = max(lambda_home, 0.1)
    mu_away = max(mu_away, 0.1)

    # Correccion Dixon-Coles (solo para scores 0-1 x 0-1)
    correction = dixon_coles_correction(lambda_home, mu_away)

    # Construir matriz de probabilidades 9x9
    score_matrix = np.zeros((MAX_GOALS + 1, MAX_GOALS + 1))
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = float(poisson.pmf(i, lambda_home)) * float(poisson.pmf(j, mu_away))
            if i <= 1 and j <= 1:
                p *= correction[i, j]
            score_matrix[i, j] = p

    # Normalizar (la correccion DC puede desplazar la suma total ligeramente)
    total = score_matrix.sum()
    if total > 1e-10:
        score_matrix /= total

    # Probabilidades de resultado:
    # home_win: home_goals > away_goals → por debajo de la diagonal principal (k=-1)
    # draw:     home_goals == away_goals → diagonal
    # away_win: away_goals > home_goals → por encima de la diagonal (k=+1)
    home_win = float(np.sum(np.tril(score_matrix, k=-1)))
    draw = float(np.trace(score_matrix))
    away_win = float(np.sum(np.triu(score_matrix, k=1)))

    logger.debug(
        "predict_match_probs(%d vs %d): H=%.3f D=%.3f A=%.3f | λ=%.2f μ=%.2f",
        home_id, away_id, home_win, draw, away_win, lambda_home, mu_away,
    )

    return {
        "home_win": round(home_win, 4),
        "draw": round(draw, 4),
        "away_win": round(away_win, 4),
        "home_xg": round(lambda_home, 3),
        "away_xg": round(mu_away, 3),
        "score_matrix": score_matrix.tolist(),
    }
