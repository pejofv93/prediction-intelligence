"""
Formateador y sender de alertas Telegram.
Flujo: sports-agent/polymarket-agent llaman POST /send-alert → alert_manager formatea y envia.
on_snapshot ELIMINADO — incompatible con min-instances=0.
"""
import logging
from datetime import datetime, timezone

import httpx

from shared.config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

_BOT_BASE = "https://api.telegram.org/bot"
_HTTP_TIMEOUT = 10.0


def _bot_url(method: str) -> str:
    return f"{_BOT_BASE}{TELEGRAM_TOKEN}/{method}"


def _escape_md(text: str) -> str:
    """Escapa caracteres conflictivos en Telegram MarkdownV1 (solo _ y `)."""
    return str(text).replace("_", "\\_").replace("`", "\\`")


async def send_message(text: str, chat_id: str | int | None = None, parse_mode: str = "Markdown") -> None:
    """Envia mensaje a chat_id (o TELEGRAM_CHAT_ID si no se especifica) via Bot API."""
    if not TELEGRAM_TOKEN:
        logger.warning("send_message: TELEGRAM_TOKEN no configurado — mensaje no enviado")
        return

    target = str(chat_id) if chat_id is not None else str(TELEGRAM_CHAT_ID or "")
    if not target:
        logger.warning("send_message: sin chat_id destino — mensaje no enviado")
        return

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                _bot_url("sendMessage"),
                json={
                    "chat_id": target,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
        if resp.status_code != 200:
            logger.error("send_message: Bot API respondio %d — %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.error("send_message: error enviando mensaje", exc_info=True)


def _format_sports_alert(prediction: dict) -> str:
    """Formatea prediccion deportiva segun el formato del spec."""
    home = _escape_md(prediction.get("home_team", "?"))
    away = _escape_md(prediction.get("away_team", "?"))
    league = _escape_md(prediction.get("league", "?"))
    team_to_back = _escape_md(prediction.get("team_to_back", "?"))

    match_date = prediction.get("match_date")
    if hasattr(match_date, "strftime"):
        date_str = match_date.strftime("%d/%m/%Y %H:%M UTC")
    else:
        date_str = str(match_date)[:16] if match_date else "?"

    odds = float(prediction.get("odds", 0))
    edge = float(prediction.get("edge", 0))
    confidence = float(prediction.get("confidence", 0))
    kelly = float(prediction.get("kelly_fraction", 0))

    factors = prediction.get("factors", {})
    poisson = float(factors.get("poisson", 0))
    elo = float(factors.get("elo", 0))
    form = float(factors.get("form", 0))
    h2h = float(factors.get("h2h", 0))

    return (
        f"⚽ SEÑAL DETECTADA\n\n"
        f"🏟 {home} vs {away}\n"
        f"🏆 {league} | 📅 {date_str}\n\n"
        f"✅ Apostar a: *{team_to_back}*\n"
        f"💰 Cuota: *{odds:.2f}*\n"
        f"📊 Edge: *+{edge:.1%}* | Confianza: *{confidence:.0%}*\n\n"
        f"Señales del modelo:\n"
        f"• Poisson: {poisson:.0%} | ELO: {elo:.0%}\n"
        f"• Forma: {form:.0%} | H2H: {h2h:.0%}\n\n"
        f"🧮 Kelly sugerido: {kelly:.1%} del bankroll\n\n"
        f"⚠️ Apuesta responsablemente. No es asesoramiento financiero."
    )


def _format_poly_alert(analysis: dict) -> str:
    """Formatea senal de Polymarket segun el formato del spec."""
    question = _escape_md(analysis.get("question", "?"))
    market_price_yes = float(analysis.get("market_price_yes", 0))
    real_prob = float(analysis.get("real_prob", 0))
    edge = float(analysis.get("edge", 0))
    confidence = float(analysis.get("confidence", 0))
    recommendation = analysis.get("recommendation", "PASS")
    reasoning = _escape_md(str(analysis.get("reasoning", ""))[:300])
    volume_spike = bool(analysis.get("volume_spike", False))
    smart_money = bool(analysis.get("smart_money_detected", False))

    smart_money_line = ""
    if volume_spike or smart_money:
        smart_money_line = "\n🐋 *SMART MONEY detectado*"

    return (
        f"🔮 OPORTUNIDAD POLYMARKET\n\n"
        f"❓ {question}\n\n"
        f"📈 Precio mercado YES: *{market_price_yes:.0%}*\n"
        f"🎯 Probabilidad estimada: *{real_prob:.0%}*\n"
        f"💎 Edge: *+{edge:.0%}* | Confianza: *{confidence:.0%}*\n\n"
        f"Recomendación: *{recommendation}*"
        f"{smart_money_line}\n\n"
        f"💭 {reasoning}\n\n"
        f"⚠️ Apuesta responsablemente. No es asesoramiento financiero."
    )


def _alert_key(data: dict, edge: float) -> str:
    """Genera clave de deduplicacion."""
    id_field = data.get("match_id") or data.get("market_id") or "unknown"
    return f"{id_field}_{round(edge, 2)}"


async def send_sports_alert(prediction: dict) -> bool:
    """
    Formatea prediccion deportiva y envia a TELEGRAM_CHAT_ID.
    Verifica en alerts_sent que no se haya enviado ya (deduplicacion).
    Devuelve True si envio.
    """
    from shared.firestore_client import col

    edge = float(prediction.get("edge", 0))
    key = _alert_key(prediction, edge)

    try:
        existing = list(col("alerts_sent").where("alert_key", "==", key).limit(1).stream())
        if existing:
            logger.debug("send_sports_alert: alerta duplicada omitida (%s)", key)
            return False
    except Exception:
        logger.error("send_sports_alert: error comprobando dedup", exc_info=True)

    text = _format_sports_alert(prediction)
    await send_message(text)

    try:
        col("alerts_sent").add({
            "alert_key": key,
            "sent_at": datetime.now(timezone.utc),
            "type": "sports",
        })
    except Exception:
        logger.error("send_sports_alert: error guardando en alerts_sent", exc_info=True)

    logger.info("send_sports_alert: alerta enviada para %s (edge=%.3f)", key, edge)
    return True


async def send_poly_alert(analysis: dict) -> bool:
    """
    Formatea senal de Polymarket y envia a TELEGRAM_CHAT_ID.
    Verifica en alerts_sent. Devuelve True si envio.
    """
    from shared.firestore_client import col

    edge = float(analysis.get("edge", 0))
    key = _alert_key(analysis, edge)

    try:
        existing = list(col("alerts_sent").where("alert_key", "==", key).limit(1).stream())
        if existing:
            logger.debug("send_poly_alert: alerta duplicada omitida (%s)", key)
            return False
    except Exception:
        logger.error("send_poly_alert: error comprobando dedup", exc_info=True)

    text = _format_poly_alert(analysis)
    await send_message(text)

    try:
        col("alerts_sent").add({
            "alert_key": key,
            "sent_at": datetime.now(timezone.utc),
            "type": "polymarket",
        })
    except Exception:
        logger.error("send_poly_alert: error guardando en alerts_sent", exc_info=True)

    logger.info("send_poly_alert: alerta enviada para %s (edge=%.3f)", key, edge)
    return True
