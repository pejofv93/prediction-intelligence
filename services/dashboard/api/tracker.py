"""
API endpoints: POST /save-bet, GET /bets, PUT /bets/{id}
Tracker personal de apuestas matched betting.
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class BetRequest(BaseModel):
    bet_type: str       # "qualifying" | "free_bet_snr" | "free_bet_sr"
    event: str
    back_stake: float
    back_odds: float
    lay_odds: float
    commission: float
    lay_stake: float
    profit_back: float
    profit_lay: float
    rating: float
    status: str = "pendiente"  # "pendiente" | "ganado_back" | "ganado_lay" | "cancelado"


class BetStatusUpdate(BaseModel):
    status: str  # "ganado_back" | "ganado_lay" | "cancelado"


@router.post("/save-bet")
async def save_bet(req: BetRequest) -> dict:
    """
    Guarda apuesta en Firestore coleccion bets.
    Devuelve: {"id": "firestore_doc_id", "status": "saved"}.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError


@router.get("/bets")
async def get_bets() -> list[dict]:
    """
    Lista apuestas ordenadas por created_at DESC.
    Incluye pnl calculado segun status.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError


@router.put("/bets/{bet_id}")
async def update_bet(bet_id: str, update: BetStatusUpdate) -> dict:
    """
    Actualiza status de la apuesta y calcula pnl segun status.
    Devuelve documento actualizado.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError
