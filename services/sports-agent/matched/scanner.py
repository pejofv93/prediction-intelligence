"""
matched/scanner.py — orquestador del escáner back/lay.

Flujo por ejecución:
  1. /v4/sports (gratis) → deportes activos; filtra por grupo Betfair-líquido y descarta
     mercados de futuros (_winner, election...). Ordena por prioridad y capa a N keys (coste).
  2. Por sport_key: fetch_event_quotes (2 créditos) → classify cada selección back/lay.
  3. Timing guard: descarta eventos ya empezados o demasiado próximos.
  4. Persiste señales en Firestore matched_signals (id determinista → idempotente) y limpia
     las expiradas. Devuelve un resumen.

Reemplaza collectors/arbitrage_detector.py (roto). Sin alertas Telegram (Fase 2).
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import (
    ODDS_API_KEY,
    MATCHED_MAX_KEYS_PER_SCAN,
    SIGNAL_MIN_MINUTES_BEFORE_KICKOFF,
)
from shared.firestore_client import col

from .odds_lay import fetch_event_quotes, budget_ok
from .engine import classify
from .alerts import should_alert, send_matched_alert

logger = logging.getLogger(__name__)

_SPORTS_URL = "https://api.the-odds-api.com/v4/sports"

# Grupos con buena liquidez de exchange (Betfair) y mercado h2h con kickoff real.
# Orden = prioridad de gasto de créditos cuando hay que capar.
_GROUP_PRIORITY = {
    "Tennis": 0, "Soccer": 1, "Basketball": 2, "Baseball": 3,
    "Ice Hockey": 4, "American Football": 5, "Rugby League": 6,
    "Aussie Rules": 7, "Boxing": 8, "Mixed Martial Arts": 9,
}
# Subcadenas de sport_key que indican mercado de futuros / outright (sin h2h por partido).
_EXCLUDE_SUBSTR = ("_winner", "championship_winner", "election")

# Fase 3 — whitelist de competiciones con profundidad real de lay en Betfair. The Odds API
# no da size del lay, así que es un proxy: solo emitimos donde el exchange tiene fondo
# (tour ATP/WTA, top-5 europeo + UEFA, NBA/Euroliga). Fuera quedan ligas menores y
# qualifiers, donde el lay es fino aunque el precio parezca bueno.
_LIQUID_SOCCER = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
}
_LIQUID_OTHER = {"basketball_nba", "basketball_euroleague"}


def _is_liquid_key(key: str) -> bool:
    """Proxy de liquidez: True si la competición tiene lay de Betfair con fondo real."""
    if key.startswith(("tennis_atp_", "tennis_wta_")):
        return True
    return key in _LIQUID_SOCCER or key in _LIQUID_OTHER

_COLL = "matched_signals"


async def _active_sport_keys() -> list[str]:
    """GET /v4/sports (no consume créditos) → keys activas Betfair-líquidas, priorizadas y capadas."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(_SPORTS_URL, params={"apiKey": ODDS_API_KEY})
        if resp.status_code != 200:
            logger.warning("matched.scanner: /v4/sports HTTP %d", resp.status_code)
            return []
        sports = resp.json() or []
    except Exception:
        logger.error("matched.scanner: error /v4/sports", exc_info=True)
        return []

    cand = []
    for s in sports:
        key = s.get("key", "")
        group = s.get("group", "")
        if not s.get("active"):
            continue
        if group not in _GROUP_PRIORITY:
            continue
        if any(sub in key for sub in _EXCLUDE_SUBSTR):
            continue
        if not _is_liquid_key(key):
            continue        # Fase 3 — fuera competiciones sin fondo de lay en Betfair
        cand.append((_GROUP_PRIORITY[group], key))

    cand.sort(key=lambda t: (t[0], t[1]))
    keys = [k for _, k in cand][:MATCHED_MAX_KEYS_PER_SCAN]
    logger.info("matched.scanner: %d keys activas seleccionadas (de %d deportes): %s",
                len(keys), len(sports), keys)
    return keys


def _too_close(commence_time: str, now: datetime) -> bool:
    """True si el partido ya empezó o arranca dentro del buffer (no apostable)."""
    if not commence_time:
        return False
    try:
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= now + timedelta(minutes=SIGNAL_MIN_MINUTES_BEFORE_KICKOFF)


