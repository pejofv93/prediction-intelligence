"""
Detector de correlaciones entre mercados Polymarket.
Detecta mercados mutuamente excluyentes e ineficiencias de arbitraje.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Umbral minimo de palabras clave en comun para considerar mercados correlacionados
_MIN_SHARED_KEYWORDS = 2
# Palabras sin valor para correlacion
_STOPWORDS = {"will", "the", "a", "an", "in", "on", "at", "to", "by", "of",
              "and", "or", "is", "be", "win", "won", "does", "do", "get", "have",
              "has", "for", "with", "this", "that", "are", "was", "were", "its"}


def _extract_keywords(text: str) -> set[str]:
    """Extrae palabras clave significativas de un texto."""
    words = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
    return {w for w in words if w not in _STOPWORDS}


async def find_correlated_markets(
    market_id: str, all_markets: list[dict]
) -> list[dict]:
    """
    Detecta mercados correlacionados por keywords.
    Ej: "Biden gana" <-> "Trump gana" son mutuamente excluyentes.
    """
    try:
        # Buscar el mercado base
        base_market = None
        for m in all_markets:
            if m.get("market_id") == market_id:
                base_market = m
                break

        if base_market is None:
            return []

        base_keywords = _extract_keywords(base_market.get("question", ""))
        if not base_keywords:
            return []

        correlated: list[dict] = []
        for m in all_markets:
            if m.get("market_id") == market_id:
                continue
            other_keywords = _extract_keywords(m.get("question", ""))
            shared = base_keywords & other_keywords
            if len(shared) >= _MIN_SHARED_KEYWORDS:
                correlated.append({
                    "market_id": m.get("market_id"),
                    "question": m.get("question", ""),
                    "price_yes": float(m.get("price_yes", 0.5)),
                    "shared_keywords": list(shared),
                })

        return correlated

    except Exception:
        logger.error("find_correlated_markets(%s): error", market_id, exc_info=True)
        return []


def detect_arbitrage(market: dict, correlated: list[dict]) -> dict:
    """
    Probabilidad = market["price_yes"] y correlated[i]["price_yes"].
    Si sum(price_yes de mercados mutuamente excluyentes) != 1.0 → ineficiencia.
    inefficiency = abs(1.0 - total_prob).
    direction = "OVERPRICED" si total > 1, "UNDERPRICED" si total < 1.
    Devuelve {"detected": bool, "inefficiency": float, "direction": str}.
    Clave "detected" (no "arbitrage_detected") — coincidir con enriched_markets.arbitrage schema.
    """
    _neutral = {"detected": False, "inefficiency": 0.0, "direction": "NONE"}
    try:
        if not correlated:
            return _neutral

        base_price = float(market.get("price_yes", 0.5))
        total_prob = base_price + sum(float(c.get("price_yes", 0.5)) for c in correlated)

        inefficiency = round(abs(1.0 - total_prob), 4)
        # Solo reportar si la ineficiencia es significativa (> 3%)
        if inefficiency < 0.03:
            return _neutral

        direction = "OVERPRICED" if total_prob > 1.0 else "UNDERPRICED"
        return {
            "detected": True,
            "inefficiency": inefficiency,
            "direction": direction,
        }

    except Exception:
        logger.error("detect_arbitrage: error", exc_info=True)
        return _neutral
