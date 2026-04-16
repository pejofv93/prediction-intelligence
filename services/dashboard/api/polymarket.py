"""
API endpoint: GET /poly
Devuelve top 20 poly_predictions ordenados por edge DESC.
"""
import logging
from datetime import datetime, timezone

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
    Top 20 poly_predictions ordenados por edge DESC.
    Campos: market_id, question, market_price_yes, real_prob, edge,
            confidence, trend, recommendation, volume_spike, analyzed_at.
    """
    try:
        docs = (
            col("poly_predictions")
            .order_by("edge", direction="DESCENDING")
            .limit(20)
            .stream()
        )
        fields = {
            "market_id", "question", "market_price_yes", "real_prob",
            "edge", "confidence", "trend", "recommendation",
            "volume_spike", "smart_money_detected", "key_factors",
            "reasoning", "analyzed_at", "alerted",
        }
        result = []
        for d in docs:
            raw = d.to_dict()
            filtered = {k: v for k, v in raw.items() if k in fields}
            result.append(_serialize(filtered))
        return result
    except Exception:
        logger.error("get_poly: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error consultando Polymarket")
