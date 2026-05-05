"""
Analizador Groq para mercados Polymarket enriched.
Recibe enriched_market → prob real + edge + reasoning.
"""
import logging
import re
import time as _time

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE
from shared.groq_client import GROQ_CALL_DELAY

# ---------------------------------------------------------------------------
# Cuota Groq — estado persistido en Firestore agent_state/groq_quota
# ---------------------------------------------------------------------------
_QUOTA_CACHE: dict = {}
_QUOTA_CACHE_TS: float = 0.0
_QUOTA_CACHE_TTL: float = 300.0  # re-read Firestore cada 5 min

# Cache de poly_model_weights para calibración (Fix 4 + Fix 7)
_WEIGHTS_CACHE: dict = {}
_WEIGHTS_CACHE_TS: float = 0.0
_WEIGHTS_CACHE_TTL: float = 600.0  # 10 min


def _is_groq_quota_exhausted() -> bool:
    """Lee agent_state/groq_quota. True si TPD agotado y aún no ha pasado la medianoche UTC."""
    global _QUOTA_CACHE, _QUOTA_CACHE_TS
    from datetime import datetime, timezone
    from shared.firestore_client import col
    now_ts = _time.monotonic()
    now_utc = datetime.now(timezone.utc)
    if now_ts - _QUOTA_CACHE_TS < _QUOTA_CACHE_TTL and _QUOTA_CACHE:
        resets_at = _QUOTA_CACHE.get("resets_at")
        if resets_at:
            if hasattr(resets_at, "tzinfo") and resets_at.tzinfo is None:
                resets_at = resets_at.replace(tzinfo=timezone.utc)
            if now_utc >= resets_at:
                _QUOTA_CACHE = {}
                return False
        return bool(_QUOTA_CACHE.get("exhausted"))
    try:
        doc = col("agent_state").document("groq_quota").get()
        _QUOTA_CACHE = doc.to_dict() if doc.exists else {}
        _QUOTA_CACHE_TS = now_ts
        resets_at = _QUOTA_CACHE.get("resets_at")
        if resets_at:
            if hasattr(resets_at, "tzinfo") and resets_at.tzinfo is None:
                resets_at = resets_at.replace(tzinfo=timezone.utc)
            if now_utc >= resets_at:
                _QUOTA_CACHE = {}
                return False
        return bool(_QUOTA_CACHE.get("exhausted"))
    except Exception:
        return False


def _set_groq_quota_exhausted() -> None:
    """Persiste el estado TPD exhausted en Firestore (reset a medianoche UTC)."""
    global _QUOTA_CACHE, _QUOTA_CACHE_TS
    from datetime import datetime, timedelta, timezone
    from shared.firestore_client import col
    now = datetime.now(timezone.utc)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    doc = {"exhausted": True, "exhausted_at": now, "resets_at": next_midnight}
    try:
        col("agent_state").document("groq_quota").set(doc)
        _QUOTA_CACHE = doc
        _QUOTA_CACHE_TS = _time.monotonic()
        logger.warning(
            "groq_analyzer: TPD agotado en todos los modelos — "
            "persistido en agent_state/groq_quota (reset %s UTC)",
            next_midnight.isoformat(),
        )
    except Exception:
        logger.error("groq_analyzer: error persistiendo quota state", exc_info=True)


def _get_poly_weights() -> dict:
    """Lee poly_model_weights/current con caché de 10 min."""
    global _WEIGHTS_CACHE, _WEIGHTS_CACHE_TS
    from shared.firestore_client import col
    now_ts = _time.monotonic()
    if now_ts - _WEIGHTS_CACHE_TS < _WEIGHTS_CACHE_TTL and _WEIGHTS_CACHE:
        return _WEIGHTS_CACHE
    try:
        doc = col("poly_model_weights").document("current").get()
        _WEIGHTS_CACHE = doc.to_dict() if doc.exists else {}
        _WEIGHTS_CACHE_TS = now_ts
    except Exception:
        pass
    return _WEIGHTS_CACHE

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


