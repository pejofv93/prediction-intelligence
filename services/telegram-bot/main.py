"""
telegram-bot — FastAPI service (modo WEBHOOK, no polling)
Telegram envia HTTP POST a /webhook cuando llega un mensaje.
Los agentes llaman POST /send-alert cuando generan senales.
min-instances=0 — compatible con webhook (no necesita conexion permanente).

Configurar webhook tras deploy (solo una vez):
curl -X POST "https://api.telegram.org/bot{TOKEN}/setWebhook?url={CLOUD_RUN_URL}/webhook"
Ver target set-webhook en Makefile.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from shared.config import TELEGRAM_SPORTS_THREAD_ID, TELEGRAM_POLY_THREAD_ID, TELEGRAM_DAILY_THREAD_ID
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="telegram-bot")

CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")


def verify_token(x_cloud_token: str = Header(...)) -> None:
    if x_cloud_token != CLOUD_RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    """
    Recibe updates de Telegram → despacha a handlers.py.
    NO protegido con x-cloud-token — Telegram lo llama directamente.
    Siempre devuelve 200 OK para que Telegram no reintente el envio.
    """
    try:
        update = await request.json()
    except Exception:
        logger.error("webhook: JSON invalido en request")
        return JSONResponse({"ok": True})

    try:
        from handlers import dispatch_update
        await dispatch_update(update)
    except Exception:
        logger.error("webhook: error en dispatch_update", exc_info=True)

    return JSONResponse({"ok": True})


@app.post("/run-btc-snapshot", dependencies=[Depends(verify_token)])
async def run_btc_snapshot() -> JSONResponse:
    """202 → background: guarda snapshot BTC en Firestore."""
    asyncio.create_task(_bg_btc_snapshot())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "btc-snapshot"})


async def _bg_btc_snapshot() -> None:
    try:
        # Import inline para no fallar si binance_tracker no está disponible
        from realtime.binance_tracker import save_btc_snapshot
        await save_btc_snapshot()
        logger.info("btc-snapshot: guardado en Firestore")
    except Exception as e:
        logger.error("btc-snapshot: error — %s", e, exc_info=True)


@app.post("/send-alert", dependencies=[Depends(verify_token)])
async def send_alert(request: Request) -> JSONResponse:
    """
    Recibe senal de sports-agent o polymarket-agent → envia alerta Telegram.
    Body: {"type": "sports"|"polymarket", "data": {...}}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON invalido")

    alert_type = body.get("type", "")
    data = body.get("data", {})

    if not alert_type or not data:
        raise HTTPException(status_code=400, detail="Faltan campos 'type' o 'data'")

    from alert_manager import send_sports_alert, send_poly_alert, send_message

    sent = False
    try:
        if alert_type == "sports":
            sent = await send_sports_alert(data)
        elif alert_type == "polymarket":
            sent = await send_poly_alert(data)
        elif alert_type == "polymarket_resolution":
            text = data.get("text", "")
            if text:
                sent = await send_message(text, message_thread_id=TELEGRAM_POLY_THREAD_ID)
        elif alert_type == "arbitrage":
            text = data.get("message", "")
            if text:
                sent = await send_message(text, message_thread_id=TELEGRAM_SPORTS_THREAD_ID)
        else:
            logger.warning("send-alert: tipo desconocido '%s'", alert_type)
    except Exception:
        logger.error("send-alert: error enviando alerta tipo=%s", alert_type, exc_info=True)

    logger.info("send-alert: tipo=%s sent=%s", alert_type, sent)
    return JSONResponse({"ok": True, "sent": sent})


@app.post("/daily-report", dependencies=[Depends(verify_token)])
async def daily_report() -> JSONResponse:
    """
    Genera y envia el reporte diario a Telegram.
    Llamado por Cloud Scheduler o GitHub Actions a las 08:00 Europe/Madrid.
    """
    asyncio.create_task(_bg_daily_report())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "daily-report"})


