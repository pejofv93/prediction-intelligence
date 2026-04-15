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
    # TODO: implementar en Sesion 5
    raise NotImplementedError


def detect_price_velocity(
    price_history: list[dict], window_minutes: int = 5
) -> dict:
    """
    velocity = (current_price - price_N_ago) / price_N_ago
    Devuelve {velocity, trend: "ACCELERATING"|"DECELERATING"|"STABLE"}
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError


def detect_large_trade(
    trade_event: dict, market_avg_trade_usd: float
) -> bool:
    """True si trade_size * price > 5x el tamano medio del mercado."""
    # TODO: implementar en Sesion 5
    raise NotImplementedError
