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


_NO_DATA = {
    "sentiment_score": 0.0,
    "news_count": 0,
    "top_headlines": [],
    "sentiment_trend": "NO_DATA",
}


def _budget_ref():
    from datetime import datetime, timezone
    from shared.firestore_client import col
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return col("tavily_budget").document(today), today


async def _check_budget() -> bool:
    """True si hay presupuesto disponible."""
    try:
        from datetime import datetime, timezone
        ref, today = _budget_ref()
        doc = ref.get()
        if doc.exists:
            data = doc.to_dict()
            return int(data.get("calls_today", 0)) < int(data.get("limit", 30))
        return True
    except Exception:
        logger.error("_check_budget: error leyendo tavily_budget", exc_info=True)
        return False


async def _increment_budget() -> None:
    """Incrementa el contador de llamadas del dia."""
    try:
        from datetime import datetime, timezone
        from google.cloud.firestore import Increment
        ref, today = _budget_ref()
        doc = ref.get()
        if doc.exists:
            ref.update({"calls_today": Increment(1), "updated_at": datetime.now(timezone.utc)})
        else:
            ref.set({"date": today, "calls_today": 1, "limit": 30, "updated_at": datetime.now(timezone.utc)})
    except Exception:
        logger.error("_increment_budget: error actualizando tavily_budget", exc_info=True)


def _get_source_weight(url: str) -> float:
    for domain, weight in SOURCE_WEIGHTS.items():
        if domain in url:
            return weight
    return SOURCE_WEIGHTS["default"]


def _classify_sentiment(text: str) -> float:
    """Heuristica simple: cuenta palabras positivas y negativas."""
    positive = ["wins", "win", "gain", "rise", "rises", "up", "pass", "passes", "approve", "approved",
                "victory", "success", "confirms", "confirmed", "positive", "growth", "advance"]
    negative = ["loses", "lose", "loss", "fall", "falls", "down", "fail", "fails", "reject", "rejected",
                "defeat", "failure", "denies", "denied", "negative", "decline", "retreat", "crash"]
    text_lower = text.lower()
    pos = sum(1 for w in positive if w in text_lower)
    neg = sum(1 for w in negative if w in text_lower)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 3)


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
    try:
        has_budget = await _check_budget()
        if not has_budget:
            logger.info("fetch_news_sentiment: presupuesto Tavily agotado hoy")
            return dict(_NO_DATA)

        from shared.groq_client import _get_tavily
        tavily = _get_tavily()

        results = tavily.search(query=market_question, max_results=5)
        articles = results.get("results", [])
        if not articles:
            return dict(_NO_DATA)

        await _increment_budget()

        weighted_score = 0.0
        total_weight = 0.0
        headlines: list[str] = []

        for article in articles:
            url = article.get("url", "")
            title = article.get("title", "")
            content = article.get("content", "")
            weight = _get_source_weight(url)

            text = f"{title} {content}"
            sentiment = _classify_sentiment(text)
            weighted_score += sentiment * weight
            total_weight += weight

            if title and len(headlines) < 3:
                headlines.append(title)

        sentiment_score = round(weighted_score / total_weight, 3) if total_weight > 0 else 0.0

        if sentiment_score > 0.2:
            trend = "IMPROVING"
        elif sentiment_score < -0.2:
            trend = "DETERIORATING"
        else:
            trend = "STABLE"

        return {
            "sentiment_score": sentiment_score,
            "news_count": len(articles),
            "top_headlines": headlines,
            "sentiment_trend": trend,
        }

    except Exception:
        logger.error("fetch_news_sentiment(%s...): error", market_question[:50], exc_info=True)
        return dict(_NO_DATA)
