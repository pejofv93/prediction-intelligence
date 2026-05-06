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

# Price context cache for "Will X reach $Y" markets (5-min TTL)
_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_PRICE_CACHE_TTL: float = 300.0

# Team elimination cache for "Will X win [tournament]" markets (4h TTL)
_TEAM_ELIM_CACHE: dict[str, tuple[bool, float]] = {}
_TEAM_ELIM_TTL: float = 14400.0

# NBA playoff series win-prob cache (30-min TTL)
_NBA_SERIES_CACHE: dict[str, tuple[float, float]] = {}
_NBA_SERIES_TTL: float = 1800.0

# League title race standings cache (4h TTL)
_TITLE_RACE_CACHE: dict[str, tuple[int | None, float]] = {}
_TITLE_RACE_TTL: float = 14400.0

_LEAGUE_TITLE_RACE_RE = re.compile(
    r'will\s+(.+?)\s+win\s+(?:the\s+)?(premier league|la liga|bundesliga|serie a|ligue 1|eredivisie|mls)',
    re.I,
)
_PTS_BEHIND_PATTERNS = [
    re.compile(r'(\d{1,2})\s*points?\s+(?:behind|adrift|off\s+the\s+pace|from\s+(?:the\s+)?(?:top|leaders?))', re.I),
    re.compile(r'trail(?:ing)?\s+(?:\w+\s+){0,3}by\s+(\d{1,2})\s*points?', re.I),
    re.compile(r'(\d{1,2})-point\s+(?:gap|deficit)', re.I),
    re.compile(r'gap\s+(?:of|is)\s+(\d{1,2})', re.I),
]

_MLB_RE = re.compile(r'\b(mlb|baseball|major league baseball)\b', re.I)

_WIN_TOURNAMENT_RE = re.compile(
    r'will\s+(.+?)\s+win\s+(?:the\s+)?(.+?)[\?\.\s]*$', re.I
)
_TOURNAMENT_KW_RE = re.compile(
    r'\b(champions league|world cup|copa del rey|copa libertadores|europa league|'
    r'premier league|la liga|bundesliga|serie a|ligue 1|nba finals|nfl|mlb|nhl|'
    r'wimbledon|us open|roland garros|australian open|masters|grand slam|'
    r'super bowl|playoff|finals?|semi.?final|quarter.?final|copa america|euro \d{4})\b',
    re.I,
)

_YAHOO_SYMBOLS: dict[str, str] = {
    "WTI":    "CL%3DF",
    "GOLD":   "GC%3DF",
    "SILVER": "SI%3DF",
}

