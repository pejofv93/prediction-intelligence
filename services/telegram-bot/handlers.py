"""
Handlers de comandos Telegram.
/start /sports /poly /stats /calc /help
"""
import logging

from shared.config import SPORTS_MIN_EDGE, POLY_MIN_EDGE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chat_id(update: dict) -> int | None:
    """Extrae chat_id del update de Telegram."""
    msg = update.get("message") or update.get("edited_message") or {}
    return msg.get("chat", {}).get("id")


def _message_text(update: dict) -> str:
    msg = update.get("message") or update.get("edited_message") or {}
    return msg.get("text", "")


def _esc(text: str) -> str:
    """Escapa _ para Telegram MarkdownV1."""
    return str(text).replace("_", "\\_")


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

async def handle_start(update: dict) -> None:
    """Mensaje de bienvenida + lista de comandos disponibles."""
    from alert_manager import send_message

    chat_id = _chat_id(update)
    text = (
        "👋 *Bienvenido a Prediction Intelligence*\n\n"
        "Detecto value bets en deportes y mercados de predicción Polymarket.\n\n"
        "📋 *Comandos disponibles:*\n"
        "/sports — Top señales deportivas activas\n"
        "/poly — Top oportunidades Polymarket\n"
        "/stats — Precisión del modelo y pesos actuales\n"
        "/calc — Calculadora matched betting\n"
        "/help — Ayuda detallada\n\n"
        "_Los datos se actualizan automáticamente vía GitHub Actions._"
    )
    await send_message(text, chat_id=chat_id)


async def handle_sports(update: dict) -> None:
    """Lee predictions Firestore: edge > 0.08, result==None, top 3 por edge DESC."""
    from alert_manager import send_message
    from shared.firestore_client import col

    chat_id = _chat_id(update)

    try:
        docs = (
            col("predictions")
            .where("edge", ">", SPORTS_MIN_EDGE)
            .where("result", "==", None)
            .order_by("edge", direction="DESCENDING")
            .limit(3)
            .stream()
        )
        predictions = [d.to_dict() for d in docs]
    except Exception:
        logger.error("handle_sports: error consultando Firestore", exc_info=True)
        await send_message("❌ Error consultando señales deportivas.", chat_id=chat_id)
        return

    if not predictions:
        await send_message(
            "📭 No hay señales deportivas activas en este momento.\n"
            "_Edge mínimo requerido: {:.0%}_".format(SPORTS_MIN_EDGE),
            chat_id=chat_id,
        )
        return

    lines = ["⚽ *TOP SEÑALES DEPORTIVAS*\n"]
    for i, p in enumerate(predictions, 1):
        home = _esc(p.get("home_team", "?"))
        away = _esc(p.get("away_team", "?"))
        league = _esc(p.get("league", "?"))
        team = _esc(p.get("team_to_back", "?"))
        odds = float(p.get("odds", 0))
        edge = float(p.get("edge", 0))
        conf = float(p.get("confidence", 0))

        match_date = p.get("match_date")
        if hasattr(match_date, "strftime"):
            date_str = match_date.strftime("%d/%m %H:%M")
        else:
            date_str = str(match_date)[:10] if match_date else "?"

        lines.append(
            f"*{i}. {home} vs {away}*\n"
            f"   🏆 {league} | 📅 {date_str}\n"
            f"   ✅ {team} @ {odds:.2f} | Edge: +{edge:.1%} | Conf: {conf:.0%}"
        )

    await send_message("\n\n".join(lines), chat_id=chat_id)


