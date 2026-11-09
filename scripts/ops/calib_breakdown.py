"""SOLO LECTURA. Desglose de la tabla de calibración del informe diario
(model_weights/current.accuracy_by_confidence), reconstruida desde predictions con el mismo
universo que learning_engine: graduadas (correct True/False), sin push, sin stale_odds,
creadas desde el último reset del contador (rebuild v63, 2026-08-27 15:51 UTC) y con el
baloncesto dentro solo hasta el despliegue de e71e47b (2026-11-01).

Para cada tramo de confianza y cada dimensión (liga, mercado, fuente, deporte, real o
sintética, enviada o no, antes o desde el 26-oct) imprime: n, acierto real, acierto esperado
por la cuota (media de 1/cuota) y probabilidad media del modelo. Al final, las predicciones
pendientes con confianza >= 0.80 y partido por jugar. No escribe nada.
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.firestore_client import col, iter_all  # noqa: E402

RESET = datetime(2026, 8, 27, 15, 51, tzinfo=timezone.utc)
CUT = datetime(2026, 10, 26, tzinfo=timezone.utc)
BASKET_OUT = datetime(2026, 11, 1, tzinfo=timezone.utc)
BUCKETS = ("65_70", "70_80", "80_90", "90_99")


def bucket(c: float):
    if c < 0.65:
        return None
    return "65_70" if c < 0.70 else "70_80" if c < 0.80 else "80_90" if c < 0.90 else "90_99"


def as_dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def is_basket(p: dict) -> bool:
    return (str(p.get("sport") or "").lower() in ("basketball", "nba")
            or str(p.get("league") or "").upper() in ("NBA", "ACB", "EUROLEAGUE"))


def grader(p: dict) -> str:
    """Quién graduó la predicción, por las marcas que deja cada camino (solo lectura).
      duplicado      → el aprendizaje nocturno le copió el resultado del primario
                       (_resolved_from_duplicate); NO suma en accuracy_by_confidence.
      backfill       → scripts/backfill_predictions_result.py (backfill_at/backfill_note).
      regrade_alt    → scripts/regrade_alt_markets_backfill.py (regrade_note).
      nocturno_fuera → aprendizaje nocturno con excluded_from_learning (época 1: stale_odds,
                       baloncesto desde e71e47b); NO suma.
      nocturno       → aprendizaje nocturno normal (model_epoch=2); SÍ suma.
      sin_marca      → ninguna de las anteriores (arreglo manual u otro camino).
    """
    if p.get("_resolved_from_duplicate"):
        return "duplicado"
    if p.get("backfill_at") or p.get("backfill_note"):
        return "backfill"
    if p.get("regrade_note"):
        return "regrade_alt"
    if p.get("excluded_from_learning") or p.get("model_epoch") == 1:
        return "nocturno_fuera"
    if p.get("model_epoch") == 2:
        return "nocturno"
    return "sin_marca"


def main() -> None:
    sent_ids = set()
    for d in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        pid = (d.to_dict() or {}).get("prediction_id")
        if pid:
            sent_ids.add(str(pid))

    agg = defaultdict(lambda: [0, 0, 0.0, 0, 0.0, 0])  # n, ok, Σ1/cuota, n_cuota, Σprob, n_prob
    totals = defaultdict(lambda: [0, 0])
    for d in iter_all("predictions"):
        p = d.to_dict() or {}
        if p.get("correct") not in (True, False) or p.get("push") or p.get("stale_odds"):
            continue
        created = as_dt(p.get("created_at"))
        b = bucket(float(p.get("confidence") or 0))
        if not created or created < RESET or not b:
            continue
        basket = is_basket(p)
        if basket and created >= BASKET_OUT:
            continue
        pid = str(p.get("prediction_id") or p.get("match_id") or d.id)
        dims = {
            "liga": p.get("league") or "?",
            "mercado": str(p.get("market_type") or "h2h").lower(),
            "fuente": p.get("data_source") or "?",
            "deporte": "basket" if basket else (p.get("sport") or "football"),
            "tipo": "sintética" if (p.get("odds_source") == "poisson_synthetic"
                                    or d.id.endswith("_synthetic")) else "real",
            "enviada": "enviada" if (pid in sent_ids or d.id in sent_ids or p.get("alerted")) else "no enviada",
            "periodo": "antes 26-oct" if created < CUT else "desde 26-oct",
        }
        # Quién gradúa, sin resultado exacto (para cuadrar con el recálculo) y por deporte
        if str(p.get("market_type") or "h2h").strip().lower() != "correct_score":
            dims["graduador"] = f"{grader(p)} · {'basket' if basket else 'fútbol'}"
        odds = float(p.get("odds") or 0)
        prob = p.get("calculated_prob")
        ok = p["correct"] is True
        totals[b][0] += 1
        totals[b][1] += ok
        for dim, val in dims.items():
            a = agg[(b, dim, str(val))]
            a[0] += 1
            a[1] += ok
            if odds > 1:
                a[2] += 1 / odds
                a[3] += 1
            if prob is not None:
                a[4] += float(prob)
                a[5] += 1

    print("Universo: Sports graduadas desde 2026-08-27 15:51 UTC (reset v63), sin push ni stale_odds;"
          " baloncesto solo hasta 2026-11-01.\n")
    for b in BUCKETS:
        n, ok = totals[b]
        print(f"== Tramo {b}: n={n} acierto={ok / n:.0%}" if n else f"== Tramo {b}: n=0")
        rows = sorted(((k, v) for k, v in agg.items() if k[0] == b), key=lambda x: (x[0][1], -x[1][0]))
        for (_, dim, val), (n, ok, inv, ninv, pr, npr) in rows:
            exp = f"{inv / ninv:.0%}" if ninv else "—"
            mod = f"{pr / npr:.0%}" if npr else "—"
            print(f"  {dim:9s} {val[:24]:24s} n={n:4d} acierto={ok / n:4.0%}"
                  f" esperado_cuota={exp:>4s} prob_modelo={mod:>4s}")
        print()

    # Solo agregados: nunca ids, equipos, selecciones ni cuotas de señales pendientes.
    now = datetime.now(timezone.utc)
    pend = defaultdict(int)
    for d in iter_all("predictions", where=("result", "==", None)):
        p = d.to_dict() or {}
        c = float(p.get("confidence") or 0)
        md = as_dt(p.get("match_date"))
        if c >= 0.80 and md and md >= now:
            pend[(bucket(c), p.get("league") or "?", str(p.get("market_type") or "h2h").lower(),
                  "enviada" if p.get("alerted") else "no enviada")] += 1
    print("Pendientes con confianza >= 0.80 y partido por jugar (solo recuentos; celdas n<3 ocultas):")
    hidden = 0
    for (b, liga, mercado, env), n in sorted(pend.items()):
        if n < 3:
            hidden += n
            continue
        print(f"  {b} {liga[:24]:24s} {mercado:12s} {env:10s} n={n}")
    print(f"  en celdas ocultas (n<3): {hidden}")
    print(f"  total: {sum(pend.values())}")


if __name__ == "__main__":
    main()