_ASSET_DETECT: list = [
    (re.compile(r'\b(bitcoin|btc)\b', re.I),            "BTC",    "bitcoin"),
    (re.compile(r'\b(ethereum|eth)\b', re.I),            "ETH",    "ethereum"),
    (re.compile(r'\b(solana|sol)\b', re.I),              "SOL",    "solana"),
    (re.compile(r'\b(xrp|ripple)\b', re.I),              "XRP",    "ripple"),
    (re.compile(r'\bbnb\b', re.I),                       "BNB",    "binancecoin"),
    (re.compile(r'\b(dogecoin|doge)\b', re.I),           "DOGE",   "dogecoin"),
    (re.compile(r'\b(cardano|ada)\b', re.I),             "ADA",    "cardano"),
    (re.compile(r'\b(avalanche|avax)\b', re.I),          "AVAX",   "avalanche-2"),
    (re.compile(r'\bchainlink\b', re.I),                 "LINK",   "chainlink"),
    (re.compile(r'\b(crude oil|wti|oil price)\b', re.I), "WTI",    None),
    (re.compile(r'\b(gold|xau)\b', re.I),                "GOLD",   None),
    (re.compile(r'\b(silver|xag)\b', re.I),              "SILVER", None),
]


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
    "crypto": ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "defi", "blockchain", "halving", "altcoin", "xrp", "bnb", "doge", "dogecoin", "stablecoin", "nft", "web3", "layer 2", "base chain"],
    "politics": ["election", "president", "vote", "congress", "senate", "minister", "parliament", "poll", "referendum", "prime minister", "chancellor", "governor", "ballot", "trump", "biden", "harris", "democrat", "republican"],
    "economy": ["fed", "interest rate", "inflation", "cpi", "gdp", "recession", "unemployment", "federal reserve", "rate hike", "rate cut", "jerome powell", "tariff", "trade war", "crude oil", "wti", "brent", "oil price", "gold price", "s&p", "nasdaq", "dow jones"],
    "sports": ["world cup", "champions league", "nba", "super bowl", "final", "tournament", "championship", "league", "nfl", "mlb", "wimbledon", "olympic", "football", "soccer", "formula 1", " f1 ", "tennis", "golf", "boxing", "ufc", "mma", "playoffs", "copa", "euro ", "roland garros", "us open", "masters", "nascar", "basketball", "baseball", "hockey", "cricket", "rugby", "atp", "wta", "fifa", "uefa", "premier league", "la liga", "bundesliga", "serie a", "grand prix"],
    "geopolitics": ["war", "ceasefire", "conflict", "nato", "military", "invasion", "sanctions", "treaty", "diplomacy", "nuclear", "iran", "hormuz", "strait", "ukraine", "russia", "china", "taiwan", "israel", "gaza", "hamas", "hezbollah", "korea", "missile", "drone", "coup", "regime", "peace deal", "cease fire", "truce", "embargo"],
    "business": ["apple", "tesla", "microsoft", "amazon", "google", "alphabet", "meta", "nvidia", "openai", "anthropic", "earnings", "merger", "acquisition", "ipo", "layoffs", "market cap", "revenue", "profit", "ceo", "stock", "shares", "valuation", "startup", "funding", "unicorn"],
    "science": ["climate", "nasa", "space", "vaccine", "fda", "cancer", "quantum", "discovery", "mission", "ai model", "chatgpt", "llm", "gpt", "gemini", "claude", "spacex", "rocket", "satellite", "drug approval", "clinical trial"],
    "culture": ["oscar", "grammy", "emmy", "movie", "album", "singer", "actor", "celebrity", "taylor swift", "award", "box office", "netflix", "spotify", "billboard", "world record", "streaming"],
}


def categorize_market(question: str) -> str:
    """Categoriza un mercado Polymarket según su pregunta. Devuelve categoria o 'other'."""
    q_lower = question.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in q_lower for kw in keywords):
            return category
    return "other"


def market_analysis_priority(enriched_market: dict) -> int:
    """Prioridad de análisis: medio volumen ($10k-$100k) primero — menos eficiente de precio."""
    vol = float(enriched_market.get("volume_24h", 0))
    if 10_000 <= vol <= 100_000:
        return 2
    if 5_000 <= vol < 10_000:
        return 1
    return 0


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


def _detect_price_market(question: str) -> tuple[str, str | None, float] | None:
    """Detect 'Will X reach $Y' markets. Returns (asset_key, coingecko_id, target_price) or None."""
    target = _extract_target_price(question)
    if target is None:
        return None
    for pattern, asset_key, cg_id in _ASSET_DETECT:
        if pattern.search(question):
            return asset_key, cg_id, target
    return None


