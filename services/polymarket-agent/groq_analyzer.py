"""
Analizador Groq para mercados Polymarket enriched.
Recibe enriched_market → prob real + edge + reasoning.
"""
import logging

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE
from shared.groq_client import GROQ_CALL_DELAY

logger = logging.getLogger(__name__)

CATEGORY_KEYWORDS = {
    "crypto": ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "defi", "blockchain", "halving", "altcoin"],
    "politics": ["election", "president", "vote", "congress", "senate", "minister", "parliament", "poll", "referendum", "prime minister", "chancellor"],
    "economy": ["fed", "interest rate", "inflation", "cpi", "gdp", "recession", "unemployment", "federal reserve", "rate hike", "rate cut", "jerome powell"],
    "sports": ["world cup", "champions league", "nba", "super bowl", "final", "tournament", "championship", "league", "nfl", "mlb", "wimbledon", "olympic"],
    "geopolitics": ["war", "ceasefire", "conflict", "nato", "military", "invasion", "sanctions", "treaty", "diplomacy", "nuclear"],
}


def categorize_market(question: str) -> str:
    """Categoriza un mercado Polymarket según su pregunta. Devuelve categoria o 'other'."""
    q_lower = question.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in q_lower for kw in keywords):
            return category
    return "other"


def _build_category_context(question: str, category: str) -> str:
    """
    Construye contexto adicional para el prompt según categoría.
    Solo añade instrucciones — no hace web_search real (eso requiere async Tavily).
    """
    if category == "crypto":
        return (
            "CONTEXTO CRYPTO: Analiza si el precio del activo cripto relevante "
            "soporta o contradice la probabilidad de mercado. "
            "Considera volatilidad histórica, halvings, ciclos de mercado. "
            "Si el precio spot contradice la probabilidad (>15% divergencia), señala como ineficiencia."
        )
    elif category == "politics":
        return (
            "CONTEXTO POLÍTICO: Considera sesgo de mercado hacia candidatos mainstream. "
            "Los mercados políticos suelen sobreestimar incumbentes y subestimar outsiders. "
            "Busca divergencias entre encuestas recientes y precio de mercado."
        )
    elif category == "economy":
        return (
            "CONTEXTO ECONÓMICO: El mercado Fed Funds Futures (CME FedWatch) "
            "es la referencia más fiable para decisiones de tipos. "
            "Considera datos macro recientes: CPI, PCE, empleos no agrícolas. "
            "Si el mercado diverge >10% de CME FedWatch, hay ineficiencia."
        )
    elif category == "sports":
        return (
            "CONTEXTO DEPORTIVO: Considera forma reciente de equipos/jugadores, "
            "cuotas de casas de apuestas como referencia de probabilidad real. "
            "Los mercados deportivos en Polymarket suelen ser menos eficientes "
            "porque los participantes son menos especializados."
        )
    elif category == "geopolitics":
        return (
            "CONTEXTO GEOPOLÍTICO: Eventos de alta incertidumbre. "
            "Sé conservador: recomienda WATCH más que BUY salvo evidencia muy clara. "
            "El mercado suele sobreestimar resolución rápida de conflictos."
        )
    return ""


SYSTEM_PROMPT = (
    "Eres un analista cuantitativo especializado en encontrar ineficiencias en mercados de prediccion. "
    "Tu objetivo es detectar DIVERGENCIAS entre el precio de mercado y la probabilidad real. "
    "Los mercados de Polymarket son frecuentemente INEFICIENTES: "
    "el precio YES no refleja correctamente la probabilidad real por sesgos cognitivos, "
    "baja liquidez, reaccion exagerada a noticias recientes o manipulacion de order book. "
    "BUSCA ACTIVAMENTE estas ineficiencias. "
    "Si el precio YES es 0.30 pero los fundamentales apuntan a 0.45, edge = +0.15 — es una oportunidad. "
    "Si el precio YES es 0.70 pero la evidencia es debil, edge = -0.15 — es oportunidad en NO. "
    "No seas conservador: los mercados eficientes no existen en Polymarket. "
    "Analiza: (1) buy_pressure del orderbook vs precio, (2) momentum del precio, "
    "(3) smart money, (4) sentiment de noticias vs precio, (5) arbitrage signals. "
    "Responde SOLO en JSON valido: "
    '{"real_prob": float, "edge": float, "confidence": float, '
    '"trend": "RISING|FALLING|STABLE", "recommendation": "BUY_YES|BUY_NO|PASS|WATCH", '
    '"key_factors": list[str], "reasoning": string} '
    "donde edge = real_prob - market_price_yes (positivo = comprar YES, negativo = comprar NO)."
)


