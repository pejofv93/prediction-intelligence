"""
Backtesting historico polymarket-agent.
Analiza mercados de Polymarket YA resueltos en los ultimos N dias.
Ejecutar UNA SOLA VEZ al inicializar el sistema.
"""
import logging

logger = logging.getLogger(__name__)


async def run_poly_backtest(days_back: int = 90) -> dict:
    """
    Analiza mercados de Polymarket YA resueltos en los ultimos N dias.
    Gamma API: GET /markets?closed=true&order=volume24hr&limit=100
    Por cada mercado resuelto: calcula que habria predicho el modelo vs resultado real.
    Guarda en Firestore coleccion poly_backtest_results.
    Devuelve {accuracy, markets_analyzed, avg_edge_detected}.
    Trigger: POST /run-poly-backtest.
    Ejecutar UNA SOLA VEZ al inicializar el sistema. NO scheduler.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