async def _fetch_current_price(asset_key: str, coingecko_id: str | None) -> float | None:
    """Fetch spot price with 5-min in-memory cache. Returns None on failure."""
    import asyncio
    import json
    import urllib.request

    now_ts = _time.monotonic()
    if asset_key in _PRICE_CACHE:
        cached_price, cached_ts = _PRICE_CACHE[asset_key]
        if now_ts - cached_ts < _PRICE_CACHE_TTL:
            return cached_price

    def _http_get(url: str, parse_fn) -> float | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "prediction-intelligence/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                return float(parse_fn(json.loads(resp.read())))
        except Exception as _e:
            logger.debug("_fetch_current_price(%s): %s — %s", asset_key, url[:60], _e)
            return None

    loop = asyncio.get_running_loop()
    price: float | None = None

    if coingecko_id:
        try:
            from realtime.correlation_tracker import get_crypto_price
            price = await asyncio.wait_for(get_crypto_price(coingecko_id), timeout=4.0)
        except asyncio.TimeoutError:
            logger.debug("_fetch_current_price(%s): CoinGecko timeout >4s", asset_key)
        except Exception as _cge:
            logger.debug("_fetch_current_price(%s): CoinGecko error — %s", asset_key, _cge)

    if price is None and asset_key in _YAHOO_SYMBOLS:
        _sym = _YAHOO_SYMBOLS[asset_key]
        _yurl = f"https://query2.finance.yahoo.com/v8/finance/chart/{_sym}?interval=1d&range=1d"
        price = await loop.run_in_executor(
            None, lambda: _http_get(_yurl, lambda d: d["chart"]["result"][0]["meta"]["regularMarketPrice"])
        )

    if price and price > 0:
        _PRICE_CACHE[asset_key] = (price, now_ts)
        logger.info("_fetch_current_price: %s=$%.4g", asset_key, price)
    return price if (price and price > 0) else None


def _clean_contradictory_reasoning(
    recommendation: str,
    reasoning: str,
    market_price: float = 0.0,
    real_prob: float = 0.0,
) -> str:
    """
    Reemplaza el reasoning con texto canónico cuando la dirección numérica
    es consistente con la recomendación. No usa detección de palabras clave
    — el texto del LLM siempre se descarta para señales BUY_NO/BUY_YES.
    """
    if not reasoning:
        return reasoning

    mp_pct = f"{market_price:.0%}"
    rp_pct = f"{real_prob:.0%}"

    if recommendation == "BUY_NO" and market_price > real_prob:
        logger.warning(
            "_clean_contradictory_reasoning: BUY_NO market=%.3f > real=%.3f "
            "— reemplazando reasoning con texto canónico",
            market_price, real_prob,
        )
        return (
            f"El mercado sobrevalora esta probabilidad. "
            f"Precio actual ({mp_pct}) está por encima "
            f"de la probabilidad real estimada ({rp_pct}). "
            f"BUY_NO es la posición correcta."
        )

    if recommendation == "BUY_YES" and real_prob > market_price:
        logger.warning(
            "_clean_contradictory_reasoning: BUY_YES real=%.3f > market=%.3f "
            "— reemplazando reasoning con texto canónico",
            real_prob, market_price,
        )
        return (
            f"El mercado infravalora esta probabilidad. "
            f"Precio actual ({mp_pct}) está por debajo "
            f"de la probabilidad real estimada ({rp_pct}). "
            f"BUY_YES es la posición correcta."
        )

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
    "REGLA ABSOLUTA — CONSISTENCIA REASONING (CRITICA): "
    "Tu campo 'reasoning' DEBE ser consistente con tu 'recommendation'. "
    "Si recommendation=BUY_NO: el reasoning DEBE explicar POR QUE el mercado esta SOBREVALUADO. "
    "NUNCA uses palabras como 'subvaluado', 'infravalorado', 'oportunidad de compra', "
    "'BUY_YES', 'edge positivo', 'comprar YES' o 'probabilidad real es mayor' "
    "en el reasoning si recommendation=BUY_NO. "
    "Si recommendation=BUY_YES: el reasoning DEBE explicar POR QUE el mercado esta INFRAVALORADO. "
    "NUNCA uses palabras como 'sobrevaluado', 'BUY_NO', 'vender' o 'edge negativo' "
    "en el reasoning si recommendation=BUY_YES. "
    "INCUMPLIR ESTA REGLA ES UN ERROR CRITICO — el sistema rechazara y sobreescribira tu reasoning. "
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