async def analyze_market(enriched_market: dict) -> dict | None:
    """
    Solo analiza si: volume_24h > 5000 AND days_to_close > 2.
    NO usa web_search (news_sentiment ya viene del enricher).
    Al llamar en batch: await asyncio.sleep(GROQ_CALL_DELAY) entre cada mercado.
    Al guardar en poly_predictions copiar desde enriched_market:
      poly_prediction["volume_spike"] = enriched_market["volume_spike"]
      poly_prediction["smart_money_detected"] = enriched_market["smart_money"]["is_smart_money"]
    Guarda resultado en Firestore poly_predictions.
    """
    import asyncio
    import json
    import re
    from datetime import datetime, timezone
    from shared.firestore_client import col
    from shared.groq_client import _get_groq, GROQ_CALL_DELAY
    from shared.config import GROQ_MODEL, GROQ_FALLBACK_MODEL

    market_id = enriched_market.get("market_id", "")

    # Filtros de volumen y dias al cierre
    try:
        market_doc = col("poly_markets").document(market_id).get()
        if not market_doc.exists:
            logger.debug("analyze_market(%s): mercado no encontrado en poly_markets", market_id)
            return None
        market_data = market_doc.to_dict()
    except Exception:
        logger.error("analyze_market(%s): error leyendo poly_markets", market_id, exc_info=True)
        return None

    volume_24h = float(market_data.get("volume_24h", 0))
    if volume_24h < 5000:
        logger.debug("analyze_market(%s): volumen insuficiente (%.0f)", market_id, volume_24h)
        return None

    end_date = market_data.get("end_date")
    if end_date:
        if hasattr(end_date, "tzinfo") and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        days_to_close = (end_date - datetime.now(timezone.utc)).days
        if days_to_close <= 2:
            logger.debug("analyze_market(%s): cierra en %d dias — omitiendo", market_id, days_to_close)
            return None
    else:
        days_to_close = 999  # sin fecha de cierre = asumimos que no cierra pronto

    question = market_data.get("question", "mercado desconocido")
    # Preferir precio del enriched_market (más reciente) sobre el guardado en Firestore,
    # que puede tener el bug de 0.5 por defecto si el scanner falló al leer outcomePrices.
    price_yes = float(
        enriched_market.get("price_yes") or market_data.get("price_yes") or 0.5
    )

    category = categorize_market(question)
    category_context = _build_category_context(question, category)

    # Fear & Greed para mercados crypto
    fear_greed: dict = {}
    if category == "crypto":
        try:
            from realtime.binance_tracker import get_fear_greed
            fear_greed = await get_fear_greed()
            fg_value = fear_greed.get("value", 50)
            fg_label = fear_greed.get("label", "Neutral")
            fg_trend = fear_greed.get("trend", "NEUTRAL")
            fear_greed_line = f"Fear & Greed Index: {fg_value} ({fg_label}) — tendencia: {fg_trend}\n"
            logger.debug("groq_analyzer: Fear&Greed=%d (%s)", fg_value, fg_label)
        except Exception as _fge:
            fear_greed_line = ""
            logger.debug("groq_analyzer: error obteniendo Fear&Greed — %s", _fge)
    else:
        fear_greed_line = ""

    # Construir user_prompt con todos los datos del enriched_market
    orderbook = enriched_market.get("orderbook", {})
    news = enriched_market.get("news_sentiment", {})
    smart_money = enriched_market.get("smart_money", {})
    arbitrage = enriched_market.get("arbitrage", {})

    user_prompt = (
        f"Mercado: {question}\n"
        f"Precio actual YES: {price_yes:.3f} (= {price_yes*100:.1f}%)\n"
        f"Volumen 24h: ${volume_24h:,.0f}\n"
        f"Dias al cierre: {days_to_close}\n"
        f"Momentum de precio: {enriched_market.get('price_momentum', 'STABLE')}\n"
        f"Volume spike: {enriched_market.get('volume_spike', False)}\n"
        f"Smart money detectado: {smart_money.get('is_smart_money', False)}\n"
        f"Order book — buy_pressure: {orderbook.get('buy_pressure', 0.5):.3f}, "
        f"spread: {orderbook.get('spread', 0):.4f}, "
        f"imbalance: {orderbook.get('imbalance_signal', 'NEUTRAL')}\n"
        f"Correlaciones: {len(enriched_market.get('correlations', []))} mercados relacionados\n"
        f"Arbitrage: detected={arbitrage.get('detected', False)}, "
        f"inefficiency={arbitrage.get('inefficiency', 0):.3f}\n"
        f"Sentiment noticias: score={news.get('score', 0):.2f}, "
        f"trend={news.get('trend', 'NO_DATA')}, "
        f"titulares={news.get('headlines', [])[:2]}\n"
        f"\nEl precio de mercado YES = {price_yes:.3f}. "
        f"Estima la probabilidad REAL de YES basandote en todos los datos. "
        f"Si buy_pressure > 0.6 y momentum es RISING, el mercado puede estar subvaluado. "
        f"Si buy_pressure < 0.4 y momentum es FALLING, puede estar sobrevaluado. "
        f"Si smart_money = True, hay informacion privilegiada — ajusta real_prob significativamente. "
        f"Si arbitrage.detected = True, hay ineficiencia confirmada — usa inefficiency como lower bound del edge. "
        f"Sé explícito sobre la divergencia: edge = real_prob - {price_yes:.3f}. "
        f"Un edge de 0.00 o cercano a cero indica mercado eficiente — justificalo con argumentos solidos."
    )
    if fear_greed_line:
        user_prompt += f"\n{fear_greed_line}"
    if category_context:
        user_prompt += f"\n\nCONTEXTO ADICIONAL:\n{category_context}"

    # Llamada a Groq con manejo de JSON mal formateado
    raw_response = ""
    groq_client = _get_groq()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    groq_tpd = False
    for attempt, model in enumerate([GROQ_MODEL, GROQ_FALLBACK_MODEL]):
        try:
            if attempt > 0:
                # Segundo intento: instruccion mas explicita
                messages[-1]["content"] += "\n\nResponde SOLO JSON, sin texto adicional."
            resp = groq_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=500,
                temperature=0.3,
            )
            raw_response = resp.choices[0].message.content
            break
        except Exception as e:
            err_str = str(e).lower()
            if "model_not_found" in err_str or "404" in err_str:
                continue
            if "429" in err_str or "rate_limit" in err_str or "quota" in err_str or "daily" in err_str:
                groq_tpd = True
                logger.warning("analyze_market(%s): Groq TPD agotado en %s — intentando Claude Haiku", market_id, model)
                break
            logger.error("analyze_market(%s): error Groq — %s", market_id, e, exc_info=True)
            return None

    # Fallback Claude Haiku cuando Groq agota el límite diario de tokens
    if not raw_response and groq_tpd:
        import os as _os
        _anthropic_key = _os.environ.get("ANTHROPIC_API_KEY", "")
        if not _anthropic_key:
            logger.warning("analyze_market(%s): ANTHROPIC_API_KEY no configurada — sin fallback Haiku", market_id)
            return None
        try:
            import anthropic as _anthropic
            _claude = _anthropic.Anthropic(api_key=_anthropic_key)
            _claude_resp = _claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": messages[-1]["content"]}],
            )
            raw_response = _claude_resp.content[0].text
            logger.info("analyze_market(%s): Claude Haiku fallback exitoso", market_id)
        except Exception as _ce:
            logger.error("analyze_market(%s): error Claude Haiku fallback — %s", market_id, _ce, exc_info=True)
            return None

    if not raw_response:
        logger.error("analyze_market(%s): sin respuesta de ningún modelo", market_id)
        return None

    # Extraer JSON de la respuesta
    result: dict | None = None
    for extractor in [
        lambda r: json.loads(r),
        lambda r: json.loads(re.search(r"\{.*\}", r, re.DOTALL).group()),
    ]:
        try:
            result = extractor(raw_response)
            break
        except Exception:
            continue

    if result is None:
        logger.error("analyze_market(%s): no se pudo parsear JSON de Groq: %s", market_id, raw_response[:200])
        return None

    # Construir documento poly_prediction
    real_prob = float(result.get("real_prob", price_yes))
    edge = float(result.get("edge", real_prob - price_yes))
    confidence = float(result.get("confidence", 0.5))
    trend = result.get("trend", enriched_market.get("price_momentum", "STABLE"))
    recommendation = result.get("recommendation", "PASS")
    key_factors = result.get("key_factors", [])
    reasoning = result.get("reasoning", "")

    # Aplicar ajuste Fear & Greed si es crypto
    if fear_greed and category == "crypto":
        try:
            from realtime.binance_tracker import apply_fear_greed_to_signal
            _tmp = {"confidence": confidence}
            _tmp = apply_fear_greed_to_signal(_tmp, fear_greed, recommendation)
            confidence = float(_tmp.get("confidence", confidence))
        except Exception as _fga:
            logger.debug("groq_analyzer: error aplicando F&G — %s", _fga)

    prediction = {
        "market_id": market_id,
        "question": question,
        "market_price_yes": price_yes,
        "real_prob": round(real_prob, 4),
        "edge": round(edge, 4),
        "confidence": round(confidence, 4),
        "trend": trend,
        "recommendation": recommendation,
        "key_factors": key_factors[:5] if key_factors else [],
        "reasoning": reasoning[:1000] if reasoning else "",
        "volume_spike": bool(enriched_market.get("volume_spike", False)),
        "smart_money_detected": bool(smart_money.get("is_smart_money", False)),
        "category": category,
        "fear_greed_index": fear_greed.get("value") if fear_greed else None,
        "fear_greed_label": fear_greed.get("label") if fear_greed else None,
        "analyzed_at": datetime.now(timezone.utc),
        "alerted": False,
    }

    try:
        col("poly_predictions").document(market_id).set(prediction)
        logger.info(
            "analyze_market(%s): guardado — edge=%.3f conf=%.2f rec=%s cat=%s",
            market_id, edge, confidence, recommendation, category,
        )
    except Exception:
        logger.error("analyze_market(%s): error guardando poly_predictions", market_id, exc_info=True)

    return prediction


