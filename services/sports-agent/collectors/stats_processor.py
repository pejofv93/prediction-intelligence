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


def calculate_schedule_difficulty(
    team_id: int,
    upcoming_opponents: list[dict],
    all_elo_ratings: dict[int, float] | None = None,
    default_elo: float = 1500.0,
) -> dict:
    """
    Calcula dificultad de calendario para los próximos N partidos.

    upcoming_opponents: lista de dicts con {team_id, team_name, elo (opcional)}.
    all_elo_ratings: dict {team_id: elo} — si None, usa default_elo para todos.

    Returns:
    {
      difficulty: float,          # 0.0-1.0 normalizado (avg_elo / 1500)
      avg_rival_elo: float,
      n_rivals: int,
      label: str,
      confidence_modifier: float
    }
    """
    if not upcoming_opponents:
        return {
            "difficulty": 0.5,
            "avg_rival_elo": default_elo,
            "n_rivals": 0,
            "label": "MODERADO",
            "confidence_modifier": 1.0,
        }

    elo_values: list[float] = []
    for opp in upcoming_opponents:
        opp_id = opp.get("team_id")
        opp_elo = opp.get("elo")

        if opp_elo is not None:
            elo_values.append(float(opp_elo))
        elif all_elo_ratings is not None and opp_id is not None and opp_id in all_elo_ratings:
            elo_values.append(float(all_elo_ratings[opp_id]))
        else:
            elo_values.append(default_elo)

    avg_rival_elo = sum(elo_values) / len(elo_values) if elo_values else default_elo

    # Normalizar: 1500 = 0.50 (base ELO), escalar relativo
    # difficulty = avg_rival_elo / (2 * default_elo) clampado a [0, 1]
    difficulty = min(max(avg_rival_elo / (2.0 * default_elo), 0.0), 1.0)

    if difficulty > 0.80:
        label = "MUY_DIFÍCIL"
        confidence_modifier = 0.90
    elif difficulty > 0.60:
        label = "DIFÍCIL"
        confidence_modifier = 0.95
    elif difficulty >= 0.40:
        label = "MODERADO"
        confidence_modifier = 1.0
    elif difficulty >= 0.25:
        label = "FÁCIL"
        confidence_modifier = 1.05
    else:
        label = "MUY_FÁCIL"
        confidence_modifier = 1.05

    logger.debug(
        "calculate_schedule_difficulty: team=%s avg_elo=%.1f difficulty=%.3f label=%s",
        team_id, avg_rival_elo, difficulty, label,
    )

    return {
        "difficulty": round(difficulty, 4),
        "avg_rival_elo": round(avg_rival_elo, 2),
        "n_rivals": len(elo_values),
        "label": label,
        "confidence_modifier": confidence_modifier,
    }


def apply_schedule_difficulty_to_signal(
    signal: dict,
    home_schedule: dict | None,
    away_schedule: dict | None,
) -> dict:
    """
    Aplica dificultad de calendario al signal.
    Usa el schedule del equipo apostado (signal.get("team_to_back")).
    Nunca falla.
    """
    try:
        team_to_back = str(signal.get("team_to_back", "")).lower()

        if team_to_back in ("home", "local"):
            schedule = home_schedule
        elif team_to_back in ("away", "visitante"):
            schedule = away_schedule
        else:
            # Si no es explícito, usar el que tenga mayor dificultad como precaución
            if home_schedule and away_schedule:
                schedule = (
                    home_schedule
                    if home_schedule.get("difficulty", 0) >= away_schedule.get("difficulty", 0)
                    else away_schedule
                )
            else:
                schedule = home_schedule or away_schedule

        if not schedule:
            return signal

        difficulty = float(schedule.get("difficulty", 0.5))
        confidence = float(signal.get("confidence", 1.0))

        if difficulty > 0.80:
            confidence *= 0.90
            logger.debug(
                "apply_schedule_difficulty: difficulty=%.3f > 0.80 → confidence *= 0.90 → %.4f",
                difficulty, confidence,
            )
        elif difficulty < 0.40:
            confidence *= 1.05
            logger.debug(
                "apply_schedule_difficulty: difficulty=%.3f < 0.40 → confidence *= 1.05 → %.4f",
                difficulty, confidence,
            )

        signal["confidence"] = round(min(max(confidence, 0.0), 1.0), 4)
        signal["schedule_difficulty"] = {
            "difficulty": difficulty,
            "label": schedule.get("label", "MODERADO"),
            "avg_rival_elo": schedule.get("avg_rival_elo"),
            "n_rivals": schedule.get("n_rivals", 0),
        }

    except Exception as e:
        logger.warning("apply_schedule_difficulty_to_signal: error — %s", e)

    return signal


