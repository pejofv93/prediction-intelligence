"""
Historial por segmento de lo ENVIADO en Sports, para la línea "📚 Historial" de cada alerta.

Solo informa: no decide si una alerta se envía ni cambia nada del modelo.

Universo (desde SINCE): alerts_sent type=sports_sent graduadas (correct True/False; push y
pendientes fuera) — fútbol, tenis y, desde el 14-nov, baloncesto — más el baloncesto enviado
antes de registrarse ahí (predictions.alerted == True con resultado y sin doc en alerts_sent).
Cuota y nivel del PRIMER envío; un reenvío no es apuesta nueva. 1u por señal.

Segmentos, del más concreto al más general (la alerta usa el primero con MIN_N graduadas):
  deporte|liga|mercado|nivel → deporte|*|mercado|nivel → deporte|*|mercado|* → deporte|*|*|*
Nivel = el que se vio en la alerta (shared/signal_tier: campo intensity; sin él, envíos
anteriores al 14-nov, se reconstruye sobre el EV con los cortes del motor).

refresh_segment_history() lo recalcula y lo guarda en model_weights/segment_history (solo
agregados); lo llama el aprendizaje nocturno tras graduar lo enviado.
"""
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SINCE = datetime(2026, 10, 22, tzinfo=timezone.utc)
MIN_N = 20          # graduadas para mostrar un segmento
MIN_N_VERDICT = 30  # por debajo, "muestra corta" aunque se muestre
Z95 = 1.96
DOC = ("model_weights", "segment_history")

BASKET = ("basketball", "nba")


def _as_dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def tier_of(intensity, ev) -> str:
    """Nivel mostrado en la alerta (mismo significado en todas partes: shared/signal_tier)."""
    from shared.signal_tier import tier_of as _tier_of
    return _tier_of(intensity, ev)


def sport_of(raw) -> str:
    s = str(raw or "football").lower()
    return "basketball" if s in BASKET else s


def segment_keys(sport: str, league: str, market: str, tier: str) -> list[str]:
    """Claves del más concreto al más general."""
    return [f"{sport}|{league}|{market}|{tier}", f"{sport}|*|{market}|{tier}",
            f"{sport}|*|{market}|*", f"{sport}|*|*|*"]


def aggregate(records: list[dict]) -> dict[str, dict]:
    """records: [{sport, league, market, tier, odds, ok}] → {clave: estadísticas}."""
    acc = defaultdict(lambda: {"n": 0, "wins": 0, "inv": 0.0, "pnl": 0.0, "pnl2": 0.0})
    for r in records:
        pnl = (r["odds"] - 1.0) if r["ok"] else -1.0
        for k in segment_keys(r["sport"], r["league"], r["market"], r["tier"]):
            a = acc[k]
            a["n"] += 1
            a["wins"] += int(r["ok"])
            a["inv"] += 1.0 / r["odds"]
            a["pnl"] += pnl
            a["pnl2"] += pnl * pnl
    out = {}
    for k, a in acc.items():
        n = a["n"]
        roi = a["pnl"] / n
        var = (a["pnl2"] - n * roi * roi) / (n - 1) if n > 1 else float("inf")
        se = math.sqrt(max(var, 0.0) / n) if n > 1 else float("inf")
        out[k] = {"n": n, "wins": a["wins"], "expected": round(a["inv"] / n, 4),
                  "roi": round(roi, 4),
                  "roi_lo": round(roi - Z95 * se, 4) if math.isfinite(se) else None,
                  "roi_hi": round(roi + Z95 * se, 4) if math.isfinite(se) else None}
    return out


def verdict(s: dict) -> str:
    if s["n"] < MIN_N_VERDICT or s.get("roi_lo") is None:
        return "muestra corta"
    if s["roi_hi"] < 0:
        return "pierde claro"
    if s["roi_lo"] > 0:
        return "gana claro"
    return "sin evidencia"


def pick(history: Optional[dict], prediction: dict) -> tuple[Optional[str], Optional[dict], int]:
    """(clave, estadísticas) del segmento más concreto con MIN_N graduadas, o (None, None,
    n del deporte) si ninguno llega."""
    segs = (history or {}).get("segments") or {}
    sport = sport_of(prediction.get("sport"))
    keys = segment_keys(sport, str(prediction.get("league") or "?"),
                        str(prediction.get("market_type") or "h2h").lower(),
                        tier_of(prediction.get("intensity"), prediction.get("ev", prediction.get("edge"))))
    for k in keys:
        s = segs.get(k)
        if s and s.get("n", 0) >= MIN_N:
            return k, s, s["n"]
    return None, None, (segs.get(keys[-1]) or {}).get("n", 0)


# ── Lectura de Firestore y guardado (aprendizaje nocturno) ──────────────────────

def collect_records() -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter

    from shared.firestore_client import col, iter_all

    records, seen = [], set()
    for d in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        a = d.to_dict() or {}
        sent_at = _as_dt(a.get("sent_at"))
        if sent_at is None or sent_at < SINCE:
            continue
        if a.get("prediction_id"):
            seen.add(str(a["prediction_id"]))
        if a.get("correct") not in (True, False):
            continue
        try:
            odds = float(a.get("odds") or 0)
        except (TypeError, ValueError):
            continue
        if odds <= 1:
            continue
        records.append({"sport": sport_of(a.get("sport")), "league": str(a.get("league") or "?"),
                        "market": str(a.get("market_type") or "h2h").lower(),
                        "tier": tier_of(a.get("intensity"), a.get("ev")),
                        "odds": odds, "ok": a["correct"] is True})
    # Baloncesto enviado antes de registrarse en alerts_sent: por la marca alerted.
    for d in col("predictions").where(filter=FieldFilter("alerted", "==", True)).stream():
        p = d.to_dict() or {}
        if sport_of(p.get("sport")) != "basketball" or d.id in seen:
            continue
        created = _as_dt(p.get("created_at"))
        if created is None or created < SINCE or p.get("push") or p.get("correct") not in (True, False):
            continue
        try:
            odds = float(p.get("odds") or 0)
        except (TypeError, ValueError):
            continue
        if odds <= 1:
            continue
        records.append({"sport": "basketball", "league": str(p.get("league") or "?"),
                        "market": str(p.get("market_type") or "h2h").lower(),
                        "tier": tier_of(p.get("intensity"), p.get("ev", p.get("edge"))),
                        "odds": odds, "ok": p["correct"] is True})
    return records


def refresh_segment_history() -> dict:
    """Recalcula y guarda model_weights/segment_history. Nunca lanza."""
    from shared.firestore_client import col

    try:
        records = collect_records()
        segs = aggregate(records)
        col(DOC[0]).document(DOC[1]).set({
            "since": SINCE.isoformat(), "min_n": MIN_N, "graded": len(records),
            "updated_at": datetime.now(timezone.utc), "segments": segs,
        })
        logger.info("refresh_segment_history: %d graduadas, %d segmentos", len(records), len(segs))
        return {"graded": len(records), "segments": len(segs)}
    except Exception:
        logger.error("refresh_segment_history: error", exc_info=True)
        return {}
