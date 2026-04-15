"""
Motor de aprendizaje diario — ajusta pesos del modelo segun resultados reales.
Se ejecuta cada dia a las 02:00 UTC via learning-engine.yml.
"""
import logging

from shared.config import LEARNING_RATE

logger = logging.getLogger(__name__)

ERROR_TYPES = [
    "poisson_overweighted",    # el modelo Poisson sobreestimo la probabilidad
    "elo_misleading",          # el rating ELO no reflejaba el estado real del equipo
    "form_misleading",         # la forma reciente era enganosa (lesiones, rotaciones)
    "h2h_irrelevant",          # el historial directo no era relevante para este partido
    "odds_inefficiency",       # la cuota era trampa (bookmaker tenia informacion privilegiada)
]

# Mapa de error_type → clave de weights para saber que peso reducir
# Solo aplica para deportes con modelo estadistico (data_source='statistical_model')
ERROR_TO_WEIGHT = {
    "poisson_overweighted": "poisson",
    "elo_misleading":       "elo",
    "form_misleading":      "form",
    "h2h_irrelevant":       "h2h",
    "odds_inefficiency":    None,  # no reduce ningun weight especifico
}


async def fetch_pending_results() -> list[dict]:
    """
    Query Firestore predictions donde:
    - result == None
    - match_date < now - 24h
    Devuelve lista de predicciones pendientes de evaluar.
    """
    # TODO: implementar en Sesion 4
    raise NotImplementedError


async def check_result(match_id: str) -> str | None:
    """
    Llama football_api.get_match_result(match_id).
    Devuelve "HOME_WIN" | "AWAY_WIN" | "DRAW" | None.
    """
    # TODO: implementar en Sesion 4
    raise NotImplementedError


def evaluate_prediction(prediction: dict, actual_result: str) -> dict:
    """
    Determina si la prediccion fue correcta.
    Clasifica el error_type si fue incorrecta usando los factores del documento.
    Devuelve {"correct": bool, "error_type": str | None}.
    """
    # TODO: implementar en Sesion 4
    raise NotImplementedError


def update_weights(
    error_type: str | None,
    top_factor: str,
    current_weights: dict,
    correct: bool,
) -> dict:
    """
    Si fallo:  weights[ERROR_TO_WEIGHT[error_type]] *= (1 - LEARNING_RATE)
    Si acierto: weights[top_factor] *= (1 + LEARNING_RATE * 0.6)
    Si error_type == None o "odds_inefficiency" → no cambiar pesos.
    Normaliza para que sumen 1.0.
    Clampea cada peso entre 0.05 y 0.60.
    Devuelve nuevos pesos.
    """
    # TODO: implementar en Sesion 4
    raise NotImplementedError


def calculate_accuracy(predictions: list[dict]) -> float:
    """Devuelve tasa de acierto (0.0-1.0) de la lista dada."""
    # TODO: implementar en Sesion 4
    raise NotImplementedError


async def run_daily_learning() -> None:
    """
    1. fetch_pending_results()
    2. Por cada prediccion: check_result() → evaluate_prediction() → update_weights()
    3. Llamar elo_rating.update_all_elos(finished_matches) con los partidos verificados
    4. Actualiza doc 'current' en model_weights con nuevos pesos + nueva version
    5. Calcula accuracy de la semana actual y compara con accuracy_log semana anterior
    6. Guarda/actualiza entrada en accuracy_log para la semana actual
    7. Actualiza prediction en Firestore con result, correct, error_type
    """
    # TODO: implementar en Sesion 4
    raise NotImplementedError

# generate_weekly_report() MOVIDA a shared/report_generator.py
# No implementar aqui — importar desde shared en el telegram-bot directamente.
