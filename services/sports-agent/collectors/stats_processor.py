"""
Procesado y enriquecimiento de datos crudos de estadisticas.
Funciones puras — sin I/O ni Firestore.
xG proxy calculado desde datos de football-data.org (understat descartado).
"""
import logging

logger = logging.getLogger(__name__)


def calculate_form_score(results: list[str]) -> float:
    """
    results: lista de "W","D","L" mas reciente primero.
    Ponderacion decreciente: posicion 0 vale 1.0, posicion N vale 1/(N+1).
    W=3pts, D=1pt, L=0pts. Normaliza a 0-100.
    Devuelve 50.0 si la lista esta vacia (valor neutral).
    """
    if not results:
        return 50.0

    POINTS = {"W": 3, "D": 1, "L": 0}
    weighted_actual = 0.0
    weighted_max = 0.0

    for i, result in enumerate(results):
        weight = 1.0 / (i + 1)
        weighted_actual += POINTS.get(result.upper(), 0) * weight
        weighted_max += 3 * weight

    if weighted_max == 0:
        return 50.0

    return (weighted_actual / weighted_max) * 100.0


def calculate_home_away_split(
    matches: list[dict], team_id: int
) -> tuple[dict, dict]:
    """
    Separa stats de local vs visitante.
    Devuelve (home_stats, away_stats) con keys:
      played, won, drawn, lost, goals_for, goals_against.
    """
    def _empty() -> dict:
        return {"played": 0, "won": 0, "drawn": 0, "lost": 0,
                "goals_for": 0, "goals_against": 0}

    home_stats = _empty()
    away_stats = _empty()

    for m in matches:
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        gh = m.get("goals_home") or 0
        ga = m.get("goals_away") or 0

        if home_id == team_id:
            stats = home_stats
            gf, gc = gh, ga
        elif away_id == team_id:
            stats = away_stats
            gf, gc = ga, gh
        else:
            logger.debug("calculate_home_away_split: equipo %d no encontrado en match", team_id)
            continue

        stats["played"] += 1
        stats["goals_for"] += gf
        stats["goals_against"] += gc

        if gf > gc:
            stats["won"] += 1
        elif gf == gc:
            stats["drawn"] += 1
        else:
            stats["lost"] += 1

    return home_stats, away_stats


def detect_streak(results: list[str]) -> dict:
    """
    results: mas reciente primero (["W","W","D","L",...]).
    Devuelve {"type": "win"|"loss"|"draw", "count": N}
    donde N es la longitud de la racha actual desde el partido mas reciente.
    Devuelve {"type": "draw", "count": 0} si la lista esta vacia.
    """
    if not results:
        return {"type": "draw", "count": 0}

    TYPE_MAP = {"W": "win", "L": "loss", "D": "draw"}
    first = results[0].upper()
    streak_type = TYPE_MAP.get(first, "draw")
    count = 0

    for r in results:
        if r.upper() == first:
            count += 1
        else:
            break

    return {"type": streak_type, "count": count}


def calculate_h2h_advantage(
    h2h_matches: list[dict], team_id: int
) -> float:
    """
    Retorna float en [-1.0, 1.0].
    1.0 = equipo gano todos. -1.0 = equipo perdio todos. 0.0 = equilibrio.
    Formula: (wins - losses) / total_matches.
    """
    if not h2h_matches:
        return 0.0

    wins = losses = draws = 0

    for m in h2h_matches:
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        gh = m.get("goals_home") or 0
        ga = m.get("goals_away") or 0

        if home_id == team_id:
            gf, gc = gh, ga
        elif away_id == team_id:
            gf, gc = ga, gh
        else:
            continue

        if gf > gc:
            wins += 1
        elif gf < gc:
            losses += 1
        else:
            draws += 1

    total = wins + losses + draws
    if total == 0:
        return 0.0

    return (wins - losses) / total


def calculate_xg_proxy(team_matches: list[dict]) -> float:
    """
    xG aproximado desde datos de football-data.org.
    Si hay datos de tiros disponibles:
      xg_proxy = (shots_on_target / shots_total) * goals_scored / matches_played
    Si no hay tiros (free tier):
      xg_proxy = goals_scored / matches_played
    Devuelve 1.0 si no hay partidos (media historica aproximada).
    """
    if not team_matches:
        return 1.0

    total_goals = 0
    total_shots = 0
    total_sot = 0  # shots on target
    has_shot_data = False

    for m in team_matches:
        goals = m.get("goals_scored") or m.get("goals_home") or m.get("goals_away") or 0
        shots = m.get("shots_total") or 0
        sot = m.get("shots_on_target") or 0

        total_goals += goals
        if shots > 0:
            total_shots += shots
            total_sot += sot
            has_shot_data = True

    n = len(team_matches)

    if has_shot_data and total_shots > 0:
        shot_accuracy = total_sot / total_shots
        return shot_accuracy * (total_goals / n)
    else:
        # Fallback: goles por partido
        return total_goals / n if n > 0 else 1.0


def build_results_list(matches: list[dict], team_id: int) -> list[str]:
    """
    Construye lista de "W"/"D"/"L" para un equipo a partir de una lista de partidos.
    Orden: mas reciente primero (asume que matches ya viene ordenado por fecha DESC).
    """
    results = []
    for m in matches:
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        gh = m.get("goals_home")
        ga = m.get("goals_away")

        if gh is None or ga is None:
            continue

        if home_id == team_id:
            gf, gc = gh, ga
        elif away_id == team_id:
            gf, gc = ga, gh
        else:
            continue

        if gf > gc:
            results.append("W")
        elif gf < gc:
            results.append("L")
        else:
            results.append("D")

    return results
