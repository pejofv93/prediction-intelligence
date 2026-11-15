"""
Revisión mensual de niveles de Sports (opción a): aprende de los segmentos que pierden SIN
dejar de enviar. Solo puede bajar la etiqueta de nivel y el Kelly que se muestran en la
alerta; si una alerta se envía o no lo sigue decidiendo el motor, que no lee nada de aquí.

Ciclo:
  1. proponer (día 1 de cada mes, o a mano): con lo enviado y graduado desde SINCE calcula,
     por segmento deporte|mercado|nivel_del_modelo, qué parte de la ventaja que veía el modelo
     se ha cumplido, y guarda una PROPUESTA en model_weights/tier_review_proposal. No aplica.
  2. aplicar (solo a mano, confirmar=APLICAR): copia la propuesta a model_weights/
     tier_adjustments, guardando la versión anterior para poder revertir.
  3. revertir (a mano, confirmar=REVERTIR): vuelve a la versión anterior.

Factor k de un segmento (1 = sin ajuste): probabilidad mostrada = mercado + k · (modelo −
mercado), con mercado = 1/cuota y modelo = (EV + 1)/cuota del primer envío.
  k observado = (acierto − esperado_mercado) / (prob_modelo_media − esperado_mercado), en [0, 1]
  k propuesto = encogido hacia 1 con K0 señales ficticias: (n·k_obs + K0) / (n + K0)
Para proponer bajar k, el segmento necesita a la vez (para no perseguir el azar):
  - al menos MIN_N graduadas,
  - acertar por debajo de lo que decía el modelo con significación tras corregir por el
    número de segmentos probados (Benjamini-Hochberg, FDR_Q),
  - acertar por debajo del modelo en las dos mitades del periodo (no una racha).
Paso máximo STEP por mes, hacia abajo o de vuelta hacia 1 si deja de cumplirlo. Nunca sube
de 1: un segmento que gana no sube su nivel.
"""
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from shared.signal_tier import EMOJI_TO_TIER, emoji_from_ev, tier_of

SINCE = datetime(2026, 10, 22, tzinfo=timezone.utc)
MIN_N = 50
K0 = 100.0
FDR_Q = 0.10
STEP = 0.10
MIN_GAP = 0.01          # el modelo debe ver al menos 1 punto más que el mercado
KELLY_CAP = 0.05        # el mismo tope que value_bet_engine.kelly_criterion
DOC_PROPOSAL = ("model_weights", "tier_review_proposal")
DOC_ADJUST = ("model_weights", "tier_adjustments")
_RANK = {"📊": 0, "✅": 1, "🔥": 2}


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def segment_key(sport: str, market: str, tier: str) -> str:
    return f"{sport}|{market}|{tier}"


def model_prob(ev, odds) -> Optional[float]:
    try:
        ev, odds = float(ev), float(odds)
    except (TypeError, ValueError):
        return None
    if odds <= 1:
        return None
    return min(0.999, max(0.001, (ev + 1) / odds))


