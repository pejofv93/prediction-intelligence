"""
Gestor WebSocket para datos en tiempo real de Polymarket CLOB.
Loop infinito — ejecutar con asyncio.create_task() desde main.py.
"""
import logging

logger = logging.getLogger(__name__)

WS_CLOB = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_RTDS = "wss://ws-live-data.polymarket.com"


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
    # TODO: implementar en Sesion 5
    raise NotImplementedError


async def send_ping(ws) -> None:
    """
    Envia ping cada 30s para mantener la conexion viva.
    Si no se recibe pong → reconectar.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
