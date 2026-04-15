"""
API endpoint: GET /predictions
Devuelve las ultimas 20 predicciones deportivas ordenadas por created_at DESC.
"""
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/predictions")
async def get_predictions() -> list[dict]:
    """
    Lee Firestore coleccion predictions.
    Devuelve lista ultimas 20 ordenadas por created_at DESC.
    Campos: match_id, home_team, away_team, league, match_date,
            team_to_back, odds, edge, confidence, factors, result, correct.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError


@router.get("/stats")
async def get_stats() -> dict:
    """
    Lee model_weights doc 'current' + accuracy_log.
    Devuelve: accuracy_global, accuracy_by_league, weights,
              weights_history, total_predictions, correct_predictions.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError
