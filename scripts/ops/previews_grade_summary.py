"""SOLO LECTURA. Fichas de partido graduadas desde SINCE (por defecto 2026-11-15), por liga y
mercado: líneas graduadas, aciertos del modelo (lado con p > 0,5) frente a la media de la liga,
Brier medio de los dos y aciertos de las líneas destacadas.

Colecciones: match_preview_grades (masculino) y match_preview_grades_women (femenino).
Solo agregados por liga y mercado (nada de equipos ni partidos). No escribe nada.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.firestore_client import col  # noqa: E402

DIV_NAMES = {"SP1": "LaLiga", "SP2": "Segunda", "E0": "Premier", "I1": "Serie A", "D1": "Bundesliga",
             "F1": "Ligue 1", "N1": "Eredivisie", "P1": "Primeira",
             "WLF": "Liga F", "WSL": "WSL", "WFBL": "Frauen-Bundesliga", "WSA": "Serie A femenina",
             "WD1": "D1 femenina"}


def main() -> int:
    since = os.environ.get("SINCE") or "2026-11-15"
    for coll in ("match_preview_grades", "match_preview_grades_women"):
        docs = [d.to_dict() or {} for d in col(coll).stream()]
        docs = [g for g in docs if str(g.get("graded_at") or "") >= since]
        status = defaultdict(int)
        for g in docs:
            status[g.get("status")] += 1
        print(f"\n== {coll}: {len(docs)} fichas con graded_at >= {since} · {dict(status)}")
        agg = defaultdict(lambda: [0, 0, 0, 0.0, 0.0, 0, 0])   # n, hit_m, hit_l, bri_m, bri_l, n_hl, hit_hl
        fichas = defaultdict(int)
        for g in docs:
            if g.get("status") != "graduada":
                continue
            liga = g.get("league") or DIV_NAMES.get(g.get("div"), g.get("div"))
            fichas[liga] += 1
            for ln in g.get("lines") or []:
                y = int(ln["y"])
                k = (liga, f"{ln['stat']}·{ln['side']}")
                a = agg[k]
                a[0] += 1
                a[1] += int((ln["p_model"] > 0.5) == bool(y))
                a[2] += int((ln["p_league"] > 0.5) == bool(y))
                a[3] += (ln["p_model"] - y) ** 2
                a[4] += (ln["p_league"] - y) ** 2
                if ln.get("highlight"):
                    a[5] += 1
                    a[6] += int((ln["p_model"] > 0.5) == bool(y))
        for liga in sorted(fichas):
            print(f"\n  {liga}: {fichas[liga]} fichas graduadas")
            print(f"    {'mercado':16} {'líneas':>6} {'acierto modelo':>15} {'acierto liga':>13} "
                  f"{'Brier mod.':>10} {'Brier liga':>10} {'destacadas':>11}")
            tot = [0, 0, 0, 0.0, 0.0, 0, 0]
            for (lg, mk), a in sorted(agg.items()):
                if lg != liga:
                    continue
                tot = [x + y for x, y in zip(tot, a)]
                hl = f"{a[6]}/{a[5]}" if a[5] else "—"
                print(f"    {mk:16} {a[0]:6} {a[1] / a[0]:14.0%} {a[2] / a[0]:12.0%} "
                      f"{a[3] / a[0]:10.3f} {a[4] / a[0]:10.3f} {hl:>11}")
            if tot[0]:
                hl = f"{tot[6]}/{tot[5]}" if tot[5] else "—"
                print(f"    {'TOTAL':16} {tot[0]:6} {tot[1] / tot[0]:14.0%} {tot[2] / tot[0]:12.0%} "
                      f"{tot[3] / tot[0]:10.3f} {tot[4] / tot[0]:10.3f} {hl:>11}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
