"""
API endpoint: GET /poly
Devuelve predicciones de las últimas 48h ordenadas por analyzed_at DESC,
luego re-ordenadas por abs(edge) DESC en memoria.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from shared.firestore_client import col

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = _serialize(v)
        else:
            out[k] = v
    return out


@router.get("/poly")
async def get_poly() -> list[dict]:
    """
    Predicciones de las últimas 48h, ordenadas por analyzed_at DESC.
    Re-ordenadas por abs(edge) DESC en memoria para destacar oportunidades.
    Campos: market_id, question, market_price_yes, real_prob, edge,
            confidence, trend, recommendation, volume_spike, analyzed_at.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        docs = (
            col("poly_predictions")
            .where(filter=FieldFilter("analyzed_at", ">=", cutoff))
            .order_by("analyzed_at", direction="DESCENDING")
            .limit(50)
            .stream()
        )
        fields = {
            "market_id", "question", "market_price_yes", "real_prob",
            "edge", "confidence", "trend", "recommendation",
            "volume_spike", "smart_money_detected", "key_factors",
            "reasoning", "analyzed_at", "alerted", "category",
            "end_date_iso", "volume_24h",
        }
        result = []
        for d in docs:
            raw = d.to_dict()
            filtered = {k: v for k, v in raw.items() if k in fields}
            result.append(_serialize(filtered))

        # Re-ordenar por abs(edge) DESC para destacar las mayores ineficiencias
        result.sort(key=lambda x: abs(float(x.get("edge", 0))), reverse=True)
        return result[:20]
    except Exception:
        logger.error("get_poly: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error consultando Polymarket")
