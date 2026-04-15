"""
Backtesting historico sports-agent.
Ejecutar UNA SOLA VEZ al inicializar el sistema.
Tiempo estimado: 30-60 min por rate limit de football-data.org.
"""
import logging

logger = logging.getLogger(__name__)


async def run_backtest(seasons: int = 2) -> dict:
    """
    Corre el modelo contra partidos historicos de las ultimas N temporadas.
    1. Fetch historical matches de football-data.org
    2. Por cada partido: calcula prediccion con el modelo actual
    3. Compara con resultado real, ajusta pesos igual que run_daily_learning()
    4. Guarda pesos calibrados en model_weights doc current
    Devuelve {accuracy, matches_processed, weights_final}.
    Trigger: POST /run-backtest (anadir a sports-agent/main.py).
    Ejecutar UNA SOLA VEZ al inicializar el sistema. NO scheduler.
    """
    # TODO: implementar en Sesion 4
    raise NotImplementedError
