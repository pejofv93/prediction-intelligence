"""
Analizador multi-deporte via Groq + Tavily.
Para deportes donde tenemos estadisticas de API-Sports pero NO modelo Poisson.
"""
import logging

logger = logging.getLogger(__name__)


async def analyze_non_football_game(
    game: dict,
    home_stats: dict,
    away_stats: dict,
) -> dict:
    """
    1. Busca noticias recientes con Tavily: lesiones, forma, contexto.
    2. Llama Groq con stats + noticias → estima probabilidades.
    System prompt: "Eres un experto en {sport}. Dados estos stats y noticias,
      estima la probabilidad de victoria local. Responde SOLO JSON:
      {home_win_prob: float, confidence: float, key_factors: list[str]}"
    Devuelve {home_win_prob, confidence, key_factors, data_source: "groq_ai"}.
    data_source distingue predicciones con modelo propio vs estimacion IA.
    """
    # TODO: implementar en Sesion 2
    raise NotImplementedError
