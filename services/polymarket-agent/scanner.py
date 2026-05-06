"""
Scanner Polymarket — fetch mercados activos con variedad de categorías.
Guarda en Firestore poly_markets.
IMPORTANTE: guardar "conditionId" como "condition_id" — necesario para CLOB orderbook.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
_HTTP_TIMEOUT = 20.0

# Categorías para buckets específicos — variedad forzada por categoría
_SCAN_CATEGORIES: dict[str, list[str]] = {
    "crypto":      ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "defi", "blockchain", "altcoin", "halving"],
    "sports":      ["world cup", "champions league", "nba", "super bowl", "final", "tournament", "championship", "nfl", "mlb", "wimbledon", "olympic", "copa"],
    "politics":    ["election", "president", "vote", "congress", "senate", "minister", "parliament", "referendum", "prime minister", "chancellor"],
    "culture":     ["oscar", "grammy", "emmy", "movie", "album", "singer", "actor", "celebrity", "taylor", "award", "box office", "netflix", "spotify", "billboard"],
    "geopolitics": ["war", "ceasefire", "iran", "strait", "hormuz", "conflict", "nato", "military", "invasion", "sanctions", "nuclear", "missile"],
    "science":     ["climate", "nasa", "space", "vaccine", "fda", "cancer", "quantum", "discovery", "mission", "ai model"],
    "business":    ["apple", "tesla", "microsoft", "amazon", "google", "meta", "nvidia", "earnings", "merger", "acquisition", "ipo", "layoffs"],
}

# Límites por bucket de categoría
_BUCKET_CRYPTO   = 10
_BUCKET_SPORTS   = 10
_BUCKET_POLITICS = 10
_BUCKET_CULTURE  = 5
_BUCKET_OTHER    = 10   # geopolítica + ciencia + negocio + otros combinados
_BUCKET_NEW      = 20   # mercados creados últimas 48h

# Keywords específicos de deportes para el bucket deportivo dedicado
_SPORTS_KEYWORDS = [
    "football", "soccer", "nba", "nfl", "mlb", "nhl", "tennis", "golf",
    "formula 1", " f1 ", "boxing", "mma", "ufc", "olympics", "world cup",
    "champions league", "premier league", "la liga", "bundesliga", "serie a",
    "ligue 1", "super bowl", "playoffs", "copa", "euro ", "wimbledon",
    "roland garros", "us open", "masters", "nascar", "basketball", "baseball",
    "hockey", "cricket", "rugby", "wrestling", "atp", "wta", "fifa", "uefa",
    "match", " win ", " wins ", "championship", "tournament", "semifinal",
    "quarterfinal", "knockout", "grand prix", "driver", "team score",
]


def _categorize_for_scan(question: str) -> str:
    q = question.lower()
    for cat, kws in _SCAN_CATEGORIES.items():
        if any(kw in q for kw in kws):
            return cat
    return "other"


def _is_sports_market(question: str) -> bool:
    """True si el mercado es deportivo según keywords específicos."""
    q = question.lower()
    return any(kw in q for kw in _SPORTS_KEYWORDS)


def _quality_ok(market: dict, now: datetime) -> bool:
    """
    Filtro de calidad mínima común a todos los buckets:
    - volume_24h >= 5000
    - days_to_close entre 2 y 30
    - price_yes no extremo (no prácticamente resuelto)
    """
    if market.get("volume_24h", 0) < 5000:
        return False
    end_date = market.get("end_date")
    if not end_date or not isinstance(end_date, datetime):
        return False
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    days = (end_date - now).total_seconds() / 86400
    if days < 2 or days > 30:
        return False
    price_yes = market.get("price_yes", 0.5)
    if price_yes < 0.05 or price_yes > 0.95:
        return False
    return True


def _rotate_by_category(markets: list[dict], max_per_cat: int, total: int) -> list[dict]:
    """Distribuye mercados por categoría respetando max_per_cat y el total máximo."""
    by_cat: dict[str, list[dict]] = {}
    for m in markets:
        cat = _categorize_for_scan(m.get("question", ""))
        by_cat.setdefault(cat, []).append(m)
    result: list[dict] = []
    for cat_markets in by_cat.values():
        result.extend(cat_markets[:max_per_cat])
        if len(result) >= total:
            break
    return result[:total]


def _build_category_buckets(by_vol: list[dict]) -> dict[str, list[dict]]:
    """Clasifica mercados ya ordenados por volumen en buckets por categoría."""
    buckets: dict[str, list[dict]] = {
        cat: [] for cat in list(_SCAN_CATEGORIES.keys()) + ["other"]
    }
    for m in by_vol:
        cat = _categorize_for_scan(m.get("question", ""))
        buckets.setdefault(cat, []).append(m)
    return buckets


async def _get_alerted_market_ids_48h() -> set[str]:
    """Devuelve market_ids alertados a Telegram en las últimas 48h.
    Usa solo sent_at (un campo) para evitar índice compuesto Firestore.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    alerted: set[str] = set()
    try:
        docs = list(
            col("alerts_sent")
            .where(filter=FieldFilter("sent_at", ">=", cutoff))
            .stream()
        )
        for doc in docs:
            d = doc.to_dict()
            if d.get("type") == "polymarket":
                mid = d.get("market_id")
                if mid:
                    alerted.add(mid)
    except Exception:
        logger.warning("_get_alerted_market_ids_48h: error leyendo alerts_sent", exc_info=True)
    return alerted


