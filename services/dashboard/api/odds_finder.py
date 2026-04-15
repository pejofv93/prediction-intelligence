"""
API endpoints: POST /find-odds y POST /fetch-offers
Busca cuotas y bonos via Groq + Tavily.
LIMITACION: resultados orientativos, posiblemente desactualizados.
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class FindOddsRequest(BaseModel):
    event: str  # ej: "Real Madrid vs Barcelona"


@router.post("/find-odds")
async def find_odds(req: FindOddsRequest) -> dict:
    """
    Backend: groq_client.analyze() con web_search=True (Tavily).
    LIMITACION CONOCIDA: cuotas orientativas, posiblemente desactualizadas.
    Devuelve: event, odds, best_back, best_lay, warning, fetched_at.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError


@router.post("/fetch-offers")
async def fetch_offers() -> list[dict]:
    """
    Backend: groq_client.analyze() con web_search=True para buscar ofertas vigentes
    en casas espanolas.
    Devuelve: lista de {bookmaker, bonus, amount, type, requirement, rating, status, advice}.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError
