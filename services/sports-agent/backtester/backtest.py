"""
Backtesting historico sports-agent.
Ejecutar UNA SOLA VEZ al inicializar el sistema para calibrar pesos.
Tiempo estimado: 30-60 min por rate limit de football-data.org (6.5s/req).

Algoritmo:
  Para cada liga en SUPPORTED_FOOTBALL_LEAGUES × ultimas N temporadas:
  1. Descarga todos los partidos FINISHED de la temporada
  2. Agrupa partidos por equipo (rolling window)
  3. Para cada partido (en orden cronologico):
     a. Toma los 10 partidos previos de cada equipo
     b. Aplica Poisson + ELO con pesos actuales
     c. Compara con resultado real
     d. Ajusta pesos via update_weights()
  4. Guarda pesos finales calibrados en model_weights/current
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from shared.config import DEFAULT_WEIGHTS, SUPPORTED_FOOTBALL_LEAGUES
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Delay entre peticiones para respetar rate limit de football-data.org (10 req/min)
_RATE_LIMIT_DELAY = 6.5
# Numero maximo de partidos historicos por equipo para calcular Poisson
_ROLLING_WINDOW = 10


async def run_backtest(seasons: int = 2) -> dict:
    """
    Corre el modelo contra partidos historicos de las ultimas N temporadas.

    1. Fetch historical matches de football-data.org por liga/temporada
    2. Para cada partido en orden cronologico: calcula prediccion con rolling stats
    3. Compara con resultado real, ajusta pesos igual que run_daily_learning()
    4. Guarda pesos calibrados en model_weights doc current
    Devuelve {accuracy, matches_processed, weights_final}.
    """
    logger.info("run_backtest: iniciando con %d temporada(s)", seasons)

    # Determinar anios de temporada a evaluar
    current_year = datetime.now(timezone.utc).year
    season_years = [current_year - i for i in range(1, seasons + 1)]

    # Cargar pesos iniciales (o DEFAULT_WEIGHTS)
    weights = _load_current_weights()
    logger.info("run_backtest: pesos iniciales=%s", weights)

    total_processed = 0
    total_correct = 0
    all_results: list[dict] = []

    for league_code, league_id in SUPPORTED_FOOTBALL_LEAGUES.items():
        for season_year in season_years:
            try:
                logger.info(
                    "run_backtest: procesando %s temporada %d", league_code, season_year
                )
                matches = await _fetch_season_matches(league_id, season_year)

                if not matches:
                    logger.warning(
                        "run_backtest: sin partidos para %s/%d", league_code, season_year
                    )
                    continue

                logger.info(
                    "run_backtest: %d partidos descargados para %s/%d",
                    len(matches), league_code, season_year,
                )

                # Ordenar cronologicamente
                matches.sort(key=lambda m: m.get("date", ""))

                # Evaluar con rolling window
                season_results = _evaluate_season(matches, weights, league_code)

                for r in season_results:
                    from learner.learning_engine import update_weights
                    weights = update_weights(
                        error_type=r.get("error_type"),
                        top_factor=r.get("top_factor", "poisson"),
                        current_weights=weights,
                        correct=r.get("correct", False),
                    )

                correct_in_season = sum(1 for r in season_results if r.get("correct"))
                total_processed += len(season_results)
                total_correct += correct_in_season
                all_results.extend(season_results)

                logger.info(
                    "run_backtest: %s/%d → %d/%d correctas (%.1f%%)",
                    league_code, season_year,
                    correct_in_season, len(season_results),
                    100.0 * correct_in_season / len(season_results) if season_results else 0,
                )

            except Exception:
                logger.error(
                    "run_backtest: error procesando %s/%d", league_code, season_year,
                    exc_info=True,
                )

    # Accuracy final
    accuracy = round(total_correct / total_processed, 4) if total_processed > 0 else 0.0

    logger.info(
        "run_backtest: completado — accuracy=%.1f%% (%d/%d) pesos_finales=%s",
        accuracy * 100, total_correct, total_processed, weights,
    )

    # Guardar pesos calibrados en Firestore
    await _save_calibrated_weights(weights, accuracy, total_processed, total_correct)

    return {
        "accuracy": accuracy,
        "matches_processed": total_processed,
        "weights_final": weights,
    }


async def _fetch_season_matches(league_id: int, season_year: int) -> list[dict]:
    """
    Descarga todos los partidos FINISHED de una liga/temporada desde football-data.org.
    Un solo request por liga/temporada — mas eficiente que partido a partido.
    Devuelve lista de partidos normalizados.
    """
    import httpx
    from shared.config import FOOTBALL_API_KEY

    if not FOOTBALL_API_KEY:
        logger.warning("_fetch_season_matches: FOOTBALL_API_KEY no configurada")
        return []

    await asyncio.sleep(_RATE_LIMIT_DELAY)

    url = f"https://api.football-data.org/v4/competitions/{league_id}/matches"
    params = {"season": str(season_year), "status": "FINISHED"}
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers, params=params)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            logger.warning(
                "_fetch_season_matches: rate limit 429 — esperando %ds", retry_after
            )
            await asyncio.sleep(retry_after)
            return []

        if resp.status_code != 200:
            logger.warning(
                "_fetch_season_matches(%d/%d): API respondio %d",
                league_id, season_year, resp.status_code,
            )
            return []

        data = resp.json()
        raw_matches = data.get("matches", [])

        return [_parse_historical_match(m) for m in raw_matches if _parse_historical_match(m)]

    except Exception:
        logger.error(
            "_fetch_season_matches(%d/%d): error de red", league_id, season_year, exc_info=True
        )
        return []


def _parse_historical_match(raw: dict) -> dict | None:
    """Normaliza un partido del endpoint /competitions/{id}/matches."""
    try:
        home = raw.get("homeTeam", {})
        away = raw.get("awayTeam", {})
        score = raw.get("score", {})
        full_time = score.get("fullTime", {})

        goals_home = full_time.get("home")
        goals_away = full_time.get("away")

        if goals_home is None or goals_away is None:
            return None

        winner = score.get("winner", "")
        if winner == "HOME_TEAM":
            result = "HOME_WIN"
        elif winner == "AWAY_TEAM":
            result = "AWAY_WIN"
        elif winner == "DRAW":
            result = "DRAW"
        else:
            return None

        return {
            "match_id": str(raw.get("id", "")),
            "date": raw.get("utcDate", ""),
            "home_team_id": home.get("id"),
            "away_team_id": away.get("id"),
            "home_team_name": home.get("name", ""),
            "away_team_name": away.get("name", ""),
            "goals_home": int(goals_home),
            "goals_away": int(goals_away),
            "result": result,
            "was_home": True,  # perspectiva del home — usado en rolling stats
        }
    except Exception:
        return None


def _evaluate_season(
    matches: list[dict],
    weights: dict,
    league_code: str,
) -> list[dict]:
    """
    Evalua cada partido de una temporada usando rolling stats (sin llamadas API).

    Para cada partido:
    1. Toma los ultimos _ROLLING_WINDOW partidos previos de cada equipo
    2. Aplica Poisson con esos datos
    3. Ensemble con ELO en DEFAULT_ELO (sin historial — cold start)
    4. Compara con resultado real
    5. Devuelve lista de resultados para actualizar pesos
    """
    from enrichers.poisson_model import fit_attack_defense, predict_match_probs
    from learner.learning_engine import evaluate_prediction, _top_factor

    # Historial rolling por equipo: {team_id: [matches en orden cronologico]}
    team_history: dict[int, list[dict]] = defaultdict(list)
    results: list[dict] = []

    for match in matches:
        home_id = match.get("home_team_id")
        away_id = match.get("away_team_id")
        actual_result = match.get("result")

        if not home_id or not away_id or not actual_result:
            continue

        # Rolling stats con los ultimos N partidos de cada equipo
        home_prev = team_history[home_id][-_ROLLING_WINDOW:]
        away_prev = team_history[away_id][-_ROLLING_WINDOW:]
        all_prev = home_prev + away_prev

        # Calcular Poisson con datos disponibles
        try:
            if len(all_prev) >= 2:
                team_params = fit_attack_defense(all_prev)
                probs = predict_match_probs(home_id, away_id, team_params)
                poisson_home_win = probs["home_win"]
                poisson_away_win = probs["away_win"]
            else:
                poisson_home_win = 0.45  # cold start neutral
                poisson_away_win = 0.30
        except Exception:
            poisson_home_win = 0.45
            poisson_away_win = 0.30

        # ELO simplificado (cold start = DEFAULT_ELO para todos → 0.5 ajustado por home adv)
        # Sin historial real de ELO no podemos usar el sistema completo
        elo_home_win_prob = 0.54  # ventaja estadistica media del local

        # Form score simplificado desde rolling window
        home_form = _calc_rolling_form(home_id, home_prev)
        away_form = _calc_rolling_form(away_id, away_prev)
        h2h_adv = _calc_h2h(home_id, away_id, all_prev)

        enriched_mock = {
            "poisson_home_win": poisson_home_win,
            "poisson_away_win": poisson_away_win,
            "elo_home_win_prob": elo_home_win_prob,
            "home_form_score": home_form,
            "away_form_score": away_form,
            "h2h_advantage": h2h_adv,
        }

        # Ensemble para home
        from analyzers.value_bet_engine import ensemble_probability
        res_home = ensemble_probability(enriched_mock, weights, team="home")
        res_away = ensemble_probability(enriched_mock, weights, team="away")

        # Simular prediccion: apostar al lado con mayor probabilidad
        if res_home["prob"] >= res_away["prob"]:
            pred_team = "home"
            signals = res_home["signals"]
        else:
            pred_team = "away"
            signals = res_away["signals"]

        # Verificar si fue correcto
        if pred_team == "home":
            correct = (actual_result == "HOME_WIN")
        else:
            correct = (actual_result == "AWAY_WIN")

        # Clasificar error
        if not correct and signals:
            dominant = max(signals, key=lambda k: signals[k])
            error_map = {
                "poisson": "poisson_overweighted",
                "elo": "elo_misleading",
                "form": "form_misleading",
                "h2h": "h2h_irrelevant",
            }
            error_type = error_map.get(dominant, "poisson_overweighted")
        else:
            dominant = max(signals, key=lambda k: signals[k]) if signals else "poisson"
            error_type = None

        results.append({
            "match_id": match.get("match_id"),
            "correct": correct,
            "error_type": error_type,
            "top_factor": dominant,
            "actual_result": actual_result,
            "league": league_code,
        })

        # Actualizar historial del equipo con este partido
        match_record = {
            "match_id": match.get("match_id"),
            "date": match.get("date", ""),
            "home_team_id": home_id,
            "away_team_id": away_id,
            "goals_home": match.get("goals_home", 0),
            "goals_away": match.get("goals_away", 0),
            "was_home": True,
        }
        team_history[home_id].append(match_record)
        # Para el visitante, crear version con perspectiva invertida
        away_record = {**match_record, "was_home": False}
        team_history[away_id].append(away_record)

    return results


def _calc_rolling_form(team_id: int, recent_matches: list[dict]) -> float:
    """
    Calcula form score simplificado (0-100) desde los ultimos partidos.
    W=3pts D=1pt L=0pts con decaimiento por posicion.
    """
    if not recent_matches:
        return 50.0

    weighted_actual = 0.0
    weighted_max = 0.0
    points = {"W": 3, "D": 1, "L": 0}

    for i, m in enumerate(reversed(recent_matches)):  # mas reciente primero
        home_id = m.get("home_team_id")
        gh = m.get("goals_home", 0) or 0
        ga = m.get("goals_away", 0) or 0

        if home_id == team_id:
            gf, gc = gh, ga
        else:
            gf, gc = ga, gh

        if gf > gc:
            result = "W"
        elif gf < gc:
            result = "L"
        else:
            result = "D"

        weight = 1.0 / (i + 1)
        weighted_actual += points[result] * weight
        weighted_max += 3 * weight

    return round((weighted_actual / weighted_max) * 100.0, 1) if weighted_max > 0 else 50.0


def _calc_h2h(home_id: int, away_id: int, recent_matches: list[dict]) -> float:
    """
    Calcula h2h_advantage simplificado desde los partidos disponibles.
    Solo considera enfrentamientos directos.
    """
    wins = losses = draws = 0
    for m in recent_matches:
        h = m.get("home_team_id")
        a = m.get("away_team_id")
        if not ((h == home_id and a == away_id) or (h == away_id and a == home_id)):
            continue

        gh = m.get("goals_home", 0) or 0
        ga = m.get("goals_away", 0) or 0

        if h == home_id:
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
    return round((wins - losses) / total, 3) if total > 0 else 0.0


def _load_current_weights() -> dict:
    """Lee pesos actuales de Firestore. Fallback a DEFAULT_WEIGHTS."""
    try:
        doc = col("model_weights").document("current").get()
        if doc.exists:
            data = doc.to_dict()
            w = data.get("weights", {})
            if all(k in w for k in DEFAULT_WEIGHTS):
                return dict(w)
    except Exception:
        logger.error("_load_current_weights: error leyendo Firestore", exc_info=True)
    return dict(DEFAULT_WEIGHTS)


async def _save_calibrated_weights(
    weights: dict,
    accuracy: float,
    total: int,
    correct: int,
) -> None:
    """Guarda los pesos calibrados por el backtest en model_weights/current."""
    try:
        doc = col("model_weights").document("current").get()
        version = 0
        if doc.exists:
            version = int(doc.to_dict().get("version", 0))

        col("model_weights").document("current").set({
            "version": version + 1,
            "updated": datetime.now(timezone.utc),
            "weights": weights,
            "accuracy_by_league": {k: 0.0 for k in SUPPORTED_FOOTBALL_LEAGUES},
            "blacklisted_leagues": [],
            "min_edge_threshold": 0.08,
            "min_confidence": 0.65,
            "total_predictions": total,
            "correct_predictions": correct,
            "backtest_accuracy": accuracy,
        })
        logger.info(
            "_save_calibrated_weights: pesos guardados v%d accuracy=%.1f%%",
            version + 1, accuracy * 100,
        )
    except Exception:
        logger.error("_save_calibrated_weights: error guardando en Firestore", exc_info=True)
