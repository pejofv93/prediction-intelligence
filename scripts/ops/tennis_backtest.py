"""Tenis top tp-v1 (F1) — backtest walk-forward por torneo, SOLO LECTURA (workflow
sports-tennis-backtest). No escribe nada.

Lee tennis_tournaments (solo categorías top: analyzers/tennis_top.py), predice cada torneo con
inicio en [EVAL_FROM, EVAL_TO] solo con lo anterior a su inicio (analyzers/tennis_preview.py) y
compara con la referencia (frecuencia de la línea por circuito × superficie × best_of en los 24
meses previos):
 1) contadores (torneos, partidos evaluados, muestra insuficiente…) y parámetros del último corte;
 2) por mercado y grupo (circuito × best_of) y en todos: partidos, Brier de la referencia y del
    modelo, diferencia con IC95 por partido y etiqueta (criterio de mp-v1);
 3) lo mismo para la alternativa de regresión (juegos totales) y la comparación directa
    tp-v1 − regresión;
 4) detalle por superficie (solo la etiqueta del IC, informativa) y calibración por tramos.
El log solo lleva agregados: ningún nombre de jugador ni partido.
"""
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from analyzers import match_preview as mp  # noqa: E402
from analyzers import tennis_preview as tp  # noqa: E402
from analyzers import tennis_top as tt  # noqa: E402

EVAL_FROM = os.environ.get("EVAL_FROM") or "2024-01-01"
EVAL_TO = os.environ.get("EVAL_TO") or None


def read_docs() -> list[dict]:
    from shared.firestore_client import iter_all
    return [d.to_dict() or {} for d in iter_all("tennis_tournaments")]


def _ci(v: dict) -> str:
    lo = "—" if v["ci_lo"] is None else f"{v['ci_lo']:+.4f}"
    hi = "—" if v["ci_hi"] is None else f"{v['ci_hi']:+.4f}"
    return f"{v['delta']:+.4f} [{lo}, {hi}]"


def print_metrics(name: str, rows: list[tuple]) -> dict:
    if not rows:
        print(f"\n[{name}] sin filas")
        return {}
    met = mp.market_metrics(rows)
    print(f"\n[{name}] mercado · grupo: partidos · Brier ref · Brier modelo · diferencia [IC95] · etiqueta")
    for v in sorted(met.values(), key=lambda v: (v["market"], v["div"] != "ALL", v["div"])):
        print(f"  {v['market']:<12} {v['div']:<8} {v['n_matches']:>6} · {v['brier_league']:.4f} · "
              f"{v['brier_model']:.4f} · {_ci(v)} · {v['label']}")
    return met


def report(docs: list[dict], eval_from: str = EVAL_FROM, eval_to=EVAL_TO) -> None:
    ms = tt.flat_matches(docs)
    print(f"partidos top: {len(ms)} · evaluación: torneos con inicio desde {eval_from}"
          f"{' hasta ' + eval_to if eval_to else ''} · ventana {tp.WINDOW_DAYS} días · "
          f"muestra mínima {tp.MIN_SURFACE} partidos en la superficie")
    rows, rows_reg, cnt = tp.backtest(ms, eval_from, eval_to)

    print("\n== 1. Contadores")
    print("  " + " · ".join(f"{k}: {v}" for k, v in sorted(cnt.items())))
    per = Counter((r[0], r[1]) for r in {(r[0], r[1], r[7]) for r in rows})
    print("  partidos evaluados por año y grupo: " + " · ".join(f"{y} {g}: {n}" for (y, g), n in sorted(per.items())))
    if ms:
        cutoff = max(m["date"] for m in ms if m["date"])
        _, win = tp.build_state(ms, tp.days_before(cutoff, -1))     # el día siguiente: todo cuenta
        print(f"  parámetros al último corte ({cutoff}): circuito superficie: juegos/set · |dif|/set · c0 · κ · λ")
        for tour in ("atp", "wta"):
            for sup in sorted({m["surface"] for m in win.ms if m["tour"] == tour}):
                f = win.factors(tour, sup)
                c0, k, lam = win.level(tour, sup)
                edge = (" (borde de la rejilla)" if c0 <= tp.C_GRID[0] or c0 >= tp.C_GRID[-1]
                        or lam in tp.LAMBDA_RANGE else "")
                print(f"    {tour.upper()} {sup:<7} {f['g0']:.3f} · {f['absd0']:.3f} · {c0:.3f} · {k:.3f} · {lam:.3f}{edge}")

    print("\n== 2. tp-v1 (cadena de Markov) frente a la referencia (Δ < 0 = mejor el modelo)")
    print_metrics("tp-v1", rows)

    print("\n== 3. Alternativa: regresión de juegos totales")
    print_metrics("regresión", rows_reg)
    reg_p = {(r[7], r[3]): r[6] for r in rows_reg}
    paired = [(*r[:5], reg_p[(r[7], r[3])], r[6], r[7]) for r in rows
              if r[2] == "total_games" and (r[7], r[3]) in reg_p]
    print("  (abajo: 'Brier ref' = regresión; Δ < 0 = mejor la cadena de Markov)")
    print_metrics("tp-v1 − regresión", paired)

    print("\n== 4a. Por superficie (solo informativo: la etiqueta va por grupo)")
    by_sup = [(r[0], f"{r[1]}-{r[8]}", *r[2:8]) for r in rows]
    met = mp.market_metrics(by_sup)
    for v in sorted((v for v in met.values() if v["div"] != "ALL"), key=lambda v: (v["market"], v["div"])):
        print(f"  {v['market']:<12} {v['div']:<14} {v['n_matches']:>6} · {_ci(v)} · {v['ci_label']}")

    print("\n== 4b. Calibración tp-v1 (todos los grupos): tramo · n · predicho · observado")
    met = mp.market_metrics(rows)
    for v in sorted((v for v in met.values() if v["div"] == "ALL"), key=lambda v: v["market"]):
        print(f"  {v['market']}: " + " · ".join(f"{c['bin']}% n={c['n']} {c['predicted']:.2f}/{c['observed']:.2f}"
                                              for c in v["calibration"]))


def main() -> int:
    report(read_docs())
    return 0


if __name__ == "__main__":
    sys.exit(main())
