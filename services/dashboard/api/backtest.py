"""
Dashboard API — Backtest results router.
GET /backtest/results  — resultados ordenados por ROI
GET /backtest/thresholds — thresholds calibrados por liga/mercado
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from shared.firestore_client import col

logger = logging.getLogger(__name__)

router = APIRouter()


def _serialize(obj):
    """Serializa datetime a ISO string para JSON."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _serialize_doc(doc: dict) -> dict:
    return {k: _serialize(v) for k, v in doc.items()}


@router.get("/backtest/results")
async def get_backtest_results(
    league: str = Query(default="all", description="Filtrar por liga (o 'all')"),
    market: str = Query(default="all", description="Filtrar por mercado (o 'all')"),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """
    Lee col("backtest_results") filtrado por league y market si se especifican.
    Ordena por roi DESC.
    Incluye analisis automatico: high_roi_leagues, negative_roi_leagues, best_market.
    """
    try:
        query = col("backtest_results")

        # Aplicar filtros si no son "all"
        if league and league != "all":
            query = query.where("league", "==", league)
        if market and market != "all":
            query = query.where("market", "==", market)

        docs = list(query.stream())
        results = [_serialize_doc(d.to_dict()) for d in docs]

        # Ordenar por roi DESC (Firestore no permite order_by combinado con where sin indice)
        results.sort(key=lambda r: float(r.get("roi") or 0), reverse=True)
        results = results[:limit]

        # Analisis automatico
        high_roi_leagues = list({
            r["league"] for r in results if float(r.get("roi") or 0) > 0.05
        })
        negative_roi_leagues = list({
            r["league"] for r in results if float(r.get("roi") or 0) < -0.05
        })

        # Mejor mercado por ROI medio
        market_roi: dict[str, list[float]] = {}
        for r in results:
            m = r.get("market", "unknown")
            roi_val = float(r.get("roi") or 0)
            market_roi.setdefault(m, []).append(roi_val)

        best_market = None
        best_market_roi = None
        for m, rois in market_roi.items():
            avg = sum(rois) / len(rois) if rois else 0
            if best_market_roi is None or avg > best_market_roi:
                best_market = m
                best_market_roi = avg

        return JSONResponse({
            "results": results,
            "total": len(results),
            "high_roi_leagues": high_roi_leagues,
            "negative_roi_leagues": negative_roi_leagues,
            "best_market": best_market,
            "best_market_avg_roi": round(best_market_roi, 4) if best_market_roi is not None else None,
        })

    except Exception as e:
        logger.error("get_backtest_results: error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "Error consultando resultados de backtest"},
        )


@router.get("/backtest/thresholds")
async def get_backtest_thresholds() -> JSONResponse:
    """
    Lee col("model_weights").document("backtest_thresholds").
    Devuelve {league: {market: threshold}} con los thresholds calibrados.
    """
    try:
        doc = col("model_weights").document("backtest_thresholds").get()
        if not doc.exists:
            return JSONResponse({"thresholds": {}, "message": "Sin thresholds calibrados aun"})

        data = doc.to_dict() or {}
        return JSONResponse({"thresholds": data})

    except Exception as e:
        logger.error("get_backtest_thresholds: error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "Error consultando thresholds"},
        )
