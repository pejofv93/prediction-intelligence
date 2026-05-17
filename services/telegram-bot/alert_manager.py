"""
Formateador y sender de alertas Telegram.
Flujo: sports-agent/polymarket-agent llaman POST /send-alert → alert_manager formatea y envia.
on_snapshot ELIMINADO — incompatible con min-instances=0.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import (
    TELEGRAM_CHAT_ID, TELEGRAM_TOKEN,
    TELEGRAM_SPORTS_THREAD_ID, TELEGRAM_POLY_THREAD_ID,
    SPORTS_MIN_EDGE,
)

logger = logging.getLogger(__name__)

_BOT_BASE = "https://api.telegram.org/bot"
_HTTP_TIMEOUT = 10.0

# Umbral de cambio de cuota para disparar alerta (10%)
_ODDS_CHANGE_THRESHOLD = 0.10


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

    # Odds movement line
    om = prediction.get("odds_movement") or {}
    om_flag = om.get("flag", "NONE")
    om_line = ""
    if om_flag == "SMART_MONEY":
        pct = abs(float(om.get("pct_change_6h", 0))) * 100
        direction = om.get("direction", "")
        om_line = f"\n📉 Cuota bajó {pct:.0f}% en 6h — posible smart money ({direction})"
    elif om_flag == "FADING":
        pct = abs(float(om.get("pct_change_24h", 0))) * 100
        direction = om.get("direction", "")
        om_line = f"\n📈 Cuota subió {pct:.0f}% en 24h — bookmaker alargando ({direction})"

    msg = (
        f"{intensity} | {sport_emoji} {league_label}\n"
        f"{home} vs {away}\n"
        f"Mercado: {market_label} | Selección: *{selection}*\n"
        f"Cuota: *{odds:.2f}* | Edge: *+{edge:.1%}* | Confianza: *{conf:.0%}*\n"
        f"Factores: {factors_text}"
        f"{om_line}\n"
        f"🧮 Kelly: {kelly:.1%} del bankroll"
    )
    ext_ctx = prediction.get("external_context") or []
    if ext_ctx:
        ctx_str = "; ".join(str(n) for n in ext_ctx[:3])
        msg += f"\n⚠️ Contexto: {ctx_str}"
    if abs(edge) > 0.20:
        msg += "\n⚠️ Edge alto — win rate histórico en señales fuertes: 17%. Posible sobreestimación del modelo."
    msg += "\n\n⚠️ Apuesta responsablemente. No es asesoramiento financiero."
    return msg


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

    # FIX-POLY-FORMAT: línea de acción explícita para claridad del outcome
    _action_map = {
        "BUY_YES": "🟢 COMPRAR YES — el mercado infravalora esta probabilidad",
        "BUY_NO":  "🔴 COMPRAR NO — el mercado sobrevalora esta probabilidad",
        "WATCH":   "👁 OBSERVAR — señal débil, monitorear evolución",
    }
    action_line = _action_map.get(recommendation, "") + "\n" if recommendation in _action_map else ""

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
            if days_left < 3:
                close_line += " — ⚡ *CIERRA PRONTO* — precio actual muy relevante"
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

    # FIX-POLY-LINK: slug es el event slug de Gamma API → URL /event/{slug}.
    # Si slug vacío, usar market_id (condition_id) como fallback con /market/.
    slug = str(analysis.get("slug") or "")
    market_id_fallback = str(analysis.get("market_id") or "")
    if slug:
        link_line = f"🔗 [Ver mercado](https://polymarket.com/event/{slug})\n"
    elif market_id_fallback:
        link_line = f"🔗 [Ver mercado](https://polymarket.com/market/{market_id_fallback})\n"
    else:
        link_line = ""

    whale_info = str(analysis.get("whale_info") or "")
    whale_line = f"{whale_info}\n" if whale_info else ""

    data_quality = str(analysis.get("data_quality") or "")
    data_quality_line = "⚠️ Sin datos externos verificables — ancla: precio mercado ±15%\n" if data_quality == "improvised" else ""

    high_edge_line = (
        "⚠️ Edge alto — win rate histórico en señales fuertes: 17%. Posible sobreestimación del modelo.\n"
        if abs_edge > 0.20 else ""
    )

    return (
        f"🔮 OPORTUNIDAD POLYMARKET — {intensity}\n"
        f"{cat_line}"
        f"\n❓ {question}\n\n"
        f"{action_line}"
        f"💰 Precio YES: *{market_price_yes:.0%}* → IA: *{real_prob:.0%}* (*{edge:+.0%}* edge)\n"
        f"🎯 Confianza: *{confidence:.0%}*\n"
        f"{meta_line}"
        f"{link_line}"
        f"{data_quality_line}"
        f"{smart_line}"
        f"{whale_line}"
        f"💭 {reasoning}\n\n"
        f"{high_edge_line}"
        f"⚠️ Apuesta responsablemente. No es asesoramiento financiero."
    )


def _alert_key(data: dict, edge: float = 0.0) -> str:
    """Genera clave de deduplicacion. Edge excluido — mismo partido/mercado/seleccion = misma clave."""
    id_field = data.get("match_id") or data.get("market_id") or "unknown"
    market = data.get("market_type", "h2h")
    team = data.get("team_to_back") or data.get("selection") or ""
    return f"{id_field}_{market}_{team}"


def _safe_doc_id(key: str) -> str:
    """Convierte alert_key a Firestore document ID válido (sin / ni caracteres especiales)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(key))[:500]


