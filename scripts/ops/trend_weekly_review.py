"""SOLO LECTURA (23-nov-2026). Semanal de «Señales de tendencia» del 16 al 22 de noviembre.

a) Ambos marcan (99/166, 60 % frente a 70 %): acierto de lo enviado por tramo de racha
   (rate_or_ratio) y qué habría pasado con lo ya graduado si se pasa a sombra o se sube
   el umbral; su calibración; v1 y B6 en sombra.
b) Goles 2+ — G5 frente a v1 en sombra (regla de promoción) y lo enviado.
c) trend_runs por día: qué día no tiene ejecución registrada.
Solo recuentos, umbrales y fechas: el log es público (sin equipos ni partidos).
"""
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from shared.config import (  # noqa: E402
    TREND_MARKET_TARGET, TREND_PROMOTION_MIN_SHADOW, TREND_SHADOW_MARKETS, TREND_VARIANT_COUNT_SINCE,
)
from shared.firestore_client import col  # noqa: E402

THRESHOLDS = (0.70, 0.75, 0.80, 0.85, 0.90, 1.00)


def _r(rows) -> str:
    h = sum(1 for r in rows if r.get("result") == "hit")
    n = sum(1 for r in rows if r.get("result") in ("hit", "miss"))
    return f"{h}/{n} ({h / n * 100:.1f}%)" if n else "0/0"


def _graded(rows):
    return [r for r in rows if r.get("result") in ("hit", "miss")]


def main() -> None:
    rows = [r for r in (d.to_dict() or {} for d in col("trend_accuracy_log").stream())
            if not r.get("duplicate_of")]
    feed = _graded([r for r in rows if not r.get("shadow") and r.get("market") not in TREND_SHADOW_MARKETS])
    shadow = _graded([r for r in rows if r.get("shadow")
                      and str(r.get("generated_at") or "")[:10] >= TREND_VARIANT_COUNT_SINCE])

    wk = {d.id: d.to_dict() or {} for d in col("trend_weekly_reports").stream()}
    w48 = wk.get("2026-W48", {})
    since = w48.get("period_from") or "2026-11-16"
    upto = w48.get("cutoff") or "9999"
    print(f"== Semanal 2026-W48: period_from={str(since)[:16]} cutoff={str(upto)[:16]} "
          f"week_graded={w48.get('week_graded')} pending={w48.get('pending')}")
    week = [r for r in feed if since < str(r.get("graded_at") or "") <= upto]

    # ── a) Ambos marcan ────────────────────────────────────────────────────────
    btts = [r for r in feed if r.get("market") == "btts"]
    btts_w = [r for r in week if r.get("market") == "btts"]
    print(f"\n== a) Ambos marcan (enviadas) — total {_r(btts)} · semana {_r(btts_w)} · "
          f"objetivo {TREND_MARKET_TARGET['btts'] * 100:.0f}%")
    print(f"  rate_or_ratio: {dict(sorted(Counter(round(float(r.get('rate_or_ratio') or 0), 2) for r in btts).items()))}")
    print("  umbral   total                 semana          acierto global del feed con ese umbral")
    others = [r for r in feed if r.get("market") != "btts"]
    for t in THRESHOLDS:
        keep = [r for r in btts if float(r.get("rate_or_ratio") or 0) >= t - 1e-9]
        keep_w = [r for r in btts_w if float(r.get("rate_or_ratio") or 0) >= t - 1e-9]
        print(f"  >= {t:.2f}  {_r(keep):20s}  {_r(keep_w):14s}  {_r(others + keep)}")
    print(f"  en sombra (fuera del acierto): feed sin Ambos marcan {_r(others)} · "
          f"feed actual {_r(feed)}")
    by_month = Counter()
    hits_month = Counter()
    for r in btts:
        m = str(r.get("graded_at") or "")[:7]
        by_month[m] += 1
        hits_month[m] += r.get("result") == "hit"
    print("  por mes de graduación: " + ", ".join(f"{m} {hits_month[m]}/{by_month[m]}" for m in sorted(by_month)))
    recent = sorted(btts, key=lambda r: str(r.get("graded_at") or ""))[-50:]
    print(f"  últimas 50 graduadas: {_r(recent)}")
    cal = col("trend_market_calibration").document("btts").get()
    cd = cal.to_dict() if cal.exists else {}
    print("  calibración: " + ", ".join(f"{k}={v}" for k, v in sorted(cd.items())
                                        if isinstance(v, (int, float, str, bool)) and len(str(v)) < 40))
    for rid in ("btts.v1", "btts.B6"):
        print(f"  sombra {rid} (desde {TREND_VARIANT_COUNT_SINCE}): {_r([r for r in shadow if r.get('rule_id') == rid])}")

    # ── b) Goles 2+ del equipo: G5 ─────────────────────────────────────────────
    g5 = [r for r in shadow if r.get("rule_id") == "team_goals_over.G5"]
    v1 = [r for r in shadow if r.get("rule_id") == "team_goals_over.v1"]
    tgo = [r for r in feed if r.get("market") == "team_goals_over"]
    tgo_nov = [r for r in tgo if str(r.get("generated_at") or r.get("graded_at") or "")[:10] >= TREND_VARIANT_COUNT_SINCE]
    print(f"\n== b) Goles 2+ — G5 sombra {_r(g5)} · v1 sombra {_r(v1)} · enviadas total {_r(tgo)} · "
          f"enviadas desde {TREND_VARIANT_COUNT_SINCE} {_r(tgo_nov)} · objetivo "
          f"{TREND_MARKET_TARGET['team_goals_over'] * 100:.0f}% · mínimo {TREND_PROMOTION_MIN_SHADOW}")
    for name, rs in (("G5", g5), ("v1", v1)):
        wk_c = Counter()
        for r in rs:
            k = str(r.get("graded_at") or "")[:10]
            wk_c[k[:8] + ("1" if k[8:10] < "16" else "2")] += 1
        print(f"  {name} graduadas por quincena: {dict(sorted(wk_c.items()))}")
    g5_ids = {r.get("match_id") for r in g5}
    both = [r for r in v1 if r.get("match_id") in g5_ids]
    print(f"  v1 en sombra en los mismos partidos que G5: {_r(both)}")
    pr = col("trend_rule_promotions").document("team_goals_over").get()
    pd = pr.to_dict() if pr.exists else {}
    print("  trend_rule_promotions/team_goals_over: " + ", ".join(
        f"{k}={v}" for k, v in sorted(pd.items()) if k in (
            "variant", "status", "confirmed", "hits", "n", "rate", "v1_hits", "v1_n", "v1_rate", "updated_at")))
    rules = col("trend_market_rules").document("team_goals_over").get()
    print(f"  trend_market_rules/team_goals_over: {'existe (' + str((rules.to_dict() or {}).get('active_rule')) + ')' if rules.exists else 'no existe → v1'}")

    # ── c) trend_runs ──────────────────────────────────────────────────────────
    print("\n== c) trend_runs del 14 al 23 de noviembre")
    for day in range(14, 24):
        k = f"2026-11-{day:02d}"
        s = col("trend_runs").document(k).get()
        if not s.exists:
            print(f"  {k}: SIN FILA")
            continue
        d = s.to_dict() or {}
        print(f"  {k}: runs={d.get('runs')} sent={d.get('sent')} last_run_at={str(d.get('last_run_at'))[:16]}")


if __name__ == "__main__":
    main()
