"""
API endpoints shadow trading:
  GET /shadow/metrics    — métricas de rendimiento
  GET /shadow/trades     — lista de trades paginada
  GET /shadow/bankroll   — bankroll actual e historial diario
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from shared.firestore_client import col
from shared.shadow_engine import calculate_metrics, _INITIAL_BANKROLL, _RETROACTIVE_DOC

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize_value(v):
    """Convierte datetime a ISO string recursivamente."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    elif isinstance(v, dict):
        return {k2: _serialize_value(v2) for k2, v2 in v.items()}
    elif isinstance(v, list):
        return [_serialize_value(i) for i in v]
    return v


def _serialize(doc: dict) -> dict:
    return {k: _serialize_value(v) for k, v in doc.items()}


@router.get("/shadow/metrics")
async def get_shadow_metrics() -> dict:
    """Todas las métricas de rendimiento del shadow trading."""
    try:
        metrics = calculate_metrics()
        return metrics
    except Exception:
        logger.error("shadow/metrics: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error calculando métricas shadow")


@router.get("/shadow/trades")
async def get_shadow_trades(
    limit: int = Query(default=50, ge=1, le=500),
    source: str = Query(default="all"),
) -> list[dict]:
    """Lista de trades paginada. source: 'all' | 'sports' | 'polymarket'"""
    try:
        query = col("shadow_trades").limit(limit)
        docs = query.stream()
        result = []
        for doc in docs:
            if doc.id == _RETROACTIVE_DOC:
                continue
            data = doc.to_dict()
            if source != "all" and data.get("source") != source:
                continue
            # Excluir signal_data para aligerar la respuesta
            data.pop("signal_data", None)
            result.append(_serialize(data))
        return result
    except Exception:
        logger.error("shadow/trades: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error consultando trades shadow")


@router.get("/shadow/bankroll")
async def get_shadow_bankroll() -> dict:
    """Bankroll actual e historial diario agregado."""
    try:
        docs = col("shadow_trades").limit(500).stream()
        trades = []
        for doc in docs:
            if doc.id == _RETROACTIVE_DOC:
                continue
            data = doc.to_dict()
            if data.get("pnl_virtual") is not None:
                trades.append(data)

        # Ordenar por closed_at
        def _closed_at(t):
            ca = t.get("closed_at")
            if ca is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if isinstance(ca, datetime) and ca.tzinfo is None:
                return ca.replace(tzinfo=timezone.utc)
            return ca

        trades_sorted = sorted(trades, key=_closed_at)

        # Construir historial diario
        daily: dict = {}
        bankroll = _INITIAL_BANKROLL
        for t in trades_sorted:
            pnl = float(t.get("pnl_virtual") or 0)
            bankroll = round(bankroll + pnl, 4)
            ca = _closed_at(t)
            day_key = ca.strftime("%Y-%m-%d") if ca != datetime.min.replace(tzinfo=timezone.utc) else "unknown"
            if day_key not in daily:
                daily[day_key] = {"date": day_key, "bankroll": bankroll, "pnl_daily": 0.0}
            daily[day_key]["pnl_daily"] = round(daily[day_key]["pnl_daily"] + pnl, 4)
            daily[day_key]["bankroll"] = bankroll

        history = list(daily.values())

        return {
            "current": bankroll,
            "initial": _INITIAL_BANKROLL,
            "history": history,
        }
    except Exception:
        logger.error("shadow/bankroll: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error calculando bankroll shadow")
