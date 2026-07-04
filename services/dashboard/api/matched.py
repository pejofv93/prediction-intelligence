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


# ── Buscador de bonos (real, mantenido a mano — sustituye al /fetch-offers LLM) ──
# Lista base de bonos de bienvenida de casas españolas. Se siembra en Firestore
# (matched_bonuses) en la primera lectura y a partir de ahí se edita ahí. NO son
# cuotas inventadas por IA: son plantillas verificables por el usuario. Los importes
# concretos cambian con las promos → marcar 'verify' y ajustar en Firestore.
_DEFAULT_BONUSES = [
    {"bookmaker": "Bet365", "title": "Créditos de apuesta de bienvenida",
     "type": "freebet_snr", "amount": 30.0, "min_odds": 1.50,
     "requirement": "Deposita y apuesta 10€ en cuota ≥1.50", "active": True, "verify": True},
    {"bookmaker": "Codere", "title": "Bono de bienvenida",
     "type": "freebet_snr", "amount": 25.0, "min_odds": 1.50,
     "requirement": "Primer depósito + apuesta cualificadora", "active": True, "verify": True},
    {"bookmaker": "Bwin", "title": "Freebet de bienvenida",
     "type": "freebet_snr", "amount": 20.0, "min_odds": 1.60,
     "requirement": "Apuesta 10€ para liberar la freebet", "active": True, "verify": True},
    {"bookmaker": "Sportium", "title": "Apuesta gratis bienvenida",
     "type": "freebet_snr", "amount": 20.0, "min_odds": 1.50,
     "requirement": "Registro + primera apuesta", "active": True, "verify": True},
]

# Tope de cuota para la jugada de una freebet: por encima la liquidez del exchange
# es ínfima aunque el SNR sea alto (mismo criterio que MATCHED_ALERT_MAX_ODDS).
_PLAY_MAX_ODDS = 7.0


def _seed_bonuses(coll) -> None:
    """Siembra los bonos por defecto si la colección está vacía."""
    try:
        if next(coll.limit(1).stream(), None) is None:
            for i, b in enumerate(_DEFAULT_BONUSES):
                coll.document(f"seed_{i}").set(b)
    except Exception as e:
        logger.warning("matched_bonuses: no se pudo sembrar — %s", e)


def _best_play(bonus: dict, signals: list[dict]) -> dict | None:
    """
    Elige la mejor jugada para un bono a partir de las señales vigentes fiables.
    freebet_*: maximiza freebet_snr_rating (respetando min_odds y tope de liquidez).
    qualifying: minimiza el peaje (mayor qualifying_rating) entre coberturas.
    El back se coloca en la casa DEL BONO; la señal aporta evento + lay Betfair de
    referencia (la cuota real en tu casa puede variar ligeramente).
    """
    min_odds = float(bonus.get("min_odds", 1.0) or 1.0)
    fiable = [s for s in signals
              if s.get("confidence") in ("high", "medium")
              and float(s.get("lay_odds", 99) or 99) <= _PLAY_MAX_ODDS]
    is_freebet = bonus.get("type", "").startswith("freebet")

    if is_freebet:
        cands = [s for s in fiable if float(s.get("back_odds", 0) or 0) >= min_odds]
        if not cands:
            return None
        best = max(cands, key=lambda s: float(s.get("freebet_snr_rating", 0) or 0))
        snr = float(best.get("freebet_snr_rating", 0) or 0)
        benefit = round(float(bonus.get("amount", 0) or 0) * snr / 100.0, 2)
        benefit_label = "Beneficio estimado de la freebet"
    else:
        cands = [s for s in fiable if s.get("signal_type") == "coverage"]
        if not cands:
            return None
        best = max(cands, key=lambda s: float(s.get("qualifying_rating", -99) or -99))
        rating = float(best.get("qualifying_rating", 0) or 0)
        benefit = round(float(bonus.get("amount", 0) or 0) * rating / 100.0, 2)
        benefit_label = "Peaje estimado del qualifying"

    return {
        "event": f"{best.get('home_team')} vs {best.get('away_team')}",
        "selection": best.get("selection"),
        "sport_key": best.get("sport_key"),
        "commence_time": best.get("commence_time"),
        "ref_back_bookmaker": best.get("back_bookmaker"),
        "ref_back_odds": best.get("back_odds"),
        "lay_bookmaker": best.get("lay_bookmaker"),
        "lay_odds": best.get("lay_odds"),
        "estimated_benefit": benefit,
        "benefit_label": benefit_label,
    }


@router.get("/matched-bonuses")
async def matched_bonuses() -> dict:
    """
    Bonos activos de casas españolas + la jugada recomendada para cada uno,
    calculada con las señales back/lay reales vigentes. Sin LLM.
    """
    from shared.firestore_client import col

    now_iso = datetime.now(timezone.utc).isoformat()
    coll = col("matched_bonuses")
    _seed_bonuses(coll)

    try:
        bonuses = [(d.to_dict() or {}) | {"id": d.id} for d in coll.stream()]
    except Exception as e:
        logger.error("matched_bonuses: error leyendo bonos — %s", e)
        bonuses = []

    # señales vigentes para calcular la jugada
    signals: list[dict] = []
    try:
        for d in col("matched_signals").stream():
            doc = d.to_dict() or {}
            if (doc.get("expires_at", "") or "") >= now_iso:
                signals.append(doc)
    except Exception:
        pass

    out = []
    for b in bonuses:
        if not b.get("active", True):
            continue
        b["play"] = _best_play(b, signals)
        out.append(b)
    out.sort(key=lambda b: -(b.get("play", {}) or {}).get("estimated_benefit", 0))

    return {
        "bonuses": out,
        "count": len(out),
        "note": "Bonos mantenidos manualmente (editables en Firestore matched_bonuses). "
                "La jugada usa una señal detectada como referencia — verifica la cuota real "
                "en tu casa y la liquidez del lay en Betfair antes de apostar.",
        "fetched_at": now_iso,
    }
