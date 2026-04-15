"""
sports-agent — FastAPI service
Endpoints: /run-collect /run-enrich /run-analyze /run-learning /run-backtest /health /status
Todos los endpoints /run-* devuelven 202 Accepted inmediatamente.
El trabajo real se ejecuta en background (asyncio.create_task).
"""
import asyncio
import logging
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="sports-agent")

CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")

# Timestamps de ultima ejecucion (en memoria — se pierden al reiniciar)
_status: dict = {"last_collect": None, "last_enrich": None, "last_analyze": None}


def verify_token(x_cloud_token: str = Header(...)) -> None:
    """Valida el token secreto para proteger los endpoints /run-*."""
    if x_cloud_token != CLOUD_RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict:
    return {
        "last_collect": _status["last_collect"],
        "last_enrich": _status["last_enrich"],
        "last_analyze": _status["last_analyze"],
    }


@app.post("/run-collect", dependencies=[Depends(verify_token)])
async def run_collect() -> JSONResponse:
    """202 inmediato → background: collectors/ → Firestore."""
    asyncio.create_task(_bg_collect())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "collect"})


@app.post("/run-enrich", dependencies=[Depends(verify_token)])
async def run_enrich() -> JSONResponse:
    """202 inmediato → background: enrichers/ → Firestore."""
    asyncio.create_task(_bg_enrich())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "enrich"})


@app.post("/run-analyze", dependencies=[Depends(verify_token)])
async def run_analyze() -> JSONResponse:
    """202 inmediato → background: value_bet_engine → Firestore."""
    asyncio.create_task(_bg_analyze())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "analyze"})


@app.post("/run-learning", dependencies=[Depends(verify_token)])
async def run_learning() -> JSONResponse:
    """202 inmediato → background: learning_engine → Firestore."""
    asyncio.create_task(_bg_learning())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "learning"})


@app.post("/run-backtest", dependencies=[Depends(verify_token)])
async def run_backtest() -> JSONResponse:
    """202 inmediato → background: backtester/backtest.py. Ejecutar UNA SOLA VEZ al arrancar."""
    asyncio.create_task(_bg_backtest())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "backtest"})


# --- Background tasks ---

async def _bg_collect() -> None:
    try:
        # TODO: implementar en Sesion 2
        # from collectors.football_api import get_upcoming_matches, get_team_stats, get_h2h
        # from collectors.api_sports_client import get_games_today
        # from collectors.firestore_writer import save_upcoming_matches, save_team_stats, save_h2h
        logger.info("collect: inicio (pendiente implementacion Sesion 2)")
        _status["last_collect"] = datetime.utcnow().isoformat()
    except Exception as e:
        logger.error("collect: error — %s", e, exc_info=True)


async def _bg_enrich() -> None:
    try:
        # TODO: implementar en Sesion 3
        # from enrichers.data_enricher import run_enrichment
        logger.info("enrich: inicio (pendiente implementacion Sesion 3)")
        _status["last_enrich"] = datetime.utcnow().isoformat()
    except Exception as e:
        logger.error("enrich: error — %s", e, exc_info=True)


async def _bg_analyze() -> None:
    try:
        # TODO: implementar en Sesion 4
        # from analyzers.value_bet_engine import generate_signal
        logger.info("analyze: inicio (pendiente implementacion Sesion 4)")
        _status["last_analyze"] = datetime.utcnow().isoformat()
    except Exception as e:
        logger.error("analyze: error — %s", e, exc_info=True)


async def _bg_learning() -> None:
    try:
        # TODO: implementar en Sesion 4
        # from learner.learning_engine import run_daily_learning
        logger.info("learning: inicio (pendiente implementacion Sesion 4)")
    except Exception as e:
        logger.error("learning: error — %s", e, exc_info=True)


async def _bg_backtest() -> None:
    try:
        # TODO: implementar en Sesion 4
        # from backtester.backtest import run_backtest
        logger.info("backtest: inicio (pendiente implementacion Sesion 4)")
    except Exception as e:
        logger.error("backtest: error — %s", e, exc_info=True)
