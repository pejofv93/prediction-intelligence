"""
Sentiment de noticias via Tavily.
Presupuesto: max 30 busquedas/dia total (free tier = 1,000/mes).
Solo se invoca para top 10 mercados por volumen del dia.
"""
import logging

logger = logging.getLogger(__name__)

SOURCE_WEIGHTS = {
    "reuters.com": 1.0,
    "apnews.com": 1.0,
    "bbc.com": 0.9,
    "bloomberg.com": 0.9,
    "ft.com": 0.8,
    "default": 0.5,
}


async def fetch_news_sentiment(market_question: str) -> dict:
    """
    PRESUPUESTO TAVILY: max 30 busquedas/dia total (free tier = 1,000/mes).
    1. Comprobar Firestore tavily_budget {date, calls_today, limit: 30}
       Si calls_today >= limit → devolver {"sentiment_score": 0.0, "news_count": 0,
       "top_headlines": [], "sentiment_trend": "NO_DATA"} sin llamar Tavily
    2. Solo se invoca para top 10 mercados por volumen del dia, no para todos
    3. Tavily search: max_results=5 (no 10, para conservar presupuesto)
    4. Incrementar calls_today en Firestore tras cada llamada exitosa
    5. Ponderar resultados por SOURCE_WEIGHTS
    Devuelve:
      sentiment_score: float (-1.0 a 1.0)   → 0.0 si NO_DATA
      news_count: int                        → 0 si NO_DATA
      top_headlines: list[str] (max 3)       → [] si NO_DATA
      sentiment_trend: "IMPROVING" | "DETERIORATING" | "STABLE" | "NO_DATA"
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