def _get_current_crypto_price(question: str, enriched_market: dict | None = None) -> float | None:
    """
    Precio spot del activo crypto.
    Lee de enriched_market['ctc_price'] si está disponible (ya fetcheado por el enricher).
    Evita fetch HTTP separado que genera rate-limits.
    """
    if enriched_market:
        price = enriched_market.get("ctc_price")
        if price:
            try:
                return float(price)
            except (TypeError, ValueError):
                pass
    return None


def _extract_target_price(question: str) -> float | None:
    """Extrae precio objetivo de preguntas tipo 'BTC to $250,000?' → 250000.0"""
    import re
    patterns = [
        r'\$([\d,]+)[kK]',          # $250k
        r'\$([\d,]+(?:\.\d+)?)',      # $250,000 or $250000
        r'([\d,]+)[kK]\s*(?:usd|USD)',  # 250k USD
    ]
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            raw = match.group(1).replace(',', '')
            try:
                val = float(raw)
                if '[kK]' in pattern or 'k' in match.group(0).lower():
                    val *= 1000
                if 50 < val < 1e8:
                    return val
            except Exception:
                continue
    return None


def _validate_crypto_price_prediction(
    question: str,
    real_prob: float,
    market_price_yes: float,
    days_to_close: int,
    reasoning: str,
    current_price: float | None = None,
) -> tuple[float, float, str]:
    """
    Aplica caps de probabilidad para predicciones de precio crypto históricamente improbables.
    Caps:
      variación > 200% en cualquier plazo  → prob máxima 0.15
      variación > 100% en < 12 meses      → prob máxima 0.25
      variación > 50%  en < 3 meses       → prob máxima 0.35
    Retorna (real_prob_ajustada, edge_ajustado, reasoning_actualizado).
    current_price: precio spot del activo, leído de enriched_market['ctc_price'].
    """
    if current_price is None:
        return real_prob, round(real_prob - market_price_yes, 4), reasoning

    target_price = _extract_target_price(question)
    if target_price is None:
        return real_prob, round(real_prob - market_price_yes, 4), reasoning

    variation = (target_price - current_price) / current_price
    abs_var = abs(variation)

    max_prob = 1.0
    cap_note = ""

    if variation < 0:
        # Caps para predicciones de bajada (independientes del plazo)
        if abs_var > 0.80:
            max_prob = 0.10
            cap_note = f"caída requerida {variation:+.0%} > 80%"
        elif abs_var > 0.70:
            max_prob = 0.20
            cap_note = f"caída requerida {variation:+.0%} > 70%"
        elif abs_var > 0.50:
            max_prob = 0.30
            cap_note = f"caída requerida {variation:+.0%} > 50%"
    else:
        # Caps para predicciones de subida
        if abs_var > 2.0:
            max_prob = 0.15
            cap_note = f"variación requerida {variation:+.0%} > 200% en cualquier plazo"
        elif abs_var > 1.0 and days_to_close < 365:
            max_prob = 0.25
            cap_note = f"variación requerida {variation:+.0%} en {days_to_close}d (< 12 meses)"
        elif abs_var > 0.5 and days_to_close < 90:
            max_prob = 0.35
            cap_note = f"variación requerida {variation:+.0%} en {days_to_close}d (< 3 meses)"

    if max_prob < 1.0 and real_prob > max_prob:
        old_prob = real_prob
        real_prob = max_prob
        note = (
            f"⚠️ Ajuste por magnitud aplicado: {cap_note}. "
            f"Prob. máxima = {max_prob:.0%} (LLM estimó {old_prob:.0%}). "
            f"Precio actual ~${current_price:,.0f} → objetivo ${target_price:,.0f}"
        )
        reasoning = f"{reasoning}\n{note}" if reasoning else note
        logger.info(
            "validate_crypto(%s): prob %.2f→%.2f cap=%.2f var=%+.0f%%",
            question[:50], old_prob, real_prob, max_prob, variation * 100,
        )

    edge = round(real_prob - market_price_yes, 4)
    return real_prob, edge, reasoning


