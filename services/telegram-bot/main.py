"""
telegram-bot — FastAPI service (modo WEBHOOK, no polling)
Telegram envia HTTP POST a /webhook cuando llega un mensaje.
Los agentes llaman POST /send-alert cuando generan senales.
min-instances=0 — compatible con webhook (no necesita conexion permanente).

Configurar webhook tras deploy (solo una vez):
curl -X POST "https://api.telegram.org/bot{TOKEN}/setWebhook?url={CLOUD_RUN_URL}/webhook"
Ver target set-webhook en Makefile.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request
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

    from alert_manager import send_sports_alert, send_poly_alert

    sent = False
    try:
        if alert_type == "sports":
            sent = await send_sports_alert(data)
        elif alert_type == "polymarket":
            sent = await send_poly_alert(data)
        else:
            logger.warning("send-alert: tipo desconocido '%s'", alert_type)
    except Exception:
        logger.error("send-alert: error enviando alerta tipo=%s", alert_type, exc_info=True)

    logger.info("send-alert: tipo=%s sent=%s", alert_type, sent)
    return JSONResponse({"ok": True, "sent": sent})


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
        iso = now.isocalendar()
        current_week = f"{iso[0]}-W{iso[1]:02d}"

        # Inicio de la semana actual (lunes 00:00 UTC)
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

        # 1. accuracy_log semana actual
        log_docs = list(
            col("accuracy_log")
            .where("week", "==", current_week)
            .limit(1)
            .stream()
        )
        log = log_docs[0].to_dict() if log_docs else {}

        # 2. model_weights doc current
        weights_doc = col("model_weights").document("current").get()
        weights_data = weights_doc.to_dict() if weights_doc.exists else {}
        weights_after = weights_data.get("weights", {})
        weights_before = log.get("weights_start", weights_after)

        # 3. predictions de la semana actual
        pred_docs = list(
            col("predictions")
            .where("created_at", ">=", week_start)
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

        # 4. poly_predictions de la semana actual
        poly_docs = list(
            col("poly_predictions")
            .where("analyzed_at", ">=", week_start)
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

        # Construir week_stats
        week_stats = {
            "week": current_week,
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
        }

        report_text = generate_weekly_report(week_stats, weights_before, weights_after)
        await send_message(report_text)

        logger.info(
            "send-weekly-report: enviado para %s (%d preds, %d poly)",
            current_week,
            week_stats["predictions_total"],
            poly_total,
        )
        return JSONResponse({"ok": True, "week": current_week})

    except Exception:
        logger.error("send-weekly-report: error no controlado", exc_info=True)
        # Intentar enviar un mensaje de error al dueño
        try:
            from alert_manager import send_message
            await send_message("⚠️ Error generando reporte semanal. Revisa los logs.")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Error generando reporte semanal")