async def _get_title_race_points_behind(team: str, league: str) -> int | None:
    """
    Searches DDG for '{team} {league} standings 2026' and parses the points gap
    between the team and the league leader. Returns int (pts behind) or None.
    Cache 4h.
    """
    import asyncio
    import urllib.parse
    import urllib.request

    cache_key = f"{team.lower()}|{league.lower()}"
    now_ts = _time.monotonic()
    if cache_key in _TITLE_RACE_CACHE:
        pts, ts = _TITLE_RACE_CACHE[cache_key]
        if now_ts - ts < _TITLE_RACE_TTL:
            return pts

    query = f"{team} {league} standings 2026 points behind leader"
    url = f"https://html.duckduckgo.com/html/?{urllib.parse.urlencode({'q': query})}"

    def _fetch() -> str:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as _e:
            logger.debug("_get_title_race_points_behind(%s): DDG error — %s", team, _e)
            return ""

    html = await asyncio.get_running_loop().run_in_executor(None, _fetch)
    if not html:
        _TITLE_RACE_CACHE[cache_key] = (None, now_ts)
        return None

    html_lower = html.lower()
    team_lower = team.lower()
    result_pts: int | None = None

    idx = html_lower.find(team_lower)
    while idx != -1 and result_pts is None:
        window = html_lower[max(0, idx - 350): idx + 350]
        for pattern in _PTS_BEHIND_PATTERNS:
            m = pattern.search(window)
            if m:
                pts = int(m.group(1))
                if 1 <= pts <= 30:
                    result_pts = pts
                    break
        idx = html_lower.find(team_lower, idx + 1)

    _TITLE_RACE_CACHE[cache_key] = (result_pts, now_ts)
    if result_pts is not None:
        logger.info(
            "_get_title_race_points_behind: %s %dpts behind leader in %s",
            team, result_pts, league,
        )
    return result_pts


async def _fetch_nba_win_prob(team_name: str) -> float | None:
    """
    Fetch game-level win probability for a team from ESPN scoreboard predictor.
    Used as proxy for series win probability in NBA playoff markets.
    Returns probability in [0, 1] or None if not found / no predictor data.
    Cache 30 min.
    """
    import asyncio
    import json
    import urllib.request

    cache_key = team_name.lower()
    now_ts = _time.monotonic()
    if cache_key in _NBA_SERIES_CACHE:
        prob, ts = _NBA_SERIES_CACHE[cache_key]
        if now_ts - ts < _NBA_SERIES_TTL:
            return prob

    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

    def _fetch() -> dict | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read())
        except Exception as _e:
            logger.debug("_fetch_nba_win_prob: ESPN error — %s", _e)
            return None

    data = await asyncio.get_running_loop().run_in_executor(None, _fetch)
    if not data:
        return None

    team_lower = team_name.lower()

    def _team_matches(competitor: dict) -> bool:
        t = competitor.get("team", {})
        candidates = [
            (t.get("displayName") or "").lower(),
            (t.get("shortDisplayName") or "").lower(),
            (t.get("abbreviation") or "").lower(),
        ]
        return any(c and (team_lower in c or c in team_lower) for c in candidates)

    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            predictor = comp.get("predictor", {})
            if not predictor:
                continue
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})

            if _team_matches(home):
                raw = predictor.get("homeTeam", {}).get("teamChance")
            elif _team_matches(away):
                raw = predictor.get("awayTeam", {}).get("teamChance")
            else:
                continue

            if raw is not None:
                prob = float(raw)
                if prob > 1.0:
                    prob /= 100.0
                _NBA_SERIES_CACHE[cache_key] = (prob, now_ts)
                logger.info("_fetch_nba_win_prob: %s=%.1f%%", team_name, prob * 100)
                return prob

    return None


def _extract_team_tournament(question: str) -> tuple[str, str] | None:
    """Extract (team, tournament) from 'Will X win [the] Y?' questions."""
    m = _WIN_TOURNAMENT_RE.search(question)
    if not m:
        return None
    team = m.group(1).strip()
    tournament = m.group(2).strip()
    if not _TOURNAMENT_KW_RE.search(question):
        return None
    if len(team) < 3 or len(team) > 60:
        return None
    return team, tournament


