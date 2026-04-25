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
        "/shadow — Métricas shadow mode (trades virtuales)\n"
        "/bankroll — Evolución P&L virtual\n"
        "/arb — Oportunidades de arbitraje activas\n"
        "/btc — Precio BTC + señales crypto Polymarket\n"
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
            f"   💎 Edge: {edge:+.0%} | Conf: {conf:.0%} {vol_spike}\n"
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
        "*/shadow* — Métricas del modo shadow (trades virtuales)\n"
        "*/bankroll* — Evolución P&L virtual (últimos 10 trades)\n"
        "*/arb* — Oportunidades de arbitraje activas\n"
        "*/btc* — Precio BTC actual + señales crypto Polymarket\n"
        "*/help* — Esta ayuda\n\n"
        "📡 *Fuentes de datos:*\n"
        "• Fútbol: football-data.org (modelo Poisson+ELO)\n"
        "• Otros deportes: API-Sports + Groq AI\n"
        "• Polymarket: Gamma API + CLOB + análisis Groq\n"
        "• BTC: Binance REST API (snapshots cada 5 min)\n\n"
        "⚠️ _Solo para fines informativos. No es asesoramiento financiero._"
    )
    await send_message(text, chat_id=chat_id)


async def handle_shadow(update: dict) -> None:
    """
    /shadow — métricas del shadow mode.
    Lee col("shadow_trades") máx 200 docs, calcula métricas con calculate_metrics().
    """
    from alert_manager import send_message
    from shared.firestore_client import col
    chat_id = _chat_id(update)

    try:
        trades = [d.to_dict() for d in col("shadow_trades").limit(200).stream()]
        try:
            from shared.shadow_engine import calculate_metrics
            m = calculate_metrics(trades)
        except ImportError:
            # Cálculo inline básico
            closed = [t for t in trades if t.get("result") in ("win", "loss")]
            wins = [t for t in closed if t.get("result") == "win"]
            stakes = sum(float(t.get("virtual_stake", 0)) for t in closed)
            pnl = sum(float(t.get("pnl_virtual", 0)) for t in closed if t.get("pnl_virtual") is not None)
            m = {
                "current_bankroll": 50.0 + pnl,
                "roi_total": pnl / stakes if stakes > 0 else 0,
                "win_rate": len(wins) / len(closed) if closed else 0,
                "total_closed": len(closed),
                "pending": len(trades) - len(closed),
                "ready_for_real": False,
            }

        bankroll = float(m.get("current_bankroll", 50.0))
        roi = float(m.get("roi_total", 0))
        wr = float(m.get("win_rate", 0))
        total = int(m.get("closed_trades", m.get("total_closed", 0)))
        pending = int(m.get("pending_trades", m.get("pending", 0)))
        ready = m.get("ready_for_real", False)

        ready_str = "✅ Listo para considerar real" if ready else "⏳ Acumulando historial"

        # CLV
        avg_clv = float(m.get("avg_clv", 0))
        clv_edge = m.get("clv_edge_confirmed", False)
        clv_line = ""
        if avg_clv != 0.0:
            edge_badge = " ✅ edge real confirmado" if clv_edge else " (acumulando datos)"
            clv_line = f"📐 CLV medio: *{avg_clv:+.1%}*{edge_badge}\n"

        text = (
            f"👻 *SHADOW MODE*\n\n"
            f"💰 Bankroll virtual: *{bankroll:.2f}€* (inicio: 50€)\n"
            f"📈 ROI total: *{roi:+.1%}*\n"
            f"🎯 Win rate: *{wr:.0%}*\n"
            f"{clv_line}"
            f"📊 Trades cerrados: {total} | Pendientes: {pending}\n\n"
            f"{ready_str}\n\n"
            f"_Datos simulados — no dinero real._"
        )
    except Exception as e:
        logger.error(f"handle_shadow error: {e}")
        text = "❌ Error consultando shadow mode."

    await send_message(text, chat_id=chat_id)


async def handle_bankroll(update: dict) -> None:
    """
    /bankroll — evolución P&L virtual.
    Muestra últimos 10 trades con P&L acumulado.
    """
    from alert_manager import send_message
    from shared.firestore_client import col
    chat_id = _chat_id(update)

    try:
        trades_raw = list(
            col("shadow_trades")
            .where("result", "in", ["win", "loss"])
            .order_by("closed_at", direction="DESCENDING")
            .limit(10)
            .stream()
        )
        if not trades_raw:
            await send_message("📭 Sin trades cerrados aún en shadow mode.", chat_id=chat_id)
            return

        trades = [d.to_dict() for d in trades_raw]

        lines = ["📊 *BANKROLL VIRTUAL — Últimos 10 trades*\n"]
        for t in reversed(trades):  # cronológico
            pnl = float(t.get("pnl_virtual") or 0)
            result = t.get("result", "?")
            emoji = "✅" if result == "win" else "❌"
            mkt = str(t.get("market", ""))[:30]
            stake = float(t.get("virtual_stake", 0))
            lines.append(f"{emoji} {mkt} | Stake: {stake:.2f}€ | P&L: {pnl:+.2f}€")

        total_pnl = sum(float(t.get("pnl_virtual") or 0) for t in trades)
        bankroll = 50.0 + total_pnl
        lines.append(f"\n💰 Bankroll actual: *{bankroll:.2f}€*")

        await send_message("\n".join(lines), chat_id=chat_id)
    except Exception as e:
        logger.error(f"handle_bankroll error: {e}")
        await send_message("❌ Error consultando bankroll.", chat_id=chat_id)