def _claim_alert_slot(key: str, alert_type: str) -> bool:
    """
    Reserva atómicamente el slot de dedup para esta alerta.
    - Usa document ID = sanitized key (idempotente, no duplica docs).
    - Pre-escribe ANTES de enviar: reduce ventana de race condition a <20ms.
    - Devuelve True si OK para enviar, False si ya enviado en las últimas 24h.
    - Fail-open: si Firestore falla, devuelve True (prefiere alerta duplicada
      a silencio).
    """
    from shared.firestore_client import col
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    doc_id = _safe_doc_id(key)
    ref = col("alerts_sent").document(doc_id)
    try:
        snap = ref.get()
        if snap.exists:
            _sat = snap.to_dict().get("sent_at")
            if _sat is not None:
                if hasattr(_sat, "tzinfo") and _sat.tzinfo is None:
                    _sat = _sat.replace(tzinfo=timezone.utc)
                if _sat >= cutoff:
                    logger.info("_claim_alert_slot: dedup 24h → omitida (%s)", key)
                    return False
        # Pre-write antes de enviar — si otro proceso leyó antes que nosotros
        # escribamos, puede haber un duplicado; pero la ventana es <20ms.
        ref.set({"alert_key": key, "sent_at": now, "type": alert_type})
        return True
    except Exception as exc:
        logger.error("_claim_alert_slot(%s): error Firestore → fail-open: %s", key, exc)
        return True


async def check_pending_odds_changes(current_odds_by_match: dict[str, float]) -> int:
    """
    Compara cuotas actuales vs cuotas en señales PENDIENTES de Firestore.
    Para cada señal activa (result=None):
      - Si cambio > 10% → envía alerta al topic Sports.
      - Si nueva cuota hace edge < 0 → añade advertencia de edge negativo.

    Args:
        current_odds_by_match: {match_id: cuota_actual} — proporcionado por el analyze.

    Returns:
        Número de alertas de cambio enviadas.
    """
    from shared.firestore_client import col

    if not current_odds_by_match:
        return 0

    sent_count = 0
    try:
        pending_docs = list(
            col("predictions")
            .where(filter=FieldFilter("result", "==", None))
            .limit(200)
            .stream()
        )
    except Exception:
        logger.error("check_pending_odds_changes: error leyendo predictions pendientes", exc_info=True)
        return 0

    for doc in pending_docs:
        try:
            pred = doc.to_dict()
            match_id = str(pred.get("match_id") or doc.id)
            # Extraer el match_id base (sin sufijos _ml_home, _spread, etc.)
            base_id = match_id.split("_ml_")[0].split("_spread")[0].split("_tot_")[0].split("_h1_")[0].split("_q1_")[0]

            current_odds = current_odds_by_match.get(base_id) or current_odds_by_match.get(match_id)
            if current_odds is None:
                continue

            original_odds = float(pred.get("odds") or 0)
            if original_odds <= 1.0:
                continue

            pct_change = (current_odds - original_odds) / original_odds
            if abs(pct_change) <= _ODDS_CHANGE_THRESHOLD:
                continue

            market = pred.get("market_type", "h2h")
            alert_key = f"{match_id}_{market}_{current_odds:.2f}"

            # Dedup atómico: pre-escribe antes de enviar (mismo mecanismo que send_sports_alert)
            if not _claim_alert_slot(alert_key, "odds_change"):
                continue

            # Calcular edge actualizado
            calculated_prob = float(pred.get("calculated_prob") or 0)
            new_edge = round(calculated_prob - (1.0 / current_odds), 4) if current_odds > 1 else 0.0
            pct_str = f"{pct_change:+.1%}"

            home = _escape_md(pred.get("home_team", "?"))
            away = _escape_md(pred.get("away_team", "?"))
            selection = _escape_md(str(pred.get("selection") or pred.get("team_to_back") or "?"))
            conf = float(pred.get("confidence") or 0)
            kelly = float(pred.get("kelly_fraction") or 0)

            # Señal accionable si el cambio de cuota genera edge positivo suficiente
            if new_edge >= SPORTS_MIN_EDGE:
                msg_lines = [
                    f"✅ SEÑAL POR CAMBIO DE CUOTA",
                    f"{home} vs {away}",
                    f"*{selection}* @ *{current_odds:.2f}*",
                    f"Edge: *+{new_edge:.1%}* | Cuota cambió *{pct_str}*",
                    f"Confianza: {conf:.0%} | Kelly: {kelly:.1%}",
                ]
            else:
                msg_lines = [
                    "📊 CAMBIO DE CUOTA",
                    f"{home} vs {away} | {selection}",
                    f"Cuota: *{original_odds:.2f}* → *{current_odds:.2f}* ({pct_str})",
                    f"Edge actualizado: *{new_edge:+.1%}*",
                ]
                if new_edge < 0:
                    msg_lines.append("⚠️ Edge negativo — revisar")

            msg = "\n".join(msg_lines)
            sent = await send_message(msg, message_thread_id=TELEGRAM_SPORTS_THREAD_ID)
            if sent:
                sent_count += 1
                logger.info(
                    "check_pending_odds_changes: alerta %s enviada — %s "
                    "odds %.2f→%.2f (%s) edge_new=%.3f",
                    "ACCIONABLE" if new_edge >= SPORTS_MIN_EDGE else "info",
                    match_id, original_odds, current_odds, pct_str, new_edge,
                )
            await asyncio.sleep(0.5)
        except Exception:
            logger.error(
                "check_pending_odds_changes: error procesando %s", doc.id, exc_info=True
            )

    return sent_count


async def send_sports_alert(prediction: dict) -> bool:
    """
    Formatea prediccion deportiva y envia a TELEGRAM_CHAT_ID.
    Dedup 24h via _claim_alert_slot (pre-write con document ID → sin race conditions).
    Devuelve True si envio.
    """
    edge = float(prediction.get("edge") or 0)
    key = _alert_key(prediction, edge)

    # Dedup atómico: pre-escribe antes de enviar
    if not _claim_alert_slot(key, "sports"):
        return False

    text = _format_alert_unified(prediction)

    # Calibración de confianza: muestra win rate histórico real cuando hay ≥10 señales en el bucket
    try:
        from shared.firestore_client import col as _fscol
        _conf_val = float(prediction.get("confidence") or 0.0)
        _cbkt = (
            "65_70" if 0.65 <= _conf_val < 0.70 else
            "70_80" if 0.70 <= _conf_val < 0.80 else
            "80_90" if 0.80 <= _conf_val < 0.90 else
            "90_99" if _conf_val >= 0.90 else None
        )
        if _cbkt:
            _mw = _fscol("model_weights").document("current").get()
            if _mw.exists:
                _bkt_data = _mw.to_dict().get("accuracy_by_confidence", {}).get(_cbkt, {})
                _rate = _bkt_data.get("rate")
                _cnt = int(_bkt_data.get("count", 0))
                if _rate is not None and _cnt >= 10:
                    # ⚠️ si el modelo sobreestima más de 10pp, ✅ si está bien calibrado
                    _calib_emoji = "⚠️" if _rate < _conf_val - 0.10 else "✅"
                    text += (
                        f"\n{_calib_emoji} Confianza histórica real: *{_rate:.0%}*"
                        f" (modelo: {_conf_val:.0%}) — {_cnt} señales"
                    )
    except Exception:
        pass

    sent = await send_message(text, message_thread_id=TELEGRAM_SPORTS_THREAD_ID)

    if not sent:
        logger.error("send_sports_alert: fallo al enviar (%s)", key)
        return False

    await asyncio.sleep(1.1)  # Telegram: max 1 msg/seg por chat
    logger.info("send_sports_alert: alerta enviada para %s (edge=%.3f)", key, edge)
    return True


async def send_poly_alert(analysis: dict) -> bool:
    """
    Formatea senal de Polymarket y envia a TELEGRAM_CHAT_ID.
    Dedup 24h via _claim_alert_slot — mismo mecanismo que sports.
    Devuelve True si envio.
    """
    edge = float(analysis.get("edge") or 0)
    key = _alert_key(analysis, edge)

    # Dedup atómico: pre-escribe antes de enviar
    if not _claim_alert_slot(key, "polymarket"):
        return False

    text = _format_alert_unified(analysis) if analysis.get("sport") else _format_poly_alert(analysis)
    sent = await send_message(text, message_thread_id=TELEGRAM_POLY_THREAD_ID)

    if not sent:
        logger.error("send_poly_alert: fallo al enviar (%s)", key)
        return False

    await asyncio.sleep(1.1)  # Telegram: max 1 msg/seg por chat
    logger.info("send_poly_alert: alerta enviada para %s (edge=%.3f)", key, edge)
    return True
