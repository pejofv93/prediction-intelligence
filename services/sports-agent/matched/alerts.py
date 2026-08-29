"""
matched/alerts.py — decisión de alerta y envío a Telegram (canal General).

Reglas de alerta (Fase 2):
  - surebets con rating >= MATCHED_ALERT_SUREBET_MIN_RATING
  - coberturas con peaje mejor que MATCHED_ALERT_COVERAGE_MIN_RATING
  - solo confianza fiable (MATCHED_ALERT_CONFIDENCE)
El dedup (no re-alertar la misma señal viva) lo gestiona el scanner con el flag
`alerted` en Firestore. Aquí solo se decide y se formatea/envía.

El envío va al canal General vía telegram-bot POST /send-alert type="matched"
(send_message con message_thread_id=None). Texto plano (sin Markdown) para evitar
errores de parseo con nombres tipo "Jović" o "Auger-Aliassime".
"""
import logging

import httpx

from shared.config import (
    CLOUD_RUN_TOKEN,
    TELEGRAM_BOT_URL,
    MATCHED_ALERT_SUREBET_MIN_RATING,
    MATCHED_ALERT_COVERAGE_MIN_RATING,
    MATCHED_ALERT_CONFIDENCE,
    MATCHED_ALERT_MAX_ODDS,
    MATCHED_ALERT_BASE_STAKE,
    MATCHED_MAX_BACK_LAY_RATIO,
)

from .models import MatchedSignal

logger = logging.getLogger(__name__)

_ALLOWED_CONFIDENCE = {c.strip() for c in MATCHED_ALERT_CONFIDENCE.split(",") if c.strip()}


def should_alert(sig: MatchedSignal) -> bool:
    """True si la señal cumple umbral, confianza, cuota y proxies de liquidez para alertar."""
    if sig.confidence not in _ALLOWED_CONFIDENCE:
        return False
    # Longshots: cuota alta → liquidez de exchange ínfima → surebet no ejecutable.
    if max(sig.back_odds, sig.lay_odds) > MATCHED_ALERT_MAX_ODDS:
        return False
    # Fase 3 — proxy de liquidez: lay muy por encima del back = mercado fino o cuota stale.
    if sig.back_odds > 0 and sig.lay_odds > sig.back_odds * MATCHED_MAX_BACK_LAY_RATIO:
        return False
    if sig.signal_type == "surebet":
        # Surebet solo con lay recién actualizado: mercado activo = hay dinero real.
        if sig.confidence != "high":
            return False
        return sig.qualifying_rating >= MATCHED_ALERT_SUREBET_MIN_RATING
    if sig.signal_type == "coverage":
        return sig.qualifying_rating >= MATCHED_ALERT_COVERAGE_MIN_RATING
    return False


def format_alert(sig: MatchedSignal, back_stake: float = MATCHED_ALERT_BASE_STAKE) -> str:
    """Mensaje de texto plano para el canal General."""
    scale = back_stake / 100.0
    lay_stake = sig.lay_stake_per_100 * scale
    liability = sig.liability_per_100 * scale
    profit = sig.profit_per_100 * scale
    conf_es = {"high": "Fiable", "medium": "Confianza media", "unknown": "Sin fecha lay"}.get(
        sig.confidence, sig.confidence)
    kickoff = (sig.commence_time or "")[:16].replace("T", " ")

    if sig.signal_type == "surebet":
        header = f"🟢 SUREBET +{sig.qualifying_rating:.2f}% · {conf_es}"
        bottom = f"Beneficio garantizado: +{profit:.2f}€ por {back_stake:.0f}€"
    else:
        header = f"🟠 COBERTURA (peaje {sig.qualifying_rating:.2f}%) · {conf_es}"
        bottom = (f"Peaje qualifying: {profit:.2f}€ por {back_stake:.0f}€ "
                  f"· EV free bet SNR {sig.freebet_snr_rating:.0f}%")

    return (
        f"{header}\n"
        f"{sig.sport_key}\n"
        f"{sig.home_team} vs {sig.away_team}\n"
        f"Apostar a: {sig.selection}\n\n"
        f"BACK {sig.back_bookmaker} @{sig.back_odds:.2f}  → {back_stake:.0f}€\n"
        f"LAY  Betfair @{sig.lay_odds:.2f}  → {lay_stake:.2f}€ (resp. {liability:.2f}€)\n\n"
        f"{bottom}\n"
        f"Saque: {kickoff}\n"
        f"⚠️ Verifica la liquidez del lay en Betfair antes de apostar."
    )


async def send_matched_alert(sig: MatchedSignal) -> bool:
    """Envía la alerta al canal General vía telegram-bot. Devuelve True si se envió."""
    if not TELEGRAM_BOT_URL:
        logger.warning("matched.alerts: TELEGRAM_BOT_URL no configurada — no se envía alerta")
        return False
    text = format_alert(sig)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                TELEGRAM_BOT_URL + "/send-alert",
                headers={"x-cloud-token": CLOUD_RUN_TOKEN},
                json={"type": "matched", "data": {"text": text}},
            )
        if resp.status_code == 200 and resp.json().get("sent"):
            return True
        logger.warning("matched.alerts: envío no confirmado (HTTP %d, body=%s)",
                       resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.error("matched.alerts: error enviando alerta a Telegram", exc_info=True)
        return False