async def fetch_active_markets(
    limit: int = 50, min_volume: float = 1000
) -> list[dict]:
    """
    GET /markets?active=true&order=volume24hr&limit={limit}
    Filtra: volume_24h >= min_volume AND end_date > now + 24h.
    Mercados sin end_date se descartan (no verificables).
    Guarda en Firestore poly_markets. Devuelve lista de mercados procesados.
    """
    now = datetime.now(timezone.utc)
    min_end_date = now + timedelta(hours=24)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": str(limit),
                },
            )
        if resp.status_code != 200:
            logger.error("fetch_active_markets: API respondio %d", resp.status_code)
            return []
        raw_data = resp.json()
        raw_markets = raw_data if isinstance(raw_data, list) else raw_data.get("markets", raw_data.get("data", []))
    except Exception:
        logger.error("fetch_active_markets: error de red", exc_info=True)
        return []

    markets: list[dict] = []
    saved = 0

    for raw in raw_markets:
        try:
            market = _parse_market(raw)
            if market is None:
                continue
            if market["volume_24h"] < min_volume:
                continue
            end_date = market.get("end_date")
            if not end_date or not isinstance(end_date, datetime):
                continue
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            if end_date < min_end_date:
                continue
            try:
                col("poly_markets").document(market["market_id"]).set(market)
                saved += 1
            except Exception:
                logger.error("fetch_active_markets: error guardando %s", market["market_id"], exc_info=True)
            markets.append(market)
        except Exception:
            logger.error("fetch_active_markets: error parseando mercado", exc_info=True)

    logger.info("fetch_active_markets: %d/%d mercados guardados (vol>%.0f)", saved, len(raw_markets), min_volume)
    return markets


