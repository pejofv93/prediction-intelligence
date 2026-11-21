"""SOLO LECTURA. Por qué no se mueven cifras de los informes de Telegram (21-nov-2026).

1) Informe diario: «Señales sports: N total» (predictions) frente a «N señales» del ROI de
   Sports (shadow_trades).
2) «📈 Señales de tendencia»: graduadas, pendientes y enviados (trend_signals,
   trend_accuracy_log, trend_runs, trend_daily_reports, match_results).
Solo recuentos y fechas: el log es público (sin equipos, sin cuotas).
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from shared.firestore_client import col  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


def _d(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v)[:10]


def _count(q) -> int:
    try:
        return int(q.count().get()[0][0].value)
    except Exception as e:  # sin agregación: se cuenta a mano
        print(f"  (count() no disponible: {type(e).__name__}; recuento por stream)")
        return sum(1 for _ in q.stream())


def predictions_block() -> None:
    print("\n== 1) predictions (cifra «Señales sports: N total»)")
    preds = col("predictions")
    print(f"  docs en predictions: {_count(preds)}")
    window = [d.to_dict() or {} for d in preds.order_by("created_at", direction="DESCENDING").limit(500).stream()]
    stale = sum(1 for d in window if d.get("stale_odds"))
    dates = sorted(_d(d.get("created_at")) for d in window)
    print(f"  ventana del informe (500 más recientes): {len(window)} docs, {stale} stale_odds → "
          f"total mostrado {len(window) - stale}; created_at {dates[0] if dates else '-'} … {dates[-1] if dates else '-'}")
    by_day = Counter(_d(d.get("created_at")) for d in window)
    print("  creadas por día (últimos 8 días): "
          + ", ".join(f"{(TODAY - timedelta(days=i)).isoformat()[5:]}={by_day.get((TODAY - timedelta(days=i)).isoformat(), 0)}"
                      for i in range(7, -1, -1)))
    print(f"  por sport en la ventana: {dict(Counter(str(d.get('sport') or '?') for d in window))}")

    print("\n== 1) shadow_trades (cifra «⚽ Sports: … N señales» del ROI)")
    st = [d.to_dict() or {} for d in col("shadow_trades").where(filter=FieldFilter("source", "==", "sports")).stream()]
    res = Counter(str(t.get("result")) for t in st)
    closed = [t for t in st if t.get("result") in ("win", "loss")]
    cd = sorted(_d(t.get("opened_at") or t.get("created_at")) for t in closed)
    print(f"  shadow_trades source=sports: {len(st)} docs; result {dict(res)}")
    print(f"  cerradas (win+loss) = la cifra del ROI: {len(closed)}; desde {cd[0] if cd else '-'} hasta {cd[-1] if cd else '-'}")
    print(f"  cerradas por sport: {dict(Counter(str(t.get('sport') or '?') for t in closed))}")


def trends_block() -> None:
    print("\n== 2) trend_runs (envíos del feed por día)")
    runs = {d.id: d.to_dict() or {} for d in col("trend_runs").stream()}
    for k in sorted(runs)[-10:]:
        r = runs[k]
        print(f"  {k}: runs {r.get('runs')} · enviados {r.get('sent')} · ranked {r.get('ranked')} · "
              f"ya enviados {r.get('skipped_already_sent')} · mercado nuevo {r.get('new_market_repeats')} · "
              f"min_fixture_score {r.get('min_fixture_score')} · última {str(r.get('last_run_at'))[:16]}")

    print("\n== 2) trend_signals pendientes (graded == False)")
    pend = [d.to_dict() or {} for d in col("trend_signals").where(filter=FieldFilter("graded", "==", False)).stream()]
    print(f"  pendientes: {len(pend)}")
    age = Counter()
    for s in pend:
        md = _d(s.get("match_date"))
        try:
            days = (TODAY - datetime.fromisoformat(md).date()).days
        except ValueError:
            age["sin fecha"] += 1
            continue
        age["futuro/hoy" if days <= 0 else "1-21 días" if days <= 21 else ">21 días (abandonadas)"] += 1
    print(f"  por antigüedad del partido: {dict(age)}")
    print(f"  por pattern_type: {dict(Counter(str(s.get('pattern_type')) for s in pend))}")
    print(f"  por mercado: {dict(Counter(str(s.get('market')) for s in pend))}")
    print(f"  por liga: {dict(Counter(str(s.get('league')) for s in pend).most_common(12))}")
    print(f"  shadow: {sum(1 for s in pend if s.get('shadow'))}")
    gen = sorted(_d(s.get("generated_at")) for s in pend)
    md = sorted(_d(s.get("match_date")) for s in pend)
    print(f"  generadas {gen[0] if gen else '-'} … {gen[-1] if gen else '-'}; partidos {md[0] if md else '-'} … {md[-1] if md else '-'}")

    print("\n== 2) trend_accuracy_log (graduaciones)")
    log = [d.to_dict() or {} for d in col("trend_accuracy_log").stream()]
    ga = sorted(str(r.get("graded_at") or "") for r in log)
    print(f"  filas: {len(log)}; última graduación {ga[-1][:16] if ga else '-'}")
    by_day = Counter(str(r.get("graded_at") or "")[:10] for r in log)
    print("  graduadas por día (últimos 10): " + ", ".join(f"{k[5:]}={by_day[k]}" for k in sorted(by_day)[-10:]))
    last_src = Counter(str(r.get("grade_source")) for r in log if str(r.get("graded_at") or "") >= (NOW - timedelta(days=10)).isoformat())
    print(f"  fuente (últimos 10 días): {dict(last_src)}")

    print("\n== 2) trend_daily_reports (marca del diario)")
    rep = sorted(((d.id, d.to_dict() or {}) for d in col("trend_daily_reports").stream()), key=lambda x: x[0])[-6:]
    for k, r in rep:
        print(f"  {k}: period_from {str(r.get('period_from'))[:16]} · cutoff {str(r.get('cutoff'))[:16]} · "
              f"graduadas {r.get('day_graded')} · pendientes {r.get('pending')}")

    print("\n== 2) match_results (fuente de graduación series+model)")
    since = (TODAY - timedelta(days=8)).isoformat()
    mr = []
    for bound in (since, datetime.fromisoformat(since).replace(tzinfo=timezone.utc)):
        try:
            mr = [d.to_dict() or {} for d in
                  col("match_results").where(filter=FieldFilter("match_date", ">=", bound)).stream()]
        except Exception as e:
            print(f"  no se pudo filtrar por match_date ({type(e).__name__})")
        if mr:
            break
    print(f"  con match_date >= {since}: {len(mr)}; por día: "
          f"{dict(sorted(Counter(_d(m.get('match_date')) for m in mr).items()))}")


def main() -> int:
    predictions_block()
    trends_block()
    return 0


if __name__ == "__main__":
    sys.exit(main())
