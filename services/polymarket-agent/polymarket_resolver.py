"""
polymarket_resolver.py — Gradúa las alertas Polymarket enviadas con el resultado real.

Fuente de verdad de "qué se alertó": alerts_sent (type=polymarket, status sent), NO
poly_predictions.alerted — esa marca se pisaba en cada reanálisis y dejaba alertas
reales sin resolver nunca.

Flujo:
  1. alerts_sent sin result (o sin enlazar a su shadow_trade)
  2. GET gamma-api /markets/{id} por mercado
  → solo si closed=True y outcomePrices decidido (>0.9 → YES, <0.1 → NO)
  → compara lo ENVIADO (recommendation/direction de la alerta) vs outcome → win/loss
  → guarda result/outcome en el doc de alerts_sent
  → update_trade_result(shadow_trade de esa alerta)
  → poly_predictions.result con la última alerta del mercado (dashboard)

Se ejecuta diariamente a las 03:00 UTC via polymarket-resolve.yml.
"""
import asyncio
import json
import logging
import time as _time
from datetime import datetime, timedelta, timezone

import httpx

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col, iter_all
from shared.shadow_engine import update_trade_result

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
_HTTP_TIMEOUT = 20.0
_GAMMA_CONCURRENCY = 8
# /run-resolve es síncrono y el workflow corta a 300s.
_SHADOW_TIME_BUDGET_S = 200
_SHADOW_MATCH_WINDOW_S = 15 * 60
_NOTIFY_MAX_AGE_H = 48
# "sent" = envío vigente; "sent_archived" = envío anterior con el mismo alert_key que
# check_and_alert archiva antes de sobrescribir el doc al re-alertar.
_SENT_STATUSES = ("sent", "sent_archived")

_YES_THRESHOLD = 0.9
_NO_THRESHOLD = 0.1


async def _send_resolution_notification(pred_data: dict, outcome: str, result: str, now: datetime) -> None:
    """Envía notificación de resolución al topic Polymarket (thread_id=2)."""
    from shared.config import TELEGRAM_BOT_URL, CLOUD_RUN_TOKEN
    if not TELEGRAM_BOT_URL:
        return

    question = pred_data.get("question") or "Mercado desconocido"
    recommendation = pred_data.get("recommendation", "")
    edge = float(pred_data.get("edge") or 0)
    end_date_iso = str(pred_data.get("end_date_iso") or "")
    close_date = end_date_iso[:10].replace("-", "/") if end_date_iso else now.strftime("%d/%m/%Y")

    result_icon = "✅ ACERTADO" if result == "win" else "❌ FALLADO"
    text = (
        f"{result_icon}\n"
        f"Mercado: {question[:120]}\n"
        f"Nuestra predicción: {recommendation}\n"
        f"Resultado: {outcome}\n"
        f"Edge original: {edge:+.0%}\n"
        f"Cierre: {close_date}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{TELEGRAM_BOT_URL}/send-alert",
                json={"type": "polymarket_resolution", "data": {"text": text}},
                headers={"x-cloud-token": CLOUD_RUN_TOKEN},
            )
    except Exception:
        logger.warning("POLY_RESOLVE: error enviando notificación de resolución", exc_info=True)


def _determine_outcome(outcome_prices: list) -> str | None:
    """
    Devuelve "YES" | "NO" | None si el mercado aún no está resuelto.
    outcome_prices[0] es el precio final de YES.
    """
    if not outcome_prices:
        return None
    try:
        price_yes_final = float(outcome_prices[0])
    except (ValueError, TypeError):
        return None
    if price_yes_final > _YES_THRESHOLD:
        return "YES"
    if price_yes_final < _NO_THRESHOLD:
        return "NO"
    return None  # Entre 0.1-0.9: no resuelto aún


def _trade_result(recommendation: str, outcome: str) -> str | None:
    """
    Mapea recommendation + outcome a "win" | "loss" | None (WATCH/PASS → skip).
    BUY_YES + YES → win
    BUY_YES + NO  → loss
    BUY_NO  + NO  → win
    BUY_NO  + YES → loss
    """
    if recommendation == "BUY_YES":
        return "win" if outcome == "YES" else "loss"
    if recommendation == "BUY_NO":
        return "win" if outcome == "NO" else "loss"
    return None  # WATCH, PASS o vacío → no se apostó