async def handle_poly(update: dict) -> None:
    """Lee poly_predictions: alerted==True o edge>0.12, top 5 por edge DESC."""
    from alert_manager import send_message
    from shared.firestore_client import col

    chat_id = _chat_id(update)

    try:
        # Consulta por edge > threshold (los alertados tienen edge > threshold por definicion)
        docs = (
            col("poly_predictions")
            .where("edge", ">", POLY_MIN_EDGE)
            .order_by("edge", direction="DESCENDING")
            .limit(5)
            .stream()
        )
        predictions = [d.to_dict() for d in docs]
    except Exception:
        logger.error("handle_poly: error consultando Firestore", exc_info=True)
        await send_message("❌ Error consultando oportunidades Polymarket.", chat_id=chat_id)
        return

    if not predictions:
        await send_message(
            "📭 No hay oportunidades Polymarket activas.\n"
            "_Edge mínimo requerido: {:.0%}_".format(POLY_MIN_EDGE),
            chat_id=chat_id,
        )
        return

    lines = ["🔮 *TOP OPORTUNIDADES POLYMARKET*\n"]
    for i, p in enumerate(predictions, 1):
        question = _esc(str(p.get("question", "?"))[:80])
        edge = float(p.get("edge", 0))
        conf = float(p.get("confidence", 0))
        price_yes = float(p.get("market_price_yes", 0))
        real_prob = float(p.get("real_prob", 0))
        rec = p.get("recommendation", "PASS")
        vol_spike = "🐋" if p.get("volume_spike") or p.get("smart_money_detected") else ""

        lines.append(
            f"*{i}. {question}*\n"
            f"   💎 Edge: +{edge:.0%} | Conf: {conf:.0%} {vol_spike}\n"
            f"   📈 YES: {price_yes:.0%} → Real: {real_prob:.0%} | {rec}"
        )

    await send_message("\n\n".join(lines), chat_id=chat_id)


async def handle_stats(update: dict) -> None:
    """Lee accuracy_log (semana actual) + model_weights doc 'current'."""
    from datetime import datetime, timezone
    from alert_manager import send_message
    from shared.firestore_client import col

    chat_id = _chat_id(update)

    try:
        now = datetime.now(timezone.utc)
        iso = now.isocalendar()
        current_week = f"{iso[0]}-W{iso[1]:02d}"

        # accuracy_log de la semana actual
        log_docs = list(
            col("accuracy_log")
            .where("week", "==", current_week)
            .limit(1)
            .stream()
        )
        log = log_docs[0].to_dict() if log_docs else {}

        # model_weights doc current
        weights_doc = col("model_weights").document("current").get()
        weights_data = weights_doc.to_dict() if weights_doc.exists else {}
        weights = weights_data.get("weights", {})

    except Exception:
        logger.error("handle_stats: error consultando Firestore", exc_info=True)
        await send_message("❌ Error consultando estadísticas.", chat_id=chat_id)
        return

    total = int(log.get("predictions_total", 0))
    correct = int(log.get("predictions_correct", 0))
    accuracy = float(log.get("accuracy", 0))
    prev_acc = log.get("prev_week_accuracy")

    delta_str = ""
    if prev_acc is not None:
        delta = accuracy - float(prev_acc)
        arrow = "▲" if delta >= 0 else "▼"
        delta_str = f"\n   {arrow} vs semana anterior: {delta:+.1%}"

    version = int(weights_data.get("version", 0))

    text = (
        f"📊 *ESTADÍSTICAS — {current_week}*\n\n"
        f"🏟 *Sports Agent:*\n"
        f"   Predicciones: {total} | Correctas: {correct}\n"
        f"   Accuracy: *{accuracy:.1%}*{delta_str}\n\n"
        f"⚙️ *Pesos modelo v{version}:*\n"
        f"   Poisson: {weights.get('poisson', 0):.2f} | "
        f"ELO: {weights.get('elo', 0):.2f}\n"
        f"   Forma: {weights.get('form', 0):.2f} | "
        f"H2H: {weights.get('h2h', 0):.2f}"
    )

    if not log:
        text = (
            f"📊 *ESTADÍSTICAS — {current_week}*\n\n"
            f"_Sin predicciones registradas esta semana aún._\n\n"
            f"⚙️ *Pesos modelo v{version}:*\n"
            f"   Poisson: {weights.get('poisson', 0):.2f} | "
            f"ELO: {weights.get('elo', 0):.2f}\n"
            f"   Forma: {weights.get('form', 0):.2f} | "
            f"H2H: {weights.get('h2h', 0):.2f}"
        )

    await send_message(text, chat_id=chat_id)


