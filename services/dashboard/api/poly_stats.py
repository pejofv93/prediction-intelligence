"""
API endpoint: GET /poly-stats
Devuelve umbrales y métricas del modelo Polymarket desde poly_model_weights/current.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from shared.firestore_client import col

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/poly-stats")
async def get_poly_stats() -> dict:
    """
    Lee poly_model_weights/current.
    Devuelve umbrales por dirección, accuracy, sample_size, version, updated_at.
    """
    try:
        doc = col("poly_model_weights").document("current").get()
        if not doc.exists:
            return {
                "version": 0,
                "updated_at": None,
                "buy_yes_min_edge": 0.08,
                "buy_yes_min_confidence": 0.65,
                "buy_no_min_edge": 0.08,
                "buy_no_min_confidence": 0.65,
                "accuracy_overall": 0.0,
                "accuracy_buy_yes": 0.0,
                "accuracy_buy_no": 0.0,
                "sample_size": 0,
                "sample_buy_yes": 0,
                "sample_buy_no": 0,
            }

        data = doc.to_dict()
        updated_at = data.get("updated_at")
        if isinstance(updated_at, datetime):
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            updated_at = updated_at.isoformat()

        return {
            "version": int(data.get("version", 0)),
            "updated_at": updated_at,
            "buy_yes_min_edge": float(data.get("buy_yes_min_edge", 0.08)),
            "buy_yes_min_confidence": float(data.get("buy_yes_min_confidence", 0.65)),
            "buy_no_min_edge": float(data.get("buy_no_min_edge", 0.08)),
            "buy_no_min_confidence": float(data.get("buy_no_min_confidence", 0.65)),
            "accuracy_overall": float(data.get("accuracy_overall", 0.0)),
            "accuracy_buy_yes": float(data.get("accuracy_buy_yes", 0.0)),
            "accuracy_buy_no": float(data.get("accuracy_buy_no", 0.0)),
            "sample_size": int(data.get("sample_size", 0)),
            "sample_buy_yes": int(data.get("sample_buy_yes", 0)),
            "sample_buy_no": int(data.get("sample_buy_no", 0)),
        }
    except Exception:
        logger.error("get_poly_stats: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error consultando poly_model_weights")