def _parse_market(raw: dict) -> dict | None:
    """Normaliza un mercado de la Gamma API al schema interno poly_markets."""
    try:
        market_id = str(raw.get("id", raw.get("market_id", "")))
        if not market_id:
            return None
        condition_id = str(raw.get("conditionId", raw.get("condition_id", "")))
        question = str(raw.get("question", raw.get("title", "")))

        # outcomePrices puede ser: lista, JSON-string de lista, float directo, None.
        # Usar "or" para manejar None/[] (raw.get con default no aplica cuando la clave existe pero es None).
        price_yes_raw = raw.get("outcomePrices") or raw.get("price_yes") or []
        if isinstance(price_yes_raw, str):
            try:
                import json as _json
                _parsed = _json.loads(price_yes_raw)
                if isinstance(_parsed, list):
                    price_yes_raw = _parsed
                elif isinstance(_parsed, (int, float)):
                    # outcomePrices es un float directo (p.ej. "0.5" sin brackets)
                    price_yes_raw = [_parsed]
                else:
                    price_yes_raw = []
            except Exception:
                price_yes_raw = []
        if isinstance(price_yes_raw, (int, float)):
            price_yes_raw = [price_yes_raw]
        if isinstance(price_yes_raw, list) and len(price_yes_raw) >= 1:
            price_yes = float(price_yes_raw[0])
        else:
            _last = raw.get("lastTradePrice") or raw.get("bestBid") or raw.get("price_yes")
            price_yes = float(_last) if _last is not None and float(_last) != 0.0 else 0.5
            if price_yes == 0.5:
                logger.warning(
                    "_parse_market(%s): price_yes defaulted a 0.5 — "
                    "outcomePrices=%r lastTradePrice=%r bestBid=%r",
                    market_id,
                    raw.get("outcomePrices"),
                    raw.get("lastTradePrice"),
                    raw.get("bestBid"),
                )
        price_yes = max(0.0, min(1.0, price_yes))
        price_no = round(1.0 - price_yes, 4)

        volume_24h = float(
            raw.get("volume24hr")
            or raw.get("volume24hrClob")
            or raw.get("volume_24h")
            or 0.0
        )

        end_date: datetime | None = None
        # Polymarket Gamma API usa distintos nombres según el tipo de mercado
        end_date_raw = (
            raw.get("endDate")
            or raw.get("end_date")
            or raw.get("closeTime")
            or raw.get("close_time")
            or raw.get("resolutionTime")
            or raw.get("resolution_time")
            or raw.get("expiry")
            or raw.get("expiration")
            or ""
        )
        if end_date_raw:
            try:
                if isinstance(end_date_raw, datetime):
                    end_date = end_date_raw
                else:
                    end_date = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
            except Exception:
                logger.warning(
                    "_parse_market(%s): no se pudo parsear end_date_raw=%r",
                    market_id, end_date_raw,
                )
        if end_date is None:
            logger.warning("_parse_market(%s): end_date no encontrado en campos conocidos", market_id)
        else:
            _end_tz = end_date if end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
            if _end_tz < datetime.now(timezone.utc) - timedelta(hours=24):
                logger.debug("_parse_market(%s): expirado (end_date=%s) — descartado", market_id, _end_tz.date())
                return None

        slug = str(raw.get("slug", raw.get("market_slug", "")))

        created_at: datetime | None = None
        _cat_raw = (
            raw.get("createdAt") or raw.get("created_at")
            or raw.get("startDate") or raw.get("start_date")
        )
        if _cat_raw:
            try:
                created_at = (
                    _cat_raw if isinstance(_cat_raw, datetime)
                    else datetime.fromisoformat(str(_cat_raw).replace("Z", "+00:00"))
                )
            except Exception:
                pass

        return {
            "market_id": market_id,
            "condition_id": condition_id,
            "question": question,
            "slug": slug,
            "end_date": end_date,
            "created_at": created_at,
            "volume_24h": volume_24h,
            "price_yes": price_yes,
            "price_no": price_no,
            "active": bool(raw.get("active", not raw.get("closed", False))),
            "updated_at": datetime.now(timezone.utc),
        }
    except Exception:
        logger.error("_parse_market: error", exc_info=True)
        return None