# ── Rendimiento bajo presión ─────────────────────────────────────────────────


def calculate_pressure_performance(
    all_matches: list[dict],
    team_id: int,
    pressure_matches: list[dict],
) -> dict:
    """
    Calcula rendimiento de un equipo en partidos de presión vs rendimiento general.

    all_matches: todos los partidos del equipo (con result/goals)
    pressure_matches: subconjunto donde había algo en juego:
      - diferencia con zona descenso < 5 puntos
      - diferencia con zona Champions < 5 puntos
      - últimas 5 jornadas de temporada

    Returns:
    {
      general_win_rate: float,
      pressure_win_rate: float,
      n_general: int,
      n_pressure: int,
      pressure_strength: bool,  # pressure_win_rate > general * 1.2
      pressure_weakness: bool,  # pressure_win_rate < general * 0.8
      confidence_modifier: float,
      label: str
    }
    """
    def _win_rate(matches: list[dict]) -> float:
        wins = draws = losses = 0
        for m in matches:
            home_id = m.get("home_team_id")
            gh = m.get("goals_home")
            ga = m.get("goals_away")
            if gh is None or ga is None:
                continue
            if home_id == team_id:
                gf, gc = gh, ga
            else:
                gf, gc = ga, gh
            if gf > gc:
                wins += 1
            elif gf < gc:
                losses += 1
            else:
                draws += 1
        total = wins + losses + draws
        return wins / total if total > 0 else 0.5

    general_wr = _win_rate(all_matches)
    pressure_wr = _win_rate(pressure_matches)
    n_pressure = len([m for m in pressure_matches
                      if m.get("goals_home") is not None])

    pressure_strength = False
    pressure_weakness = False
    modifier = 1.0
    label = "NEUTRAL"

    if n_pressure >= 5:  # mínimo 5 partidos de presión para ser significativo
        if pressure_wr > general_wr * 1.2:
            pressure_strength = True
            modifier = 1.10
            label = "FORTALEZA_PRESION"
        elif pressure_wr < general_wr * 0.8:
            pressure_weakness = True
            modifier = 0.85
            label = "DEBILIDAD_PRESION"

    return {
        "general_win_rate": round(general_wr, 4),
        "pressure_win_rate": round(pressure_wr, 4),
        "n_general": len(all_matches),
        "n_pressure": n_pressure,
        "pressure_strength": pressure_strength,
        "pressure_weakness": pressure_weakness,
        "confidence_modifier": modifier,
        "label": label,
    }


def apply_pressure_performance_to_signal(
    signal: dict,
    pressure_data: dict,
    is_decisive_match: bool = False,
) -> dict:
    """
    Aplica rendimiento bajo presión al signal.
    Solo ajusta si is_decisive_match=True (partidos con algo en juego).
    Clampa confidence a [0.0, 1.0]. Nunca falla.
    """
    try:
        if not is_decisive_match:
            return signal
        if not pressure_data:
            return signal

        modifier = float(pressure_data.get("confidence_modifier", 1.0))
        if modifier == 1.0:
            return signal

        confidence = float(signal.get("confidence", 0.65))
        confidence = min(1.0, max(0.0, confidence * modifier))
        signal["confidence"] = round(confidence, 4)
        signal["pressure_performance"] = {
            "label": pressure_data.get("label", "NEUTRAL"),
            "general_win_rate": pressure_data.get("general_win_rate"),
            "pressure_win_rate": pressure_data.get("pressure_win_rate"),
            "n_pressure": pressure_data.get("n_pressure", 0),
            "modifier": modifier,
        }
        logger.debug(
            "pressure_performance: %s → confidence *= %.2f → %.4f",
            pressure_data.get("label"), modifier, confidence,
        )
    except Exception as e:
        logger.warning("apply_pressure_performance_to_signal: error — %s", e)

    return signal