async def handle_arb(update: dict) -> None:
    """
    /arb — últimas oportunidades de arbitraje (últimas 2h).
    Lee col("arb_opportunities") con expires_at > now.
    """
    from alert_manager import send_message
    from shared.firestore_client import col
    from datetime import datetime, timezone
    chat_id = _chat_id(update)

    try:
        now = datetime.now(timezone.utc)
        docs = list(
            col("arb_opportunities")
            .where("expires_at", ">", now)
            .order_by("expires_at", direction="DESCENDING")
            .limit(5)
            .stream()
        )

        if not docs:
            await send_message("📭 Sin oportunidades de arbitraje activas en este momento.", chat_id=chat_id)
            return

        lines = ["🎯 *ARBITRAJE ACTIVO*\n"]
        for d in docs:
            arb = d.to_dict()
            profit = float(arb.get("profit_pct", 0))
            league = _esc(str(arb.get("league", "?")))
            home = _esc(str(arb.get("home", "?")))
            away = _esc(str(arb.get("away", "?")))
            bh_o = float(arb.get("best_home_odds", 0))
            ba_o = float(arb.get("best_away_odds", 0))
            bh_b = _esc(str(arb.get("best_home_book", "?")))
            ba_b = _esc(str(arb.get("best_away_book", "?")))
            lines.append(
                f"⚽ {league}\n"
                f"{home} vs {away}\n"
                f"Back {home}: {bh_b} @ {bh_o:.2f}\n"
                f"Back {away}: {ba_b} @ {ba_o:.2f}\n"
                f"Beneficio garantizado: *+{profit:.1f}%*\n"
                f"⚠️ Apuesta responsablemente.\n"
            )

        await send_message("\n".join(lines), chat_id=chat_id)
    except Exception as e:
        logger.error(f"handle_arb error: {e}")
        await send_message("❌ Error consultando arbitraje.", chat_id=chat_id)


async def handle_btc(update: dict) -> None:
    """
    /btc — precio BTC actual + señales crypto Polymarket.
    """
    from alert_manager import send_message
    from shared.firestore_client import col
    chat_id = _chat_id(update)

    try:
        # Intentar leer snapshot Firestore
        btc_price = 0.0
        btc_change = 0.0

        try:
            docs = list(
                col("binance_snapshots")
                .where("symbol", "==", "BTCUSDT")
                .order_by("recorded_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            if docs:
                snap = docs[0].to_dict()
                btc_price = float(snap.get("price", 0))
                btc_change = float(snap.get("change_24h_pct", 0))
        except Exception:
            pass

        # Si no hay snapshot, hacer fetch directo
        if btc_price == 0:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT")
            if resp.status_code == 200:
                d = resp.json()
                btc_price = float(d.get("lastPrice", 0))
                btc_change = float(d.get("priceChangePercent", 0))

        # Señales crypto Polymarket activas
        crypto_signals = []
        try:
            docs = list(
                col("poly_predictions")
                .where("category", "==", "crypto")
                .where("alerted", "==", False)
                .order_by("edge", direction="DESCENDING")
                .limit(3)
                .stream()
            )
            for d in docs:
                p = d.to_dict()
                if float(p.get("edge", 0)) > 0.08:
                    crypto_signals.append(p)
        except Exception:
            pass

        arrow = "📈" if btc_change >= 0 else "📉"
        text = (
            f"₿ *BITCOIN*\n\n"
            f"Precio: *${btc_price:,.0f}*\n"
            f"{arrow} 24h: *{btc_change:+.2f}%*\n\n"
        )

        if crypto_signals:
            text += "🔮 *Señales Polymarket Crypto:*\n"
            for s in crypto_signals:
                q = _esc(str(s.get("question", ""))[:60])
                edge = float(s.get("edge", 0))
                price = float(s.get("market_price_yes", 0))
                text += f"• {q}\n  YES: {price:.0%} | Edge: {edge:+.0%}\n"
        else:
            text += "_Sin señales crypto activas en Polymarket._"

        await send_message(text, chat_id=chat_id)
    except Exception as e:
        logger.error(f"handle_btc error: {e}")
        await send_message("❌ Error consultando BTC.", chat_id=chat_id)


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
        elif command == "shadow":
            await handle_shadow(update)
        elif command == "bankroll":
            await handle_bankroll(update)
        elif command == "arb":
            await handle_arb(update)
        elif command == "btc":
            await handle_btc(update)
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