async def _fetch_raw_pool(order: str = "volume24hr", limit: int = 500) -> list[dict]:
    """Fetch genérico de mercados activos de la Gamma API. Devuelve parsed list."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false",
                        "order": order, "ascending": "false", "limit": str(limit)},
            )
        if resp.status_code != 200:
            logger.error("_fetch_raw_pool(%s): API respondio %d", order, resp.status_code)
            return []
        raw_data = resp.json()
        raw_list = raw_data if isinstance(raw_data, list) else raw_data.get("markets", raw_data.get("data", []))
    except Exception:
        logger.error("_fetch_raw_pool(%s): error de red", order, exc_info=True)
        return []
    result = []
    for raw in raw_list:
        try:
            m = _parse_market(raw)
            if m:
                result.append(m)
        except Exception:
            pass
    return result


async def fetch_diverse_markets(min_volume: float = 500) -> list[dict]:
    """
    ~65 mercados por ciclo en 6 buckets con variedad forzada por categoría:
      - Crypto   10: top 10 crypto por volumen (BTC, ETH, SOL…)
      - Sports   10: top 10 deportes por volumen (NBA, UCL, F1…)
      - Politics 10: top 10 políticos por volumen (elecciones, congreso…)
      - Culture   5: top 5 cultura/entretenimiento (Oscar, Grammy…)
      - Other    10: geopolítica + ciencia + negocio + resto (máx 10 en total)
      - New      20: creados últimas 48h (precio aún no calibrado)
    Guarda en Firestore poly_markets. Devuelve lista combinada.
    """
    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)

    # Pool principal por volumen descendente
    vol_pool = await _fetch_raw_pool(order="volume24hr", limit=500)
    qualified = [m for m in vol_pool if _quality_ok(m, now)]
    by_vol = sorted(qualified, key=lambda m: m["volume_24h"], reverse=True)

    # Clasificar por categoría
    buckets = _build_category_buckets(by_vol)

    used_ids: set[str] = set()

    def _take(source: list[dict], n: int) -> list[dict]:
        result = [m for m in source if m["market_id"] not in used_ids][:n]
        used_ids.update(m["market_id"] for m in result)
        return result

    # Buckets específicos — variedad garantizada por categoría
    bucket_crypto   = _take(buckets["crypto"], _BUCKET_CRYPTO)
    # Sports usa los keywords extendidos (_is_sports_market) + categoría
    sports_all = [m for m in by_vol if _is_sports_market(m.get("question", ""))]
    bucket_sports   = _take(sports_all, _BUCKET_SPORTS)
    bucket_politics = _take(buckets["politics"], _BUCKET_POLITICS)
    bucket_culture  = _take(buckets["culture"], _BUCKET_CULTURE)

    # Other: geopolítica + ciencia + negocio + sin categoría — máximo 10 total
    other_pool = (
        buckets.get("geopolitics", [])
        + buckets.get("science", [])
        + buckets.get("business", [])
        + buckets.get("other", [])
    )
    bucket_other = _take(other_pool, _BUCKET_OTHER)

    # Nuevos (últimas 48h)
    new_pool = await _fetch_raw_pool(order="startDate", limit=150)
    new_qualified = [
        m for m in new_pool
        if _quality_ok(m, now)
        and m["market_id"] not in used_ids
        and m.get("created_at") is not None
        and (
            m["created_at"] if m["created_at"].tzinfo
            else m["created_at"].replace(tzinfo=timezone.utc)
        ) >= cutoff_48h
    ]
    bucket_new = new_qualified[:_BUCKET_NEW]

    combined = (
        bucket_crypto + bucket_sports + bucket_politics
        + bucket_culture + bucket_other + bucket_new
    )

    saved = 0
    for market in combined:
        try:
            col("poly_markets").document(market["market_id"]).set(market)
            saved += 1
        except Exception:
            logger.error("fetch_diverse_markets: error guardando %s", market["market_id"], exc_info=True)

    logger.info(
        "fetch_diverse_markets: crypto=%d sports=%d politics=%d culture=%d other=%d nuevos=%d total=%d saved=%d",
        len(bucket_crypto), len(bucket_sports), len(bucket_politics),
        len(bucket_culture), len(bucket_other), len(bucket_new),
        len(combined), saved,
    )
    return combined


async def fetch_market_orderbook(condition_id: str) -> dict:
    """
    GET https://clob.polymarket.com/order-book/{condition_id}
    Sin auth para lectura publica.
    Si falla → devuelve buy_ratio=0.5 (neutral, no crashear).
    buy_ratio = sum(bid sizes) / (sum(bid sizes) + sum(ask sizes))
    """
    neutral = {"bids": [], "asks": [], "spread": 0.0, "buy_ratio": 0.5}
    if not condition_id:
        return neutral
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{CLOB_API}/order-book/{condition_id}")
        if resp.status_code != 200:
            logger.debug("fetch_market_orderbook(%s): respondio %d", condition_id, resp.status_code)
            return neutral
        data = resp.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        bid_total = sum(float(b.get("size", 0)) for b in bids)
        ask_total = sum(float(a.get("size", 0)) for a in asks)
        total = bid_total + ask_total
        buy_ratio = (bid_total / total) if total > 0 else 0.5
        spread = 0.0
        if bids and asks:
            try:
                best_bid = max(float(b.get("price", 0)) for b in bids)
                best_ask = min(float(a.get("price", 1)) for a in asks)
                spread = round(best_ask - best_bid, 4)
            except Exception:
                pass
        return {"bids": bids[:10], "asks": asks[:10], "spread": spread, "buy_ratio": round(buy_ratio, 4)}
    except Exception as e:
        logger.warning("fetch_market_orderbook(%s): error — devolviendo neutral: %s", condition_id, e)
        return neutral
