"""
API endpoint: GET /poly
Devuelve top 20 poly_predictions ordenados por edge DESC.
"""
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/poly")
async def get_poly() -> list[dict]:
    """
    Lee Firestore coleccion poly_predictions.
    Devuelve top 20 ordenados por edge DESC.
    Campos: market_id, question, market_price_yes, real_prob, edge,
            confidence, trend, recommendation, volume_spike, analyzed_at.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError
