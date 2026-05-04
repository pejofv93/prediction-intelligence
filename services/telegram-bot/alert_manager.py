"""
Formateador y sender de alertas Telegram.
Flujo: sports-agent/polymarket-agent llaman POST /send-alert → alert_manager formatea y envia.
on_snapshot ELIMINADO — incompatible con min-instances=0.
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

_BOT_BASE = "https://api.telegram.org/bot"
_HTTP_TIMEOUT = 10.0


def _bot_url(method: str) -> str:
    return f"{_BOT_BASE}{TELEGRAM_TOKEN}/{method}"


def _escape_md(text: str) -> str:
    """Escapa caracteres conflictivos en Telegram MarkdownV1 (solo _ y `)."""
    return str(text).replace("_", "\\_").replace("`", "\\`")


def _truncate_reasoning(text: str, max_chars: int = 800) -> str:
    """Trunca en el último punto/exclamación/interrogación para no cortar a mitad de frase."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    for sep in (".", "!", "?"):
        last = truncated.rfind(sep)
        if last > max_chars // 2:
            return truncated[: last + 1]
    return truncated


async def send_message(
    text: str,
    chat_id: str | int | None = None,
    parse_mode: str = "Markdown",
    message_thread_id: int | None = None,
) -> bool:
    """
    Envia mensaje a chat_id via Bot API. Devuelve True si el envio fue exitoso.
    Reintenta hasta 3 veces en 429 usando retry_after del response body.
    Devuelve False sin reintentar en cualquier otro error.
    message_thread_id: topic thread en supergrupos (Sports=4, Polymarket=2, None=General).
    """
    if not TELEGRAM_TOKEN:
        logger.warning("send_message: TELEGRAM_TOKEN no configurado")
        return False

    target = str(chat_id) if chat_id is not None else str(TELEGRAM_CHAT_ID or "")
    if not target:
        logger.warning("send_message: sin chat_id destino")
        return False

    payload = {
        "chat_id": target,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(_bot_url("sendMessage"), json=payload)

            if resp.status_code == 200:
                return True

            if resp.status_code == 429:
                try:
                    retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    retry_after = 5
                logger.warning(
                    "send_message: 429 Too Many Requests — esperando %ds (intento %d/3)",
                    retry_after, attempt + 1,
                )
                await asyncio.sleep(retry_after)
                continue

            logger.error("send_message: Bot API respondio %d — %s", resp.status_code, resp.text[:200])
            return False

        except Exception:
            logger.error("send_message: error en intento %d/3", attempt + 1, exc_info=True)
            if attempt < 2:
                await asyncio.sleep(2)

    logger.error("send_message: todos los reintentos fallaron")
    return False


_SPORT_EMOJI = {
    "football": "⚽", "nba": "🏀", "basketball": "🏀",
    "euroleague": "🏀", "tennis": "🎾",
}

_LEAGUE_LABEL = {
    "SA": "Serie A", "PD": "La Liga", "BL1": "Bundesliga", "FL1": "Ligue 1",
    "CL": "Champions League", "EL": "Europa League", "ECL": "Conference League",
    "TU1": "Süper Lig", "NBA": "NBA", "EUROLEAGUE": "EuroLeague",
    "ATP_FRENCH_OPEN": "ATP Roland Garros", "WTA_FRENCH_OPEN": "WTA Roland Garros",
    "ATP_WIMBLEDON": "ATP Wimbledon", "WTA_WIMBLEDON": "WTA Wimbledon",
    "ATP_US_OPEN": "ATP US Open", "WTA_US_OPEN": "WTA US Open",
    "ATP_BARCELONA": "ATP Barcelona", "ATP_MUNICH": "ATP Munich",
    "WTA_STUTTGART": "WTA Stuttgart",
}

_MARKET_LABEL = {
    "h2h": "1X2", "totals": "Goles O/U", "btts": "Ambos marcan",
    "double_chance": "Doble oportunidad", "asian_handicap": "Hándicap asiático",
    "totals_3.5": "Goles O/U 3.5", "set_handicap": "Hándicap sets",
    "total_sets": "Total sets", "total_games": "Total games",
    "spread": "Hándicap puntos", "player_points": "Puntos jugador",
    "player_rebounds": "Rebotes jugador", "player_assists": "Asistencias jugador",
}


def _format_alert_unified(prediction: dict) -> str:
    """Formato unificado para todos los deportes y mercados."""
    sport = prediction.get("sport", "football")
    market_type = prediction.get("market_type", "h2h")
    edge = float(prediction.get("edge") or 0)
    conf = float(prediction.get("confidence") or 0)
    odds = float(prediction.get("odds") or 0)
    kelly = float(prediction.get("kelly_fraction") or 0)
    league_code = prediction.get("league", "?")

    # Intensidad
    if edge >= 0.15:
        intensity = "🔥 SEÑAL FUERTE"
    elif edge >= 0.10:
        intensity = "✅ SEÑAL DETECTADA"
    else:
        intensity = "📊 SEÑAL MODERADA"

    sport_emoji = _SPORT_EMOJI.get(sport, "🏟")
    league_label = _escape_md(_LEAGUE_LABEL.get(league_code, league_code))
    home = _escape_md(prediction.get("home_team", "?"))
    away = _escape_md(prediction.get("away_team", "?"))

    market_label = _escape_md(_MARKET_LABEL.get(market_type, market_type.replace("_", " ").title()))

    selection = prediction.get("selection") or prediction.get("team_to_back", "?")
    selection = _escape_md(str(selection))

    # Top 3 factores por valor absoluto
    signals = prediction.get("signals") or prediction.get("factors") or {}
    top3 = sorted(signals.items(), key=lambda x: -abs(float(x[1])))[:3]
    factors_text = ", ".join(f"{k.replace('_', ' ')} {float(v):.2f}" for k, v in top3) or "—"

    return (
        f"{intensity} | {sport_emoji} {league_label}\n"
        f"{home} vs {away}\n"
        f"Mercado: {market_label} | Selección: *{selection}*\n"
        f"Cuota: *{odds:.2f}* | Edge: *+{edge:.1%}* | Confianza: *{conf:.0%}*\n"
        f"Factores: {factors_text}\n"
        f"🧮 Kelly: {kelly:.1%} del bankroll\n\n"
        f"⚠️ Apuesta responsablemente. No es asesoramiento financiero."
    )


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

    odds = float(prediction.get("odds") or 0)
    edge = float(prediction.get("edge") or 0)
    confidence = float(prediction.get("confidence") or 0)
    kelly = float(prediction.get("kelly_fraction") or 0)

    if edge >= 0.15:
        label = "🔥 SEÑAL FUERTE"
    elif edge >= 0.10:
        label = "✅ SEÑAL DETECTADA"
    else:
        label = "📊 SEÑAL MODERADA"
    header = f"{emoji} {label}\n\n"
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


_CATEGORY_EMOJI = {
    "crypto":      "🪙",
    "sports":      "🏀",
    "geopolitics": "🌍",
    "economy":     "📉",
    "politics":    "🗳️",
}


def _format_poly_alert(analysis: dict) -> str:
    """Formatea senal de Polymarket con categoria, intensidad, cierre y volumen."""
    from datetime import datetime, timezone

    question = _escape_md(analysis.get("question", "?"))
    market_price_yes = float(analysis.get("market_price_yes", 0))
    real_prob = float(analysis.get("real_prob", 0))
    edge = float(analysis.get("edge") or 0)
    abs_edge = abs(edge)
    confidence = float(analysis.get("confidence") or 0)
    recommendation = analysis.get("recommendation", "PASS")
    reasoning = _escape_md(_truncate_reasoning(str(analysis.get("reasoning", ""))))
    volume_spike = bool(analysis.get("volume_spike", False))
    smart_money = bool(analysis.get("smart_money_detected", False))
    category = str(analysis.get("category") or "").lower()
    volume_24h = float(analysis.get("volume_24h") or 0)

    # Intensidad basada en edge absoluto
    if abs_edge > 0.15:
        intensity = "🔴 SEÑAL FUERTE"
    elif abs_edge > 0.10:
        intensity = "🟡 SEÑAL DETECTADA"
    else:
        intensity = "🟢 SEÑAL MODERADA"

    # Categoría con emoji
    cat_emoji = _CATEGORY_EMOJI.get(category, "🔮")
    cat_line = f"{cat_emoji} {category}\n" if category else ""

    # Días hasta cierre — icono según urgencia (>30d filtrado en alert_engine)
    close_line = ""
    end_date_iso = analysis.get("end_date_iso")
    if end_date_iso:
        try:
            end_dt = datetime.fromisoformat(str(end_date_iso))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            days_left = (end_dt - datetime.now(timezone.utc)).days
            date_label = end_dt.strftime("%d/%m")
            if days_left <= 7:
                time_icon = "⚡"
            elif days_left <= 14:
                time_icon = "📅"
            else:
                time_icon = "🕐"
            close_line = f"{time_icon} Cierra en {days_left}d ({date_label})"
        except Exception:
            pass

    # Volumen 24h
    vol_line = ""
    if volume_24h > 0:
        if volume_24h >= 1_000_000:
            vol_str = f"${volume_24h / 1_000_000:.1f}M"
        elif volume_24h >= 1_000:
            vol_str = f"${volume_24h:,.0f}"
        else:
            vol_str = f"${volume_24h:.0f}"
        vol_line = f"💧 Vol 24h: {vol_str}"

    # Línea combinada cierre + volumen
    meta_parts = [p for p in (close_line, vol_line) if p]
    meta_line = (" | ".join(meta_parts) + "\n") if meta_parts else ""

    # Smart money
    smart_line = "\n🧠 *SMART MONEY detectado*\n" if (volume_spike or smart_money) else "\n"

    # Link directo al mercado
    slug = str(analysis.get("slug") or "")
    link_line = f"🔗 [Ver mercado](https://polymarket.com/event/{slug})\n" if slug else ""

    return (
        f"🔮 OPORTUNIDAD POLYMARKET — {intensity}\n"
        f"{cat_line}"
        f"\n❓ {question}\n\n"
        f"💰 Precio YES: *{market_price_yes:.0%}* → IA: *{real_prob:.0%}* (*{edge:+.0%}* edge)\n"
        f"🎯 Confianza: *{confidence:.0%}* | Recomendación: *{recommendation}*\n"
        f"{meta_line}"
        f"{link_line}"
        f"{smart_line}"
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

    edge = float(prediction.get("edge") or 0)
    key = _alert_key(prediction, edge)

    try:
        existing = list(col("alerts_sent").where(filter=FieldFilter("alert_key", "==", key)).limit(1).stream())
        if existing:
            logger.debug("send_sports_alert: alerta duplicada omitida (%s)", key)
            return False
    except Exception:
        logger.error("send_sports_alert: error comprobando dedup", exc_info=True)

    text = _format_alert_unified(prediction)
    sent = await send_message(text, message_thread_id=4)

    if not sent:
        logger.error("send_sports_alert: fallo al enviar — NO guardado en alerts_sent (%s)", key)
        return False

    await asyncio.sleep(1.1)  # Telegram: max 1 msg/seg por chat

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

    edge = float(analysis.get("edge") or 0)
    key = _alert_key(analysis, edge)

    try:
        existing = list(
            col("alerts_sent")
            .where(filter=FieldFilter("alert_key", "==", key))
            .where(filter=FieldFilter("status", "==", "sent"))
            .limit(1)
            .stream()
        )
        if existing:
            logger.debug("send_poly_alert: alerta duplicada omitida (%s)", key)
            return False
    except Exception:
        logger.error("send_poly_alert: error comprobando dedup", exc_info=True)

    text = _format_alert_unified(analysis) if analysis.get("sport") else _format_poly_alert(analysis)
    sent = await send_message(text, message_thread_id=3)

    if not sent:
        logger.error("send_poly_alert: fallo al enviar — NO guardado en alerts_sent (%s)", key)
        return False

    await asyncio.sleep(1.1)  # Telegram: max 1 msg/seg por chat

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
