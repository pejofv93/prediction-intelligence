"""
matched/scanner.py — orquestador del escáner back/lay.

Flujo por ejecución:
  1. /v4/sports (gratis) → deportes activos; filtra por grupo Betfair-líquido y descarta
     mercados de futuros (_winner, election...). Rota: elige N keys (coste) por partidos
     en las próximas 72h (/events, gratis) × horas sin escanear, con tope de tenis.
  2. Por sport_key: fetch_event_quotes (2 créditos) → classify cada selección back/lay.
  3. Timing guard: descarta eventos ya empezados o demasiado próximos.
  4. Persiste señales en Firestore matched_signals (id determinista → idempotente) y limpia
     las expiradas. Devuelve un resumen.

Reemplaza collectors/arbitrage_detector.py (roto). Sin alertas Telegram (Fase 2).
"""
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import (
    ODDS_API_KEY,
    MATCHED_KEY_WINDOW_HOURS,
    MATCHED_MAX_KEYS_PER_SCAN,
    MATCHED_MAX_TENNIS_KEYS_PER_SCAN,
    SIGNAL_MIN_MINUTES_BEFORE_KICKOFF,
)
from shared.firestore_client import col

from .odds_lay import fetch_event_quotes, budget_ok
from .engine import classify
from .alerts import alert_cut_reason, send_matched_alert

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
# Registro por escaneo: un doc por ejecución (id = fecha-hora UTC) + "latest". Los logs de
# Cloud Run solo duran 30 días; esto no cambia qué se alerta, solo lo deja anotado.
_RUNS_COLL = "matched_scan_runs"
_QUOTA_APIS = ("the_odds_api", "the_odds_api_matched")

# Selección de keys del último _active_sport_keys (para el registro del escaneo).
_KEY_STATS: dict = {}


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
    not_liquid = []
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
            not_liquid.append(key)
            continue        # Fase 3 — fuera competiciones sin fondo de lay en Betfair
        cand.append((_GROUP_PRIORITY[group], key))

    cand.sort(key=lambda t: (t[0], t[1]))
    now = datetime.now(timezone.utc)
    counts = await _upcoming_counts([k for _, k in cand], now)
    keys, selection = _choose_keys(cand, counts, _last_scanned(), now)
    _KEY_STATS.clear()
    _KEY_STATS.update({
        "keys_excluded_whitelist": sorted(not_liquid),
        "keys_over_cap": [s["key"] for s in selection if not s["chosen"]],
        "key_selection": selection,
    })
    logger.info("matched.scanner: %d keys activas seleccionadas (de %d deportes): %s",
                len(keys), len(sports), keys)
    return keys


