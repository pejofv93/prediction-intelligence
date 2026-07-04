"""
matched/engine.py — matemática back/lay y clasificación surebet/coverage.

Las fórmulas replican services/dashboard/api/calculator.py (qualifying / free_bet_snr),
la calculadora ya validada del dashboard. Se re-implementan aquí para que sports-agent
no dependa del paquete del dashboard (servicio distinto).

Regla de un motor, dos umbrales:
  qualifying_rating (%) = beneficio garantizado / back_stake × 100
    >= MATCHED_SUREBET_MIN_RATING          → "surebet"  (ganas apostando normal + lay)
    en [COVERAGE_MIN, SUREBET_MIN)          → "coverage" (pierdes poco → ideal para cubrir un bono)
    < COVERAGE_MIN                          → se descarta (pérdida qualifying excesiva)
"""
import hashlib
from datetime import datetime, timezone

from shared.config import (
    MATCHED_LAY_COMMISSION,
    MATCHED_SUREBET_MIN_RATING,
    MATCHED_COVERAGE_MIN_RATING,
)

from .models import BackLayQuote, MatchedSignal


def _qualifying(back_stake: float, back_odds: float, lay_odds: float, commission: float):
    """
    Apuesta normal (qualifying) cubierta con lay. Espejo de calc_qualifying del dashboard.
    Devuelve (lay_stake, liability, profit_back, profit_lay, rating).
    profit_back ≈ profit_lay por construcción del lay_stake → el resultado es garantizado.
    """
    lay_stake = (back_stake * back_odds) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission) - back_stake
    rating = ((profit_back + profit_lay) / 2 / back_stake) * 100
    return lay_stake, liability, profit_back, profit_lay, rating


def _freebet_snr_rating(back_stake: float, back_odds: float, lay_odds: float, commission: float) -> float:
    """
    % de una free bet SIN retorno de stake (SNR) que se convierte en beneficio garantizado
    si el back de esta selección fuera una free bet. Metadato útil para valorar bonos.
    Espejo de calc_free_bet_snr del dashboard.
    """
    lay_stake = (back_stake * (back_odds - 1)) / (lay_odds - commission)
    profit_lay = lay_stake * (1 - commission)
    return (profit_lay / back_stake) * 100


def _signal_id(sport_key: str, event_id: str, selection: str) -> str:
    raw = f"{sport_key}:{event_id}:{selection}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def classify(
    quote: BackLayQuote,
    *,
    sport_key: str,
    event_id: str,
    home_team: str,
    away_team: str,
    commence_time: str,
    commission: float = MATCHED_LAY_COMMISSION,
) -> MatchedSignal | None:
    """
    Aplica el cálculo back/lay a una selección y devuelve una MatchedSignal si la señal
    supera el umbral (surebet o coverage), o None si la pérdida qualifying es excesiva.
    """
    back_odds = quote.back_odds
    lay_odds = quote.lay_odds
    if back_odds <= 1.0 or lay_odds <= 1.0 or lay_odds <= commission:
        return None

    lay_stake, liability, profit_back, profit_lay, rating = _qualifying(
        100.0, back_odds, lay_odds, commission
    )
    profit = min(profit_back, profit_lay)   # conservador: el peor de los dos lados

    if rating >= MATCHED_SUREBET_MIN_RATING:
        signal_type = "surebet"
    elif rating >= MATCHED_COVERAGE_MIN_RATING:
        signal_type = "coverage"
    else:
        return None

    now = datetime.now(timezone.utc)
    return MatchedSignal(
        signal_id=_signal_id(sport_key, event_id, quote.selection),
        signal_type=signal_type,
        sport_key=sport_key,
        event_id=event_id,
        commence_time=commence_time,
        home_team=home_team,
        away_team=away_team,
        selection=quote.selection,
        back_bookmaker=quote.back_bookmaker,
        back_odds=round(back_odds, 3),
        lay_bookmaker=quote.lay_bookmaker,
        lay_odds=round(lay_odds, 3),
        commission=commission,
        qualifying_rating=round(rating, 3),
        freebet_snr_rating=round(_freebet_snr_rating(100.0, back_odds, lay_odds, commission), 3),
        lay_stake_per_100=round(lay_stake, 2),
        liability_per_100=round(liability, 2),
        profit_per_100=round(profit, 2),
        detected_at=now.isoformat(),
        expires_at=commence_time or now.isoformat(),
    )
