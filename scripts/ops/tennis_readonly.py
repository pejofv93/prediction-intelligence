"""Tenis top (tp-v1) F0 — SOLO LECTURA (workflow sports-tennis-readonly). No escribe nada.

Consultas del plan sobre tennis_tournaments:
 1) Valores distintos de `series` por circuito con torneos y partidos, si entran en la lista
    cerrada de categorías top y qué torneos quedan fuera (por serie o por nombre: eventos por
    equipos, Juegos Olímpicos, Next Gen Finals). Series y torneos son categorías públicas.
 2) Partidos por circuito, superficie y best_of (todo y solo top); proporción de `valid` y de cada
    `status`; partidos por año.
 3) Partidos por jugador y superficie en los 24 meses anteriores al último partido cargado, como
    histograma (solo recuentos), y cuántos partidos del último año tenían a los dos jugadores con
    la muestra mínima (8) en la superficie.
El log no lleva NINGÚN nombre de jugador.
"""
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from analyzers import tennis_top as tt  # noqa: E402

MIN_SURFACE = 8


def read_docs() -> list[dict]:
    from shared.firestore_client import iter_all
    return [d.to_dict() or {} for d in iter_all("tennis_tournaments")]


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f} %" if b else "—"


def report(docs: list[dict]) -> None:
    print(f"docs de tennis_tournaments: {len(docs)} · años: "
          f"{dict(sorted(Counter(d.get('year') for d in docs).items()))}")

    print("\n== 1. Series por circuito (torneos · partidos · categoría top · motivo)")
    ss = tt.series_summary(docs)
    for (tour, series), v in sorted(ss.items()):
        cat = v["categoria"] or "FUERA"
        print(f"{tour.upper()} {series or '(vacía)':<22} torneos {v['torneos']:>4} · partidos {v['partidos']:>6} · "
              f"{cat:<10} · {dict(v['motivos'])}")
    fuera = sorted({(tour, n) for (tour, _), v in ss.items() for n in v["fuera"]})
    print(f"torneos fuera ({len(fuera)}; nombres de torneo, públicos):")
    for tour, n in fuera:
        print(f"  {tour.upper()} {n}")

    all_m = tt.flat_matches(docs, top_only=False)
    top_m = tt.flat_matches(docs, top_only=True)
    print(f"\n== 2. Forma de los datos · partidos: todos {len(all_m)} · top {len(top_m)}")
    for name, ms in (("todos", all_m), ("top", top_m)):
        print(f"[{name}] circuito · superficie · best_of: partidos · valid · status")
        for (tour, sup, bo), v in sorted(tt.shape_summary(ms).items()):
            print(f"  {tour.upper()} {sup:<7} bo{bo}: {v['partidos']:>6} · valid {_pct(v['valid'], v['partidos'])} · "
                  f"{dict(v['status'].most_common())}")
    by_year = Counter((m["tour"], (m["date"] or "?")[:4]) for m in top_m)
    print("[top] partidos por circuito y año: " + " · ".join(f"{t.upper()} {y}: {n}" for (t, y), n in sorted(by_year.items())))
    by_cat = Counter((m["tour"], m["category"]) for m in top_m)
    print("[top] partidos por categoría: " + " · ".join(f"{t.upper()} {c}: {n}" for (t, c), n in sorted(by_cat.items())))

    ref = max((m["date"] for m in top_m if m["date"]), default=None)
    if not ref:
        print("\n== 3. sin partidos top")
        return
    print(f"\n== 3. Partidos por jugador y superficie en 24 meses hasta {ref} (solo top; jugadores por tramo)")
    hist = tt.player_surface_histogram(top_m, ref, 24, MIN_SURFACE)
    labels = [tt.bucket_label(a, z) for a, z in tt.BUCKETS]
    print(f"  {'circuito/superficie':<20} {'jug.':>5} {'part.':>6} " + " ".join(f"{x:>6}" for x in labels)
          + f"  ≥{MIN_SURFACE}   último año: ambos ≥{MIN_SURFACE}")
    for (tour, sup), v in hist.items():
        print(f"  {tour.upper() + ' ' + sup:<20} {v['jugadores']:>5} {v['partidos']:>6} "
              + " ".join(f"{v['buckets'][x]:>6}" for x in labels)
              + f"  {v['jugadores_min']:>4}   {v['partidos_ambos_min']}/{v['partidos_ultimo_año']} "
              f"({_pct(v['partidos_ambos_min'], v['partidos_ultimo_año'])})")


def main() -> int:
    report(read_docs())
    return 0


if __name__ == "__main__":
    sys.exit(main())