async def _bg_daily_report() -> None:
    """
    1. check_model_health() → health dict
    2. calculate_metrics() → shadow_metrics (ROI/bankroll de shadow_trades)
    3. Contar predictions sports directamente (result=None/correct) para estadísticas reales
    4. Buscar top signal en predictions (result==None, unified_score DESC)
    5. format_daily_report(health, shadow_metrics, top_signal, pred_stats)
    6. Si health.degraded: send_message(format_health_alert(health))
    """
    try:
        from shared.model_health import check_model_health, format_health_alert, format_daily_report
        from shared.shadow_engine import calculate_metrics
        from shared.firestore_client import col
        from alert_manager import send_message

        health = check_model_health()
        shadow_metrics = calculate_metrics()

        # Contar predicciones sports — separando sintéticas (POISSON_SYNTHETIC) de cuotas reales
        pred_stats = {
            "total": 0, "pending": 0, "resolved": 0, "correct": 0, "incorrect": 0,
            "synthetic": 0, "real_odds": 0,
        }
        tier_stats: dict = {"fuerte": [], "detectada": [], "moderada": []}
        try:
            all_preds = list(col("predictions").limit(500).stream())
            for doc in all_preds:
                d = doc.to_dict()
                pred_stats["total"] += 1
                is_synthetic = d.get("is_synthetic", False) or d.get("source", "") == "POISSON_SYNTHETIC"
                if is_synthetic:
                    pred_stats["synthetic"] += 1
                else:
                    pred_stats["real_odds"] += 1
                if d.get("result") is None and d.get("correct") is None:
                    pred_stats["pending"] += 1
                else:
                    pred_stats["resolved"] += 1
                    if d.get("correct") is True:
                        pred_stats["correct"] += 1
                    elif d.get("correct") is False:
                        pred_stats["incorrect"] += 1
                # Clasificar por tier de edge para el desglose del reporte
                _edge = float(d.get("edge") or d.get("ev") or 0)
                if _edge > 0.20:
                    tier_stats["fuerte"].append(d)
                elif _edge > 0.12:
                    tier_stats["detectada"].append(d)
                elif _edge > 0.08:
                    tier_stats["moderada"].append(d)
        except Exception as _pe:
            logger.warning("daily-report: error leyendo predictions — %s", _pe)

        # Buscar top signal
        top_signal = None
        try:
            docs = list(
                col("predictions")
                .where(filter=FieldFilter("result", "==", None))
                .order_by("unified_score", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            if docs:
                top_signal = docs[0].to_dict()
            else:
                docs = list(
                    col("poly_predictions")
                    .where(filter=FieldFilter("alerted", "==", False))
                    .order_by("edge", direction="DESCENDING")
                    .limit(1)
                    .stream()
                )
                if docs:
                    top_signal = docs[0].to_dict()
        except Exception:
            pass

        report = format_daily_report(health, shadow_metrics, top_signal, pred_stats, tier_stats)
        await send_message(report, message_thread_id=TELEGRAM_DAILY_THREAD_ID)

        if health.get("degraded"):
            await send_message(format_health_alert(health), message_thread_id=TELEGRAM_DAILY_THREAD_ID)

        logger.info("daily-report: enviado correctamente")
    except Exception as e:
        logger.error("daily-report: error — %s", e, exc_info=True)


@app.post("/send-weekly-report", dependencies=[Depends(verify_token)])
async def send_weekly_report() -> JSONResponse:
    """
    Llamado por weekly-report.yml scheduler (lunes 08:00 UTC).
    Construye week_stats desde Firestore y envia reporte via Telegram.

    Queries:
    1. accuracy_log donde week == current_week
    2. model_weights doc 'current'
    3. predictions donde created_at >= semana_actual
    4. poly_predictions donde analyzed_at >= semana_actual
    """
    from shared.firestore_client import col
    from shared.report_generator import generate_weekly_report
    from alert_manager import send_message

    try:
        now = datetime.now(timezone.utc)

        # El reporte del lunes cubre la semana que ACABA DE TERMINAR (lun-dom anterior).
        # week_end = este lunes 00:00 UTC  |  week_start = el lunes anterior 00:00 UTC
        week_end = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_start = week_end - timedelta(weeks=1)
        prev_week_dt = week_end - timedelta(days=1)  # domingo de la semana que acaba
        prev_iso = prev_week_dt.isocalendar()
        prev_week = f"{prev_iso[0]}-W{prev_iso[1]:02d}"

        # 1. accuracy_log semana anterior (la que acaba de terminar)
        log_docs = list(
            col("accuracy_log")
            .where(filter=FieldFilter("week", "==", prev_week))
            .limit(1)
            .stream()
        )
        log = log_docs[0].to_dict() if log_docs else {}

        # 2. model_weights doc current
        weights_doc = col("model_weights").document("current").get()
        weights_data = weights_doc.to_dict() if weights_doc.exists else {}
        weights_after = weights_data.get("weights", {})
        weights_before = log.get("weights_start", weights_after)

        # 3. predictions de la semana anterior
        pred_docs = list(
            col("predictions")
            .where(filter=FieldFilter("created_at", ">=", week_start))
            .where(filter=FieldFilter("created_at", "<", week_end))
            .stream()
        )
        week_preds = [d.to_dict() for d in pred_docs]

        best_match = "N/A"
        best_edge = 0.0
        best_result = "N/A"
        worst_match = "N/A"
        worst_edge = 0.0
        worst_error = "N/A"

        if week_preds:
            correct_preds = [p for p in week_preds if p.get("correct") is True]
            if correct_preds:
                best_pred = max(correct_preds, key=lambda p: float(p.get("edge", 0)))
                best_match = (
                    f"{best_pred.get('home_team', '?')} vs {best_pred.get('away_team', '?')}"
                )
                best_edge = float(best_pred.get("edge", 0))
                best_result = best_pred.get("result", "N/A") or "N/A"

            wrong_preds = [p for p in week_preds if p.get("correct") is False]
            if wrong_preds:
                worst_pred = min(wrong_preds, key=lambda p: float(p.get("confidence", 1)))
                worst_match = (
                    f"{worst_pred.get('home_team', '?')} vs {worst_pred.get('away_team', '?')}"
                )
                worst_edge = float(worst_pred.get("edge", 0))
                worst_error = worst_pred.get("error_type", "N/A") or "N/A"

        # 4. poly_predictions de la semana anterior
        poly_docs = list(
            col("poly_predictions")
            .where(filter=FieldFilter("analyzed_at", ">=", week_start))
            .where(filter=FieldFilter("analyzed_at", "<", week_end))
            .stream()
        )
        poly_preds = [d.to_dict() for d in poly_docs]
        poly_total = len(poly_preds)
        poly_alerts = sum(1 for p in poly_preds if p.get("alerted") is True)
        poly_avg_edge = (
            sum(float(p.get("edge", 0)) for p in poly_preds) / poly_total
            if poly_total > 0
            else 0.0
        )

        # Accuracy BUY_YES / BUY_NO (solo mercados con resultado conocido)
        resolved_poly = [p for p in poly_preds if p.get("resolved") is True]
        poly_buy_yes = [p for p in resolved_poly if p.get("recommendation") == "BUY_YES"]
        poly_buy_no = [p for p in resolved_poly if p.get("recommendation") == "BUY_NO"]
        poly_buy_yes_correct = sum(1 for p in poly_buy_yes if p.get("outcome") == "correct")
        poly_buy_no_correct = sum(1 for p in poly_buy_no if p.get("outcome") == "correct")

        # Mejor señal poly (mayor edge entre las alertadas)
        alerted_poly = [p for p in poly_preds if p.get("alerted") is True]
        poly_best_market = "—"
        poly_best_edge = 0.0
        if alerted_poly:
            best_poly = max(alerted_poly, key=lambda p: abs(float(p.get("edge") or 0)))
            poly_best_market = str(best_poly.get("question") or "")[:40]
            poly_best_edge = abs(float(best_poly.get("edge") or 0))

        # Bankroll virtual (shadow trades)
        try:
            from shared.shadow_engine import calculate_metrics
            shadow_metrics = calculate_metrics()
        except Exception:
            logger.warning("send-weekly-report: error calculando shadow_metrics", exc_info=True)
            shadow_metrics = {}

        # Construir week_stats
        week_stats = {
            "week": prev_week,
            "predictions_total": int(log.get("predictions_total", 0)),
            "predictions_correct": int(log.get("predictions_correct", 0)),
            "accuracy": float(log.get("accuracy", 0.0)),
            "prev_week_accuracy": log.get("prev_week_accuracy"),
            "accuracy_by_league": log.get("accuracy_by_league", {}),
            "best_match": best_match,
            "best_edge": best_edge,
            "best_result": best_result,
            "worst_match": worst_match,
            "worst_edge": worst_edge,
            "worst_error": worst_error,
            "poly_total": poly_total,
            "poly_alerts": poly_alerts,
            "poly_avg_edge": poly_avg_edge,
            "poly_buy_yes_correct": poly_buy_yes_correct,
            "poly_buy_yes_total": len(poly_buy_yes),
            "poly_buy_no_correct": poly_buy_no_correct,
            "poly_buy_no_total": len(poly_buy_no),
            "poly_best_market": poly_best_market,
            "poly_best_edge": poly_best_edge,
            "bankroll_current": shadow_metrics.get("current_bankroll", 50.0),
            "roi_total": shadow_metrics.get("roi_total", 0.0),
            "roi_sports": shadow_metrics.get("roi_sports", 0.0),
            "win_rate": shadow_metrics.get("win_rate", 0.0),
            "closed_trades": shadow_metrics.get("closed_trades", 0),
            "streak": shadow_metrics.get("streak", 0),
        }

        report_text = generate_weekly_report(week_stats, weights_before, weights_after)
        await send_message(report_text, message_thread_id=TELEGRAM_DAILY_THREAD_ID)

        logger.info(
            "send-weekly-report: enviado para %s (%d preds, %d poly)",
            prev_week,
            week_stats["predictions_total"],
            poly_total,
        )
        return JSONResponse({"ok": True, "week": prev_week})

    except Exception:
        logger.error("send-weekly-report: error no controlado", exc_info=True)
        # Intentar enviar un mensaje de error al dueño
        try:
            from alert_manager import send_message
            await send_message("⚠️ Error generando reporte semanal. Revisa los logs.", message_thread_id=TELEGRAM_DAILY_THREAD_ID)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Error generando reporte semanal")
