"""Selecciones mp-n1 — SOLO LECTURA (workflow sports-nt-backtest). No escribe nada.

Lee hl_team_stats (grupo nt) de Firestore. No pide nada a Highlightly.
1) Recuento por grupo de competición y año (partidos terminados · con cada estadística).
2) Tramo de ajuste (antes de FIT_END): dispersión (r) y líneas.
3) Backtest walk-forward (desde FIT_END): Brier del modelo frente a dos referencias
   (competición: mismo tipo y sede; global: toda la ventana), por mercado y grupo, IC95 por
   bootstrap sobre partidos.
Datos de Highlightly: el log solo lleva agregados por grupo y mercado (nunca equipos ni partidos).
"""
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from analyzers import match_preview as mp  # noqa: E402
from analyzers import match_preview_nt as mn  # noqa: E402
from analyzers import match_preview_women as mw  # noqa: E402

FIT_END = os.environ.get("FIT_END") or mn.FIT_END


def read_docs() -> list[dict]:
    from shared.firestore_client import iter_all
    return [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "nt"))]


def _ci(v) -> str:
    lo = "—" if v["ci_lo"] is None else f"{v['ci_lo']:+.4f}"
    hi = "—" if v["ci_hi"] is None else f"{v['ci_hi']:+.4f}"
    return f"[{lo}, {hi}]"


def print_metrics(name: str, rows: list[tuple], detail: bool = False) -> dict:
    if not rows:
        print(f"\n[{name}] sin filas")
        return {}
    met = mn.metrics(rows)
    print(f"\n[{name}]  mercado: partidos · Brier ref · Brier modelo · diferencia [IC95 bootstrap] · veredicto")
    for v in sorted((v for v in met.values() if v["div"] == "ALL"), key=lambda v: v["market"]):
        print(f"  {v['market']:14} {v['n_matches']:5d} · {v['brier_league']:.4f} · {v['brier_model']:.4f} · "
              f"{v['delta']:+.4f} {_ci(v)} · {mn.verdict(v)}")
    per_g = Counter(v["div"] for v in met.values() if v["div"] != "ALL" and v["ci_label"] == mp.LABEL_EDGE)
    print(f"  celdas grupo×mercado con IC entero bajo 0: {dict(per_g) or 'ninguna'}")
    if detail:
        print(f"  [{name}] por grupo:  grupo · mercado · partidos · diferencia [IC95] · veredicto")
        for v in sorted((v for v in met.values() if v["div"] != "ALL"), key=lambda v: (v["div"], v["market"])):
            print(f"    {mn.GROUP_NAMES.get(v['div'], v['div']):<26} {v['market']:14} {v['n_matches']:4d} · "
                  f"{v['delta']:+.4f} {_ci(v)} · {mn.verdict(v)}")
    return met


def main() -> int:
    docs = read_docs()
    print(f"docs grupo nt: {len(docs)}")
    matches = mn.matches_from_hl(docs, fit_end=FIT_END)
    print(f"partidos terminados con marcador: {len(matches)} · ajuste < {FIT_END} ≤ backtest")
    print(f"tramos: {dict(Counter(m['season'] for m in matches))} · neutrales: "
          f"{sum(m['neutral'] for m in matches)} · amistosos: {sum(m['friendly'] for m in matches)}")

    print("\n== 1. Recuento (partidos terminados · con cada estadística en los dos lados)")
    print("grupo                       año   partidos " + " ".join(f"{s:>7}" for s in mp.STATS))
    for (g, y), c in sorted(mn.coverage(matches).items()):
        print(f"{mn.GROUP_NAMES.get(g, g):<26}  {y}  {c['matches']:>8} " + " ".join(f"{c[s]:>7}" for s in mp.STATS))

    print(f"\nParámetros: ventana {mn.WINDOW_DAYS} d · vida media {mn.HALF_LIFE_DAYS:.0f} d · K {mn.SHRINK_K} · "
          f"mínimo {mn.MIN_MATCHES} partidos · {mn.ITERATIONS} pasadas de ajuste por rival · "
          f"bootstrap {mn.BOOT_REPS}")

    print("\n== 2a. Dispersión en el tramo de ajuste (r de la binomial negativa; — = Poisson)")
    disp, det = mn.fit_dispersion(mn.predictions(matches, period=mn.FIT), period=mn.FIT)
    for mk, v in det.items():
        print(f"{mk:<14} n={v['n']:>5}  r {v['r'] if v['r'] is not None else '—'}")

    print("\n== 2b. Líneas fijadas con el tramo de ajuste (frecuencia de 'más de' en ese tramo)")
    lines = mw.fit_lines(matches, seasons=(mn.FIT,))
    rates = mw.line_rates(matches, lines, seasons=(mn.FIT,))
    for s in mp.STATS:
        t = " ".join(f"{l}:{rates[s]['team'][l]:.0%}" for l in lines[s][0]) if rates[s]["team"] else "—"
        tt = " ".join(f"{l}:{rates[s]['total'][l]:.0%}" for l in lines[s][1]) if rates[s]["total"] else "—"
        print(f"{s:<8} por equipo  {t}\n{'':<8} total       {tt}")

    if any(not lines[s][0] or not lines[s][1] for s in mp.STATS):
        print("\nAVISO: alguna estadística sin líneas (sin datos en el tramo de ajuste): queda fuera.")
        lines = {s: v for s, v in lines.items() if v[0] and v[1]}

    print("\n== 3. Backtest walk-forward (Δ = Brier modelo − Brier referencia; < 0 = mejor el modelo)")
    preds = (p for p in mn.predictions(matches, period=mn.BT) if p["stat"] in lines)
    rows = mn.backtest_rows(preds, lines, disp, period=mn.BT)
    comp = rows["modelo_vs_competicion"]
    print(f"líneas evaluadas: {len(comp)} · partidos: {len({r[7] for r in comp})}")
    print("partidos evaluados por grupo: " + ", ".join(
        f"{mn.GROUP_NAMES.get(g, g)} {n}" for g, n in sorted(Counter(
            g for g, _ in {(r[1], r[7]) for r in comp}).items())))
    met = print_metrics("modelo_vs_competicion", comp, detail=True)
    print_metrics("modelo_vs_global", rows["modelo_vs_global"])

    if met:
        print("\nCalibración (modelo_vs_competicion, todos los grupos; tramo: predicho → observado, n):")
        for mk in sorted({v["market"] for v in met.values()}):
            cal = met.get(f"{mk}|ALL", {}).get("calibration", [])
            print(f"{mk:<14} " + " · ".join(f"{c['bin']}: {c['predicted']:.2f}→{c['observed']:.2f} ({c['n']})"
                                            for c in cal))
    print("\nNo se ha escrito nada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