def _alert_edge_from_key(alert: dict) -> float | None:
    """alert_key = "{market_id}_{round(edge, 2)}"."""
    try:
        return float(str(alert.get("alert_key") or "").rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None


def _alert_selection(alert: dict) -> str | None:
    """BUY_YES / BUY_NO de lo que se ENVIÓ. Las alertas nuevas guardan recommendation;
    las antiguas solo direction, y unas pocas de abril ni eso: entonces el signo del
    edge del alert_key (check_and_alert usa edge >= 0 → BUY_YES)."""
    for key in ("recommendation", "direction"):
        v = str(alert.get(key) or "").upper()
        if v in ("BUY_YES", "BUY_NO"):
            return v
    edge = _alert_edge_from_key(alert)
    if edge is None:
        return None
    return "BUY_YES" if edge >= 0 else "BUY_NO"


def _load_pending_alerts() -> list[tuple[str, dict]]:
    """Alertas Polymarket enviadas aún sin graduar o sin enlazar a su shadow_trade."""
    out = []
    for doc in iter_all("alerts_sent", where=("type", "==", "polymarket")):
        a = doc.to_dict() or {}
        if a.get("status") not in _SENT_STATUSES or not a.get("market_id"):
            continue
        if a.get("result") in ("win", "loss") and a.get("shadow_synced"):
            continue
        out.append((doc.id, a))
    return out


async def _fetch_gamma_markets(market_ids: list[str]) -> dict[str, dict | None]:
    """GET /markets/{id} con concurrencia limitada. None si Gamma no responde."""
    sem = asyncio.Semaphore(_GAMMA_CONCURRENCY)
    out: dict[str, dict | None] = {}

    async def _one(client: httpx.AsyncClient, mid: str) -> None:
        async with sem:
            try:
                resp = await client.get(f"{GAMMA_API}/markets/{mid}")
                if resp.status_code != 200:
                    out[mid] = None
                    return
                raw = resp.json()
                out[mid] = (raw[0] if raw else None) if isinstance(raw, list) else raw
            except Exception:
                out[mid] = None

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        await asyncio.gather(*(_one(client, m) for m in market_ids))
    return out


def _match_shadow_trade(trades: list, sent_at) -> tuple[str, dict] | None:
    """shadow_trade creado por esta alerta: track_new_signal corre justo después del
    envío, así que es el de opened_at más cercano a sent_at (±15 min)."""
    if not sent_at:
        return None
    best = None
    for doc in trades:
        t = doc.to_dict() or {}
        opened = t.get("opened_at")
        if not opened:
            continue
        if hasattr(opened, "tzinfo") and opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        gap = abs((opened - sent_at).total_seconds())
        if gap <= _SHADOW_MATCH_WINDOW_S and (best is None or gap < best[0]):
            best = (gap, doc.id, t)
    return (best[1], best[2]) if best else None


async def resolve_closed_markets() -> dict:
    """
    Gradúa TODAS las alertas Polymarket enviadas contra el resultado real de Gamma y
    sincroniza su shadow_trade. Cada envío cuenta por separado (un mercado alertado
    3 veces = 3 alertas).

    Devuelve {resolved, skipped_unresolved, shadow_synced, shadow_deferred, errors}.
    """
    started = _time.monotonic()
    now = datetime.now(timezone.utc)
    resolved = skipped_unresolved = shadow_synced = shadow_deferred = errors = 0

    try:
        pending = _load_pending_alerts()
    except Exception:
        logger.error("POLY_RESOLVE: error leyendo alerts_sent", exc_info=True)
        return {"resolved": 0, "skipped_unresolved": 0, "shadow_synced": 0,
                "shadow_deferred": 0, "errors": 1}

    market_ids = sorted({
        str(a["market_id"]) for _, a in pending if a.get("result") not in ("win", "loss")
    })
    logger.info(
        "POLY_RESOLVE: %d alertas pendientes (%d mercados por consultar en Gamma)",
        len(pending), len(market_ids),
    )
    gamma = await _fetch_gamma_markets(market_ids) if market_ids else {}

    # Última alerta graduada por mercado → result en poly_predictions (dashboard).
    latest_by_market: dict[str, tuple] = {}
    shadow_cache: dict[str, list] = {}

    for alert_id, alert in pending:
        market_id = str(alert["market_id"])
        sent_at = alert.get("sent_at")
        if sent_at is not None and hasattr(sent_at, "tzinfo") and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        selection = _alert_selection(alert)
        try:
            result = alert.get("result")
            outcome = alert.get("outcome")
            if result not in ("win", "loss"):
                raw = gamma.get(market_id)
                # Solo mercados cerrados: un mercado vivo a 0.95 NO está resuelto.
                if not raw or raw.get("closed") is not True:
                    skipped_unresolved += 1
                    continue
                op = raw.get("outcomePrices") or []
                if isinstance(op, str):
                    try:
                        op = json.loads(op)
                    except Exception:
                        op = []
                outcome = _determine_outcome(op)
                result = _trade_result(selection or "", outcome) if outcome else None
                if result is None:
                    skipped_unresolved += 1
                    continue
                col("alerts_sent").document(alert_id).update({
                    "result": result,
                    "outcome": outcome,
                    "graded_selection": selection,
                    "resolved_at": now,
                    "market_closed_time": raw.get("closedTime"),
                })
                resolved += 1
                logger.info(
                    "POLY_RESOLVE: alerta %s (%s) — %s vs outcome=%s → %s",
                    alert_id, market_id, selection, outcome, result,
                )
                prev = latest_by_market.get(market_id)
                if prev is None or (sent_at and (prev[0] is None or sent_at > prev[0])):
                    latest_by_market[market_id] = (sent_at, result, outcome)

                # Notificar solo cierres recientes: el primer pase gradúa cientos de
                # alertas atrasadas y no deben llegar todas a Telegram de golpe.
                try:
                    from scanner import _parse_gamma_ts
                    _ct = _parse_gamma_ts(raw.get("closedTime"))
                    if _ct is not None and now - _ct <= timedelta(hours=_NOTIFY_MAX_AGE_H):
                        _edge = alert.get("edge")
                        await _send_resolution_notification(
                            {
                                "question": alert.get("question") or raw.get("question"),
                                "recommendation": selection,
                                "edge": _edge if _edge is not None else _alert_edge_from_key(alert),
                                "end_date_iso": alert.get("end_date_iso") or raw.get("endDate"),
                            },
                            outcome, result, now,
                        )
                except Exception:
                    logger.warning("POLY_RESOLVE: error notificando %s", alert_id, exc_info=True)

            # Sincronizar el shadow_trade de esta alerta (ledger de ROI y aprendizaje).
            # update_trade_result recalcula el bankroll leyendo todo shadow_trades, así
            # que va con presupuesto de tiempo; el resto se sincroniza en la siguiente
            # ejecución (la alerta ya queda graduada).
            if _time.monotonic() - started > _SHADOW_TIME_BUDGET_S:
                shadow_deferred += 1
                continue
            if market_id not in shadow_cache:
                shadow_cache[market_id] = [
                    d for d in col("shadow_trades")
                    .where(filter=FieldFilter("signal_id", "==", market_id))
                    .stream()
                    if str((d.to_dict() or {}).get("source", "")).startswith("polymarket")
                ]
            match = _match_shadow_trade(shadow_cache[market_id], sent_at)
            shadow_id = None
            if match:
                shadow_id, trade = match
                trade_result = _trade_result(str(trade.get("selection") or ""), outcome) or result
                # Pendiente → se gradúa. Ya resuelto con otro resultado → el resolver
                # antiguo lo graduó con la recomendación actual, no la enviada: se corrige.
                if trade.get("result") != "void" and trade.get("result") != trade_result:
                    await update_trade_result(shadow_id, trade_result)
            col("alerts_sent").document(alert_id).update({
                "shadow_synced": True,
                "shadow_trade_id": shadow_id,
            })
            shadow_synced += 1
        except Exception:
            logger.error("POLY_RESOLVE: error procesando alerta %s", alert_id, exc_info=True)
            errors += 1

    for market_id, (_sent, result, outcome) in latest_by_market.items():
        try:
            col("poly_predictions").document(market_id).update({
                "result": result,
                "outcome": outcome,
                "resolved_at": now,
            })
        except Exception:
            # El doc puede no existir (purgado por mantenimiento) — no es error.
            logger.debug("POLY_RESOLVE: poly_predictions/%s no actualizado", market_id)

    logger.info(
        "POLY_RESOLVE: completado — resolved=%d skipped_unresolved=%d shadow_synced=%d "
        "shadow_deferred=%d errors=%d",
        resolved, skipped_unresolved, shadow_synced, shadow_deferred, errors,
    )
    return {
        "resolved": resolved,
        "skipped_unresolved": skipped_unresolved,
        "shadow_synced": shadow_synced,
        "shadow_deferred": shadow_deferred,
        "errors": errors,
    }
