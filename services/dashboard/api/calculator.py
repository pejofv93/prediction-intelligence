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
    """lay_stake, liability, profit_back, profit_lay, rating — fórmula spec exacta."""
    lay_stake = (back_stake * back_odds) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission) - back_stake
    rating = ((profit_back + profit_lay) / 2 / back_stake) * 100
    return lay_stake, liability, profit_back, profit_lay, rating


def calc_free_bet_snr(
    back_stake: float, back_odds: float, lay_odds: float, commission: float
) -> tuple:
    """Free bet sin retorno de stake (SNR)."""
    lay_stake = (back_stake * (back_odds - 1)) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission)
    rating = (profit_lay / back_stake) * 100
    return lay_stake, liability, profit_back, profit_lay, rating


def calc_free_bet_sr(
    back_stake: float, back_odds: float, lay_odds: float, commission: float
) -> tuple:
    """Free bet con retorno de stake (SR)."""
    lay_stake = (back_stake * back_odds) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * back_odds - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission)
    rating = (profit_lay / back_stake) * 100
    return lay_stake, liability, profit_back, profit_lay, rating


_CALC_FUNCS = {
    "qualifying": calc_qualifying,
    "free_bet_snr": calc_free_bet_snr,
    "free_bet_sr": calc_free_bet_sr,
}

_TYPE_LABELS = {
    "qualifying": "Qualifying Bet",
    "free_bet_snr": "Free Bet SNR",
    "free_bet_sr": "Free Bet SR",
}


@router.post("/calc")
async def calculate(req: CalcRequest) -> dict:
    """Devuelve: lay_stake, liability, profit_back, profit_lay, rating, steps."""
    from fastapi import HTTPException

    if req.type not in _CALC_FUNCS:
        raise HTTPException(status_code=400, detail=f"Tipo '{req.type}' no válido. Usa: qualifying, free_bet_snr, free_bet_sr")

    if req.lay_odds <= req.commission:
        raise HTTPException(status_code=400, detail="lay_odds debe ser mayor que commission")

    if req.back_stake <= 0 or req.back_odds <= 1 or req.lay_odds <= 1:
        raise HTTPException(status_code=400, detail="Valores inválidos: stake > 0, odds > 1")

    lay_stake, liability, profit_back, profit_lay, rating = _CALC_FUNCS[req.type](
        req.back_stake, req.back_odds, req.lay_odds, req.commission
    )

    label = _TYPE_LABELS[req.type]
    steps = [
        f"Hacer back de €{req.back_stake:.2f} a {req.back_odds:.2f} en la casa ({label})",
        f"Hacer lay de €{lay_stake:.2f} a {req.lay_odds:.2f} en el exchange",
        f"Responsabilidad en exchange: €{liability:.2f}",
    ]

    return {
        "type": req.type,
        "lay_stake": round(lay_stake, 2),
        "liability": round(liability, 2),
        "profit_back": round(profit_back, 2),
        "profit_lay": round(profit_lay, 2),
        "rating": round(rating, 2),
        "steps": steps,
    }
