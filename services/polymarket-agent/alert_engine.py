"""
Motor de alertas Polymarket → telegram-bot.
Envia alerta si edge > 0.12 + confianza > 0.65 + (volume_spike OR smart_money).
"""
import logging

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE, TELEGRAM_BOT_URL

logger = logging.getLogger(__name__)


async def check_and_alert(analysis: dict) -> bool:
    """
    Envia alerta Telegram si:
      edge > POLY_MIN_EDGE (0.12)
      confidence > POLY_MIN_CONFIDENCE (0.65)
      volume_spike == True OR smart_money.is_smart_money == True
    Verifica en alerts_sent que no se haya enviado ya.
    NO usa on_snapshot — llama directamente POST {TELEGRAM_BOT_URL}/send-alert.
      Body: {"type": "polymarket", "data": analysis}
      Header: x-cloud-token
      Si falla el POST → loggear y continuar (no bloquear el pipeline)
    Devuelve True si envio alerta.
    """
    # TODO: implementar en Sesion 5
    raise NotImplementedError
