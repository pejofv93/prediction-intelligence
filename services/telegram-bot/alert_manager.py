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


_SPORT_EMOJI = {
    "football": "⚽",
    "nba": "🏀",
    "basketball": "🏀",
    "euroleague": "🏀",
    "tennis": "🎾",
}

_LEAGUE_LABEL = {
    "SA": "Serie A", "PD": "La Liga", "BL1": "Bundesliga", "FL1": "Ligue 1",
    "BL2": "2. Bundesliga", "FL2": "Ligue 2", "CL": "Champions League",
    "EL": "Europa League", "ECL": "Conference League", "PPL": "Primeira Liga",
    "DED": "Eredivisie", "SD": "Segunda División", "SB": "Serie B",
    "TU1": "Süper Lig", "NBA": "NBA", "EUROLEAGUE": "EuroLeague",
}


def _format_sports_alert(prediction: dict) -> str:
    """Formatea predicción deportiva. Soporta market_type h2h y totals."""
    home = _escape_md(prediction.get("home_team", "?"))
    away = _escape_md(prediction.get("away_team", "?"))
    league_code = prediction.get("league", "?")
    league = _escape_md(_LEAGUE_LABEL.get(league_code, league_code))
    sport = prediction.get("sport", "football")
    market_type = prediction.get("market_type", "h2h")
    emoji = _SPORT_EMOJI.get(sport, "🏟")

    match_date = prediction.get("match_date")
    if hasattr(match_date, "strftime"):
        date_str = match_date.strftime("%d/%m %H:%M UTC")
    else:
        raw = str(match_date or "")
        date_str = raw[:16].replace("T", " ") if raw else "?"

    odds = float(prediction.get("odds", 0))
    edge = float(prediction.get("edge", 0))
    confidence = float(prediction.get("confidence", 0))
    kelly = float(prediction.get("kelly_fraction", 0))

    header = f"{emoji} SEÑAL DETECTADA\n\n"
    match_line = f"🏟 {home} vs {away}\n🏆 {league} | 📅 {date_str}\n\n"

    if market_type == "totals":
        selection = _escape_md(prediction.get("selection", "Over 2.5"))
        factors = prediction.get("factors", {})
        xg_total = factors.get("expected_total", "?")
        home_xg = factors.get("home_xg")
        away_xg = factors.get("away_xg")
        xg_line = ""
        if home_xg is not None and away_xg is not None:
            xg_line = f"• xG: {float(home_xg):.2f} + {float(away_xg):.2f} = {float(xg_total):.2f} esp.\n"
        bet_line = f"✅ Apostar: *{selection} goles @ {odds:.2f}*\n"
        stats_block = (
            f"📊 Edge: *+{edge:.1%}* | Confianza: *{confidence:.0%}*\n\n"
            f"Modelo Poisson:\n"
            f"{xg_line}"
        )
    else:
        team_to_back = _escape_md(prediction.get("team_to_back", "?"))
        signals = prediction.get("signals", prediction.get("factors", {}))
        poisson = float(signals.get("poisson") or 0)
        elo = float(signals.get("elo") or 0)
        form = float(signals.get("form") or 0)
        h2h_sig = float(signals.get("h2h") or 0)
        bet_line = f"✅ Apostar a: *{team_to_back}* a ganar @ *{odds:.2f}*\n"
        stats_block = (
            f"📊 Edge: *+{edge:.1%}* | Confianza: *{confidence:.0%}*\n\n"
            f"Señales del modelo:\n"
            f"• Poisson: {poisson:.0%} | ELO: {elo:.0%}\n"
            f"• Forma: {form:.0%} | H2H: {h2h_sig:.0%}\n"
        )

    kelly_line = f"\n🧮 Kelly sugerido: {kelly:.1%} del bankroll\n\n"
    footer = "⚠️ Apuesta responsablemente. No es asesoramiento financiero."

    return header + match_line + bet_line + stats_block + kelly_line + footer


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