def segment_stats(records: list[dict]) -> dict:
    """records: [{ok, odds, p, at}] → n, aciertos, esperado mercado, prob modelo, k_obs,
    z y p-valor (acierto por debajo del modelo), y si pierde en las dos mitades."""
    n = len(records)
    h = sum(r["ok"] for r in records) / n
    m = sum(1 / r["odds"] for r in records) / n
    p = sum(r["p"] for r in records) / n
    k_obs = None if p - m < MIN_GAP else max(0.0, min(1.0, (h - m) / (p - m)))
    se = math.sqrt(p * (1 - p) / n)
    z = (h - p) / se if se else 0.0
    srt = sorted(records, key=lambda r: r["at"])
    halves = (srt[: n // 2], srt[n // 2:])
    both = all(hv and sum(r["ok"] for r in hv) / len(hv) < sum(r["p"] for r in hv) / len(hv) for hv in halves)
    return {"n": n, "wins": int(round(h * n)), "hit": round(h, 4), "expected": round(m, 4),
            "model": round(p, 4), "k_obs": None if k_obs is None else round(k_obs, 4),
            "z": round(z, 3), "p_value": round(_phi(z), 5), "both_halves": both}


def benjamini_hochberg(pvals: dict[str, float], q: float = FDR_Q) -> set[str]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    cut = 0
    for i, (_, p) in enumerate(items, 1):
        if p <= q * i / m:
            cut = i
    return {k for k, _ in items[:cut]}


def propose(records_by_key: dict[str, list[dict]], current: dict[str, float]) -> dict:
    """{key: stats + k_current + k_proposed + reason} para los segmentos con MIN_N o con k < 1."""
    stats = {k: segment_stats(v) for k, v in records_by_key.items() if len(v) >= MIN_N}
    tested = {k: s["p_value"] for k, s in stats.items() if s["k_obs"] is not None}
    significant = benjamini_hochberg(tested) if tested else set()
    out = {}
    for k in sorted(set(stats) | {k for k, v in current.items() if v < 1}):
        s = stats.get(k)
        k_cur = float(current.get(k, 1.0))
        if s and k in significant and s["both_halves"] and s["hit"] < s["model"]:
            k_target = (s["n"] * s["k_obs"] + K0) / (s["n"] + K0)
            k_new = max(k_cur - STEP, min(k_cur, k_target))
            reason = "pierde frente al modelo (significativo y en las dos mitades)"
        else:
            k_new = min(1.0, k_cur + STEP) if k_cur < 1 else 1.0
            reason = ("sin muestra suficiente" if not s else
                      "no cumple las tres condiciones") + (": vuelve hacia 1" if k_cur < 1 else "")
        out[k] = {**(s or {"n": len(records_by_key.get(k, []))}), "k_current": round(k_cur, 2),
                  "k_proposed": round(k_new, 2), "reason": reason}
    return out


def adjust(prediction: dict, factors: dict[str, float]) -> Optional[dict]:
    """Nivel y Kelly a mostrar con el factor de su segmento, o None si no hay ajuste.
    Solo puede bajar el nivel y el Kelly; no cambia EV, cuota ni el envío."""
    from shared.segment_history import sport_of
    odds = float(prediction.get("odds") or 0)
    _ev = prediction.get("ev") if prediction.get("ev") is not None else prediction.get("edge")
    p = model_prob(_ev, odds)
    if not factors or p is None:
        return None
    emoji = str(prediction.get("intensity") or "") or emoji_from_ev(_ev)
    if emoji not in _RANK:
        emoji = emoji_from_ev(_ev)
    key = segment_key(sport_of(prediction.get("sport")), str(prediction.get("market_type") or "h2h").lower(),
                      EMOJI_TO_TIER[emoji])
    k = factors.get(key)
    if k is None or k >= 1:
        return None
    m = 1 / odds
    ev_adj = (m + k * (p - m)) * odds - 1
    new_emoji = min(emoji, emoji_from_ev(ev_adj), key=lambda e: _RANK[e])
    kelly = float(prediction.get("kelly_fraction") or 0)
    kelly_adj = min(kelly, KELLY_CAP, max(0.0, ev_adj) / (odds - 1)) if odds > 1 else 0.0
    return {"segment": key, "k": k, "intensity": new_emoji, "intensity_model": emoji,
            "kelly_fraction": round(kelly_adj, 4), "kelly_model": kelly, "ev_adjusted": round(ev_adj, 4)}


# ── Firestore (workflow sports-tier-review y telegram-bot) ──────────────────────

def _as_dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def collect_records() -> dict[str, list[dict]]:
    """Lo enviado y graduado desde SINCE, por segmento con el nivel DEL MODELO (antes de
    cualquier ajuste: intensity_model; si no, el mostrado)."""
    from shared.firestore_client import iter_all
    from shared.segment_history import sport_of

    out = defaultdict(list)
    for d in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        a = d.to_dict() or {}
        at = _as_dt(a.get("sent_at"))
        if at is None or at < SINCE or a.get("correct") not in (True, False):
            continue
        p = model_prob(a.get("ev"), a.get("odds"))
        if p is None:
            continue
        tier = tier_of(a.get("intensity_model") or a.get("intensity"), a.get("ev"))
        key = segment_key(sport_of(a.get("sport")), str(a.get("market_type") or "h2h").lower(), tier)
        out[key].append({"ok": a["correct"] is True, "odds": float(a["odds"]), "p": p, "at": at})
    return out


def current_factors() -> dict[str, float]:
    from shared.firestore_client import col
    snap = col(DOC_ADJUST[0]).document(DOC_ADJUST[1]).get()
    d = snap.to_dict() if snap.exists else None
    return dict((d or {}).get("factors") or {})
