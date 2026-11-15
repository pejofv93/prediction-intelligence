"""Fútbol femenino mp-w1 — SOLO LECTURA (workflow sports-women-backtest). No escribe nada.

1) Recuento por liga y temporada de los partidos terminados y con cada estadística.
2) Dispersión (r) y líneas con 2023/24 y 2024/25.
3) Backtest walk-forward 2025/26 + 2026/27: Brier del modelo frente a la media de la liga,
   por mercado y liga, con IC95 por partido.
Datos de Highlightly: el log solo lleva agregados por liga y mercado (nunca equipos ni partidos).
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from analyzers import match_preview as mp  # noqa: E402
from analyzers import match_preview_women as mw  # noqa: E402


def read_docs() -> list[dict]:
    from shared.firestore_client import iter_all
    return [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "women"))]


def main() -> int:
    docs = read_docs()
    print(f"docs grupo women: {len(docs)}")
    matches = mw.matches_from_hl(docs)
    print(f"partidos terminados de las 5 ligas: {len(matches)}\n")

    print("== 1. Recuento (partidos terminados · con cada estadística en los dos lados)")
    cov = mw.coverage(matches)
    print("liga               temp   partidos " + " ".join(f"{s:>7}" for s in mp.STATS))
    for (div, season), c in sorted(cov.items()):
        print(f"{mw.DIV_NAMES[div]:<18} {season}  {c['matches']:>8} " + " ".join(f"{c[s]:>7}" for s in mp.STATS))

    hist = mw.history(matches)
    print("\n== 2a. Dispersión con 2023/24 + 2024/25 (r de la binomial negativa; — = Poisson)")
    disp, detail = mw.fit_dispersion(hist)
    print("mp-v1 (hombres): " + ", ".join(f"{a}_{b} {v}" for (a, b), v in mp.DISPERSION.items()))
    for mk, v in detail.items():
        per = " · ".join(f"{mw.DIV_NAMES[d]} {x['r'] if x['r'] is not None else '—'} (n={x['n']})"
                         for d, x in v["by_div"].items())
        print(f"{mk:<14} n={v['n']:>5}  r común {v['r'] if v['r'] is not None else '—':>8}   por liga: {per}")

    print("\n== 2b. Líneas fijadas con 2023/24 + 2024/25 (frecuencia de 'más de' en esas temporadas)")
    lines = mw.fit_lines(matches)
    rates = mw.line_rates(matches, lines)
    for s in mp.STATS:
        t = " ".join(f"{l}:{rates[s]['team'][l]:.0%}" for l in lines[s][0])
        tt = " ".join(f"{l}:{rates[s]['total'][l]:.0%}" for l in lines[s][1])
        print(f"{s:<8} por equipo  {t}\n{'':<8} total       {tt}")
        print(f"{'':<8} (mp-v1: {mp.LINES[s][0]} / {mp.LINES[s][1]})")

    print("\n== 3. Backtest walk-forward 2025/26 + 2026/27 (Δ = Brier modelo − Brier liga; < 0 = mejor el modelo)")
    rows, met = mw.backtest(hist, lines, disp)
    print(f"líneas evaluadas: {len(rows)} · partidos: {len({r[7] for r in rows})}")
    by_season = {}
    for r in rows:
        by_season.setdefault((r[1], r[0]), set()).add(r[7])
    print("partidos por liga y temporada: " + ", ".join(
        f"{mw.DIV_NAMES[d]} {s} {len(v)}" for (d, s), v in sorted(by_season.items())))
    order = [mw.DIV_NAMES[d] for d in mw.WOMEN_DIVS.values()]
    print(f"\n{'mercado':<14} {'liga':<18} {'partidos':>8} {'Brier liga':>10} {'Brier mod.':>10} "
          f"{'Δ':>9} {'IC95':>21}  BSS     etiqueta (regla mp-v1)")
    for mk in sorted({v["market"] for v in met.values()}):
        for div in list(mw.WOMEN_DIVS.values()) + ["ALL"]:
            v = met.get(f"{mk}|{div}")
            if not v:
                continue
            ci = f"[{v['ci_lo']:+.5f}, {v['ci_hi']:+.5f}]" if v["ci_lo"] is not None else "—"
            name = "TODAS" if div == "ALL" else mw.DIV_NAMES[div]
            mark = "✔" if v["label"] == mp.LABEL_EDGE else ("(IC<0)" if v["ci_label"] == mp.LABEL_EDGE else "")
            print(f"{mk:<14} {name:<18} {v['n_matches']:>8} {v['brier_league']:>10.5f} {v['brier_model']:>10.5f} "
                  f"{v['delta']:>+9.5f} {ci:>21}  {v['bss_vs_league'] if v['bss_vs_league'] is not None else '—':>6}  {mark}")
    print("\nCalibración (todas las ligas, tramo: predicho → observado, n):")
    for mk in sorted({v["market"] for v in met.values()}):
        cal = met.get(f"{mk}|ALL", {}).get("calibration", [])
        print(f"{mk:<14} " + " · ".join(f"{c['bin']}: {c['predicted']:.2f}→{c['observed']:.2f} ({c['n']})" for c in cal))
    n_edge = sum(1 for v in met.values() if v["div"] != "ALL" and v["label"] == mp.LABEL_EDGE)
    print(f"\nCeldas liga×mercado con ✔ (IC de la celda y del mercado en todas < 0): {n_edge} de "
          f"{sum(1 for v in met.values() if v['div'] != 'ALL')}. No se ha escrito nada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
