"""
polymarket-agent — FastAPI service
Endpoints: /run-scan /run-enrich /run-analyze /run-poly-backtest /run-websocket /run-learn /health
Todos los endpoints /run-* devuelven 202 Accepted inmediatamente.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable
from google.cloud.firestore_v1.base_query import FieldFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Monkey-patch: google-cloud-firestore 2.x accede a gapic_callable._retry en _UnaryStreamMultiCallable
# que no tiene ese atributo → AttributeError. Fix: devolver False (no reintentar) si falta el atributo.
# El método está en query.Query, no en base_query.BaseQuery.
try:
    from google.cloud.firestore_v1.query import Query as _FSQuery
    _orig_retry_fn = _FSQuery._retry_query_after_exception

    def _safe_retry_query_after_exception(self, exc, retry, transaction):
        try:
            return _orig_retry_fn(self, exc, retry, transaction)
        except AttributeError:
            return False

    _FSQuery._retry_query_after_exception = _safe_retry_query_after_exception
    logger.info("patch: _retry_query_after_exception en query.Query aplicado OK")
except Exception as _patch_err:
    logger.warning("patch: no aplicado — %s", _patch_err)

app = FastAPI(title="polymarket-agent")

# Flag para ejecutar retroactive_eval una sola vez por arranque
_retroactive_done = False

# Flag para etiquetar grupos de mercados una sola vez al arranque
_groups_labeled = False

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
    """Síncrono: ejecuta el enrich completo y devuelve el resultado.
    Cloud Run mantiene la instancia viva durante todo el proceso (timeout=1200s)."""
    result = await _bg_enrich()
    return JSONResponse(status_code=200, content=result)


@app.post("/run-analyze", dependencies=[Depends(verify_token)])
async def run_analyze() -> JSONResponse:
    """Síncrono: ejecuta el analyze completo y devuelve el resultado.
    Cloud Run mantiene la instancia viva durante todo el proceso (timeout=1200s)."""
    result = await _bg_analyze()
    return JSONResponse(status_code=200, content=result)


@app.get("/recent-predictions", dependencies=[Depends(verify_token)])
async def recent_predictions(limit: int = 20) -> JSONResponse:
    """Diagnóstico: devuelve las últimas N predicciones de poly_predictions ordenadas por analyzed_at."""
    from datetime import datetime, timezone
    from shared.firestore_client import col
    try:
        docs = list(
            col("poly_predictions")
            .order_by("analyzed_at", direction="DESCENDING")
            .limit(min(limit, 50))
            .stream(timeout=15.0)
        )
        result = []
        for d in docs:
            f = d.to_dict()
            at = f.get("analyzed_at")
            result.append({
                "market_id": d.id,
                "question": (f.get("question") or "")[:80],
                "category": f.get("category"),
                "price_yes": f.get("market_price_yes"),
                "real_prob": f.get("real_prob"),
                "edge": f.get("edge"),
                "confidence": f.get("confidence"),
                "recommendation": f.get("recommendation"),
                "alerted": f.get("alerted"),
                "analyzed_at": at.isoformat() if hasattr(at, "isoformat") else str(at),
            })
        return JSONResponse(content={"count": len(result), "predictions": result})
    except Exception as e:
        logger.error("recent-predictions: error — %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/sample-enriched", dependencies=[Depends(verify_token)])
async def sample_enriched(limit: int = 5) -> JSONResponse:
    """Diagnóstico: devuelve N enriched_markets recientes con su news_sentiment (headlines, score, trend)."""
    from shared.firestore_client import col
    try:
        docs = list(
            col("enriched_markets")
            .order_by("enriched_at", direction="DESCENDING")
            .limit(min(limit, 20))
            .stream(timeout=15.0)
        )
        result = []
        for d in docs:
            f = d.to_dict()
            ns = f.get("news_sentiment", {})
            at = f.get("enriched_at")
            result.append({
                "market_id": d.id,
                "question": (f.get("question") or "")[:80],
                "volume_24h": f.get("volume_24h"),
                "news_sentiment": {
                    "score": ns.get("score"),
                    "count": ns.get("count"),
                    "trend": ns.get("trend"),
                    "headlines": ns.get("headlines", []),
                },
                "enriched_at": at.isoformat() if hasattr(at, "isoformat") else str(at),
            })
        return JSONResponse(content={"count": len(result), "markets": result})
    except Exception as e:
        logger.error("sample-enriched: error — %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/test-gamma-price", dependencies=[Depends(verify_token)])
async def test_gamma_price() -> JSONResponse:
    """Diagnóstico: fetcha 3 mercados de Gamma API y muestra outcomePrices raw + price_yes parseado."""
    import httpx
    GAMMA_API = "https://gamma-api.polymarket.com"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": "3"},
            )
        raw_list = resp.json()
        if not isinstance(raw_list, list):
            raw_list = raw_list.get("markets", raw_list.get("data", []))
        result = []
        for raw in raw_list[:3]:
            from scanner import _parse_market
            parsed = _parse_market(raw)
            result.append({
                "market_id": raw.get("id"),
                "question": raw.get("question", "")[:80],
                "raw_outcomePrices": raw.get("outcomePrices"),
                "raw_lastTradePrice": raw.get("lastTradePrice"),
                "raw_bestBid": raw.get("bestBid"),
                "parsed_price_yes": parsed.get("price_yes") if parsed else None,
            })
        return JSONResponse(content={"markets": result})
    except Exception as e:
        logger.error("test-gamma-price: error — %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/admin/reset-quota/{api_name}", dependencies=[Depends(verify_token)])
async def admin_reset_quota(api_name: str) -> JSONResponse:
    """Resetea cuota mensual de una API en Firestore (used=0, remaining=limit).
    Ejemplo: POST /admin/reset-quota/oddspapi"""
    from shared.api_quota_manager import quota, _MONTHLY_LIMITS
    if api_name not in _MONTHLY_LIMITS:
        raise HTTPException(
            status_code=404,
            detail=f"API '{api_name}' no reconocida. Disponibles: {list(_MONTHLY_LIMITS.keys())}",
        )
    result = quota.reset_monthly_quota(api_name)
    logger.info("admin_reset_quota: %s reseteada por request manual", api_name)
    return JSONResponse(content={"status": "ok", **result})


@app.post("/run-resolve", dependencies=[Depends(verify_token)])
async def run_resolve() -> JSONResponse:
    """Síncrono: resuelve shadow_trades pendientes de Polymarket contra resultados reales."""
    result = await _bg_resolve()
    return JSONResponse(status_code=200, content=result)


@app.post("/run-learn", dependencies=[Depends(verify_token)])
async def run_learn() -> JSONResponse:
    """Síncrono: ejecuta poly_learning_engine — ajusta umbrales en poly_model_weights."""
    try:
        from datetime import datetime, timezone
        start = datetime.now(timezone.utc)
        from poly_learning_engine import run_poly_learning
        doc = run_poly_learning()
        # Invalida cache de umbrales en alert_engine para el próximo run-analyze
        import alert_engine as _ae
        _ae._LEARNED_THRESHOLDS = None
        elapsed = round((datetime.now(timezone.utc) - start).total_seconds(), 1)
        if not doc:
            return JSONResponse(status_code=200, content={"status": "skipped", "reason": "no resolved trades"})
        return JSONResponse(status_code=200, content={
            "status": "ok",
            "elapsed_s": elapsed,
            "version": doc.get("version"),
            "accuracy_overall": doc.get("accuracy_overall"),
            "accuracy_buy_yes": doc.get("accuracy_buy_yes"),
            "accuracy_buy_no":  doc.get("accuracy_buy_no"),
            "sample_size": doc.get("sample_size"),
            "min_edge": doc.get("min_edge"),
            "buy_yes_min_edge": doc.get("buy_yes_min_edge"),
            "buy_no_min_edge":  doc.get("buy_no_min_edge"),
        })
    except Exception as e:
        logger.error("run-learn: error no controlado — %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/run-poly-backtest", dependencies=[Depends(verify_token)])
async def run_poly_backtest() -> JSONResponse:
    """202 inmediato → background: backtester/backtest_poly.py. Ejecutar UNA SOLA VEZ."""
    asyncio.create_task(_bg_poly_backtest())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "poly-backtest"})


@app.post("/run-news-trigger", dependencies=[Depends(verify_token)])
async def run_news_trigger() -> JSONResponse:
    """202 inmediato → background: news_trigger.run_news_trigger() — top 10 mercados con DDG."""
    asyncio.create_task(_bg_news_trigger())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "news-trigger"})


@app.post("/analyze-urgent", dependencies=[Depends(verify_token)])
async def analyze_urgent(market_id: str) -> JSONResponse:
    """Re-analiza un mercado específico inmediatamente (sin esperar rotación de batch)."""
    if not market_id:
        raise HTTPException(status_code=400, detail="market_id requerido")
    asyncio.create_task(_bg_analyze_urgent(market_id))
    return JSONResponse(status_code=202, content={"status": "accepted", "market_id": market_id})


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

        from scanner import fetch_diverse_markets
        from price_tracker import save_price_snapshot

        markets = await fetch_diverse_markets(min_volume=500)
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


async def _bg_enrich() -> dict:
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

        try:
            col("poly_markets").document("_conn_warmup").get()
        except Exception:
            pass
        try:
            docs_raw = list(col("poly_markets").limit(200).stream(timeout=120.0))
        except Exception as e:
            logger.error("enrich: error leyendo poly_markets — %s: %s", type(e).__name__, e)
            return {"status": "error", "error": f"{type(e).__name__}: {e}", "enriched": 0}
        markets = [d.to_dict() for d in docs_raw]

        count = await run_enrichment(markets)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("enrich: %d mercados enriquecidos en %.1fs", count, elapsed)
        return {"status": "ok", "enriched": count, "elapsed_s": round(elapsed, 1)}

    except Exception as e:
        logger.error("enrich: error no controlado — %s", e, exc_info=True)
        return {"status": "error", "error": str(e), "enriched": 0}


async def _bg_analyze() -> dict:
    """
    Pipeline de analisis:
    1. Lee enriched_markets de Firestore
    2. analyze_market() con Groq → poly_predictions
    3. check_and_alert() para senales con edge suficiente
    4. run_maintenance() limpia datos antiguos
    """
    try:
        from datetime import datetime, timezone
        start = datetime.now(timezone.utc)
        logger.info("analyze: iniciando pipeline")

        from groq_analyzer import analyze_market, run_maintenance
        from alert_engine import check_and_alert
        from shared.firestore_client import col
        from shared.groq_client import GROQ_CALL_DELAY

        try:
            col("enriched_markets").document("_conn_warmup").get()
            logger.info("analyze: warmup Firestore OK")
        except Exception as e:
            logger.warning("analyze: warmup fallo — %s: %s", type(e).__name__, e)
        # Freshness guard: aborta si el último enrich fue hace más de 90 min.
        # enrich corre a :30 h-1 y analyze a :00 h → gap típico 30 min + delays GH Actions
        # 90 min cubre gap base (30min) + hasta 60min de delay acumulado entre los dos jobs.
        try:
            _latest_docs = (
                col("enriched_markets")
                .order_by("enriched_at", direction="DESCENDING")
                .limit(1)
                .get()
            )
            if not _latest_docs:
                logger.warning("analyze: enriched_markets vacía — ejecuta /run-enrich primero")
                return {"status": "skipped", "reason": "empty", "analyzed": 0, "alerts": 0}
            logger.info("analyze: probe enriched_markets → %d doc(s)", len(_latest_docs))
            _enriched_at = _latest_docs[0].to_dict().get("enriched_at")
            if _enriched_at:
                if hasattr(_enriched_at, "tzinfo") and _enriched_at.tzinfo is None:
                    _enriched_at = _enriched_at.replace(tzinfo=timezone.utc)
                _age_min = (datetime.now(timezone.utc) - _enriched_at).total_seconds() / 60
                if _age_min > 90:
                    logger.warning(
                        "analyze: enriched_markets desactualizado (%.0f min) — "
                        "abortando, espera a que /run-enrich complete",
                        _age_min,
                    )
                    return {"status": "skipped", "reason": "stale_data", "age_min": round(_age_min), "analyzed": 0, "alerts": 0}
                logger.info("analyze: enriched_markets fresco (%.0f min) — OK", _age_min)
        except Exception as e:
            logger.warning("analyze: freshness check fallo — %s: %s", type(e).__name__, e)
        try:
            raw_docs = list(
                col("enriched_markets")
                .order_by("enriched_at", direction="DESCENDING")
                .limit(100)
                .stream(timeout=120.0)
            )
        except Exception as e:
            logger.error("analyze: error leyendo enriched_markets — %s: %s", type(e).__name__, e)
            return {"status": "error", "error": f"{type(e).__name__}: {e}", "analyzed": 0, "alerts": 0}

        if not raw_docs:
            logger.warning("analyze: enriched_markets vacía — ejecuta /run-enrich primero")
            return {"status": "skipped", "reason": "empty", "analyzed": 0, "alerts": 0}

        # Balanceo por categoría: top 5 frescos por enriched_at de cada categoría activa
        from collections import Counter
        from datetime import timedelta
        from groq_analyzer import categorize_market

        # Excluir mercados analizados en <12h salvo que precio haya cambiado >3%
        _cutoff_12h = datetime.now(timezone.utc) - timedelta(hours=12)
        _recent_preds: dict[str, dict] = {}
        try:
            _pred_docs = list(
                col("poly_predictions")
                .where(filter=FieldFilter("analyzed_at", ">=", _cutoff_12h))
                .stream(timeout=20.0)
            )
            for _pd in _pred_docs:
                _pdata = _pd.to_dict()
                _recent_preds[_pd.id] = {"price": float(_pdata.get("market_price_yes", 0.5))}
            logger.info("analyze: %d mercados analizados en las últimas 12h", len(_recent_preds))
        except Exception as _rpe:
            logger.warning("analyze: error leyendo poly_predictions recientes — %s", _rpe)

        _now_utc = datetime.now(timezone.utc)
        _cutoff_end_date = _now_utc + timedelta(hours=24)

        # Cargar poly_markets una vez como fallback para docs enriched que aún no
        # tienen end_date/price_yes (enriquecidos antes del fix de market_enricher)
        _poly_cache: dict[str, dict] = {}
        try:
            for _pd in col("poly_markets").stream():
                _poly_cache[_pd.id] = _pd.to_dict()
            logger.info("analyze: poly_cache cargado (%d docs)", len(_poly_cache))
        except Exception as _pce:
            logger.warning("analyze: error cargando poly_cache — %s", _pce)

        _markets_by_cat: dict[str, list[dict]] = {}
        _skipped_rotation = 0
        _skipped_prefilter = 0
        for _raw in raw_docs:
            _m = _raw.to_dict()
            _mid = _m.get("market_id", "")
            _poly = _poly_cache.get(_mid, {})

            # Pre-filtro 1: end_date < now + 24h (expiran pronto o ya expirados)
            _end = _m.get("end_date") or _poly.get("end_date")
            if _end:
                if hasattr(_end, "tzinfo") and _end.tzinfo is None:
                    _end = _end.replace(tzinfo=timezone.utc)
                if _end < _cutoff_end_date:
                    _skipped_prefilter += 1
                    logger.debug("analyze: %s pre-filtrado — end_date=%s <24h", _mid, _end.date())
                    continue

            # Pre-filtro 2: price_yes prácticamente resuelto
            _py = float(_m.get("price_yes") or _poly.get("price_yes") or 0.5)
            if _py < 0.05 or _py > 0.95:
                _skipped_prefilter += 1
                logger.debug("analyze: %s pre-filtrado — price_yes=%.3f extremo", _mid, _py)
                continue

            # Filtro rotación: analizado <12h sin Δprecio >3%
            if _mid in _recent_preds:
                _last_price = _recent_preds[_mid]["price"]
                _price_chg = abs(_py - _last_price) / max(_last_price, 0.001)
                if _price_chg <= 0.03:
                    _skipped_rotation += 1
                    logger.debug(
                        "analyze: %s omitido — analizado <12h, precio sin cambio (%.1f%%)",
                        _mid, _price_chg * 100,
                    )
                    continue

            _cat = categorize_market(_m.get("question", ""))
            _markets_by_cat.setdefault(_cat, []).append(_m)

        logger.info(
            "analyze: pre-filtro descartó %d (expirados/resueltos) | rotación %d",
            _skipped_prefilter, _skipped_rotation,
        )
        if _skipped_rotation:
            logger.info(
                "analyze: %d mercados excluidos por rotación (analiz. <12h sin Δprecio >3%%)",
                _skipped_rotation,
            )

        _dt_min = datetime.min.replace(tzinfo=timezone.utc)
        docs_balanced: list[dict] = []
        for _cat_markets in _markets_by_cat.values():
            # Ordenar por enriched_at DESC — priorizar datos frescos
            _top = sorted(
                _cat_markets,
                key=lambda x: x.get("enriched_at") or _dt_min,
                reverse=True,
            )[:5]
            docs_balanced.extend(_top)
        docs_balanced.sort(
            key=lambda x: x.get("enriched_at") or _dt_min,
            reverse=True,
        )

        cat_counter = Counter(categorize_market(m.get("question", "")) for m in docs_balanced)
        logger.info("analyze: categorías a analizar: %s", dict(cat_counter))
        logger.info("analyze: mercados a analizar: %d", len(docs_balanced))

        predictions_generated = 0
        alerts_sent = 0
        skipped_volume = 0
        skipped_groq = 0

        for i, enriched in enumerate(docs_balanced):
            if i > 0:
                await asyncio.sleep(GROQ_CALL_DELAY)

            try:
                prediction = await analyze_market(enriched)
                if prediction is None:
                    skipped_volume += 1
                else:
                    predictions_generated += 1

                    # Whale detection (heurística de volumen)
                    try:
                        from price_tracker import detect_whale_activity, apply_whale_to_signal
                        whale_data = await detect_whale_activity(enriched.get("market_id", ""))
                        if whale_data.get("whale_detected"):
                            prediction = apply_whale_to_signal(prediction, whale_data)
                            logger.info(
                                "analyze: ballena en %s — manipulation=%s",
                                enriched.get("market_id"),
                                whale_data.get("possible_manipulation"),
                            )
                    except Exception as _we:
                        logger.debug("analyze: error en whale detection — %s", _we)

                    # Smart wallet tracker (CLOB-based, win_rate > 65%)
                    try:
                        from wallet_tracker import get_top_traders, check_wallet_activity
                        await get_top_traders(enriched.get("market_id", ""))
                        _wt = await check_wallet_activity(
                            enriched.get("market_id", ""),
                            prediction.get("recommendation", "PASS"),
                        )
                        if _wt.get("whale_signal"):
                            _new_conf = round(
                                max(0.50, min(0.95, float(prediction.get("confidence", 0.65)) + _wt["confidence_adj"])),
                                4,
                            )
                            prediction["confidence"] = _new_conf
                            prediction["whale_info"] = _wt["message"]
                            logger.info(
                                "analyze: wallet_tracker %s — conf→%.2f (%s)",
                                enriched.get("market_id"), _new_conf, _wt["message"],
                            )
                    except Exception as _wte:
                        logger.debug("analyze: wallet_tracker error — %s", _wte)

                    # CLV temporal factor en unified_score
                    try:
                        from shared.unified_score import calculate_unified_score
                        from datetime import datetime, timezone
                        market_doc = col("poly_markets").document(
                            prediction.get("market_id", "")
                        ).get()
                        days_to_close = None
                        if market_doc.exists:
                            end_date = market_doc.to_dict().get("end_date")
                            if end_date:
                                if hasattr(end_date, "tzinfo") and end_date.tzinfo is None:
                                    end_date = end_date.replace(tzinfo=timezone.utc)
                                days_to_close = max(
                                    0,
                                    (end_date - datetime.now(timezone.utc)).days,
                                )
                        prediction["unified_score"] = calculate_unified_score(
                            prediction, days_to_close=days_to_close
                        )
                    except Exception as _se:
                        logger.debug(
                            "analyze: error recalculando unified_score con time factor — %s",
                            _se,
                        )

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
            len(docs_balanced), predictions_generated, alerts_sent,
            skipped_volume, skipped_groq, elapsed,
        )

        # Asignar grupos tematicos a mercados (una sola vez al deploy)
        global _groups_labeled
        if not _groups_labeled:
            try:
                from correlation_engine import save_market_group_labels
                await save_market_group_labels()
                _groups_labeled = True
            except Exception:
                logger.debug("analyze: error en save_market_group_labels — se reintentara")

        # Limpieza de datos antiguos
        try:
            await run_maintenance()
        except Exception:
            logger.error("analyze: error en run_maintenance", exc_info=True)

        return {
            "status": "ok",
            "total": len(docs_balanced),
            "analyzed": predictions_generated,
            "alerts": alerts_sent,
            "skip_vol": skipped_volume,
            "skip_err": skipped_groq,
            "elapsed_s": round(elapsed, 1),
        }

    except Exception as e:
        logger.error("analyze: error no controlado — %s", e, exc_info=True)
        return {"status": "error", "error": str(e), "analyzed": 0, "alerts": 0}


async def _bg_resolve() -> dict:
    """Resuelve shadow_trades pendientes de Polymarket contra resultados reales de Gamma API."""
    try:
        from datetime import datetime, timezone
        start = datetime.now(timezone.utc)
        logger.info("resolve: iniciando pipeline")

        from polymarket_resolver import resolve_closed_markets
        result = await resolve_closed_markets()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info(
            "resolve: completado en %.1fs — resolved=%d skipped_no_pred=%d skipped_unresolved=%d errors=%d",
            elapsed,
            result.get("resolved", 0),
            result.get("skipped_no_pred", 0),
            result.get("skipped_unresolved", 0),
            result.get("errors", 0),
        )
        result["elapsed_s"] = round(elapsed, 1)
        return result

    except Exception as e:
        logger.error("resolve: error no controlado — %s", e, exc_info=True)
        return {"status": "error", "error": str(e), "resolved": 0}


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


async def _bg_news_trigger() -> None:
    """Busca breaking news en top 10 mercados activos y fuerza re-análisis si hay impacto."""
    try:
        from news_trigger import run_news_trigger
        result = await run_news_trigger()
        logger.info(
            "news-trigger: checked=%d triggered=%d errors=%d",
            result.get("checked", 0), result.get("triggered", 0), result.get("errors", 0),
        )
    except Exception as e:
        logger.error("news-trigger: error no controlado — %s", e, exc_info=True)


async def _bg_analyze_urgent(market_id: str) -> None:
    """Re-analiza un mercado específico de forma urgente (fuera del ciclo de batch)."""
    try:
        from datetime import datetime, timezone
        from shared.firestore_client import col
        from groq_analyzer import analyze_market
        from alert_engine import check_and_alert
        from shared.groq_client import GROQ_CALL_DELAY

        logger.info("analyze-urgent: iniciando para market_id=%s", market_id)
        enriched_doc = col("enriched_markets").document(market_id).get()
        if not enriched_doc.exists:
            logger.warning("analyze-urgent(%s): no encontrado en enriched_markets", market_id)
            return

        enriched = enriched_doc.to_dict()
        prediction = await analyze_market(enriched)
        if prediction is None:
            logger.info("analyze-urgent(%s): sin señal generada", market_id)
            return

        alerted = await check_and_alert(prediction)
        if alerted:
            try:
                from shared.shadow_engine import track_new_signal
                await track_new_signal(prediction, "polymarket")
            except Exception:
                pass
        logger.info("analyze-urgent(%s): edge=%.3f rec=%s alerted=%s", market_id, prediction.get("edge", 0), prediction.get("recommendation"), alerted)

    except Exception as e:
        logger.error("analyze-urgent(%s): error no controlado — %s", market_id, e, exc_info=True)
