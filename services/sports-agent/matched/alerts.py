"""
matched/alerts.py — decisión de alerta y envío a Telegram (canal General).

Reglas de alerta (Fase 2):
  - surebets con rating >= MATCHED_ALERT_SUREBET_MIN_RATING
  - coberturas con peaje mejor que MATCHED_ALERT_COVERAGE_MIN_RATING
  - solo confianza fiable (MATCHED_ALERT_CONFIDENCE)
  - solo si back y lay son de casas con licencia en España o del exchange de betfair.es
    (28-nov); las de "solo referencia" se guardan sin enviarse (corte "reference_only")
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

from shared.es_bookmakers import es_counterpart, reference_only_reason

from .models import MatchedSignal

logger = logging.getLogger(__name__)

_ALLOWED_CONFIDENCE = {c.strip() for c in MATCHED_ALERT_CONFIDENCE.split(",") if c.strip()}


def alert_cut_reason(sig: MatchedSignal) -> str | None:
    """None si la señal se alerta; si no, el primer filtro que la corta (para el registro
    por escaneo en matched_scan_runs). El orden de los filtros es el de siempre y, al final,
    "reference_only" (decisión del usuario, 28-nov): si la casa del back o el exchange del lay
    no tienen licencia en España (ni es el exchange de betfair.es), la señal se guarda en
    Firestore pero NO se envía a Telegram."""
    reason = _threshold_cut_reason(sig)
    if reason is None and reference_only_reason(sig.back_bookmaker, sig.lay_bookmaker):
        return "reference_only"
    return reason


def _threshold_cut_reason(sig: MatchedSignal) -> str | None:
    """Umbral, confianza, cuota y proxies de liquidez (los filtros de siempre)."""
    if sig.confidence not in _ALLOWED_CONFIDENCE:
        return "confidence"
    # Longshots: cuota alta → liquidez de exchange ínfima → surebet no ejecutable.
    if max(sig.back_odds, sig.lay_odds) > MATCHED_ALERT_MAX_ODDS:
        return "max_odds"
    # Fase 3 — proxy de liquidez: lay muy por encima del back = mercado fino o cuota stale.
    if sig.back_odds > 0 and sig.lay_odds > sig.back_odds * MATCHED_MAX_BACK_LAY_RATIO:
        return "lay_back_ratio"
    if sig.signal_type == "surebet":
        # Surebet solo con lay recién actualizado: mercado activo = hay dinero real.
        if sig.confidence != "high":
            return "surebet_not_high"
        return None if sig.qualifying_rating >= MATCHED_ALERT_SUREBET_MIN_RATING else "surebet_rating"
    if sig.signal_type == "coverage":
        return None if sig.qualifying_rating >= MATCHED_ALERT_COVERAGE_MIN_RATING else "coverage_rating"
    return "unknown_type"


def should_alert(sig: MatchedSignal) -> bool:
    """True si la señal cumple umbral, confianza, cuota y proxies de liquidez para alertar."""
    return alert_cut_reason(sig) is None


def _format_reference_only(sig: MatchedSignal, reason: str) -> str:
    """Alerta de una señal con casa o exchange sin licencia en España: se enseña como
    referencia de mercado, SIN stakes ni instrucciones de apuesta (decisión del usuario,
    19-nov: solo casas .es y el exchange de betfair.es)."""
    kind = (f"surebet {sig.qualifying_rating:+.2f}%" if sig.signal_type == "surebet"
            else f"cobertura, peaje {sig.qualifying_rating:.2f}%")
    kickoff = (sig.commence_time or "")[:16].replace("T", " ")
    lines = [
        f"ℹ️ SOLO REFERENCIA ({kind}) — no apostable desde España",
        sig.sport_key,
        f"{sig.home_team} vs {sig.away_team}",
        f"Selección: {sig.selection}",
        "",
        f"Cuotas vistas: back {sig.back_bookmaker} @{sig.back_odds:.2f} · "
        f"lay {sig.lay_bookmaker} @{sig.lay_odds:.2f}",
        f"Saque: {kickoff}",
        f"⚠️ {reason}.",
    ]
    counterpart = es_counterpart(sig.back_bookmaker)
    if counterpart is not None:
        lines.append(f"Existe {counterpart.name} ({counterpart.domain}): su cuota puede ser "
                     "distinta, compruébala en la calculadora ES.")
    lines.append("Sin instrucciones de apuesta: no son cuotas de casas con licencia en España "
                 "ni del exchange de betfair.es.")
    return "\n".join(lines)


def format_alert(sig: MatchedSignal, back_stake: float = MATCHED_ALERT_BASE_STAKE) -> str:
    """Mensaje de texto plano para el canal General. Si la casa del back o el exchange del
    lay no tienen licencia en España, solo referencia (sin stakes ni instrucciones)."""
    reason = reference_only_reason(sig.back_bookmaker, sig.lay_bookmaker)
    if reason:
        return _format_reference_only(sig, reason)
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