async def run_maintenance() -> None:
    """
    Ejecutar al final de cada /run-analyze.
    1. Borrar poly_price_history donde timestamp < now - 30 dias (batch delete)
    2. Borrar enriched_markets donde enriched_at < now - 7 dias
    Usar batch writes de Firestore (max 500 ops/batch) para no exceder limites.
    """
    from datetime import datetime, timedelta, timezone
    from shared.firestore_client import col, get_client

    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    cutoff_7d = now - timedelta(days=7)
    db = get_client()

    async def _batch_delete(query, label: str) -> int:
        total = 0
        while True:
            docs = list(query.limit(500).stream())
            if not docs:
                break
            batch = db.batch()
            for d in docs:
                batch.delete(d.reference)
            try:
                batch.commit()
                total += len(docs)
            except Exception:
                logger.error("run_maintenance: error en batch delete %s", label, exc_info=True)
                break
        return total

    try:
        deleted_history = await _batch_delete(
            col("poly_price_history").where("timestamp", "<", cutoff_30d),
            "poly_price_history",
        )
        logger.info("run_maintenance: %d docs poly_price_history eliminados (>30d)", deleted_history)
    except Exception:
        logger.error("run_maintenance: error limpiando poly_price_history", exc_info=True)

    try:
        deleted_enriched = await _batch_delete(
            col("enriched_markets").where("enriched_at", "<", cutoff_7d),
            "enriched_markets",
        )
        logger.info("run_maintenance: %d docs enriched_markets eliminados (>7d)", deleted_enriched)
    except Exception:
        logger.error("run_maintenance: error limpiando enriched_markets", exc_info=True)
