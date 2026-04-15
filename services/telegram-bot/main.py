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
async def webhook(request: Request) -> dict:
    """
    Recibe updates de Telegram → despacha a handlers.py.
    NO protegido con x-cloud-token — Telegram lo llama directamente.
    """
    # TODO: implementar en Sesion 6
    # from handlers import dispatch_update
    data = await request.json()
    logger.info("webhook: update recibido (pendiente implementacion Sesion 6)")
    return {"ok": True}


@app.post("/send-alert", dependencies=[Depends(verify_token)])
async def send_alert(request: Request) -> dict:
    """
    Recibe senal de sports-agent o polymarket-agent → envia alerta Telegram.
    Body: {"type": "sports"|"polymarket", "data": {...}}
    """
    # TODO: implementar en Sesion 6
    # from alert_manager import send_sports_alert, send_poly_alert
    data = await request.json()
    logger.info("send-alert: tipo=%s (pendiente implementacion Sesion 6)", data.get("type"))
    return {"ok": True}


@app.post("/send-weekly-report", dependencies=[Depends(verify_token)])
async def send_weekly_report() -> dict:
    """
    Llamado por weekly-report.yml scheduler (lunes 08:00 UTC).
    Llama shared.report_generator.generate_weekly_report() y envia resultado.
    """
    # TODO: implementar en Sesion 6
    logger.info("send-weekly-report: pendiente implementacion Sesion 6")
    return {"ok": True}