async def _upcoming_counts(keys: list[str], now: datetime) -> dict[str, int | None]:
    """Partidos de cada key con kickoff entre el buffer y +MATCHED_KEY_WINDOW_HOURS.
    GET /v4/sports/{key}/events no consume créditos (x-requests-last: 0, verificado
    2026-10-27). None = la llamada falló (la key no se descarta por eso)."""
    t0 = now + timedelta(minutes=SIGNAL_MIN_MINUTES_BEFORE_KICKOFF)
    t1 = now + timedelta(hours=MATCHED_KEY_WINDOW_HOURS)
    params = {"apiKey": ODDS_API_KEY,
              "commenceTimeFrom": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "commenceTimeTo": t1.strftime("%Y-%m-%dT%H:%M:%SZ")}
    out: dict[str, int | None] = {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        for key in keys:
            try:
                resp = await client.get(f"{_SPORTS_URL}/{key}/events", params=params)
                if resp.status_code != 200:
                    logger.warning("matched.scanner: /events %s HTTP %d", key, resp.status_code)
                    out[key] = None
                    continue
                if resp.headers.get("x-requests-last") not in (None, "0"):
                    logger.warning("matched.scanner: /events %s cobró %s créditos",
                                   key, resp.headers.get("x-requests-last"))
                out[key] = len(resp.json() or [])
            except Exception:
                logger.warning("matched.scanner: error /events %s", key, exc_info=True)
                out[key] = None
    return out


def _last_scanned() -> dict[str, datetime]:
    """Último escaneo de cada key según los registros de matched_scan_runs."""
    last: dict[str, datetime] = {}
    try:
        for d in col(_RUNS_COLL).stream():
            run = d.to_dict() or {}
            try:
                at = datetime.fromisoformat(str(run.get("at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            for k in run.get("keys") or []:
                if k not in last or at > last[k]:
                    last[k] = at
    except Exception:
        logger.warning("matched.scanner: no se pudo leer el histórico de escaneos", exc_info=True)
    return last


def _choose_keys(cand: list[tuple[int, str]], counts: dict[str, int | None],
                 last: dict[str, datetime], now: datetime) -> tuple[list[str], list[dict]]:
    """
    Elige MATCHED_MAX_KEYS_PER_SCAN keys por puntuación = partidos en la ventana ×
    min(horas sin escanear / 24, 3) (nunca escaneada = 3), con como mucho
    MATCHED_MAX_TENNIS_KEYS_PER_SCAN de tenis. Sin partidos en la ventana → fuera.
    Si /events falló en TODAS las keys, se vuelve al orden fijo (grupo, alfabético).
    `cand` ya viene ordenado por (grupo, key): es el desempate.
    Devuelve (keys elegidas, detalle por key para matched_scan_runs).
    """
    if cand and all(counts.get(k) is None for _, k in cand):
        keys = [k for _, k in cand][:MATCHED_MAX_KEYS_PER_SCAN]
        return keys, [{"key": k, "events": None, "hours_since_scan": None, "score": None,
                       "chosen": k in keys,
                       "reason": "elegida (orden fijo: /events falló)" if k in keys
                       else "tope de keys (orden fijo: /events falló)"} for _, k in cand]

    rows = []
    for order, (_, key) in enumerate(cand):
        n = counts.get(key)
        hours = (now - last[key]).total_seconds() / 3600 if key in last else None
        factor = 3.0 if hours is None else min(max(hours, 0.0) / 24, 3.0)
        # /events falló solo en esta key: no se descarta, cuenta como 1 partido.
        score = (1 if n is None else n) * factor
        rows.append({"key": key, "events": n,
                     "hours_since_scan": None if hours is None else round(hours, 1),
                     "score": round(score, 2), "chosen": False, "_order": order})

    keys: list[str] = []
    tennis = 0
    for r in sorted(rows, key=lambda r: (-r["score"], r["_order"])):
        is_tennis = r["key"].startswith("tennis_")
        if r["events"] == 0:
            r["reason"] = f"sin partidos en {MATCHED_KEY_WINDOW_HOURS}h"
        elif len(keys) >= MATCHED_MAX_KEYS_PER_SCAN:
            r["reason"] = "tope de keys"
        elif is_tennis and tennis >= MATCHED_MAX_TENNIS_KEYS_PER_SCAN:
            r["reason"] = "tope de tenis"
        elif r["score"] <= 0:
            r["reason"] = "escaneada hace nada"
        else:
            r["chosen"] = True
            r["reason"] = "elegida"
            keys.append(r["key"])
            tennis += is_tennis
    for r in rows:
        del r["_order"]
    return keys, rows


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


def _persist(signals: list, now: datetime) -> tuple[int, list, dict]:
    """
    Escribe señales (idempotente por signal_id) preservando el flag `alerted` de
    ejecuciones previas (dedup), limpia expiradas y devuelve (nº escritas, a_alertar,
    recuento). `a_alertar` = señales que cumplen umbral y NO habían sido alertadas todavía.
    recuento = señales nuevas/re-detectadas y cortadas por filtro (alert_cut_reason, o
    "already_alerted" si ya salió en un escaneo anterior).
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
    stats = {"new": 0, "redetected": 0, "cut": Counter(), "cut_written_fail": 0}
    for sig in signals:
        prev = existing.get(sig.signal_id)
        prev_alerted = bool(prev and prev.get("alerted"))
        stats["redetected" if prev else "new"] += 1
        reason = "already_alerted" if prev_alerted else alert_cut_reason(sig)
        if reason:
            stats["cut"][reason] += 1
        doc = sig.to_doc()
        doc["alerted"] = prev_alerted            # preservar dedup entre escaneos
        doc["alerted_at"] = (prev or {}).get("alerted_at", "") if prev_alerted else ""
        try:
            col(_COLL).document(sig.signal_id).set(doc)
            written += 1
            if reason is None:
                to_alert.append(sig)
        except Exception:
            stats["cut_written_fail"] += 1
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

    stats["cut"] = dict(stats["cut"])
    return written, to_alert, stats


async def _dispatch_alerts(to_alert: list, now: datetime) -> tuple[int, list]:
    """Envía alertas a Telegram y marca alerted=True en Firestore.
    Devuelve (nº enviadas, signal_ids cuyo envío falló)."""
    sent = 0
    failed: list = []
    for sig in to_alert:
        if not await send_matched_alert(sig):
            failed.append(sig.signal_id)
            continue
        try:
            col(_COLL).document(sig.signal_id).set(
                {"alerted": True, "alerted_at": now.isoformat()}, merge=True)
        except Exception:
            logger.warning("matched.scanner: alerta enviada pero no se marcó %s",
                           sig.signal_id, exc_info=True)
        sent += 1
    return sent, failed


def _quota_snapshot() -> dict:
    """Créditos mensuales usados/restantes de The Odds API (pool y reserva matched)."""
    try:
        from shared.api_quota_manager import quota
        st = quota.get_monthly_status()
        return {a: {"used": st[a]["used"], "remaining": st[a]["remaining"], "limit": st[a]["limit"]}
                for a in _QUOTA_APIS if a in st}
    except Exception:
        logger.warning("matched.scanner: no se pudo leer la cuota para el registro", exc_info=True)
        return {}


def _save_run(summary: dict, now: datetime, detail: dict) -> None:
    """Guarda el escaneo: doc por ejecución (histórico) y "latest" (como antes). Nunca lanza."""
    doc = {**summary, **detail, "at": now.isoformat()}
    try:
        col(_RUNS_COLL).document(now.strftime("%Y%m%dT%H%M%SZ")).set(doc)
        col(_RUNS_COLL).document("latest").set(doc)
    except Exception:
        logger.warning("matched.scanner: no se pudo guardar el registro del escaneo", exc_info=True)


async def run_matched_scan() -> dict:
    """Punto de entrada del escáner. Devuelve resumen para el endpoint/logs."""
    now = datetime.now(timezone.utc)
    quota_before = _quota_snapshot()

    ok, reason = budget_ok()
    if not ok:
        logger.warning("matched.scanner: abortado — %s", reason)
        summary = {"status": "skipped", "reason": reason, "signals": 0}
        _save_run(summary, now, {"quota_before": quota_before})
        return summary

    keys = await _active_sport_keys()
    if not keys:
        summary = {"status": "no_sports", "signals": 0}
        _save_run(summary, now, {"quota_before": quota_before, **_KEY_STATS})
        return summary

    all_signals = []
    scanned_keys = 0
    skipped_timing = 0
    budget_cut = ""

    for key in keys:
        ok, reason = budget_ok()
        if not ok:
            logger.info("matched.scanner: corte de presupuesto tras %d keys — %s", scanned_keys, reason)
            budget_cut = reason
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

    written, to_alert, sig_stats = _persist(all_signals, now)
    alerts_sent, alerts_failed = await _dispatch_alerts(to_alert, now)
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
        "signals_new": sig_stats["new"],
        "signals_redetected": sig_stats["redetected"],
        "cut": sig_stats["cut"],
    }
    logger.info("matched.scanner: %s", summary)
    _save_run(summary, now, {
        "keys": keys[:scanned_keys],
        "keys_not_scanned": keys[scanned_keys:],
        "budget_cut": budget_cut,
        **_KEY_STATS,
        "signals_write_failed": sig_stats["cut_written_fail"],
        "alerted_ids": [s.signal_id for s in to_alert if s.signal_id not in alerts_failed],
        "alert_failed_ids": alerts_failed,
        "quota_before": quota_before,
        "quota_after": _quota_snapshot(),
    })
    return summary
