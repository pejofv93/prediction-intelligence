"""
Sentiment de noticias via DuckDuckGo (sin API key, sin límites de uso).
"""
import asyncio
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


def _get_source_weight(url: str) -> float:
    for domain, weight in SOURCE_WEIGHTS.items():
        if domain in url:
            return weight
    return SOURCE_WEIGHTS["default"]


def _classify_sentiment(text: str) -> float:
    """Heurística simple: cuenta palabras positivas y negativas."""
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


def _fetch_ddg_news(query: str) -> list[dict]:
    """Síncrono — se llama desde run_in_executor para no bloquear el event loop."""
    from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        return list(ddgs.news(query, max_results=5))


async def fetch_news_sentiment(market_question: str) -> dict:
    """
    Busca noticias con DuckDuckGo y calcula sentiment ponderado por fuente.
    Sin API key, sin límite de llamadas.
    Devuelve:
      sentiment_score: float (-1.0 a 1.0)
      news_count: int
      top_headlines: list[str] (max 3)
      sentiment_trend: "IMPROVING" | "DETERIORATING" | "STABLE" | "NO_DATA"
    """
    try:
        loop = asyncio.get_event_loop()
        articles = await loop.run_in_executor(None, _fetch_ddg_news, market_question)

        if not articles:
            return dict(_NO_DATA)

        weighted_score = 0.0
        total_weight = 0.0
        headlines: list[str] = []

        for article in articles:
            url = article.get("url", "")
            title = article.get("title", "")
            body = article.get("body", "")
            weight = _get_source_weight(url)

            text = f"{title} {body}"
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

        logger.info(
            "fetch_news_sentiment(%s...): %d artículos, score=%.3f trend=%s",
            market_question[:40], len(articles), sentiment_score, trend,
        )

        return {
            "sentiment_score": sentiment_score,
            "news_count": len(articles),
            "top_headlines": headlines,
            "sentiment_trend": trend,
        }

    except Exception:
        logger.error("fetch_news_sentiment(%s...): error", market_question[:50], exc_info=True)
        return dict(_NO_DATA)
