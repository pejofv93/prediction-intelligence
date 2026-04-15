"""
API endpoint: POST /calc
Calculadora matched betting (qualifying bet, free bet SNR, free bet SR).
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class CalcRequest(BaseModel):
    type: str           # "qualifying" | "free_bet_snr" | "free_bet_sr"
    back_stake: float
    back_odds: float
    lay_odds: float
    commission: float   # decimal: 5% → 0.05


def calc_qualifying(
    back_stake: float, back_odds: float, lay_odds: float, commission: float
) -> tuple:
    """lay_stake, liability, profit_back, profit_lay, rating"""
    # TODO: implementar en Sesion 7
    raise NotImplementedError


def calc_free_bet_snr(
    back_stake: float, back_odds: float, lay_odds: float, commission: float
) -> tuple:
    """lay_stake, liability, profit_back, profit_lay, rating"""
    # TODO: implementar en Sesion 7
    raise NotImplementedError


def calc_free_bet_sr(
    back_stake: float, back_odds: float, lay_odds: float, commission: float
) -> tuple:
    """lay_stake, liability, profit_back, profit_lay, rating"""
    # TODO: implementar en Sesion 7
    raise NotImplementedError


@router.post("/calc")
async def calculate(req: CalcRequest) -> dict:
    """
    Devuelve: lay_stake, liability, profit_back, profit_lay, rating, steps.
    """
    # TODO: implementar en Sesion 7
    raise NotImplementedError
