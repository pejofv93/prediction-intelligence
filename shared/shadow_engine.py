"""
Shadow trading engine — rastrea señales de sports y polymarket como trades virtuales.
Coleccion Firestore: shadow_trades (prefix añadido por col()).
"""
import logging
import uuid
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col

logger = logging.getLogger(__name__)

_INITIAL_BANKROLL = 50.0
_MIN_STAKE = 0.50
_MAX_STAKE = 25.0
_RETROACTIVE_DOC = "retroactive_done"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _calc_virtual_stake(kelly: float) -> float:
    return min(_MAX_STAKE, max(_MIN_STAKE, kelly * _INITIAL_BANKROLL))


def _calc_pnl(result: str, virtual_stake: float, odds: float) -> Optional[float]:
    if result == "win":
        return round((odds - 1) * virtual_stake, 4)
    elif result == "loss":
        return round(-virtual_stake, 4)
    elif result == "void":
        return 0.0
    return None


def _calc_bankroll_after() -> float:
    """Recalcula bankroll desde 50.0 sumando todos los pnl de trades cerrados."""
    try:
        docs = col("shadow_trades").stream()
        bankroll = _INITIAL_BANKROLL
        for doc in docs:
            data = doc.to_dict()
            if data.get("result") in ("win", "loss", "void") and data.get("pnl_virtual") is not None:
                bankroll += float(data["pnl_virtual"])
        return round(bankroll, 4)
    except Exception as e:
        logger.error("shadow: error calculando bankroll_after: %s", e)
        return _INITIAL_BANKROLL


async def retroactive_eval(db=None) -> dict:
    """
    Una sola vez al deploy. Lee prodpredictions con result!=null y
    prodpoly_predictions con alerted=True. Crea shadow_trades retroactivos.
    Devuelve {"sports": int, "poly": int} con nº de trades creados.
    """
    try:
        guard_doc = col("shadow_trades").document(_RETROACTIVE_DOC).get()
        if guard_doc.exists:
            logger.info("shadow: retroactive_eval ya ejecutada, omitiendo")
            return {"sports": 0, "poly": 0}
    except Exception as e:
        logger.error("shadow: error leyendo guard doc: %s", e)
        return {"sports": 0, "poly": 0}

    sports_count = 0
    poly_count = 0

    # --- Sports predictions con result ---
    try:
        docs = (
            col("predictions")
            .where(filter=FieldFilter("result", "!=", None))
            .limit(200)
            .stream()
        )
        for doc in docs:
            try:
                p = doc.to_dict()
                kelly = float(p.get("kelly_fraction") or 0.05)
                virtual_stake = _calc_virtual_stake(kelly)
                odds = float(p.get("odds") or 2.0)
                result_val = "win" if p.get("correct") else "loss"
                pnl = _calc_pnl(result_val, virtual_stake, odds)

                trade_id = str(uuid.uuid4())
                trade = {
                    "trade_id": trade_id,
                    "signal_id": str(p.get("prediction_id") or p.get("match_id") or doc.id),
                    "source": "sports",
                    "market": str(p.get("market_type") or "1X2"),
                    "selection": str(p.get("team_to_back") or p.get("selection") or ""),
                    "odds": odds,
                    "edge": float(p.get("edge") or 0.0),
                    "confidence": float(p.get("confidence") or 0.0),
                    "kelly_fraction": kelly,
                    "virtual_stake": virtual_stake,
                    "opened_at": p.get("created_at") or _now_utc(),
                    "closed_at": _now_utc(),
                    "result": result_val,
                    "pnl_virtual": pnl,
                    "bankroll_after": _INITIAL_BANKROLL,  # retroactivo no acumula
                    "signal_data": p,
                    "category": str(p.get("league") or p.get("sport") or ""),
                    "unified_score": None,
                }
                col("shadow_trades").document(trade_id).set(trade)
                sports_count += 1
            except Exception as e:
                logger.error("shadow: error creando trade sports retroactivo: %s", e)
    except Exception as e:
        logger.error("shadow: error leyendo predictions retroactivas: %s", e)

    # --- Poly predictions con alerted=True ---
    try:
        docs = (
            col("poly_predictions")
            .where(filter=FieldFilter("alerted", "==", True))
            .limit(200)
            .stream()
        )
        for doc in docs:
            try:
                p = doc.to_dict()
                edge = float(p.get("edge") or 0.0)
                kelly = min(0.25, max(0.01, edge / 2))
                virtual_stake = _calc_virtual_stake(kelly)
                market_price_yes = float(p.get("market_price_yes") or 0.5)
                odds = round(1 / market_price_yes, 2) if market_price_yes > 0 else 2.0

                trade_id = str(uuid.uuid4())
                question = str(p.get("question") or "")
                trade = {
                    "trade_id": trade_id,
                    "signal_id": str(p.get("market_id") or doc.id),
                    "source": "polymarket",
                    "market": question[:100],
                    "selection": str(p.get("recommendation") or "YES"),
                    "odds": odds,
                    "edge": edge,
                    "confidence": float(p.get("confidence") or 0.0),
                    "kelly_fraction": kelly,
                    "virtual_stake": virtual_stake,
                    "opened_at": p.get("analyzed_at") or _now_utc(),
                    "closed_at": None,
                    "result": "pending",
                    "pnl_virtual": None,
                    "bankroll_after": None,
                    "signal_data": p,
                    "category": p.get("category"),
                    "unified_score": None,
                }
                col("shadow_trades").document(trade_id).set(trade)
                poly_count += 1
            except Exception as e:
                logger.error("shadow: error creando trade poly retroactivo: %s", e)
    except Exception as e:
        logger.error("shadow: error leyendo poly_predictions retroactivas: %s", e)

    # --- Guardar guard doc ---
    try:
        col("shadow_trades").document(_RETROACTIVE_DOC).set({
            "done": True,
            "at": _now_utc(),
            "sports": sports_count,
            "poly": poly_count,
        })
        logger.info("shadow: retroactive_eval completada — sports=%d poly=%d", sports_count, poly_count)
    except Exception as e:
        logger.error("shadow: error guardando guard doc: %s", e)

    return {"sports": sports_count, "poly": poly_count}


