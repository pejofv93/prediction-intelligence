"""
Motor de aprendizaje diario — ajusta pesos del modelo segun resultados reales.
Se ejecuta diariamente a las 02:00 UTC via learning-engine.yml.

Flujo:
  fetch_pending_results → check_result (football_api) → evaluate_prediction
  → update_weights → update_all_elos → actualiza model_weights + accuracy_log
"""
import asyncio
import logging
import unicodedata
from datetime import datetime, timedelta, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import DEFAULT_WEIGHTS, LEARNING_RATE, SUPPORTED_FOOTBALL_LEAGUES
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Tipos de error mapeados a los 4 signals del ensemble
ERROR_TYPES = [
    "poisson_overweighted",  # el modelo Poisson sobreestimo la probabilidad
    "elo_misleading",        # el rating ELO no reflejaba el estado real del equipo
    "form_misleading",       # la forma reciente era enganosa (lesiones, rotaciones)
    "h2h_irrelevant",        # el historial directo no era relevante para este partido
    "odds_inefficiency",     # la cuota era trampa (bookmaker tenia informacion privilegiada)
]

# Mapa de error_type → clave de weights para saber que peso reducir
# Solo aplica para data_source='statistical_model'
# Para groq_ai sports: no se ajustan pesos estadisticos
ERROR_TO_WEIGHT: dict[str, str | None] = {
    "poisson_overweighted": "poisson",
    "elo_misleading":       "elo",
    "form_misleading":      "form",
    "h2h_irrelevant":       "h2h",
    "odds_inefficiency":    None,  # no reduce ningun weight especifico
}

# Ligas de futbol con modelo estadistico completo
_FOOTBALL_LEAGUES = set(SUPPORTED_FOOTBALL_LEAGUES.keys())


def _norm(s: str) -> str:
    """Normaliza string para comparación: strip, lower, sin acentos."""
    return (
        unicodedata.normalize("NFD", str(s).strip().lower())
        .encode("ascii", "ignore")
        .decode()
    )