async def handle_calc(update: dict, args: list[str]) -> None:
    """
    /calc <stake> <back_odds> <lay_odds> <comision%>
    Calcula qualifying bet.
    """
    from alert_manager import send_message

    chat_id = _chat_id(update)

    usage = (
        "📐 *Calculadora Matched Betting*\n\n"
        "Uso: `/calc <stake> <back\\_odds> <lay\\_odds> <comision%>`\n\n"
        "Ejemplo: `/calc 10 3.5 3.6 5`\n"
        "_(comision en %: Betfair = 5)_"
    )

    if len(args) < 4:
        await send_message(usage, chat_id=chat_id)
        return

    try:
        back_stake = float(args[0])
        back_odds = float(args[1])
        lay_odds = float(args[2])
        commission = float(args[3]) / 100  # porcentaje → decimal
    except ValueError:
        await send_message("❌ Parámetros no válidos.\n\n" + usage, chat_id=chat_id)
        return

    if lay_odds <= commission:
        await send_message("❌ Las cuotas de lay deben ser mayores que la comisión.", chat_id=chat_id)
        return

    # Fórmula qualifying bet
    lay_stake = (back_stake * back_odds) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission) - back_stake
    rating = ((profit_back + profit_lay) / 2 / back_stake) * 100

    text = (
        f"🧮 *QUALIFYING BET*\n\n"
        f"Back: €{back_stake:.2f} @ {back_odds:.2f}\n"
        f"Lay: €{back_stake:.2f} @ {lay_odds:.2f} (comisión {commission:.0%})\n\n"
        f"📌 *Resultados:*\n"
        f"   Lay stake: *€{lay_stake:.2f}*\n"
        f"   Responsabilidad: €{liability:.2f}\n"
        f"   P/L si gana back: {profit_back:+.2f}€\n"
        f"   P/L si gana lay: {profit_lay:+.2f}€\n"
        f"   Rating: *{rating:+.1f}%*\n\n"
        f"📋 *Pasos:*\n"
        f"1. Back €{back_stake:.2f} a {back_odds:.2f} en la casa\n"
        f"2. Lay €{lay_stake:.2f} a {lay_odds:.2f} en el exchange\n"
        f"3. Responsabilidad en exchange: €{liability:.2f}"
    )

    await send_message(text, chat_id=chat_id)


async def handle_help(update: dict) -> None:
    """Lista todos los comandos con descripcion breve."""
    from alert_manager import send_message

    chat_id = _chat_id(update)
    text = (
        "📖 *AYUDA — Prediction Intelligence*\n\n"
        "*/start* — Mensaje de bienvenida\n"
        "*/sports* — Top 3 señales deportivas activas (edge > 8%)\n"
        "*/poly* — Top 5 oportunidades Polymarket (edge > 12%)\n"
        "*/stats* — Precisión del modelo esta semana + pesos actuales\n"
        "*/calc <stake> <back> <lay> <com%>* — Calculadora qualifying bet\n"
        "*/help* — Esta ayuda\n\n"
        "📡 *Fuentes de datos:*\n"
        "• Fútbol: football-data.org (modelo Poisson+ELO)\n"
        "• Otros deportes: API-Sports + Groq AI\n"
        "• Polymarket: Gamma API + CLOB + análisis Groq\n\n"
        "⚠️ _Solo para fines informativos. No es asesoramiento financiero._"
    )
    await send_message(text, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def dispatch_update(update: dict) -> None:
    """Despacha el update al handler correcto segun el comando."""
    text = _message_text(update).strip()

    if not text.startswith("/"):
        return  # ignorar mensajes que no son comandos

    # Parsear comando y argumentos (ignorar @botname si viene en grupos)
    parts = text.split()
    raw_cmd = parts[0].lstrip("/").lower()
    command = raw_cmd.split("@")[0]  # /cmd@botname → cmd
    args = parts[1:]

    try:
        if command == "start":
            await handle_start(update)
        elif command == "sports":
            await handle_sports(update)
        elif command == "poly":
            await handle_poly(update)
        elif command == "stats":
            await handle_stats(update)
        elif command == "calc":
            await handle_calc(update, args)
        elif command == "help":
            await handle_help(update)
        else:
            # Comando desconocido — sugerir /help
            from alert_manager import send_message
            chat_id = _chat_id(update)
            await send_message(
                f"❓ Comando `/{command}` no reconocido. Usa /help para ver los disponibles.",
                chat_id=chat_id,
            )
    except Exception:
        logger.error("dispatch_update: error en comando /%s", command, exc_info=True)
        try:
            from alert_manager import send_message
            chat_id = _chat_id(update)
            await send_message("❌ Error procesando el comando. Inténtalo de nuevo.", chat_id=chat_id)
        except Exception:
            pass
