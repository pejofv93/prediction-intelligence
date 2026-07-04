"""
API endpoint: GET /matched-signals
Lee las señales reales del detector back/lay (colección Firestore matched_signals,
poblada por services/sports-agent/matched/scanner.py).

Sustituye a la búsqueda de cuotas por LLM (/find-odds), que inventaba cuotas.
Aquí las cuotas son reales: back de casas eu + lay de betfair_ex_eu (The Odds API).
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/matched-signals")
async def matched_signals(signal_type: str = "", limit: int = 100) -> dict:
    """
    Devuelve las señales vigentes (no expiradas), surebets primero y por rating desc.
    signal_type: "surebet" | "coverage" | "" (todas).
    """
    from shared.firestore_client import col

    now_iso = datetime.now(timezone.utc).isoformat()
    signals: list[dict] = []
    try:
        for d in col("matched_signals").stream():
            doc = d.to_dict() or {}
            exp = doc.get("expires_at", "")
            if exp and exp < now_iso:
                continue
            if signal_type and doc.get("signal_type") != signal_type:
                continue
            signals.append(doc)
    except Exception as e:
        logger.error("matched_signals: error leyendo Firestore — %s", e)
        return {"signals": [], "count": 0, "surebets": 0, "coverage": 0,
                "error": "No se pudieron leer las señales", "fetched_at": now_iso}

    signals.sort(key=lambda s: (
        0 if s.get("signal_type") == "surebet" else 1,
        -float(s.get("qualifying_rating", 0) or 0),
    ))

    n_sure = sum(1 for s in signals if s.get("signal_type") == "surebet")
    return {
        "signals": signals[:limit],
        "count": len(signals),
        "surebets": n_sure,
        "coverage": len(signals) - n_sure,
        "warning": "El lay de Betfair no incluye liquidez/size — verifica que el importe "
                   "esté disponible antes de apostar. Ratings muy altos suelen ser lay fino.",
        "fetched_at": now_iso,
    }
