"""
API endpoints: GET /es-bookmakers, POST /matched-calc
Casas con licencia en España (lista única shared/es_bookmakers.py) y calculadora de
matched betting entre casas ES + lay en betfair.es (shared/matched_calc.py).

La calculadora antigua (POST /calc, back/lay genérico) se mantiene sin cambios.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from shared.es_bookmakers import BETFAIR_ES_DEFAULT_COMMISSION, ES_BOOKMAKERS
from shared.matched_calc import BonusBet, CalcError, CoverLeg, calculate

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/es-bookmakers")
async def es_bookmakers() -> dict:
    """Lista única de casas con licencia en España + el exchange betfair.es."""
    return {
        "bookmakers": [b.to_dict() for b in ES_BOOKMAKERS],
        "default_commission": BETFAIR_ES_DEFAULT_COMMISSION,
        "note": "Solo casas con licencia DGOJ (.es) y el exchange de betfair.es. "
                "Comisión por defecto 5 % hasta confirmar la real de tu cuenta.",
    }


class LegIn(BaseModel):
    outcome: str
    kind: str = "back"              # back | exchange_back | lay
    odds: float
    bookmaker: str = ""
    commission: float | None = None


class MatchedCalcRequest(BaseModel):
    outcomes: list[str] = Field(default_factory=lambda: ["1", "X", "2"])
    bonus_kind: str                 # qualifying | freebet_snr | freebet_sr | refund
    bonus_outcome: str
    bonus_bookmaker: str
    stake: float
    odds: float
    refund_amount: float = 0.0
    refund_value: float = 0.0
    legs: list[LegIn]


@router.post("/matched-calc")
async def matched_calc(req: MatchedCalcRequest) -> dict:
    """Reparto de stakes con beneficio igual en todos los resultados. 400 si la entrada
    no es válida (casa sin licencia ES, cobertura en la casa del bono, patas que no cubren…)."""
    bonus = BonusBet(
        kind=req.bonus_kind, stake=req.stake, odds=req.odds,
        bookmaker=req.bonus_bookmaker, outcome=req.bonus_outcome,
        refund_amount=req.refund_amount, refund_value=req.refund_value,
    )
    legs = [CoverLeg(outcome=l.outcome, kind=l.kind, odds=l.odds,
                     bookmaker=l.bookmaker, commission=l.commission) for l in req.legs]
    try:
        return calculate(req.outcomes, bonus, legs)
    except CalcError as e:
        raise HTTPException(status_code=400, detail=str(e))
