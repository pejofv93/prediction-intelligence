"""
Formateador y sender de alertas Telegram.
Flujo: sports-agent/polymarket-agent llaman POST /send-alert → alert_manager formatea y envia.
on_snapshot ELIMINADO — incompatible con min-instances=0.
"""
import logging

from shared.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def send_sports_alert(prediction: dict) -> bool:
    """
    Formatea prediccion deportiva y envia a TELEGRAM_CHAT_ID.
    Verifica en alerts_sent que no se haya enviado ya (deduplicacion).
    Devuelve True si envio.

    Formato:
    ⚽ SENAL DETECTADA
    🏟 {home_team} vs {away_team}
    🏆 {league} | 📅 {match_date}
    ✅ Apostar a: *{team_to_back}*
    💰 Cuota: *{odds}*
    📊 Edge: *+{edge:.1%}* | Confianza: *{confidence:.0%}*
    Senales del modelo:
    • Poisson: {poisson:.0%} | ELO: {elo:.0%}
    • Forma: {form:.0%} | H2H: {h2h:.0%}
    🧮 Kelly sugerido: {kelly_fraction:.1%} del bankroll
    ⚠️ Apuesta responsablemente. No es asesoramiento financiero.
    """
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def send_poly_alert(analysis: dict) -> bool:
    """
    Formatea senal de Polymarket y envia a TELEGRAM_CHAT_ID.
    Verifica en alerts_sent. Devuelve True si envio.

    Formato:
    🔮 OPORTUNIDAD POLYMARKET
    ❓ {question}
    📈 Precio mercado YES: *{market_price_yes:.0%}*
    🎯 Probabilidad estimada: *{real_prob:.0%}*
    💎 Edge: *+{edge:.0%}* | Confianza: *{confidence:.0%}*
    Recomendacion: *{recommendation}*
    💭 {reasoning}
    ⚠️ Apuesta responsablemente. No es asesoramiento financiero.
    """
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def send_message(text: str) -> None:
    """Envia mensaje raw a TELEGRAM_CHAT_ID via Bot API."""
    # TODO: implementar en Sesion 6
    raise NotImplementedError
