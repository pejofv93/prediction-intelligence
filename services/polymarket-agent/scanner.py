"""
Scanner Polymarket — fetch top 50 mercados activos por volumen.
Guarda en Firestore poly_markets.
IMPORTANTE: guardar "conditionId" como "condition_id" — necesario para CLOB orderbook.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.firestore_client import col

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
_HTTP_TIMEOUT = 20.0


async def fetch_active_markets(
    limit: int = 50, min_volume: float = 1000
) -> list[dict]:
    """
    GET /markets?active=true&order=volume24hr&limit={limit}
    Filtra: volume_24h >= min_volume AND end_date > now + 2 days.
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
            if end_date and isinstance(end_date, datetime):
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

        return {
            "market_id": market_id,
            "condition_id": condition_id,
            "question": question,
            "end_date": end_date,
            "volume_24h": volume_24h,
            "price_yes": price_yes,
            "price_no": price_no,
            "active": bool(raw.get("active", not raw.get("closed", False))),
            "updated_at": datetime.now(timezone.utc),
        }
    except Exception:
        logger.error("_parse_market: error", exc_info=True)
        return None


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
    except Exception:
        logger.error("fetch_market_orderbook(%s): error — devolviendo neutral", condition_id, exc_info=True)
        return neutral