def _build_category_context(question: str, category: str) -> str:
    """
    Construye contexto adicional para el prompt según categoría.
    Solo añade instrucciones contextuales — el news_sentiment ya viene del enricher (DuckDuckGo).
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


def _clean_contradictory_reasoning(recommendation: str, reasoning: str) -> str:
    """
    Elimina frases del reasoning que contradigan la recommendation final.
    Si recommendation=BUY_NO pero el texto dice "recomendar BUY_YES" o variantes
    positivas → reemplaza con nota aclaratoria.
    """
    if not reasoning:
        return reasoning

    r_lower = reasoning.lower()

    _BUY_YES_PHRASES = [
        "recomendar buy_yes", "recomiendo buy_yes", "recomienda buy_yes",
        "oportunidad de compra yes", "comprar yes", "apostar por yes",
        "buy yes", "buy_yes recomendado",
    ]
    _BUY_NO_PHRASES = [
        "recomendar buy_no", "recomiendo buy_no", "recomienda buy_no",
        "oportunidad de compra no", "comprar no", "apostar por no",
        "buy no", "buy_no recomendado",
    ]

    if recommendation == "BUY_NO":
        has_contradiction = any(p in r_lower for p in _BUY_YES_PHRASES)
        if has_contradiction:
            logger.warning(
                "_clean_contradictory_reasoning: reasoning contradice BUY_NO — limpiando"
            )
            cleaned = re.sub(
                r'(?i)(recomendar|recomiendo|recomienda)\s+buy[_\s]?yes\b[^.]*\.',
                "[contradicción eliminada: el edge negativo indica BUY_NO].",
                reasoning,
            )
            cleaned = re.sub(
                r'(?i)oportunidad\s+de\s+compra\s+yes\b[^.]*\.',
                "[contradicción eliminada: mercado sobrevaluado según análisis cuantitativo].",
                cleaned,
            )
            return cleaned

    if recommendation == "BUY_YES":
        has_contradiction = any(p in r_lower for p in _BUY_NO_PHRASES)
        if has_contradiction:
            logger.warning(
                "_clean_contradictory_reasoning: reasoning contradice BUY_YES — limpiando"
            )
            cleaned = re.sub(
                r'(?i)(recomendar|recomiendo|recomienda)\s+buy[_\s]?no\b[^.]*\.',
                "[contradicción eliminada: el edge positivo indica BUY_YES].",
                reasoning,
            )
            return cleaned

    return reasoning


def _validate_prob_in_reasoning(real_prob: float, reasoning: str) -> str:
    """
    Extract probability mentions from reasoning text and compare against real_prob.
    If any mention diverges by >0.10, log a warning and prepend a disambiguation note
    so the Telegram message unambiguously shows the authoritative JSON value.
    """
    if not reasoning:
        return reasoning

    candidates: list[float] = []
    for m in re.finditer(r'\b(0\.\d{2,3})\b', reasoning):
        val = float(m.group(1))
        if 0.05 < val < 0.95:
            candidates.append(val)
    for m in re.finditer(r'\b(\d{1,2}(?:\.\d+)?)\s*%', reasoning):
        val = float(m.group(1)) / 100
        if 0.05 < val < 0.95:
            candidates.append(val)

    if not candidates:
        return reasoning

    max_delta = max(abs(c - real_prob) for c in candidates)
    if max_delta > 0.10:
        logger.warning(
            "prob_consistency: real_prob=%.3f pero reasoning menciona %s — delta=%.3f, usando JSON",
            real_prob,
            [f"{c:.3f}" for c in candidates],
            max_delta,
        )
        return f"[prob estructurada: {real_prob:.0%}] {reasoning}"

    return reasoning


SYSTEM_PROMPT = (
    "Eres un analista cuantitativo especializado en encontrar ineficiencias en mercados de prediccion. "
    "Tu objetivo es detectar DIVERGENCIAS entre el precio de mercado y la probabilidad real. "
    "Los mercados de Polymarket son frecuentemente INEFICIENTES: "
    "el precio YES no refleja correctamente la probabilidad real por sesgos cognitivos, "
    "baja liquidez, reaccion exagerada a noticias recientes o manipulacion de order book. "
    "BUSCA ACTIVAMENTE estas ineficiencias. "
    "Si el precio YES es 0.30 pero los fundamentales apuntan a 0.45, edge = +0.15 — es una oportunidad. "
    "Si el precio YES es 0.70 pero la evidencia es debil, edge = -0.15 — es oportunidad en NO. "
    "Se preciso: sobreestimar confianza es peor que subestimarla. "
    "Una senal con confianza inflada destruye la calibracion del sistema — prefiere PASS antes que un BUY con confianza falsa. "
    "Analiza: (1) buy_pressure del orderbook vs precio, (2) momentum del precio, "
    "(3) smart money, (4) sentiment de noticias vs precio, (5) arbitrage signals. "
    "Responde SOLO en JSON valido: "
    '{"real_prob": float, "edge": float, "confidence": float, '
    '"trend": "RISING|FALLING|STABLE", "recommendation": "BUY_YES|BUY_NO|PASS|WATCH", '
    '"key_factors": list[str], "reasoning": string} '
    "donde edge = real_prob - market_price_yes (positivo = comprar YES, negativo = comprar NO). "
    "REGLA CRITICA DE CONSISTENCIA (obligatoria): "
    "Si real_prob < precio_mercado → edge es negativo → recommendation DEBE ser BUY_NO o PASS. NUNCA BUY_YES. "
    "Si real_prob > precio_mercado → edge es positivo → recommendation DEBE ser BUY_YES o PASS. NUNCA BUY_NO. "
    "Una recommendation contradictoria con el signo del edge es un error grave — "
    "verifica siempre que tu recommendation sea coherente con real_prob vs precio_mercado antes de responder. "
    "CONSISTENCIA DEL REASONING (obligatorio): Tu campo 'reasoning' DEBE alinearse con tu recommendation. "
    "Si recommendation=BUY_NO, el reasoning debe explicar por que el mercado esta SOBREVALUADO "
    "(real_prob < precio_mercado): el mercado paga demasiado por YES. "
    "Si recommendation=BUY_YES, el reasoning debe explicar por que el mercado esta INFRAVALORADO "
    "(real_prob > precio_mercado): el mercado infravalora la probabilidad de YES. "
    "NUNCA escribas en el reasoning una conclusion opuesta al JSON que vas a devolver. "
    "Si tu razonamiento interno te lleva a una conclusion distinta a tu recommendation, "
    "revisa tu estimacion de real_prob y ajusta hasta que sean coherentes. "
    "ESCALA DE CONFIANZA (confidence): "
    "0.50 = muy incierto, datos insuficientes o contradictorios; "
    "0.65 = evidencia moderada, una o dos senales alineadas; "
    "0.75 = evidencia solida, multiples senales convergentes; "
    "0.85 = muy alta certeza, reservar para eventos casi seguros con evidencia inequivoca. "
    "La mayoria de mercados deberia quedar entre 0.55 y 0.72. "
    "Solo supera 0.80 si tienes 3 o mas senales independientes alineadas. "
    "CONSISTENCIA: Si este mercado ya fue analizado previamente, tu estimacion debe ser coherente. "
    "Una variacion de mas de 15 puntos porcentuales respecto al analisis anterior indica un error "
    "de razonamiento — revisa la evidencia antes de cambiar drasticamente tu estimacion."
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
    from datetime import datetime, timedelta, timezone
    from shared.firestore_client import col
    from shared.groq_client import _get_groq, GROQ_CALL_DELAY
    from shared.config import GROQ_MODEL_ROTATION

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

    now_utc = datetime.now(timezone.utc)
    end_date = market_data.get("end_date")
    if end_date:
        if hasattr(end_date, "tzinfo") and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        # Descartar si el mercado cierra en menos de 24h (incluyendo ya expirados)
        if end_date < now_utc + timedelta(hours=24):
            logger.info(
                "analyze_market(%s): mercado cierra/cerró en <24h (end_date=%s) — omitiendo",
                market_id, end_date.isoformat(),
            )
            return None
        days_to_close = (end_date - now_utc).days
    else:
        logger.warning(
            "analyze_market(%s): end_date no disponible en Firestore — omitiendo por seguridad",
            market_id,
        )
        return None

    question = market_data.get("question", "mercado desconocido")
    # Preferir precio del enriched_market (más reciente) sobre el guardado en Firestore,
    # que puede tener el bug de 0.5 por defecto si el scanner falló al leer outcomePrices.
    price_yes = float(
        enriched_market.get("price_yes") or market_data.get("price_yes") or 0.5
    )

    # FIX 1: mercado prácticamente resuelto — no tiene sentido analizarlo
    if price_yes < 0.05 or price_yes > 0.95:
        logger.debug(
            "analyze_market(%s): mercado prácticamente resuelto (price_yes=%.3f) — omitiendo",
            market_id, price_yes,
        )
        return None

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

    # Leer análisis anterior para ancla de consistencia (solo si < 24h)
    _last_prob: float | None = None
    try:
        _prev = col("poly_predictions").document(market_id).get()
        if _prev.exists:
            _prev_data = _prev.to_dict()
            _prev_at = _prev_data.get("analyzed_at")
            if _prev_at:
                if hasattr(_prev_at, "tzinfo") and _prev_at.tzinfo is None:
                    _prev_at = _prev_at.replace(tzinfo=timezone.utc)
                if (now_utc - _prev_at).total_seconds() < 86400:
                    _last_prob = float(_prev_data.get("real_prob") or 0)
    except Exception as _lpe:
        logger.debug("groq_analyzer(%s): error leyendo pred anterior — %s", market_id, _lpe)

    # Construir user_prompt con todos los datos del enriched_market
    orderbook = enriched_market.get("orderbook", {})
    news = enriched_market.get("news_sentiment", {})
    smart_money = enriched_market.get("smart_money", {})
    arbitrage = enriched_market.get("arbitrage", {})

    user_prompt = (
        f"Mercado: {question}\n"
        f"Categoría: {category}\n"
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
        f"Un edge de 0.00 o cercano a cero indica mercado eficiente — justificalo con argumentos solidos.\n"
        f"VERIFICACION FINAL OBLIGATORIA antes de responder:\n"
        f"  - Si real_prob < {price_yes:.3f} → escribe recommendation=BUY_NO o PASS. NUNCA BUY_YES.\n"
        f"  - Si real_prob > {price_yes:.3f} → escribe recommendation=BUY_YES o PASS. NUNCA BUY_NO.\n"
        f"  - Verifica que edge = real_prob - {price_yes:.3f} en tu JSON."
    )
    if price_yes < 0.15:
        user_prompt += (
            f"\nADVERTENCIA MERCADO DE BAJA PROBABILIDAD: precio YES = {price_yes:.1%} (<15%). "
            f"Tu estimación máxima razonable de real_prob es {price_yes * 3:.1%} (precio × 3). "
            f"Superar este límite indica sesgo de confirmación. "
            f"Para mercados geopolíticos o políticos de tan baja probabilidad, "
            f"recomienda WATCH o PASS salvo evidencia inequívoca y verificable."
        )
    if fear_greed_line:
        user_prompt += f"\n{fear_greed_line}"
    if _last_prob is not None:
        user_prompt += (
            f"\nANCLA DE CONSISTENCIA: tu análisis anterior de este mercado estimó "
            f"probabilidad real = {_last_prob:.1%}. "
            f"Si tu nueva estimación difiere en más de 15pp ({_last_prob - 0.15:.1%}–{_last_prob + 0.15:.1%}), "
            f"justifica explícitamente qué cambió."
        )
    if category_context:
        user_prompt += f"\n\nCONTEXTO ADICIONAL:\n{category_context}"

    # Llamada a Groq con rotación de modelos
    raw_response = ""
    groq_client = _get_groq()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Fix 3: si la cuota ya está persistida como agotada, ir directo al fallback
    if _is_groq_quota_exhausted():
        logger.info(
            "analyze_market(%s): Groq TPD agotado (agent_state) — usando fallback básico",
            market_id,
        )
        all_tpd = True
    else:
        all_tpd = True
        for attempt, model in enumerate(GROQ_MODEL_ROTATION):
            try:
                if attempt > 0:
                    messages[-1]["content"] = user_prompt + "\n\nResponde SOLO JSON, sin texto adicional."
                resp = groq_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=500,
                    temperature=0.35,
                )
                raw_response = resp.choices[0].message.content
                all_tpd = False
                break
            except Exception as e:
                err_str = str(e).lower()
                if "model_not_found" in err_str or "404" in err_str:
                    logger.warning("analyze_market(%s): modelo %s no encontrado — probando siguiente", market_id, model)
                    continue
                if "429" in err_str or "rate_limit" in err_str or "quota" in err_str or "daily" in err_str:
                    logger.warning("analyze_market(%s): TPD agotado en %s — probando siguiente", market_id, model)
                    continue
                logger.error("analyze_market(%s): error Groq en %s — %s", market_id, model, e, exc_info=True)
                return None

    # Fallback básico sin LLM cuando todos los modelos Groq están agotados
    if not raw_response and all_tpd:
        _set_groq_quota_exhausted()
        logger.warning("analyze_market(%s): todos los modelos Groq agotados — usando análisis básico", market_id)
        orderbook_fb = enriched_market.get("orderbook", {})
        buy_pressure = float(orderbook_fb.get("buy_pressure", 0.5))
        momentum = enriched_market.get("price_momentum", "STABLE")
        arb = enriched_market.get("arbitrage", {})
        sm = enriched_market.get("smart_money", {})

        real_prob = price_yes
        if sm.get("is_smart_money"):
            real_prob += 0.06 if buy_pressure > 0.5 else -0.06
        if enriched_market.get("volume_spike"):
            real_prob += 0.03 if momentum == "RISING" else (-0.03 if momentum == "FALLING" else 0)
        if arb.get("detected"):
            real_prob += float(arb.get("inefficiency", 0)) * 0.5
        real_prob = max(0.01, min(0.99, real_prob))
        edge_fb = round(real_prob - price_yes, 4)

        if edge_fb >= POLY_MIN_EDGE:
            rec_fb = "BUY_YES"
        elif edge_fb <= -POLY_MIN_EDGE:
            rec_fb = "BUY_NO"
        else:
            rec_fb = "PASS"

        result = {
            "real_prob": round(real_prob, 4),
            "edge": edge_fb,
            "confidence": 0.25,
            "trend": momentum,
            "recommendation": rec_fb,
            "key_factors": ["fallback_no_llm", "all_groq_tpd_exhausted"],
            "reasoning": f"Análisis básico sin LLM: buy_pressure={buy_pressure:.2f}, momentum={momentum}, smart_money={sm.get('is_smart_money', False)}",
        }
    elif not raw_response:
        logger.error("analyze_market(%s): sin respuesta de ningún modelo", market_id)
        return None
    else:
        # Extraer JSON de la respuesta del LLM
        result = None
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

    # Fix 7: corrección de sesgo LLM por categoría (calibración histórica)
    try:
        _weights = _get_poly_weights()
        _llm_bias = _weights.get("llm_bias_by_category", {})
        _bd = _llm_bias.get(category, {})
        if int(_bd.get("n", 0)) >= 5 and abs(float(_bd.get("bias", 0.0))) > 0.03:
            _bias_val = float(_bd["bias"])
            real_prob = round(max(0.05, min(0.95, real_prob - _bias_val)), 4)
            edge = round(real_prob - price_yes, 4)
            logger.debug(
                "analyze_market(%s): bias LLM cat=%s bias=%.3f n=%d → real_prob=%.3f",
                market_id, category, _bias_val, int(_bd["n"]), real_prob,
            )
    except Exception:
        pass

    # Garantizar coherencia: si el texto del reasoning menciona una prob distinta
    # a real_prob en >0.10, prepender nota aclaratoria para el mensaje Telegram.
    # real_prob del JSON estructurado es siempre el valor canónico.
    reasoning = _validate_prob_in_reasoning(real_prob, reasoning)
    # Eliminar frases del reasoning que contradigan la recommendation (BUY_NO/BUY_YES cruzados).
    reasoning = _clean_contradictory_reasoning(recommendation, reasoning)

    # Fix 6: precio crypto desde enriched_market['ctc_price'] — evita fetch HTTP separado
    # Fix 4 + validador de precio crypto — caps para predicciones históricamente improbables
    if category == "crypto" and _extract_target_price(question) is not None:
        _ctc = enriched_market.get("ctc_price")
        _ctc_price = float(_ctc) if _ctc else None
        real_prob, edge, reasoning = _validate_crypto_price_prediction(
            question, real_prob, price_yes, days_to_close, reasoning,
            current_price=_ctc_price,
        )
        # Recalcular recommendation tras ajuste
        if edge >= POLY_MIN_EDGE:
            recommendation = "BUY_YES"
        elif edge <= -POLY_MIN_EDGE:
            recommendation = "BUY_NO"
        else:
            recommendation = "PASS"

    # Cap para mercados de baja probabilidad (precio < 15%): real_prob ≤ precio × 2.5
    # El LLM tiende a inflar probs en mercados geopolíticos/políticos extremos.
    if price_yes < 0.15 and real_prob > price_yes * 2.5:
        old_prob = real_prob
        real_prob = round(min(price_yes * 2.5, 0.95), 4)
        edge = round(real_prob - price_yes, 4)
        note = f"⚠️ Cap prob baja: precio={price_yes:.1%} → real_prob máx={real_prob:.1%}"
        reasoning = f"{note}\n{reasoning}" if reasoning else note
        logger.info(
            "analyze_market(%s): LOW_PRICE_CAP %.3f→%.3f (price_yes=%.3f, cat=%s)",
            market_id, old_prob, real_prob, price_yes, category,
        )

    # Para geopolítica/política con precio < 15%: exigir edge ≥ 0.20 para BUY
    if price_yes < 0.15 and category in ("geopolitics", "politics"):
        if abs(edge) < 0.20 and recommendation in ("BUY_YES", "BUY_NO"):
            logger.info(
                "analyze_market(%s): LOW_PRICE_GEO_FILTER edge=%.3f<0.20 → PASS (cat=%s price=%.3f)",
                market_id, abs(edge), category, price_yes,
            )
            recommendation = "PASS"

    # Validar consistencia recommendation ↔ edge. Si Groq devuelve combinación contraria,
    # auto-corregir: el edge (derivado de real_prob) es la verdad, la rec es el error del LLM.
    if edge > 0 and recommendation == "BUY_NO":
        recommendation = "BUY_YES"
        logger.info(
            "analyze_market(%s): rec auto-corregida BUY_NO→BUY_YES "
            "(edge=%.3f>0, real_prob=%.3f > market=%.3f)",
            market_id, edge, real_prob, price_yes,
        )
    elif edge < 0 and recommendation == "BUY_YES":
        recommendation = "BUY_NO"
        logger.info(
            "analyze_market(%s): rec auto-corregida BUY_YES→BUY_NO "
            "(edge=%.3f<0, real_prob=%.3f < market=%.3f)",
            market_id, edge, real_prob, price_yes,
        )

    # Fix 4: calibrar confidence con accuracy histórica por bucket de edge
    try:
        _weights = _get_poly_weights()
        _by_bucket = _weights.get("accuracy_by_bucket", {})
        _abs_edge = abs(edge)
        _bucket = "high" if _abs_edge >= 0.15 else ("mid" if _abs_edge >= 0.12 else "low")
        _bs = _by_bucket.get(_bucket, {})
        _bn = int(_bs.get("n", 0))
        _bacc = float(_bs.get("accuracy", 0.0))
        if _bn >= 10 and _bacc > 0:
            _conf_cap = round(min(1.0, _bacc + 0.10), 4)
            if confidence > _conf_cap:
                logger.debug(
                    "analyze_market(%s): conf capped bucket=%s acc=%.0f%% n=%d: %.2f→%.2f",
                    market_id, _bucket, _bacc * 100, _bn, confidence, _conf_cap,
                )
                confidence = _conf_cap
    except Exception:
        pass

    # E1. Spread profundo — mercado ilíquido si spread > 8% → reducir confidence 20%
    try:
        _ob = enriched_market.get("orderbook", {})
        _spread = float(_ob.get("spread", 0))
        if _spread > 0.08:
            confidence = round(confidence * 0.80, 4)
            key_factors = [f"illiquid_spread_{_spread:.0%}"] + (key_factors or [])
            logger.info(
                "analyze_market(%s): spread=%.0f%% > 8%% — mercado ilíquido, conf→%.2f",
                market_id, _spread * 100, confidence,
            )
    except Exception:
        pass

    # E2. Corrección por correlación de mercados — pull real_prob si inconsistente
    try:
        _correlations = enriched_market.get("correlations", [])
        _high_corr = [c for c in _correlations if len(c.get("shared_keywords", [])) >= 3]
        if _high_corr:
            _corr_prices = [float(c.get("price_yes", 0.5)) for c in _high_corr]
            _corr_avg = sum(_corr_prices) / len(_corr_prices)
            _incon = abs(real_prob - _corr_avg)
            if _incon > 0.15:
                _old_prob = real_prob
                # Pull 30% hacia el promedio de mercados correlacionados
                real_prob = round(real_prob * 0.70 + _corr_avg * 0.30, 4)
                edge = round(real_prob - price_yes, 4)
                if edge >= POLY_MIN_EDGE:
                    recommendation = "BUY_YES"
                elif edge <= -POLY_MIN_EDGE:
                    recommendation = "BUY_NO"
                else:
                    recommendation = "PASS"
                logger.info(
                    "analyze_market(%s): correlación inconsistente (%.0f%%) — prob %.2f→%.2f (corr_avg=%.2f)",
                    market_id, _incon * 100, _old_prob, real_prob, _corr_avg,
                )
    except Exception:
        pass

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
        "end_date_iso": end_date.isoformat() if end_date else None,
        "slug": market_data.get("slug", ""),
        "volume_24h": volume_24h,
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
    from google.cloud.firestore_v1.base_query import FieldFilter
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
            col("poly_price_history").where(filter=FieldFilter("timestamp", "<", cutoff_30d)),
            "poly_price_history",
        )
        logger.info("run_maintenance: %d docs poly_price_history eliminados (>30d)", deleted_history)
    except Exception:
        logger.error("run_maintenance: error limpiando poly_price_history", exc_info=True)

    try:
        deleted_enriched = await _batch_delete(
            col("enriched_markets").where(filter=FieldFilter("enriched_at", "<", cutoff_7d)),
            "enriched_markets",
        )
        logger.info("run_maintenance: %d docs enriched_markets eliminados (>7d)", deleted_enriched)
    except Exception:
        logger.error("run_maintenance: error limpiando enriched_markets", exc_info=True)
