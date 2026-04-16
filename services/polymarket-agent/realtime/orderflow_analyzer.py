"""
Analizador de flujo de ordenes en tiempo real (eventos WebSocket).
"""
import logging

logger = logging.getLogger(__name__)


def analyze_orderbook_snapshot(book_event: dict) -> dict:
    """
    Calcula: buy_pressure, spread, depth, imbalance_signal.
    buy_pressure = sum(bid_sizes) / (sum(bid_sizes) + sum(ask_sizes))
    imbalance: "STRONG_BUY">0.70, "BUY">0.60, "NEUTRAL", "SELL"<0.40, "STRONG_SELL"<0.30
    """
    try:
        bids = book_event.get("bids", [])
        asks = book_event.get("asks", [])

        bid_total = sum(float(b.get("size", 0)) for b in bids)
        ask_total = sum(float(a.get("size", 0)) for a in asks)
        total = bid_total + ask_total

        buy_pressure = round(bid_total / total, 4) if total > 0 else 0.5
        depth = round(total, 2)

        spread = 0.0
        if bids and asks:
            try:
                best_bid = max(float(b.get("price", 0)) for b in bids)
                best_ask = min(float(a.get("price", 1)) for a in asks)
                spread = round(best_ask - best_bid, 4)
            except Exception:
                pass

        if buy_pressure > 0.70:
            imbalance_signal = "STRONG_BUY"
        elif buy_pressure > 0.60:
            imbalance_signal = "BUY"
        elif buy_pressure < 0.30:
            imbalance_signal = "STRONG_SELL"
        elif buy_pressure < 0.40:
            imbalance_signal = "SELL"
        else:
            imbalance_signal = "NEUTRAL"

        return {
            "buy_pressure": buy_pressure,
            "spread": spread,
            "depth": depth,
            "imbalance_signal": imbalance_signal,
        }
    except Exception:
        logger.error("analyze_orderbook_snapshot: error", exc_info=True)
        return {"buy_pressure": 0.5, "spread": 0.0, "depth": 0.0, "imbalance_signal": "NEUTRAL"}


def detect_price_velocity(
    price_history: list[dict], window_minutes: int = 5
) -> dict:
    """
    velocity = (current_price - price_N_ago) / price_N_ago
    Devuelve {velocity, trend: "ACCELERATING"|"DECELERATING"|"STABLE"}
    """
    try:
        if len(price_history) < 2:
            return {"velocity": 0.0, "trend": "STABLE"}

        from datetime import timezone
        now_price = float(price_history[-1].get("price", 0.5))
        cutoff_ago = None

        for entry in reversed(price_history[:-1]):
            ts = entry.get("timestamp")
            if ts is None:
                continue
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            latest_ts = price_history[-1].get("timestamp")
            if hasattr(latest_ts, "tzinfo") and latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=timezone.utc)
            if latest_ts and (latest_ts - ts).total_seconds() / 60 >= window_minutes:
                cutoff_ago = entry
                break

        if cutoff_ago is None:
            cutoff_ago = price_history[0]

        old_price = float(cutoff_ago.get("price", 0.5))
        if old_price == 0:
            return {"velocity": 0.0, "trend": "STABLE"}

        velocity = round((now_price - old_price) / old_price, 4)

        # Trend: comparar con primera mitad del periodo
        mid_idx = len(price_history) // 2
        mid_price = float(price_history[mid_idx].get("price", old_price))
        if mid_price != 0:
            early_velocity = (mid_price - old_price) / old_price
            late_velocity = (now_price - mid_price) / mid_price if mid_price != 0 else 0
            if abs(late_velocity) > abs(early_velocity) * 1.2:
                trend = "ACCELERATING"
            elif abs(late_velocity) < abs(early_velocity) * 0.8:
                trend = "DECELERATING"
            else:
                trend = "STABLE"
        else:
            trend = "STABLE"

        return {"velocity": velocity, "trend": trend}
    except Exception:
        logger.error("detect_price_velocity: error", exc_info=True)
        return {"velocity": 0.0, "trend": "STABLE"}


def detect_large_trade(
    trade_event: dict, market_avg_trade_usd: float
) -> bool:
    """True si trade_size * price > 5x el tamano medio del mercado."""
    try:
        size = float(trade_event.get("size", 0))
        price = float(trade_event.get("price", 0.5))
        trade_usd = size * price
        if market_avg_trade_usd <= 0:
            return trade_usd > 1000  # fallback: considerar grande si > $1000
        return trade_usd > (market_avg_trade_usd * 5)
    except Exception:
        logger.error("detect_large_trade: error", exc_info=True)
        return False
