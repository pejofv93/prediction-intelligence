"""
Analizador Groq para mercados Polymarket enriched.
Recibe enriched_market → prob real + edge + reasoning.
"""
import logging

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE
from shared.groq_client import GROQ_CALL_DELAY

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Eres un analista experto en mercados de prediccion. "
    "Se te proporciona un mercado de Polymarket con datos estadisticos completos: "
    "historial de precios, order book, smart money, correlaciones y sentiment de noticias. "
    "Integra TODOS los datos. Responde SOLO en JSON: "
    '{"real_prob": float, "edge": float, "confidence": float, '
    '"trend": "RISING|FALLING|STABLE", "recommendation": "BUY_YES|BUY_NO|PASS|WATCH", '
    '"key_factors": list[str], "reasoning": string}'
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
    price_yes = float(market_data.get("price_yes", 0.5))

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
        f"\nAnaliza todos estos datos y estima la probabilidad real de YES. "
        f"Proporciona edge = real_prob - {price_yes:.3f}."
    )

    # Llamada a Groq con manejo de JSON mal formateado
    raw_response = ""
    groq_client = _get_groq()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    for attempt, model in enumerate([GROQ_MODEL, GROQ_FALLBACK_MODEL]):
        try:
            if attempt > 0:
                # Segundo intento: instruccion mas explicita
                messages[-1]["content"] += "\n\nResponde SOLO JSON, sin texto adicional."
            resp = groq_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=1024,
                temperature=0.3,
            )
            raw_response = resp.choices[0].message.content
            break
        except Exception as e:
            if "model_not_found" in str(e).lower() or "404" in str(e):
                continue
            logger.error("analyze_market(%s): error Groq — %s", market_id, e, exc_info=True)
            return None

    if not raw_response:
        logger.error("analyze_market(%s): sin respuesta de Groq", market_id)
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
        "reasoning": reasoning[:500] if reasoning else "",
        "volume_spike": bool(enriched_market.get("volume_spike", False)),
        "smart_money_detected": bool(smart_money.get("is_smart_money", False)),
        "analyzed_at": datetime.now(timezone.utc),
        "alerted": False,
    }

    try:
        col("poly_predictions").document(market_id).set(prediction)
        logger.info(
            "analyze_market(%s): guardado — edge=%.3f conf=%.2f rec=%s",
            market_id, edge, confidence, recommendation,
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