async def _check_team_eliminated(team: str, tournament: str) -> bool:
    """
    Returns True if DDG results strongly suggest team is eliminated from tournament.
    On any fetch error returns False (don't block the signal).
    """
    import asyncio
    import urllib.parse
    import urllib.request

    cache_key = f"{team.lower()}|{tournament.lower()}"
    now_ts = _time.monotonic()
    if cache_key in _TEAM_ELIM_CACHE:
        eliminated, cached_ts = _TEAM_ELIM_CACHE[cache_key]
        if now_ts - cached_ts < _TEAM_ELIM_TTL:
            return eliminated

    query = f"{team} {tournament} 2026 eliminated OR knocked out OR eliminado"
    url = f"https://html.duckduckgo.com/html/?{urllib.parse.urlencode({'q': query})}"

    def _fetch() -> str:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; prediction-bot/1.0)"}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as _e:
            logger.debug("_check_team_eliminated(%s): DDG fetch error — %s", team, _e)
            return ""

    loop = asyncio.get_running_loop()
    html = await loop.run_in_executor(None, _fetch)

    if not html:
        _TEAM_ELIM_CACHE[cache_key] = (False, now_ts)
        return False

    html_lower = html.lower()
    team_lower = team.lower()
    elimination_signals = [
        "eliminat", "knocked out", "out of the", "already eliminated",
        "has been eliminated", "were eliminated", "fuera de",
    ]

    eliminated = False
    for signal in elimination_signals:
        idx = html_lower.find(signal)
        while idx != -1:
            window = html_lower[max(0, idx - 200): idx + 200]
            if team_lower in window:
                eliminated = True
                break
            idx = html_lower.find(signal, idx + 1)
        if eliminated:
            break

    _TEAM_ELIM_CACHE[cache_key] = (eliminated, now_ts)
    return eliminated


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
        # Descartar solo si el mercado cierra en menos de 2h (ya expirado o inminente)
        if end_date < now_utc + timedelta(hours=2):
            logger.info(
                "analyze_market(%s): CLOSING_SOON_SKIP — mercado cierra en <2h (end_date=%s)",
                market_id, end_date.isoformat(),
            )
            return None
        days_to_close = (end_date - now_utc).days
        # Mercados entre 2h y 48h: blend LLM 50% + precio mercado 50%
        hours_to_close = (end_date - now_utc).total_seconds() / 3600
        _closing_soon = hours_to_close < 48
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

    # Discard "Will X win [tournament]" markets where team is already eliminated
    _nba_win_prob: float | None = None
    if category == "sports":
        _tt = _extract_team_tournament(question)
        if _tt:
            _tm, _trn = _tt
            try:
                if await asyncio.wait_for(_check_team_eliminated(_tm, _trn), timeout=5.0):
                    logger.info(
                        "analyze_market(%s): TEAM_ELIMINATED — %s descartado (%s)",
                        market_id, _tm, _trn,
                    )
                    return None
            except asyncio.TimeoutError:
                logger.warning(
                    "analyze_market(%s): _check_team_eliminated timeout >5s — skip (%s)",
                    market_id, _tm,
                )
            except Exception as _te:
                logger.debug(
                    "analyze_market(%s): error verificando eliminación de %s — %s",
                    market_id, _tm, _te,
                )

        # NBA playoff series: pre-fetch ESPN win probability for post-LLM floor
        q_lower = question.lower()
        if "nba" in q_lower and ("series" in q_lower or "playoffs" in q_lower or "finals" in q_lower):
            _nba_m = _WIN_TOURNAMENT_RE.search(question)
            if _nba_m:
                _nba_team = _nba_m.group(1).strip()
                try:
                    _nba_win_prob = await asyncio.wait_for(
                        _fetch_nba_win_prob(_nba_team), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "analyze_market(%s): _fetch_nba_win_prob timeout >5s — skip (%s)",
                        market_id, _nba_team,
                    )
                except Exception as _nwe:
                    logger.debug(
                        "analyze_market(%s): error fetch NBA win prob — %s", market_id, _nwe
                    )

    # Detect "Will X reach $Y" markets and fetch live price
    _price_ctx = _detect_price_market(question)
    _current_price: float | None = None
    _pct_needed: float | None = None
    if _price_ctx:
        _p_asset, _p_cg_id, _p_target = _price_ctx
        _current_price = await _fetch_current_price(_p_asset, _p_cg_id)
        if _current_price and _current_price > 0:
            _pct_needed = (_p_target / _current_price - 1) * 100
            if abs(_pct_needed) > 100:
                logger.info(
                    "analyze_market(%s): PRICE_UNREACHABLE — %s target=$%.0f current=$%.2f "
                    "requiere %.0f%% en %dd — descartando",
                    market_id, _p_asset, _p_target, _current_price, _pct_needed, days_to_close,
                )
                return None

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
    if _current_price is not None and _pct_needed is not None and _price_ctx:
        _direction = "subida" if _pct_needed > 0 else "bajada"
        user_prompt += (
            f"\n\nCONTEXTO DE PRECIO ({_price_ctx[0]}): "
            f"Precio actual: ${_current_price:,.2f}. "
            f"Precio objetivo: ${_price_ctx[2]:,.0f}. "
            f"Requiere {_direction} de {abs(_pct_needed):.1f}% en {days_to_close} días. "
            f"Calibra tu real_prob considerando la magnitud de este movimiento."
        )

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
                if "model_not_found" in err_str or "404" in err_str or "model_decommissioned" in err_str or "decommissioned" in err_str:
                    logger.warning("analyze_market(%s): modelo %s no disponible — probando siguiente", market_id, model)
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
            # Retry con temperatura=0 y prompt JSON estricto (máx 2 intentos)
            _json_strict = (
                "RESPONDE ÚNICAMENTE CON JSON VÁLIDO. "
                "NO escribas texto, explicaciones ni prosa. "
                "SOLO el objeto JSON solicitado."
            )
            for _retry in range(2):
                try:
                    _r = groq_client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + _json_strict},
                            {"role": "user", "content": user_prompt + "\n\n" + _json_strict},
                        ],
                        max_tokens=500,
                        temperature=0.0,
                    )
                    _raw = _r.choices[0].message.content
                    for _ext in [
                        lambda r: json.loads(r),
                        lambda r: json.loads(re.search(r"\{.*\}", r, re.DOTALL).group()),
                    ]:
                        try:
                            result = _ext(_raw)
                            break
                        except Exception:
                            continue
                    if result is not None:
                        logger.info(
                            "analyze_market(%s): JSON retry %d/2 OK (temp=0)",
                            market_id, _retry + 1,
                        )
                        break
                    logger.warning(
                        "analyze_market(%s): JSON retry %d/2 — sigue sin JSON válido",
                        market_id, _retry + 1,
                    )
                except Exception as _re:
                    logger.warning(
                        "analyze_market(%s): JSON retry %d/2 error — %s",
                        market_id, _retry + 1, _re,
                    )

            if result is None:
                logger.error(
                    "analyze_market(%s): no se pudo parsear JSON tras 2 reintentos: %s",
                    market_id, raw_response[:200],
                )
                return None

    # Construir documento poly_prediction
    real_prob = float(result.get("real_prob", price_yes))
    edge = float(result.get("edge", real_prob - price_yes))
    confidence = float(result.get("confidence", 0.5))
    trend = result.get("trend", enriched_market.get("price_momentum", "STABLE"))
    recommendation = result.get("recommendation", "PASS")
    key_factors = result.get("key_factors", [])
    reasoning = result.get("reasoning", "")

    # Mercados con resolución <48h: blend LLM 50% + precio mercado 50%
    # El precio de mercado converge a la probabilidad real en los días finales.
    if _closing_soon:
        blended = round((real_prob * 0.5) + (price_yes * 0.5), 4)
        logger.info(
            "analyze_market(%s): CLOSING_SOON_BLEND days=%d real_prob %.3f→%.3f (blend con price_yes=%.3f)",
            market_id, days_to_close, real_prob, blended, price_yes,
        )
        real_prob = blended
        edge = round(real_prob - price_yes, 4)

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
    # Capa 2: reemplazar reasoning completo si contradice recommendation.
    reasoning = _clean_contradictory_reasoning(recommendation, reasoning, price_yes, real_prob)

    # FIX 2: near-target floor — target < 10% de distancia → mínimo 60% + no señal contraria
    if _price_ctx and _pct_needed is not None and abs(_pct_needed) < 10.0:
        _near_floor = 0.60
        if real_prob < _near_floor:
            _old_near = real_prob
            real_prob = _near_floor
            edge = round(real_prob - price_yes, 4)
            _near_dir = "subida" if _pct_needed > 0 else "bajada"
            _near_note = (
                f"⚠️ Floor target cercano: {_price_ctx[0]} a solo "
                f"{abs(_pct_needed):.1f}% del objetivo ({_near_dir}) → prob_min={_near_floor:.0%}"
            )
            reasoning = f"{_near_note}\n{reasoning}" if reasoning else _near_note
            logger.info(
                "analyze_market(%s): NEAR_TARGET_FLOOR %.3f→%.3f pct_needed=%.1f%% asset=%s",
                market_id, _old_near, real_prob, _pct_needed, _price_ctx[0],
            )
        # No generar señal contraria: BUY_NO en target alcista o BUY_YES en target bajista
        if _pct_needed > 0 and recommendation == "BUY_NO":
            recommendation = "PASS"
            logger.info(
                "analyze_market(%s): NEAR_TARGET_NO_CONTRA BUY_NO→PASS (target +%.1f%%)",
                market_id, _pct_needed,
            )
        elif _pct_needed < 0 and recommendation == "BUY_YES":
            recommendation = "PASS"
            logger.info(
                "analyze_market(%s): NEAR_TARGET_NO_CONTRA BUY_YES→PASS (target %.1f%%)",
                market_id, _pct_needed,
            )

    # Cap por magnitud de movimiento requerido — todas las categorías
    if _price_ctx and _pct_needed is not None and abs(_pct_needed) > 50 and real_prob > 0.15:
        _old_prob = real_prob
        real_prob = 0.15
        edge = round(real_prob - price_yes, 4)
        _dir = "subida" if _pct_needed > 0 else "bajada"
        _note = (
            f"⚠️ Cap precio: {_price_ctx[0]} requiere {_dir} de "
            f"{abs(_pct_needed):.1f}% → prob_max=15%"
        )
        reasoning = f"{_note}\n{reasoning}" if reasoning else _note
        logger.info(
            "analyze_market(%s): PRICE_MOVE_CAP %.3f→0.150 pct_needed=%.1f%% asset=%s",
            market_id, _old_prob, _pct_needed, _price_ctx[0],
        )
        if edge >= POLY_MIN_EDGE:
            recommendation = "BUY_YES"
        elif edge <= -POLY_MIN_EDGE:
            recommendation = "BUY_NO"
        else:
            recommendation = "PASS"

    # Validador de precio crypto — caps adicionales para predicciones históricamente improbables
    if category == "crypto" and _extract_target_price(question) is not None:
        real_prob, edge, reasoning = _validate_crypto_price_prediction(
            question, real_prob, price_yes, days_to_close, reasoning,
            current_price=_current_price,
        )
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

    # MLB extreme probs — no señal cuando el modelo no tiene datos reales de béisbol
    if category == "sports" and recommendation in ("BUY_YES", "BUY_NO"):
        if _MLB_RE.search(question):
            if real_prob < 0.10 or real_prob > 0.90:
                logger.info(
                    "analyze_market(%s): MLB_NO_DATA_EXTREME real_prob=%.2f fuera [10%%,90%%] → PASS",
                    market_id, real_prob,
                )
                recommendation = "PASS"

    # Title race cap — equipo a >10 pts del líder cerca del final de temporada → prob_max 15%
    if category == "sports" and recommendation in ("BUY_YES", "WATCH") and real_prob > 0.15 and days_to_close < 90:
        _title_m = _LEAGUE_TITLE_RACE_RE.search(question)
        if _title_m:
            _lr_team = _title_m.group(1).strip()
            _lr_league = _title_m.group(2).strip()
            try:
                _pts_behind = await asyncio.wait_for(
                    _get_title_race_points_behind(_lr_team, _lr_league), timeout=5.0
                )
                if _pts_behind is not None and _pts_behind > 10:
                    _lr_cap = 0.15
                    if real_prob > _lr_cap:
                        _old_lr = real_prob
                        real_prob = _lr_cap
                        edge = round(real_prob - price_yes, 4)
                        if edge >= POLY_MIN_EDGE:
                            recommendation = "BUY_YES"
                        elif edge <= -POLY_MIN_EDGE:
                            recommendation = "BUY_NO"
                        else:
                            recommendation = "PASS"
                        _lr_note = (
                            f"⚠️ TITLE_RACE_CHECK: {_lr_team} {_pts_behind}pts "
                            f"behind {_lr_league} leader → prob_max=15%"
                        )
                        reasoning = f"{_lr_note}\n{reasoning}" if reasoning else _lr_note
                        logger.info(
                            "analyze_market(%s): TITLE_RACE_CHECK %.3f→%.3f %s +%dpts behind",
                            market_id, _old_lr, real_prob, _lr_team, _pts_behind,
                        )
            except asyncio.TimeoutError:
                logger.warning(
                    "analyze_market(%s): _get_title_race_points_behind timeout >5s — skip (%s)",
                    market_id, _lr_team,
                )
            except Exception as _tre:
                logger.debug("analyze_market(%s): TITLE_RACE_CHECK error — %s", market_id, _tre)

    # FIX 3: NBA playoff series — floor si ESPN muestra equipo muy favorito (> 85%)
    if _nba_win_prob is not None and _nba_win_prob > 0.85:
        _nba_floor = 0.75
        if real_prob < _nba_floor:
            _old_nba = real_prob
            real_prob = _nba_floor
            edge = round(real_prob - price_yes, 4)
            _nba_note = (
                f"⚠️ NBA playoff floor: ESPN win_prob={_nba_win_prob:.0%} > 85% "
                f"→ prob_min={_nba_floor:.0%}"
            )
            reasoning = f"{_nba_note}\n{reasoning}" if reasoning else _nba_note
            logger.info(
                "analyze_market(%s): NBA_PLAYOFF_FLOOR %.3f→%.3f espn_win_prob=%.1f%%",
                market_id, _old_nba, real_prob, _nba_win_prob * 100,
            )
        # Nunca BUY_NO contra equipo con >85% de ganar según ESPN
        if recommendation == "BUY_NO":
            recommendation = "PASS"
            logger.info(
                "analyze_market(%s): NBA_NO_CONTRA BUY_NO→PASS (espn=%.1f%%)",
                market_id, _nba_win_prob * 100,
            )
        if edge >= POLY_MIN_EDGE:
            recommendation = "BUY_YES"
        elif edge > -POLY_MIN_EDGE:
            recommendation = "PASS"

    # Aplicar ajuste Fear & Greed si es crypto
    if fear_greed and category == "crypto":
        try:
            from realtime.binance_tracker import apply_fear_greed_to_signal
            _tmp = {"confidence": confidence}
            _tmp = apply_fear_greed_to_signal(_tmp, fear_greed, recommendation)
            confidence = float(_tmp.get("confidence", confidence))
        except Exception as _fga:
            logger.debug("groq_analyzer: error aplicando F&G — %s", _fga)

    # Capa final: re-aplicar limpieza de reasoning con los valores definitivos
    # (recommendation puede haber cambiado por auto-corrección, correlación, etc.)
    reasoning = _clean_contradictory_reasoning(recommendation, reasoning, price_yes, real_prob)

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
        "days_to_close": days_to_close,
        "slug": market_data.get("slug") or enriched_market.get("slug", ""),
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
