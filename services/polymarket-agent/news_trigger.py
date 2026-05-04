"""
News trigger — busca breaking news para los top 10 mercados activos
y fuerza re-análisis via POST /analyze-urgent si hay impacto alto.
Ejecutado cada 30min por GitHub Actions (poly-news-trigger.yml).
"""
import asyncio
import logging
import os

import httpx

from shared.firestore_client import col

logger = logging.getLogger(__name__)

_MAX_MARKETS = 10
_POLY_AGENT_URL = os.environ.get("POLY_AGENT_URL", "")
_CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")

_HIGH_IMPACT_KEYWORDS = [
    "breaking", "urgente", "alert", "shock", "crash", "surge", "unexpected",
    "ceasefire", "war", "invasion", "election", "deal", "verdict", "agreement",
    "sanctions", "killed", "resigned", "arrested", "banned", "fired",
    "approved", "rejected", "default", "collapse", "explosion",
]


def _is_high_impact(headlines: list[str]) -> bool:
    text = " ".join(headlines).lower()
    return any(kw in text for kw in _HIGH_IMPACT_KEYWORDS)


async def run_news_trigger() -> dict:
    """
    1. Lee top 10 mercados activos por volumen desde poly_markets.
    2. Para cada uno: busca noticias con DuckDuckGo.
    3. Si high-impact → POST /analyze-urgent?market_id={id} al polymarket-agent.
    """
    triggered = 0
    checked = 0
    errors = 0
    try:
        docs = list(
            col("poly_markets")
            .order_by("volume_24h", direction="DESCENDING")
            .limit(_MAX_MARKETS)
            .stream()
        )
        markets = [d.to_dict() for d in docs]
        if not markets:
            logger.warning("news_trigger: poly_markets vacía o sin datos de volumen")
            return {"checked": 0, "triggered": 0, "errors": 0}

        from ddgs import DDGS
        ddgs = DDGS()

        async with httpx.AsyncClient(timeout=20.0) as client:
            for m in markets:
                market_id = m.get("market_id", "")
                question = (m.get("question") or "")[:100]
                if not market_id or not question:
                    continue

                checked += 1
                try:
                    results = ddgs.news(question, max_results=5)
                    headlines = [r.get("title", "") for r in (results or [])]

                    if not _is_high_impact(headlines):
                        logger.debug("news_trigger(%s): sin breaking news", market_id[:12])
                        continue

                    logger.info(
                        "news_trigger(%s): HIGH IMPACT — %s",
                        market_id[:12], headlines[:2],
                    )

                    if not _POLY_AGENT_URL or not _CLOUD_RUN_TOKEN:
                        logger.warning("news_trigger: POLY_AGENT_URL o CLOUD_RUN_TOKEN no configurados")
                        continue

                    resp = await client.post(
                        f"{_POLY_AGENT_URL}/analyze-urgent",
                        params={"market_id": market_id},
                        headers={"x-cloud-token": _CLOUD_RUN_TOKEN},
                        timeout=30.0,
                    )
                    if resp.status_code in (200, 202):
                        triggered += 1
                        logger.info("news_trigger(%s): re-análisis urgente lanzado", market_id[:12])
                    else:
                        logger.warning(
                            "news_trigger(%s): analyze-urgent devolvió %d",
                            market_id[:12], resp.status_code,
                        )

                except Exception as _me:
                    errors += 1
                    logger.warning("news_trigger(%s): error — %s", market_id[:12], _me)

    except Exception:
        logger.error("news_trigger: error no controlado", exc_info=True)
        errors += 1

    logger.info("news_trigger: checked=%d triggered=%d errors=%d", checked, triggered, errors)
    return {"checked": checked, "triggered": triggered, "errors": errors}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        print("ERROR: GOOGLE_CLOUD_PROJECT no definido")
        sys.exit(1)
    result = asyncio.run(run_news_trigger())
    print(f"checked={result['checked']} triggered={result['triggered']} errors={result['errors']}")
