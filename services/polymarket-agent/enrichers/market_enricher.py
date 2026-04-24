"""
Orquestador de enrichers de mercado Polymarket.
Combina: price_tracker, orderbook, correlaciones, smart_money, news_sentiment.
"""
import logging
from datetime import datetime, timezone

from shared.firestore_client import col

logger = logging.getLogger(__name__)


async def enrich_market(market: dict, all_markets: list[dict] | None = None) -> dict:
    """
    Orquesta todos los enrichers. Guarda en Firestore enriched_markets.
    Output enriched_market incluye todos los campos de poly_markets mas:
      price_momentum: str
      volume_spike: bool
      smart_money: {is_smart_money, hours_before_news}
      orderbook: {buy_pressure, spread, depth, imbalance_signal}
      correlations: list[dict]
      arbitrage: {detected, inefficiency, direction}
      news_sentiment: {score, count, headlines, trend}
      data_quality: str  # "full" | "partial"
      enriched_at: datetime
    """
    from price_tracker import price_momentum, volume_spike, smart_money_detection
    from enrichers.orderbook_analyzer import analyze_orderbook
    from enrichers.correlation_detector import find_correlated_markets, detect_arbitrage
    from enrichers.news_sentiment import fetch_news_sentiment

    market_id = market.get("market_id", "")
    question = market.get("question", "")
    data_quality = "full"

    # 1. Price momentum
    try:
        momentum = await price_momentum(market_id)
    except Exception:
        logger.error("enrich_market(%s): error en price_momentum", market_id, exc_info=True)
        momentum = "STABLE"
        data_quality = "partial"

    # 2. Volume spike
    try:
        vol_spike = await volume_spike(market_id)
    except Exception:
        logger.error("enrich_market(%s): error en volume_spike", market_id, exc_info=True)
        vol_spike = False
        data_quality = "partial"

    # 3. Smart money (heuristica de precio)
    try:
        smart_money = await smart_money_detection(market_id)
    except Exception:
        logger.error("enrich_market(%s): error en smart_money_detection", market_id, exc_info=True)
        smart_money = {"is_smart_money": False, "hours_before_news": None}
        data_quality = "partial"

    # 4. Orderbook
    try:
        orderbook = await analyze_orderbook(market_id)
    except Exception:
        logger.error("enrich_market(%s): error en analyze_orderbook", market_id, exc_info=True)
        orderbook = {"buy_pressure": 0.5, "spread": 0.0, "depth": 0.0, "imbalance_signal": "NEUTRAL"}
        data_quality = "partial"

    # 5. Correlaciones (requiere lista de mercados activos)
    try:
        if all_markets is None:
            all_markets = []
        correlations = await find_correlated_markets(market_id, all_markets)
        arbitrage = detect_arbitrage(market, correlations)
    except Exception:
        logger.error("enrich_market(%s): error en correlations", market_id, exc_info=True)
        correlations = []
        arbitrage = {"detected": False, "inefficiency": 0.0, "direction": "NONE"}
        data_quality = "partial"

    # 6. News sentiment
    try:
        news = await fetch_news_sentiment(question)
    except Exception:
        logger.error("enrich_market(%s): error en fetch_news_sentiment", market_id, exc_info=True)
        news = {"sentiment_score": 0.0, "news_count": 0, "top_headlines": [], "sentiment_trend": "NO_DATA"}
        data_quality = "partial"

    enriched = {
        "market_id": market_id,
        "question": question,
        "volume_24h": float(market.get("volume_24h", 0)),
        "price_momentum": momentum,
        "volume_spike": vol_spike,
        "smart_money": smart_money,
        "orderbook": orderbook,
        "correlations": correlations,
        "arbitrage": arbitrage,
        "news_sentiment": {
            "score": news.get("sentiment_score", 0.0),
            "count": news.get("news_count", 0),
            "headlines": news.get("top_headlines", []),
            "trend": news.get("sentiment_trend", "NO_DATA"),
        },
        "data_quality": data_quality,
        "enriched_at": datetime.now(timezone.utc),
    }

    try:
        col("enriched_markets").document(market_id).set(enriched)
        logger.info("enrich_market(%s): guardado en Firestore (quality=%s)", market_id, data_quality)
    except Exception:
        logger.error("enrich_market(%s): error guardando en Firestore", market_id, exc_info=True)

    return enriched


async def run_enrichment(markets: list[dict] | None = None) -> int:
    """
    Procesa todos los mercados activos de Firestore poly_markets.
    Si se pasan markets como parametro, los usa directamente.
    Devuelve el numero de mercados enriquecidos.
    """
    try:
        if markets is None:
            docs = list(col("poly_markets").stream())
            markets = [d.to_dict() for d in docs]

        if not markets:
            logger.warning("run_enrichment: sin mercados activos")
            return 0

        count = 0
        for market in markets:
            try:
                await enrich_market(market, all_markets=markets)
                count += 1
            except Exception:
                logger.error(
                    "run_enrichment: error enriqueciendo %s",
                    market.get("market_id"), exc_info=True,
                )

        logger.info("run_enrichment: %d/%d mercados enriquecidos", count, len(markets))
        return count

    except Exception:
        logger.error("run_enrichment: error no controlado", exc_info=True)
        return 0
