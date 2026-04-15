"""
API endpoints: POST /save-bet, GET /bets, PUT /bets/{id}
Tracker personal de apuestas matched betting.
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException


class BetRequest(BaseModel):
    bet_type: str       # "qualifying" | "free_bet_snr" | "free_bet_sr"
    event: str
    back_stake: float
    back_odds: float
    lay_odds: float
    commission: float
    lay_stake: float
    liability: float = 0.0
    profit_back: float
    profit_lay: float
    rating: float
    status: str = "pendiente"


class BetStatusUpdate(BaseModel):
    status: str  # "ganado_back" | "ganado_lay" | "cancelado"


def _calc_pnl(doc: dict) -> float:
    """Calcula P&L segun el status de la apuesta."""
    status = doc.get("status", "pendiente")
    if status == "ganado_back":
        return float(doc.get("profit_back", 0))
    elif status == "ganado_lay":
        return float(doc.get("profit_lay", 0))
    elif status == "cancelado":
        return 0.0
    return 0.0  # pendiente


def _serialize(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


@router.post("/save-bet")
async def save_bet(req: BetRequest) -> dict:
    """Guarda apuesta en Firestore coleccion bets."""
    from shared.firestore_client import col

    now = datetime.now(timezone.utc)
    doc = {
        "bet_type": req.bet_type,
        "event": req.event,
        "back_stake": req.back_stake,
        "back_odds": req.back_odds,
        "lay_odds": req.lay_odds,
        "commission": req.commission,
        "lay_stake": req.lay_stake,
        "liability": req.liability,
        "profit_back": req.profit_back,
        "profit_lay": req.profit_lay,
        "rating": req.rating,
        "status": req.status,
        "pnl": _calc_pnl(req.dict()),
        "created_at": now,
        "updated_at": now,
    }
    try:
        ref = col("bets").add(doc)
        doc_id = ref[1].id if isinstance(ref, tuple) else ref.id
        return {"id": doc_id, "status": "saved"}
    except Exception:
        logger.error("save_bet: error guardando en Firestore", exc_info=True)
        raise HTTPException(status_code=500, detail="Error guardando apuesta")


@router.get("/bets")
async def get_bets() -> list[dict]:
    """Lista apuestas ordenadas por created_at DESC con pnl calculado."""
    from shared.firestore_client import col

    try:
        docs = (
            col("bets")
            .order_by("created_at", direction="DESCENDING")
            .limit(100)
            .stream()
        )
        result = []
        for d in docs:
            raw = d.to_dict()
            raw["id"] = d.id
            raw["pnl"] = _calc_pnl(raw)
            result.append(_serialize(raw))
        return result
    except Exception:
        logger.error("get_bets: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error consultando apuestas")


@router.put("/bets/{bet_id}")
async def update_bet(bet_id: str, update: BetStatusUpdate) -> dict:
    """Actualiza status y calcula pnl. Devuelve documento actualizado."""
    from shared.firestore_client import col

    valid_statuses = {"pendiente", "ganado_back", "ganado_lay", "cancelado"}
    if update.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Status debe ser uno de: {valid_statuses}")

    try:
        ref = col("bets").document(bet_id)
        doc = ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Apuesta no encontrada")

        data = doc.to_dict()
        data["status"] = update.status
        data["pnl"] = _calc_pnl(data)
        data["updated_at"] = datetime.now(timezone.utc)

        ref.update({"status": update.status, "pnl": data["pnl"], "updated_at": data["updated_at"]})

        data["id"] = bet_id
        return _serialize(data)
    except HTTPException:
        raise
    except Exception:
        logger.error("update_bet(%s): error", bet_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Error actualizando apuesta")
