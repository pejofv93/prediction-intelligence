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

# Flag para ejecutar retroactive_eval una sola vez por arranque
_retroactive_done = False

CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")


def verify_token(x_cloud_token: str = Header(...)) -> None:
    if x_cloud_token != CLOUD_RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.on_event("startup")
async def _startup() -> None:
    """Lanza retroactive_eval una sola vez al arrancar."""
    asyncio.create_task(_bg_retroactive_eval())


async def _bg_retroactive_eval() -> None:
    """Ejecuta retroactive_eval en background una sola vez."""
    global _retroactive_done
    if _retroactive_done:
        return
    _retroactive_done = True
    try:
        from shared.shadow_engine import retroactive_eval
        result = await retroactive_eval()
        logger.info("retroactive_eval completada: %s", result)
    except Exception as e:
        logger.error("retroactive_eval: error — %s", e, exc_info=True)


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
    """
    Pipeline de escaneo:
    1. fetch_active_markets() → guarda en Firestore poly_markets
    2. Por cada mercado: save_price_snapshot() → poly_price_history
    """
    try:
        from datetime import datetime, timezone
        start = datetime.now(timezone.utc)
        logger.info("scan: iniciando pipeline")

        from scanner import fetch_active_markets
        from price_tracker import save_price_snapshot

        markets = await fetch_active_markets(limit=50, min_volume=1000)
        if not markets:
            logger.warning("scan: ningun mercado obtenido")
            return

        for market in markets:
            market_id = market.get("market_id", "")
            try:
                await save_price_snapshot(
                    market_id=market_id,
                    price_yes=float(market.get("price_yes", 0.5)),
                    price_no=float(market.get("price_no", 0.5)),
                    volume_24h=float(market.get("volume_24h", 0)),
                )
            except Exception:
                logger.error("scan: error guardando snapshot de %s", market_id, exc_info=True)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("scan: %d mercados escaneados en %.1fs", len(markets), elapsed)

    except Exception as e:
        logger.error("scan: error no controlado — %s", e, exc_info=True)


async def _bg_enrich() -> None:
    """
    Pipeline de enriquecimiento:
    1. Lee mercados activos de poly_markets
    2. run_enrichment() → applica todos los enrichers → enriched_markets
    3. run_smart_money_analysis() para top mercados por volumen
    """
    try:
        from datetime import datetime, timezone
        start = datetime.now(timezone.utc)
        logger.info("enrich: iniciando pipeline")

        from enrichers.market_enricher import run_enrichment
        from shared.firestore_client import col

        docs = list(col("poly_markets").stream())
        markets = [d.to_dict() for d in docs]

        count = await run_enrichment(markets)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("enrich: %d mercados enriquecidos en %.1fs", count, elapsed)

    except Exception as e:
        logger.error("enrich: error no controlado — %s", e, exc_info=True)


async def _bg_analyze() -> None:
    """
    Pipeline de analisis:
    1. Lee enriched_markets de Firestore
    2. analyze_market() con Groq → poly_predictions
    3. check_and_alert() para senales con edge suficiente
    4. run_maintenance() limpia datos antiguos
    """
    try:
        import asyncio
        from datetime import datetime, timezone
        start = datetime.now(timezone.utc)
        logger.info("analyze: iniciando pipeline")

        from groq_analyzer import analyze_market, run_maintenance
        from alert_engine import check_and_alert
        from shared.firestore_client import col
        from shared.groq_client import GROQ_CALL_DELAY

        docs = list(col("enriched_markets").stream())
        if not docs:
            logger.warning("analyze: sin enriched_markets en Firestore")
        else:
            predictions_generated = 0
            alerts_sent = 0

            skipped_volume = 0
            skipped_groq = 0

            for i, doc in enumerate(docs):
                enriched = doc.to_dict()
                if i > 0:
                    await asyncio.sleep(GROQ_CALL_DELAY)

                try:
                    prediction = await analyze_market(enriched)
                    if prediction is None:
                        skipped_volume += 1
                    else:
                        predictions_generated += 1

                        # Calcular unified_score antes de alertar
                        try:
                            from shared.unified_score import calculate_unified_score
                            prediction["unified_score"] = calculate_unified_score(prediction)
                        except Exception as _ue:
                            logger.error("analyze: error calculando unified_score — %s", _ue)

                        alerted = await check_and_alert(prediction)
                        if alerted:
                            alerts_sent += 1
                            # Registrar señal en shadow trading
                            try:
                                from shared.shadow_engine import track_new_signal
                                await track_new_signal(prediction, "polymarket")
                            except Exception as _se:
                                logger.error("analyze: error en track_new_signal — %s", _se)
                        else:
                            logger.debug(
                                "analyze: %s — edge=%.3f conf=%.2f → no alerta",
                                enriched.get("market_id"),
                                float(prediction.get("edge", 0)),
                                float(prediction.get("confidence", 0)),
                            )
                except Exception:
                    skipped_groq += 1
                    logger.error(
                        "analyze: error en mercado %s",
                        enriched.get("market_id"), exc_info=True,
                    )

            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            logger.info(
                "analyze: total=%d analizados=%d alertas=%d skip_vol=%d skip_err=%d en %.1fs",
                len(docs), predictions_generated, alerts_sent,
                skipped_volume, skipped_groq, elapsed,
            )

        # Limpieza de datos antiguos
        try:
            await run_maintenance()
        except Exception:
            logger.error("analyze: error en run_maintenance", exc_info=True)

    except Exception as e:
        logger.error("analyze: error no controlado — %s", e, exc_info=True)


async def _bg_poly_backtest() -> None:
    """Backtesting historico de mercados Polymarket resueltos — ejecutar UNA SOLA VEZ."""
    try:
        from datetime import datetime, timezone
        logger.info("poly-backtest: iniciando — analisis de 90 dias de mercados resueltos")
        start = datetime.now(timezone.utc)

        from backtester.backtest_poly import run_poly_backtest
        result = await run_poly_backtest(days_back=90)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info(
            "poly-backtest: completado en %.0fs — accuracy=%.1f%% mercados=%d avg_edge=%.3f",
            elapsed,
            result.get("accuracy", 0) * 100,
            result.get("markets_analyzed", 0),
            result.get("avg_edge_detected", 0),
        )

    except Exception as e:
        logger.error("poly-backtest: error no controlado — %s", e, exc_info=True)


async def _bg_websocket() -> None:
    """Loop infinito de WebSocket CLOB para monitoreo en tiempo real de top 20 mercados."""
    try:
        logger.info("websocket: iniciando monitoreo en tiempo real")
        from realtime.websocket_manager import start_monitoring
        await start_monitoring(top_n_markets=20)
    except Exception as e:
        logger.error("websocket: error no controlado — %s", e, exc_info=True)
