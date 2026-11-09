"""SOLO LECTURA. Comprueba que los porcentajes que calcula export_rates.py desde los CSV
coinciden con los de fdco_team_market_rates en una muestra de equipos.

Compara, por mercado → línea → lado: p_adj, p_league, n_cur, n_prev y meets_min_sample.
Los equipos cuyo número de partidos difiere se cuentan aparte como "desfasados": el CSV
trae partidos que el colector aún no ha cargado, y ahí la diferencia es esperada.
Imprime solo recuentos, la diferencia máxima y hasta 10 celdas distintas (equipo·mercado).

Uso (necesita credenciales de lectura de Firestore): python trends_web/compare_rates.py --muestra 40
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from export_rates import build  # noqa: E402

TOL = 1e-4  # Firestore guarda 4 decimales
FIELDS = ("p_adj", "p_league", "n_cur", "n_prev", "meets_min_sample")


def compare_doc(mine: dict, stored: dict) -> tuple[int, int, float, list[str]]:
    """(celdas comparadas, celdas distintas, diferencia máxima, ejemplos)."""
    cells = diff = 0
    max_gap = 0.0
    examples = []
    for market, m in mine["markets"].items():
        for lk, splits in m["lines"].items():
            for split, c in splits.items():
                s = ((stored.get("markets") or {}).get(market, {}).get("lines", {}).get(lk, {}).get(split))
                cells += 1
                bad = s is None
                for f in FIELDS:
                    if bad:
                        break
                    a, b = c.get(f), s.get(f)
                    if isinstance(a, float) or isinstance(b, float):
                        if a is None or b is None:
                            bad = a is not b
                        else:
                            max_gap = max(max_gap, abs(a - b))
                            bad = abs(a - b) > TOL
                    else:
                        bad = a != b
                if bad:
                    diff += 1
                    if len(examples) < 10:
                        examples.append(f"{mine['div']}·{mine['team']}·{market}·{lk}·{split}")
    return cells, diff, max_gap, examples


def main() -> int:
    from shared.firestore_client import col

    ap = argparse.ArgumentParser()
    ap.add_argument("--muestra", type=int, default=40)
    ap.add_argument("--semilla", type=int, default=20261109)
    args = ap.parse_args()

    docs, _, failed = build()
    if failed:
        print(f"ERROR: descargas fallidas {failed}")
        return 1
    ids = sorted(docs)
    sample = random.Random(args.semilla).sample(ids, min(args.muestra, len(ids)))
    tot_cells = tot_diff = missing = stale = 0
    max_gap = 0.0
    examples = []
    for doc_id in sample:
        snap = col("fdco_team_market_rates").document(doc_id).get()
        if not snap.exists:
            missing += 1
            continue
        stored = snap.to_dict() or {}
        mine = docs[doc_id]
        if (stored.get("matches_current"), stored.get("matches_previous")) != \
                (mine["matches_current"], mine["matches_previous"]):
            stale += 1
            continue
        c, d, g, ex = compare_doc(mine, stored)
        tot_cells += c
        tot_diff += d
        max_gap = max(max_gap, g)
        examples += ex[: 10 - len(examples)]
    compared = len(sample) - missing - stale
    print(f"Equipos en la muestra: {len(sample)} de {len(ids)}")
    print(f"  comparados: {compared} · desfasados (otro nº de partidos): {stale} · sin doc en la colección: {missing}")
    print(f"  celdas comparadas: {tot_cells} · iguales: {tot_cells - tot_diff} · distintas: {tot_diff}")
    print(f"  diferencia máxima en porcentajes: {max_gap:.6f}")
    for e in examples:
        print(f"  distinta: {e}")
    return 0 if tot_diff == 0 and missing == 0 and compared > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