def _persist(signals: list, now: datetime) -> tuple[int, list]:
    """
    Escribe señales (idempotente por signal_id) preservando el flag `alerted` de
    ejecuciones previas (dedup), limpia expiradas y devuelve (nº escritas, a_alertar).
    `a_alertar` = señales que cumplen umbral y NO habían sido alertadas todavía.
    """
    now_iso = now.isoformat()

    # Lectura única de la colección: sirve para dedup (alerted previo) y limpieza.
    existing: dict[str, dict] = {}
    try:
        existing = {d.id: (d.to_dict() or {}) for d in col(_COLL).stream()}
    except Exception:
        logger.warning("matched.scanner: error leyendo señales existentes", exc_info=True)

    written = 0
    to_alert: list = []
    for sig in signals:
        prev = existing.get(sig.signal_id)
        prev_alerted = bool(prev and prev.get("alerted"))
        doc = sig.to_doc()
        doc["alerted"] = prev_alerted            # preservar dedup entre escaneos
        doc["alerted_at"] = (prev or {}).get("alerted_at", "") if prev_alerted else ""
        try:
            col(_COLL).document(sig.signal_id).set(doc)
            written += 1
            if not prev_alerted and should_alert(sig):
                to_alert.append(sig)
        except Exception:
            logger.warning("matched.scanner: error persistiendo %s", sig.signal_id, exc_info=True)

    # Limpieza acotada de expiradas (usa la lectura ya hecha, sin índice)
    try:
        stale = [doc_id for doc_id, d in existing.items()
                 if (d.get("expires_at", "") or "") < now_iso]
        for doc_id in stale[:300]:
            col(_COLL).document(doc_id).delete()
        if stale:
            logger.info("matched.scanner: %d señales expiradas eliminadas", len(stale[:300]))
    except Exception:
        logger.warning("matched.scanner: error limpiando expiradas", exc_info=True)

    return written, to_alert


async def _dispatch_alerts(to_alert: list, now: datetime) -> int:
    """Envía alertas a Telegram y marca alerted=True en Firestore. Devuelve nº enviadas."""
    sent = 0
    for sig in to_alert:
        if await send_matched_alert(sig):
            try:
                col(_COLL).document(sig.signal_id).set(
                    {"alerted": True, "alerted_at": now.isoformat()}, merge=True)
            except Exception:
                logger.warning("matched.scanner: alerta enviada pero no se marcó %s",
                               sig.signal_id, exc_info=True)
            sent += 1
    return sent


async def run_matched_scan() -> dict:
    """Punto de entrada del escáner. Devuelve resumen para el endpoint/logs."""
    now = datetime.now(timezone.utc)

    ok, reason = budget_ok()
    if not ok:
        logger.warning("matched.scanner: abortado — %s", reason)
        return {"status": "skipped", "reason": reason, "signals": 0}

    keys = await _active_sport_keys()
    if not keys:
        return {"status": "no_sports", "signals": 0}

    all_signals = []
    scanned_keys = 0
    skipped_timing = 0

    for key in keys:
        ok, reason = budget_ok()
        if not ok:
            logger.info("matched.scanner: corte de presupuesto tras %d keys — %s", scanned_keys, reason)
            break
        events = await fetch_event_quotes(key)
        scanned_keys += 1
        for ev in events:
            if _too_close(ev["commence_time"], now):
                skipped_timing += 1
                continue
            for quote in ev["quotes"]:
                sig = classify(
                    quote,
                    sport_key=ev["sport_key"],
                    event_id=ev["event_id"],
                    home_team=ev["home_team"],
                    away_team=ev["away_team"],
                    commence_time=ev["commence_time"],
                )
                if sig is not None:
                    all_signals.append(sig)

    written, to_alert = _persist(all_signals, now)
    alerts_sent = await _dispatch_alerts(to_alert, now)
    n_sure = sum(1 for s in all_signals if s.signal_type == "surebet")
    n_cov = sum(1 for s in all_signals if s.signal_type == "coverage")

    summary = {
        "status": "ok",
        "keys_scanned": scanned_keys,
        "signals": len(all_signals),
        "surebets": n_sure,
        "coverage": n_cov,
        "skipped_timing": skipped_timing,
        "written": written,
        "alerts_new": len(to_alert),
        "alerts_sent": alerts_sent,
    }
    logger.info("matched.scanner: %s", summary)
    try:
        col("matched_scan_runs").document("latest").set({**summary, "at": now.isoformat()})
    except Exception:
        logger.warning("matched.scanner: no se pudo guardar el resumen de scan", exc_info=True)
    return summary
