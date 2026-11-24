"""Datos inventados de tennis_tournaments para los tests de tenis top (jugadores inventados).

Cada jugador tiene una fuerza y un saque propios; los partidos se simulan juego a juego con
probabilidad de mantener el saque, así que los juegos por set y los tie-breaks son coherentes.
Docs con la misma forma que escribe collectors/tennis_data.py (sin cuotas).
"""
import random
from datetime import date, timedelta

SURFACES = ("Hard", "Clay", "Hard", "Grass", "Hard", "Clay")
SERIES = {"atp": ("ATP250", "ATP500", "Masters 1000", "Grand Slam"),
          "wta": ("WTA250", "WTA500", "WTA1000", "Grand Slam")}


def players(tour: str, n: int, rng: random.Random) -> list[dict]:
    return [{"name": f"Inventado{tour.upper()}{i:02d} {chr(65 + i % 26)}.",
             "skill": rng.gauss(0, 0.45), "serve": rng.gauss(0, 0.25)} for i in range(n)]


def _game(p_hold: float, rng) -> bool:
    return rng.random() < p_hold


def _set(h1: float, h2: float, first1: bool, rng) -> tuple[int, int, bool]:
    a = b = 0
    srv1 = first1
    while True:
        if a == 6 and b == 6:
            return (7, 6, False) if rng.random() < 0.5 + (h1 - h2) else (6, 7, False)
        won1 = _game(h1, rng) if srv1 else not _game(h2, rng)
        a, b = (a + 1, b) if won1 else (a, b + 1)
        srv1 = not srv1
        if (a >= 6 or b >= 6) and abs(a - b) >= 2:
            return a, b, srv1
        if a == 7 or b == 7:
            return a, b, srv1


def play(p1: dict, p2: dict, base: float, bo: int, rng) -> list[tuple[int, int]]:
    """Sets (juegos p1, juegos p2) de un partido completo."""
    import math
    lg = math.log(base / (1 - base))
    h1 = 1 / (1 + math.exp(-(lg + p1["serve"] + p1["skill"] - p2["skill"])))
    h2 = 1 / (1 + math.exp(-(lg + p2["serve"] + p2["skill"] - p1["skill"])))
    need = bo // 2 + 1
    sets, s1, s2, first1 = [], 0, 0, True
    while s1 < need and s2 < need:
        a, b, _ = _set(h1, h2, first1, rng)
        sets.append((a, b))
        s1, s2 = (s1 + 1, s2) if a > b else (s1, s2 + 1)
        first1 = not first1 if (a + b) % 2 else first1
    return sets


def make_docs(start: str = "2022-01-03", weeks: int = 104, seed: int = 7, n_players: int = 24,
              per_tournament: int = 12, extra: bool = True) -> list[dict]:
    """Un torneo por semana y circuito (superficie rotando; bo5 en los Grand Slam ATP), con
    `per_tournament` partidos entre jugadores al azar. Con extra=True añade una United Cup y unos
    Juegos Olímpicos (fuera del top) y un partido retirado y un walkover por torneo."""
    rng = random.Random(seed)
    pool = {t: players(t, n_players, rng) for t in ("atp", "wta")}
    base = {"Hard": 0.78, "Clay": 0.72, "Grass": 0.83}
    d0 = date.fromisoformat(start)
    docs = []
    for wk in range(weeks):
        day = d0 + timedelta(days=7 * wk)
        surface = SURFACES[(wk // 4) % len(SURFACES)]
        for tour in ("atp", "wta"):
            series = SERIES[tour][wk % 4]
            bo = 5 if (tour == "atp" and series == "Grand Slam") else 3
            matches = []
            for i in range(per_tournament):
                p1, p2 = rng.sample(pool[tour], 2)
                sets = play(p1, p2, base[surface] - (0.06 if tour == "wta" else 0), bo, rng)
                w1 = sum(1 for a, b in sets if a > b)
                w, lo = (p1, p2) if w1 > len(sets) - w1 else (p2, p1)
                ws = sets if w is p1 else [(b, a) for a, b in sets]
                status, comment, valid = "completed", "Completed", True
                if extra and i == per_tournament - 1:
                    status, comment, valid, ws = "retired", "Retired", False, ws[:1] + [(2, 1)]
                if extra and i == per_tournament - 2:
                    status, comment, valid, ws = "walkover", "Walkover", False, []
                matches.append({
                    "date": (day + timedelta(days=i % 6)).isoformat(), "round": f"R{i % 4}",
                    "winner": w["name"], "loser": lo["name"],
                    "wrank": pool[tour].index(w) + 1, "lrank": pool[tour].index(lo) + 1,
                    "sets": [list(s) for s in ws], "n_sets": len(ws),
                    "total_games": sum(a + b for a, b in ws),
                    "tiebreaks": sum(1 for a, b in ws if max(a, b) == 7 and min(a, b) == 6),
                    "status": status, "comment": comment, "valid": valid,
                })
            docs.append({"tour": tour, "year": day.year, "num": wk + 1, "tournament": f"Torneo {wk + 1}",
                         "location": "Ciudad", "series": series, "surface": surface, "court": "Outdoor",
                         "best_of": bo, "matches": matches, "start": matches[0]["date"],
                         "end": max(m["date"] for m in matches), "n_matches": len(matches)})
    if extra:
        for name, series in (("United Cup", "ATP250"), ("Olympics", "Olympics")):
            docs.append({"tour": "atp", "year": d0.year, "num": 900, "tournament": name, "location": "Ciudad",
                         "series": series, "surface": "Hard", "best_of": 3, "start": start, "end": start,
                         "matches": [{"date": start, "round": "RR", "winner": pool["atp"][0]["name"],
                                      "loser": pool["atp"][1]["name"], "sets": [[6, 4], [6, 4]], "n_sets": 2,
                                      "total_games": 20, "tiebreaks": 0, "status": "completed", "valid": True}]})
    return docs
