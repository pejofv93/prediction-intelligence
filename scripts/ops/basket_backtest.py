"""SOLO LECTURA. Backtest walk-forward por jornada del modelo de baloncesto bp-v1
(analyzers/basket_preview.py) con los partidos ya guardados en Firestore. 0 peticiones a APIs.

Liga (env LIGA, por defecto EUR):
  EUR → eur_games (Euroliga, live.euroleague.net)
  NBA → nba_games (ESPN, cuando se cargue)
  ACB → hlb_games con league = "ACB" (Highlightly baloncesto, cuando se cargue)

La temporada más antigua es de calentamiento: de ella salen las líneas fijas (percentiles
25/50/75 de la liga, salvo que ya estén en FIXED_LINES) y los primeros residuos. Se evalúan las
demás. Mercados: puntos, rebotes, asistencias y triples, por equipo y total. Referencias: media
de la liga y media del equipo hasta la fecha.

No escribe nada. Log: solo agregados (recuentos, líneas de la liga, Brier, IC95, n por mercado):
nada de equipos ni partidos.
"""
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, ROOT)

from analyzers import basket_preview as bp  # noqa: E402

SOURCES = {"EUR": ("eur_games", None), "NBA": ("nba_games", None),
           "ACB": ("hlb_games", ("league", "==", "ACB"))}


def load_docs(league: str) -> list[dict]:
    from shared.firestore_client import iter_all
    coll, where = SOURCES[league]
    return [d.to_dict() or {} for d in iter_all(coll, where=where)]


def split_seasons(games: list[dict]) -> tuple[str, list[str]]:
    """(temporada de calentamiento, temporadas evaluadas): la más antigua y el resto."""
    seasons = sorted({g["season"] for g in games}, key=lambda s: int("".join(c for c in s if c.isdigit()) or 0))
    if len(seasons) < 2:
        raise ValueError(f"hacen falta al menos 2 temporadas; hay {len(seasons)}")
    return seasons[0], seasons[1:]


def fmt_metrics(name: str, met: dict) -> list[str]:
    out = [f"\n[{name}]  mercado: partidos · líneas · Brier ref · Brier modelo · diferencia [IC95] · etiqueta"]
    for v in sorted((v for v in met.values() if v["div"] != "ALL"), key=lambda v: v["market"]):
        lo = "—" if v["ci_lo"] is None else f"{v['ci_lo']:+.4f}"
        hi = "—" if v["ci_hi"] is None else f"{v['ci_hi']:+.4f}"
        out.append(f"  {v['market']:11} {v['n_matches']:5d} · {v['n_lines']:6d} · {v['brier_league']:.4f} · "
                   f"{v['brier_model']:.4f} · {v['delta']:+.4f} [{lo}, {hi}] · {v['ci_label']}")
    return out


def run(games: list[dict], league: str) -> list[str]:
    """Todo el cálculo sobre partidos ya convertidos; devuelve las líneas del informe."""
    from analyzers.match_preview import market_metrics
    warm, evals = split_seasons(games)
    lines_out = [f"Liga {league}: {len(games)} partidos · calentamiento {warm} · evaluadas {evals}",
                 "Partidos por temporada: " + str(dict(sorted(Counter(g["season"] for g in games).items()))),
                 "Con prórroga: " + str(sum(1 for g in games if g["ot"]))]
    lines = bp.lines_for(league, [g for g in games if g["season"] == warm])
    origin = "aprobadas (FIXED_LINES)" if bp.FIXED_LINES.get(league) else f"percentiles 25/50/75 de {warm}"
    lines_out.append(f"\nLíneas fijas ({origin}):")
    for mk in sorted(lines):
        lines_out.append(f"  {mk:11} {lines[mk]}")
    hist = bp.History(games)
    counts = Counter()
    rows = bp.backtest_rows(hist, league, evals, lines, counts=counts)
    lines_out.append(f"\nRecuentos: {dict(counts)}")
    lines_out.append(f"Sesgo medio del total (real − modelo): {bp.mean_bias(hist, league, evals)}")
    for ref, key in (("modelo frente a la media de la liga", "vs_liga"),
                     ("modelo frente a la media del equipo", "vs_equipo")):
        if not rows[key]:
            lines_out.append(f"\n[{ref}] sin filas")
            continue
        lines_out += fmt_metrics(ref, market_metrics(rows[key]))
    by_season = Counter(r[0] for r in rows["vs_liga"])
    lines_out.append(f"\nLíneas evaluadas por temporada: {dict(sorted(by_season.items()))}")
    return lines_out


def main() -> int:
    league = (os.environ.get("LIGA") or "EUR").upper()
    if league not in SOURCES:
        print(f"LIGA debe ser una de {sorted(SOURCES)}")
        return 1
    docs = load_docs(league)
    games = bp.games_from_docs(docs, league)
    print(f"Docs leídos: {len(docs)} · utilizables: {len(games)}")
    try:
        for line in run(games, league):
            print(line)
    except ValueError as e:
        print(f"No se puede evaluar: {e}")
        return 1
    print("\nSin escribir nada (solo lectura).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
