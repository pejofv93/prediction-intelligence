"""
polymarket-agent — FastAPI service
Endpoints: /run-scan /run-enrich /run-analyze /run-poly-backtest /run-websocket /health
Todos los endpoints /run-* devuelven 202 Accepted inmediatamente.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="polymarket-agent")

CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")


def verify_token(x_cloud_token: str = Header(...)) -> None:
    if x_cloud_token != CLOUD_RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/run-scan", dependencies=[Depends(verify_token)])
async def run_scan() -> JSONResponse:
    """202 inmediato → background: scanner + price_tracker → poly_markets + poly_price_history."""
    asyncio.create_task(_bg_scan())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "scan"})


@app.post("/run-enrich", dependencies=[Depends(verify_token)])
async def run_enrich() -> JSONResponse:
    """202 inmediato → background: enrichers + realtime smart_money_analysis → enriched_markets."""
    asyncio.create_task(_bg_enrich())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "enrich"})


@app.post("/run-analyze", dependencies=[Depends(verify_token)])
async def run_analyze() -> JSONResponse:
    """202 inmediato → background: groq_analyzer + maintenance → poly_predictions."""
    asyncio.create_task(_bg_analyze())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "analyze"})


@app.post("/run-poly-backtest", dependencies=[Depends(verify_token)])
async def run_poly_backtest() -> JSONResponse:
    """202 inmediato → background: backtester/backtest_poly.py. Ejecutar UNA SOLA VEZ."""
    asyncio.create_task(_bg_poly_backtest())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "poly-backtest"})


@app.post("/run-websocket", dependencies=[Depends(verify_token)])
async def run_websocket() -> JSONResponse:
    """202 inmediato → inicia asyncio.create_task(websocket_loop) — loop infinito."""
    asyncio.create_task(_bg_websocket())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "websocket"})


# --- Background tasks ---

async def _bg_scan() -> None:
    try:
        # TODO: implementar en Sesion 5
        # from scanner import fetch_active_markets
        # from price_tracker import save_price_snapshot
        logger.info("scan: inicio (pendiente implementacion Sesion 5)")
    except Exception as e:
        logger.error("scan: error — %s", e, exc_info=True)


async def _bg_enrich() -> None:
    try:
        # TODO: implementar en Sesion 5
        # from enrichers.market_enricher import enrich_market
        logger.info("enrich: inicio (pendiente implementacion Sesion 5)")
    except Exception as e:
        logger.error("enrich: error — %s", e, exc_info=True)


async def _bg_analyze() -> None:
    try:
        # TODO: implementar en Sesion 5
        # from groq_analyzer import analyze_market, run_maintenance
        logger.info("analyze: inicio (pendiente implementacion Sesion 5)")
    except Exception as e:
        logger.error("analyze: error — %s", e, exc_info=True)


async def _bg_poly_backtest() -> None:
    try:
        # TODO: implementar en Sesion 5
        # from backtester.backtest_poly import run_poly_backtest
        logger.info("poly-backtest: inicio (pendiente implementacion Sesion 5)")
    except Exception as e:
        logger.error("poly-backtest: error — %s", e, exc_info=True)


async def _bg_websocket() -> None:
    try:
        # TODO: implementar en Sesion 5
        # from realtime.websocket_manager import start_monitoring
        logger.info("websocket: inicio (pendiente implementacion Sesion 5)")
    except Exception as e:
        logger.error("websocket: error — %s", e, exc_info=True)