async def track_new_signal(signal: dict, source: str) -> str:
    """Crea trade virtual con result='pending'. Devuelve trade_id."""
    trade_id = str(uuid.uuid4())
    try:
        if source == "sports":
            signal_id = str(signal.get("prediction_id") or signal.get("match_id") or "")
            market = str(signal.get("market_type") or "1X2")
            selection = str(signal.get("team_to_back") or signal.get("selection") or "")
            odds = float(signal.get("odds") or 2.0)
            edge = float(signal.get("edge") or 0.0)
            confidence = float(signal.get("confidence") or 0.0)
            kelly = float(signal.get("kelly_fraction") or 0.05)
            category = str(signal.get("league") or signal.get("sport") or "")
        else:  # polymarket
            signal_id = str(signal.get("market_id") or "")
            question = str(signal.get("question") or "")
            market = question[:100]
            selection = str(signal.get("recommendation") or "YES")
            market_price_yes = float(signal.get("market_price_yes") or 0.5)
            odds = round(1 / market_price_yes, 2) if market_price_yes > 0 else 2.0
            edge = float(signal.get("edge") or 0.0)
            confidence = float(signal.get("confidence") or 0.0)
            kelly = min(0.25, max(0.01, edge / 2))
            category = signal.get("category")

        virtual_stake = _calc_virtual_stake(kelly)

        trade = {
            "trade_id": trade_id,
            "signal_id": signal_id,
            "source": source,
            "market": market,
            "selection": selection,
            "odds": odds,
            "edge": edge,
            "confidence": confidence,
            "kelly_fraction": kelly,
            "virtual_stake": virtual_stake,
            "opened_at": _now_utc(),
            "closed_at": None,
            "result": "pending",
            "pnl_virtual": None,
            "bankroll_after": None,
            "signal_data": signal,
            "category": category,
            "unified_score": signal.get("unified_score"),
        }
        col("shadow_trades").document(trade_id).set(trade)
        logger.info("shadow: trade creado trade_id=%s source=%s market=%s", trade_id, source, market)
    except Exception as e:
        logger.error("shadow: error en track_new_signal: %s", e)

    return trade_id


def calculate_clv(trade: dict, closing_odds: float) -> float:
    """
    CLV = (odds_at_signal / closing_odds) - 1
    CLV > 0: apostaste mejor que el mercado final
    CLV < 0: el mercado era mas inteligente

    trade: dict con campo "odds" (cuota cuando se genero la senal)
    closing_odds: cuota de cierre (ultima antes del partido)

    Returns float rounded to 4 decimal places.
    """
    try:
        odds_at_signal = float(trade.get("odds") or 0)
        if odds_at_signal <= 0 or closing_odds <= 0:
            return 0.0
        clv = (odds_at_signal / closing_odds) - 1.0
        return round(clv, 4)
    except Exception as e:
        logger.error("calculate_clv: error: %s", e)
        return 0.0


async def update_trade_clv(trade_id: str, closing_odds: float) -> None:
    """
    Calcula y guarda CLV para un trade.
    Lee el doc de shadow_trades/{trade_id}, calcula CLV con calculate_clv,
    actualiza el campo "clv" en Firestore.
    """
    try:
        doc_ref = col("shadow_trades").document(trade_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.error("update_trade_clv: trade_id=%s no encontrado", trade_id)
            return
        trade = doc.to_dict()
        clv = calculate_clv(trade, closing_odds)
        doc_ref.update({"clv": clv})
        logger.info(
            "update_trade_clv: trade_id=%s clv=%.4f closing_odds=%.2f",
            trade_id, clv, closing_odds,
        )
    except Exception as e:
        logger.error("update_trade_clv: error para trade_id=%s: %s", trade_id, e)


async def update_trade_result(trade_id: str, result: str, odds_final: float = None) -> None:
    """Actualiza resultado de un trade. result: 'win'|'loss'|'void'"""
    try:
        doc_ref = col("shadow_trades").document(trade_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.error("shadow: trade_id=%s no encontrado", trade_id)
            return

        data = doc.to_dict()
        virtual_stake = float(data.get("virtual_stake") or _MIN_STAKE)
        effective_odds = odds_final if odds_final is not None else float(data.get("odds") or 2.0)

        pnl = _calc_pnl(result, virtual_stake, effective_odds)
        bankroll = _calc_bankroll_after()
        # Añadir este trade al bankroll final
        if pnl is not None:
            bankroll = round(bankroll + pnl, 4)

        doc_ref.update({
            "result": result,
            "closed_at": _now_utc(),
            "pnl_virtual": pnl,
            "bankroll_after": bankroll,
        })
        logger.info("shadow: trade_id=%s actualizado result=%s pnl=%.2f bankroll=%.2f",
                    trade_id, result, pnl or 0, bankroll)
    except Exception as e:
        logger.error("shadow: error en update_trade_result: %s", e)


def calculate_metrics(trades: list = None) -> dict:
    """Calcula métricas de rendimiento. Si trades=None, leerlos de Firestore."""
    try:
        if trades is None:
            docs = col("shadow_trades").limit(500).stream()
            trades = []
            for doc in docs:
                if doc.id == _RETROACTIVE_DOC:
                    continue
                trades.append(doc.to_dict())

        closed = [t for t in trades if t.get("result") in ("win", "loss")]
        pending = [t for t in trades if t.get("result") == "pending"]
        wins = [t for t in closed if t.get("result") == "win"]

        # ROI global
        closed_pnl = [float(t.get("pnl_virtual") or 0) for t in closed]
        closed_stakes = [float(t.get("virtual_stake") or _MIN_STAKE) for t in closed]
        total_stake = sum(closed_stakes)
        total_pnl = sum(closed_pnl)
        roi_total = round(total_pnl / total_stake, 4) if total_stake > 0 else 0.0

        # ROI por source
        def _roi_for(source: str):
            src_trades = [t for t in closed if t.get("source") == source]
            s_pnl = sum(float(t.get("pnl_virtual") or 0) for t in src_trades)
            s_stake = sum(float(t.get("virtual_stake") or _MIN_STAKE) for t in src_trades)
            s_wins = [t for t in src_trades if t.get("result") == "win"]
            return {
                "roi": round(s_pnl / s_stake, 4) if s_stake > 0 else 0.0,
                "win_rate": round(len(s_wins) / len(src_trades), 4) if src_trades else 0.0,
                "n": len(src_trades),
            }

        # Win rate
        win_rate = round(len(wins) / len(closed), 4) if closed else 0.0

        # Avg edge y odds
        all_edges = [float(t.get("edge") or 0) for t in trades]
        all_odds = [float(t.get("odds") or 0) for t in trades]
        avg_edge = round(mean(all_edges), 4) if all_edges else 0.0
        avg_odds = round(mean(all_odds), 4) if all_odds else 0.0

        # Bankroll actual
        pnl_all = [float(t.get("pnl_virtual") or 0) for t in trades if t.get("pnl_virtual") is not None]
        current_bankroll = round(_INITIAL_BANKROLL + sum(pnl_all), 4)

        # Last 20 win rate
        closed_sorted = sorted(
            closed,
            key=lambda t: t.get("closed_at") or datetime.min.replace(tzinfo=timezone.utc),
        )
        last_20 = closed_sorted[-20:] if len(closed_sorted) >= 20 else closed_sorted
        last_20_wins = [t for t in last_20 if t.get("result") == "win"]
        last_20_win_rate = round(len(last_20_wins) / len(last_20), 4) if last_20 else 0.0

        # Sharpe
        if len(closed_pnl) >= 3:
            try:
                sharpe = round(mean(closed_pnl) / stdev(closed_pnl), 4)
            except Exception:
                sharpe = 0.0
        else:
            sharpe = 0.0

        # Best/worst trade
        best_trade = max(closed_pnl) if closed_pnl else 0.0
        worst_trade = min(closed_pnl) if closed_pnl else 0.0

        # Racha actual
        streak = 0
        if closed_sorted:
            last_result = closed_sorted[-1].get("result")
            for t in reversed(closed_sorted):
                if t.get("result") == last_result:
                    streak += 1
                else:
                    break
            if last_result == "loss":
                streak = -streak

        # By source
        by_source = {
            "sports": _roi_for("sports"),
            "polymarket": _roi_for("polymarket"),
        }

        # By category
        by_category: dict = {}
        for t in closed:
            cat = t.get("category") or "other"
            if cat not in by_category:
                by_category[cat] = {"pnl": 0.0, "stake": 0.0, "wins": 0, "n": 0}
            by_category[cat]["pnl"] += float(t.get("pnl_virtual") or 0)
            by_category[cat]["stake"] += float(t.get("virtual_stake") or _MIN_STAKE)
            by_category[cat]["n"] += 1
            if t.get("result") == "win":
                by_category[cat]["wins"] += 1
        by_category_out = {}
        for cat, d in by_category.items():
            by_category_out[cat] = {
                "roi": round(d["pnl"] / d["stake"], 4) if d["stake"] > 0 else 0.0,
                "win_rate": round(d["wins"] / d["n"], 4) if d["n"] > 0 else 0.0,
                "n": d["n"],
            }

        # Last 20 roi
        last_20_pnl = sum(float(t.get("pnl_virtual") or 0) for t in last_20)
        last_20_stake = sum(float(t.get("virtual_stake") or _MIN_STAKE) for t in last_20)
        roi_last20 = round(last_20_pnl / last_20_stake, 4) if last_20_stake > 0 else 0.0

        ready_for_real = (
            roi_last20 > 0.10
            and last_20_win_rate > 0.55
            and len(closed) >= 20
        )

        # CLV metrics
        clv_values = [float(t["clv"]) for t in trades if t.get("clv") is not None]
        avg_clv = round(mean(clv_values), 4) if clv_values else 0.0
        clv_positive_rate = (
            round(sum(1 for v in clv_values if v > 0) / len(clv_values), 4)
            if clv_values else 0.0
        )
        clv_edge_confirmed = avg_clv > 0.03

        return {
            "total_trades": len(trades),
            "closed_trades": len(closed),
            "pending_trades": len(pending),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": win_rate,
            "roi_total": roi_total,
            "roi_sports": by_source["sports"]["roi"],
            "roi_poly": by_source["polymarket"]["roi"],
            "roi_last20": roi_last20,
            "last_20_win_rate": last_20_win_rate,
            "avg_edge": avg_edge,
            "avg_odds": avg_odds,
            "current_bankroll": current_bankroll,
            "sharpe": sharpe,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "streak": streak,
            "by_source": by_source,
            "by_category": by_category_out,
            "ready_for_real": ready_for_real,
            "avg_clv": avg_clv,
            "clv_positive_rate": clv_positive_rate,
            "clv_edge_confirmed": clv_edge_confirmed,
        }
    except Exception as e:
        logger.error("shadow: error en calculate_metrics: %s", e)
        return {
            "total_trades": 0,
            "closed_trades": 0,
            "pending_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "roi_total": 0.0,
            "roi_sports": 0.0,
            "roi_poly": 0.0,
            "roi_last20": 0.0,
            "last_20_win_rate": 0.0,
            "avg_edge": 0.0,
            "avg_odds": 0.0,
            "current_bankroll": _INITIAL_BANKROLL,
            "sharpe": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "streak": 0,
            "by_source": {},
            "by_category": {},
            "ready_for_real": False,
            "avg_clv": 0.0,
            "clv_positive_rate": 0.0,
            "clv_edge_confirmed": False,
        }