async def fetch_pending_results() -> list[dict]:
    """
    Busca predicciones en Firestore donde:
    - result == None (aun sin evaluar)
    - match_date < now - 24h (el partido ya debio jugarse)
    Devuelve lista de predicciones pendientes de evaluar.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    try:
        # Query equality en result — el filtro de match_date se aplica en Python
        # para evitar requerir indice compuesto en Firestore
        docs = col("predictions").where(filter=FieldFilter("result", "==", None)).stream()
        pending = []
        for doc in docs:
            data = doc.to_dict()
            match_date = data.get("match_date")

            if match_date is None:
                continue

            # Parsear string ISO si Firestore lo devuelve como str en lugar de timestamp
            if isinstance(match_date, str):
                try:
                    match_date = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
                except ValueError:
                    logger.warning("fetch_pending_results: match_date no parseable: %s", match_date)
                    continue

            # Normalizar timezone si es naive
            if hasattr(match_date, "tzinfo") and match_date.tzinfo is None:
                match_date = match_date.replace(tzinfo=timezone.utc)

            if match_date < cutoff:
                pending.append(data)

        logger.info("fetch_pending_results: %d predicciones pendientes", len(pending))
        return pending

    except Exception:
        logger.error("fetch_pending_results: error leyendo Firestore", exc_info=True)
        return []


async def check_result(match_id: str) -> str | None:
    """
    Llama a football_api.get_match_result(match_id).
    Devuelve "HOME_WIN" | "AWAY_WIN" | "DRAW" | None.
    """
    try:
        from collectors.football_api import get_match_result
        result = await get_match_result(match_id)
        if result is None:
            return None
        # get_match_result devuelve un dict {"result": "HOME_WIN", ...}
        return result.get("result")
    except Exception:
        logger.error("check_result(%s): error consultando API", match_id, exc_info=True)
        return None


def evaluate_prediction(prediction: dict, actual_result: str) -> dict:
    """
    Determina si la prediccion fue correcta y clasifica el error_type si fallo.

    Logica de clasificacion de errores:
    - Identifica que signal tenia el valor mas alto (factor dominante)
    - Mapea ese factor al tipo de error correspondiente
    - Si la prediccion fue correcta, error_type = None

    Devuelve {"correct": bool, "error_type": str | None}
    """
    team_to_back = _norm(prediction.get("team_to_back", ""))
    home_team = _norm(prediction.get("home_team", ""))
    away_team = _norm(prediction.get("away_team", ""))
    home_id = str(prediction.get("home_team_id", "")).strip()
    away_id = str(prediction.get("away_team_id", "")).strip()

    # Determinar si la prediccion fue correcta (comparación normalizada)
    if team_to_back == home_team or team_to_back == home_id:
        correct = (actual_result == "HOME_WIN")
    elif team_to_back == away_team or team_to_back == away_id:
        correct = (actual_result == "AWAY_WIN")
    else:
        # No se puede determinar — considerar incorrecto
        logger.warning(
            "evaluate_prediction: team_to_back '%s' no coincide con home/away '%s'/'%s'",
            team_to_back, home_team, away_team,
        )
        correct = False

    if correct:
        return {"correct": True, "error_type": None}

    # Clasificar tipo de error para data_source='statistical_model'
    data_source = prediction.get("data_source", "statistical_model")
    if data_source != "statistical_model":
        return {"correct": False, "error_type": None}

    factors = prediction.get("factors", {})
    if not factors:
        return {"correct": False, "error_type": "poisson_overweighted"}

    # El signal mas determinante (mayor valor) es el responsable del error
    signal_to_error = {
        "poisson": "poisson_overweighted",
        "elo":     "elo_misleading",
        "form":    "form_misleading",
        "h2h":     "h2h_irrelevant",
    }

    # Buscar el factor con mayor desviacion respecto al resultado real
    # Si el resultado fue lo contrario a lo predicho, el factor mas alto fue el "culpable"
    relevant_factors = {k: v for k, v in factors.items() if k in signal_to_error}
    if not relevant_factors:
        return {"correct": False, "error_type": "odds_inefficiency"}

    # Verificar si las cuotas eran sospechosamente bajas (odds_inefficiency)
    odds = prediction.get("odds", 2.0)
    edge = prediction.get("edge", 0.0)
    if edge > 0.15 and odds < 1.5:
        # Cuota muy baja con edge muy alto — sospechoso
        return {"correct": False, "error_type": "odds_inefficiency"}

    # Identificar el factor dominante
    dominant_factor = max(relevant_factors, key=lambda k: relevant_factors[k])
    error_type = signal_to_error.get(dominant_factor, "poisson_overweighted")

    return {"correct": False, "error_type": error_type}


def update_weights(
    error_type: str | None,
    top_factor: str,
    current_weights: dict,
    correct: bool,
) -> dict:
    """
    Ajusta los pesos del ensemble segun el resultado de la prediccion.

    Si fallo:  weights[ERROR_TO_WEIGHT[error_type]] *= (1 - LEARNING_RATE)
    Si acierto: weights[top_factor] *= (1 + LEARNING_RATE * 0.6)
    Si error_type == None o "odds_inefficiency" → no cambiar pesos.

    Normaliza para que sumen 1.0.
    Clampea cada peso entre 0.05 y 0.60.
    Devuelve nuevos pesos.
    """
    weights = dict(current_weights)

    if correct:
        # Reforzar el factor dominante (aprendizaje mas lento para evitar overfitting)
        if top_factor in weights:
            weights[top_factor] *= (1.0 + LEARNING_RATE * 0.6)
    else:
        # Reducir el peso del factor culpable del error
        if error_type is not None and error_type != "odds_inefficiency":
            weight_key = ERROR_TO_WEIGHT.get(error_type)
            if weight_key and weight_key in weights:
                weights[weight_key] *= (1.0 - LEARNING_RATE)

    # Clampear entre 0.05 y 0.60 antes de normalizar
    for k in weights:
        weights[k] = max(0.05, min(0.60, weights[k]))

    # Normalizar para que sumen 1.0
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 6) for k, v in weights.items()}

    return weights


def calculate_accuracy(predictions: list[dict]) -> float:
    """
    Devuelve tasa de acierto (0.0–1.0) de la lista dada.
    Solo cuenta predicciones con correct != None.
    """
    evaluated = [p for p in predictions if p.get("correct") is not None]
    if not evaluated:
        return 0.0
    correct_count = sum(1 for p in evaluated if p.get("correct") is True)
    return round(correct_count / len(evaluated), 4)


def _get_week_label(dt: datetime) -> str:
    """Devuelve etiqueta de semana ISO: ej. '2025-W14'."""
    iso_cal = dt.isocalendar()
    return f"{iso_cal.year}-W{iso_cal.week:02d}"


def _top_factor(signals: dict) -> str:
    """Devuelve la clave del signal con mayor valor en el prediction.factors."""
    valid = {k: v for k, v in signals.items() if k in DEFAULT_WEIGHTS}
    if not valid:
        return "poisson"
    return max(valid, key=lambda k: valid[k])


async def run_daily_learning() -> None:
    """
    Pipeline completo de aprendizaje diario:

    1. fetch_pending_results()
    2. Por cada prediccion: check_result() → evaluate_prediction() → update_weights()
    3. update_all_elos() con los partidos verificados (actualiza ELOs)
    4. Actualiza doc 'current' en model_weights con nuevos pesos + nueva version
    5. Calcula accuracy de la semana actual
    6. Guarda/actualiza accuracy_log para la semana actual
    7. Actualiza cada prediction en Firestore con result, correct, error_type
    """
    now = datetime.now(timezone.utc)
    current_week = _get_week_label(now)

    # --- 1. Obtener predicciones pendientes ---
    pending = await fetch_pending_results()
    if not pending:
        logger.info("run_daily_learning: sin predicciones pendientes")
        return

    logger.info("run_daily_learning: procesando %d predicciones", len(pending))

    # --- 2. Cargar pesos actuales ---
    try:
        weights_doc = col("model_weights").document("current").get()
        if weights_doc.exists:
            data = weights_doc.to_dict()
            current_weights = data.get("weights", dict(DEFAULT_WEIGHTS))
            current_version = int(data.get("version", 0))
        else:
            current_weights = dict(DEFAULT_WEIGHTS)
            current_version = 0
    except Exception:
        logger.error("run_daily_learning: error leyendo model_weights — usando defaults", exc_info=True)
        current_weights = dict(DEFAULT_WEIGHTS)
        current_version = 0

    weights_start = dict(current_weights)

    # --- 3. Procesar cada prediccion ---
    processed_predictions: list[dict] = []
    finished_matches_for_elo: list[dict] = []
    accuracy_by_league: dict[str, list[bool]] = {k: [] for k in _FOOTBALL_LEAGUES}

    # 3a. Paralelizar todas las llamadas check_result (I/O bound → asyncio.gather)
    _match_ids = [str(p.get("match_id", "")) for p in pending]
    _raw_results = await asyncio.gather(
        *[check_result(mid) for mid in _match_ids],
        return_exceptions=True,
    )
    logger.info(
        "run_daily_learning: check_result paralelo completado — %d/%d con resultado",
        sum(1 for r in _raw_results if r is not None and not isinstance(r, Exception)),
        len(_raw_results),
    )

    # 3b. Procesar resultados en orden (weight updates son acumulativos)
    for prediction, actual_result in zip(pending, _raw_results):
        match_id = prediction.get("match_id", "")
        league = prediction.get("league", "")

        try:
            if isinstance(actual_result, Exception):
                logger.error(
                    "run_daily_learning: check_result(%s) excepción — %s",
                    match_id, actual_result,
                )
                continue
            if actual_result is None:
                # Partido sin resultado todavia — omitir
                continue

            # Evaluar prediccion
            evaluation = evaluate_prediction(prediction, actual_result)
            correct = evaluation["correct"]
            error_type = evaluation["error_type"]

            # Identificar factor dominante
            factors = prediction.get("factors", {})
            top = _top_factor(factors)

            # Ajustar pesos solo para predicciones con modelo estadistico
            data_source = prediction.get("data_source", "")
            if data_source == "statistical_model":
                current_weights = update_weights(error_type, top, current_weights, correct)

            # Guardar para actualizacion de ELOs (partidos de futbol verificados)
            if league in _FOOTBALL_LEAGUES and prediction.get("home_team_id") and prediction.get("away_team_id"):
                finished_matches_for_elo.append({
                    "home_team_id": prediction.get("home_team_id"),
                    "away_team_id": prediction.get("away_team_id"),
                    "result": actual_result,
                    "date": str(prediction.get("match_date", "")),
                })

            # Acumulat accuracy por liga
            if league in accuracy_by_league:
                accuracy_by_league[league].append(correct)

            # Actualizar el documento prediction en Firestore
            processed_predictions.append({
                "match_id": match_id,
                "result": actual_result,
                "correct": correct,
                "error_type": error_type,
            })

            # Sincronizar shadow_trade — crea el doc si no existe, luego lo resuelve
            try:
                from shared.shadow_engine import track_new_signal, update_trade_result
                shadow_result = "win" if correct else "loss"
                existing = list(
                    col("shadow_trades")
                    .where(filter=FieldFilter("signal_id", "==", str(match_id)))
                    .where(filter=FieldFilter("source", "==", "sports"))
                    .limit(1)
                    .stream()
                )
                if existing:
                    trade_id = existing[0].id
                    created = False
                else:
                    trade_id = await track_new_signal(prediction, "sports")
                    created = True
                await update_trade_result(trade_id, shadow_result)
                logger.info(
                    "run_daily_learning: shadow_trade %s → %s (%s trade_id=%s)",
                    match_id, shadow_result, "created" if created else "updated", trade_id,
                )
            except Exception:
                logger.error(
                    "run_daily_learning: error sincronizando shadow_trade para %s",
                    match_id, exc_info=True,
                )

            logger.debug(
                "run_daily_learning: %s → %s | correct=%s error=%s",
                match_id, actual_result, correct, error_type,
            )

        except Exception:
            logger.error(
                "run_daily_learning: error procesando prediccion %s", match_id, exc_info=True
            )

    # --- 4. Actualizar ELOs ---
    if finished_matches_for_elo:
        try:
            from enrichers.elo_rating import update_all_elos
            await update_all_elos(finished_matches_for_elo)
            logger.info("run_daily_learning: ELOs actualizados para %d partidos", len(finished_matches_for_elo))
        except Exception:
            logger.error("run_daily_learning: error actualizando ELOs", exc_info=True)

    if not processed_predictions:
        logger.info("run_daily_learning: ninguna prediccion pudo resolverse hoy")
        return

    # --- 5. Guardar model_weights actualizado ---
    # Calcular accuracy por liga
    acc_by_league = {
        league: round(sum(results) / len(results), 4) if results else 0.0
        for league, results in accuracy_by_league.items()
    }

    new_version = current_version + 1
    total_in_db, correct_in_db = _get_historical_counts()

    try:
        col("model_weights").document("current").set({
            "version": new_version,
            "updated": now,
            "weights": current_weights,
            "accuracy_by_league": acc_by_league,
            "blacklisted_leagues": [],
            "min_edge_threshold": 0.08,
            "min_confidence": 0.65,
            "total_predictions": total_in_db + len(processed_predictions),
            "correct_predictions": correct_in_db + sum(
                1 for p in processed_predictions if p.get("correct")
            ),
        })
        logger.info(
            "run_daily_learning: model_weights actualizado → version %d pesos=%s",
            new_version, current_weights,
        )
    except Exception:
        logger.error("run_daily_learning: error guardando model_weights", exc_info=True)

    # --- 6. Actualizar accuracy_log de la semana ---
    week_predictions = [p for p in processed_predictions]
    week_accuracy = calculate_accuracy(
        [{"correct": p.get("correct")} for p in week_predictions]
    )

    # Buscar accuracy de la semana anterior para el delta del reporte
    prev_week_accuracy = _get_prev_week_accuracy(current_week)

    try:
        acc_log_ref = col("accuracy_log").document(current_week)
        acc_log_doc = acc_log_ref.get()

        if acc_log_doc.exists:
            existing = acc_log_doc.to_dict()
            total_prev = existing.get("predictions_total", 0)
            correct_prev = existing.get("predictions_correct", 0)
            total_new = total_prev + len(week_predictions)
            correct_new = correct_prev + sum(1 for p in week_predictions if p.get("correct"))
            updated_accuracy = round(correct_new / total_new, 4) if total_new > 0 else 0.0

            acc_log_ref.update({
                "predictions_total": total_new,
                "predictions_correct": correct_new,
                "accuracy": updated_accuracy,
                "accuracy_by_league": acc_by_league,
                "weights_end": current_weights,
                "prev_week_accuracy": prev_week_accuracy,
            })
        else:
            correct_count = sum(1 for p in week_predictions if p.get("correct"))
            acc_log_ref.set({
                "week": current_week,
                "predictions_total": len(week_predictions),
                "predictions_correct": correct_count,
                "accuracy": week_accuracy,
                "prev_week_accuracy": prev_week_accuracy,
                "accuracy_by_league": acc_by_league,
                "weights_start": weights_start,
                "weights_end": current_weights,
                "created_at": now,
            })

        logger.info(
            "run_daily_learning: accuracy_log[%s] actualizado — accuracy=%.1f%%",
            current_week, week_accuracy * 100,
        )
    except Exception:
        logger.error("run_daily_learning: error guardando accuracy_log", exc_info=True)

    # --- 7. Actualizar cada prediction con result/correct/error_type ---
    for upd in processed_predictions:
        payload = {
            "result": upd["result"],
            "correct": upd["correct"],
            "error_type": upd["error_type"],
        }
        mid = upd["match_id"]
        # Actualizar el doc principal
        try:
            col("predictions").document(str(mid)).update(payload)
        except Exception:
            logger.error(
                "run_daily_learning: error actualizando prediction %s", mid, exc_info=True
            )
        # Actualizar también {match_id}_synthetic si existe
        try:
            synthetic_ref = col("predictions").document(f"{mid}_synthetic")
            snap = synthetic_ref.get()
            if snap.exists:
                synthetic_ref.update(payload)
                logger.debug("run_daily_learning: %s_synthetic actualizado", mid)
        except Exception:
            logger.warning(
                "run_daily_learning: error actualizando %s_synthetic", mid, exc_info=True
            )

    logger.info(
        "run_daily_learning: completado — %d procesadas, accuracy semana=%.1f%%",
        len(processed_predictions), week_accuracy * 100,
    )


def _get_historical_counts() -> tuple[int, int]:
    """Lee totales historicos de model_weights para acumularlos correctamente."""
    try:
        doc = col("model_weights").document("current").get()
        if doc.exists:
            data = doc.to_dict()
            return int(data.get("total_predictions", 0)), int(data.get("correct_predictions", 0))
    except Exception:
        pass
    return 0, 0


def _get_prev_week_accuracy(current_week: str) -> float | None:
    """Lee accuracy de la semana anterior desde accuracy_log."""
    try:
        # Calcular etiqueta de la semana anterior
        now = datetime.now(timezone.utc)
        prev_week_dt = now - timedelta(weeks=1)
        prev_week = _get_week_label(prev_week_dt)

        doc = col("accuracy_log").document(prev_week).get()
        if doc.exists:
            return doc.to_dict().get("accuracy")
    except Exception:
        logger.error("_get_prev_week_accuracy: error leyendo Firestore", exc_info=True)
    return None
