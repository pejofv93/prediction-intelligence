"""
Gestor WebSocket para datos en tiempo real de Polymarket CLOB.
Loop infinito — ejecutar con asyncio.create_task() desde main.py.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WS_CLOB = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_RTDS = "wss://ws-live-data.polymarket.com"

_PING_INTERVAL = 30   # segundos entre pings
_MAX_BACKOFF = 60     # segundos maximos de espera entre reconexiones


async def _save_realtime_event(event: dict) -> None:
    """Guarda evento en Firestore realtime_events."""
    try:
        from shared.firestore_client import col
        col("realtime_events").add(event)
    except Exception:
        logger.error("_save_realtime_event: error guardando", exc_info=True)


async def _handle_event(raw_event: dict, condition_to_market: dict[str, str]) -> None:
    """Procesa un evento WebSocket del CLOB."""
    from realtime.orderflow_analyzer import analyze_orderbook_snapshot, detect_large_trade

    try:
        event_type = raw_event.get("event_type") or raw_event.get("type", "")
        asset_id = raw_event.get("asset_id") or raw_event.get("market", "")
        market_id = condition_to_market.get(asset_id, asset_id)
        now = datetime.now(timezone.utc)

        if event_type == "book":
            snapshot = analyze_orderbook_snapshot(raw_event)
            await _save_realtime_event({
                "market_id": market_id,
                "condition_id": asset_id,
                "event_type": "book",
                "timestamp": now,
                "best_bid": raw_event.get("best_bid"),
                "best_ask": raw_event.get("best_ask"),
                "trade_price": None,
                "trade_size": None,
                "buy_pressure": snapshot.get("buy_pressure"),
                "is_large_trade": False,
            })

        elif event_type in ("price_change", "last_trade_price"):
            price = float(raw_event.get("price", 0))
            size = float(raw_event.get("size", 0))
            is_large = detect_large_trade({"price": price, "size": size}, market_avg_trade_usd=500.0)

            await _save_realtime_event({
                "market_id": market_id,
                "condition_id": asset_id,
                "event_type": event_type,
                "timestamp": now,
                "best_bid": None,
                "best_ask": None,
                "trade_price": price,
                "trade_size": size,
                "buy_pressure": None,
                "is_large_trade": is_large,
            })

            if is_large:
                logger.info(
                    "WS: trade grande detectado en %s — $%.0f",
                    market_id, size * price,
                )

    except Exception:
        logger.error("_handle_event: error procesando evento", exc_info=True)


async def send_ping(ws) -> None:
    """
    Envia ping cada 30s para mantener la conexion viva.
    Si no se recibe pong → reconectar.
    """
    try:
        await ws.ping()
    except Exception:
        logger.debug("send_ping: fallo al enviar ping")
        raise  # propagar para que el caller reconecte


async def start_monitoring(top_n_markets: int = 20) -> None:
    """
    1. Lee top N mercados de Firestore poly_markets (por volume_24h)
    2. Extrae condition_ids
    3. Conecta WebSocket y suscribe: {"assets_ids": [...], "type": "market"}
    4. Por cada evento recibido:
       - "book" → analyze_orderbook_snapshot() → guarda realtime_events
       - "price_change" → guarda realtime_events + comprueba anomalias
       - "last_trade_price" → detecta large trades → smart_money_detector si > umbral
    5. Reconexion automatica con backoff exponencial (1s, 2s, 4s, 8s...)
    6. Loop infinito — ejecutar con asyncio.create_task() desde main.py
    """
    import websockets

    from shared.firestore_client import col

    backoff = 1

    while True:
        try:
            # Leer top N mercados activos por volumen
            docs = (
                col("poly_markets")
                .order_by("volume_24h", direction="DESCENDING")
                .limit(top_n_markets)
                .stream()
            )
            markets = [d.to_dict() for d in docs]

            if not markets:
                logger.warning("start_monitoring: sin mercados en Firestore — reintentando en 60s")
                await asyncio.sleep(60)
                continue

            condition_ids = [m["condition_id"] for m in markets if m.get("condition_id")]
            condition_to_market = {m["condition_id"]: m["market_id"] for m in markets if m.get("condition_id")}

            if not condition_ids:
                logger.warning("start_monitoring: sin condition_ids — reintentando en 60s")
                await asyncio.sleep(60)
                continue

            logger.info("start_monitoring: conectando WS para %d mercados", len(condition_ids))

            async with websockets.connect(WS_CLOB) as ws:
                backoff = 1  # reset backoff al conectar con exito

                # Suscribir a todos los mercados
                subscribe_msg = json.dumps({
                    "assets_ids": condition_ids,
                    "type": "market",
                })
                await ws.send(subscribe_msg)
                logger.info("start_monitoring: suscrito a %d mercados", len(condition_ids))

                ping_task = asyncio.create_task(_ping_loop(ws))

                try:
                    async for raw_msg in ws:
                        try:
                            event = json.loads(raw_msg)
                            if isinstance(event, list):
                                for e in event:
                                    await _handle_event(e, condition_to_market)
                            elif isinstance(event, dict):
                                await _handle_event(event, condition_to_market)
                        except json.JSONDecodeError:
                            pass  # mensajes no-JSON (pong, etc.) — ignorar
                        except Exception:
                            logger.error("start_monitoring: error procesando mensaje", exc_info=True)
                finally:
                    ping_task.cancel()

        except Exception as e:
            logger.error("start_monitoring: conexion perdida (%s) — reintentando en %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)


async def _ping_loop(ws) -> None:
    """Envia ping cada _PING_INTERVAL segundos para mantener la conexion."""
    while True:
        await asyncio.sleep(_PING_INTERVAL)
        try:
            await send_ping(ws)
        except Exception:
            logger.debug("_ping_loop: fallo en ping — cerrando loop")
            break
