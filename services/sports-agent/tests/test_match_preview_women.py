"""mp-w1 (fútbol femenino): adaptador de hl_team_stats, dispersión, líneas y backtest."""
import random

from analyzers import match_preview as mp
from analyzers import match_preview_women as mw


def _doc(lid, season, date, h, a, gh, ga, stats=True, **kw):
    side = lambda c: {"corners": c, "fouls": 10, "yellows": 1, "reds": 0, "shots_on": 4, "shots_off": 5,
                      "shots_blocked": 2}
    d = {"group": "women", "league_id": lid, "season": season, "date": date, "finished": True,
         "home_team_id": h, "away_team_id": a, "goals_home": gh, "goals_away": ga,
         "status": "done" if stats else "pending"}
    if stats:
        d["stats"] = {"home": side(5), "away": side(3), "complete": True}
    d.update(kw)
    return d


def test_adaptador():
    docs = [_doc(121626, 2025, "2025-10-01", 1, 2, 3, 0),
            _doc(121626, 2025, "2025-10-02", 3, 4, 1, 1, stats=False),
            _doc(447559, 2024, "2024-10-01", 1, 2, 1, 0),                 # Champions: fuera
            _doc(38228, 2025, "2025-10-03", 5, 6, None, None),            # sin marcador: fuera
            _doc(38228, 2025, "2025-10-04", 5, 6, 1, 0, finished=False)]  # sin terminar: fuera
    ms = mw.matches_from_hl(docs)
    assert len(ms) == 2
    m = ms[0]
    assert (m["div"], m["season"], m["home"]) == ("WLF", "2526", "1")
    assert m["h"] == {"corners": 5, "fouls": 10, "sot": 4, "shots": 11, "cards": 1, "goals": 3}
    assert ms[1]["h"]["corners"] is None and ms[1]["h"]["goals"] == 1
    assert mw.coverage(ms)[("WLF", "2526")] == {"matches": 2, "goals": 2, "corners": 1, "cards": 1,
                                                "shots": 1, "sot": 1, "fouls": 1}
    assert mw.season_code(2026) == "2627"


def _league(seasons=("2324", "2425", "2526"), teams=10, seed=1, nb=False):
    rnd = random.Random(seed)
    out = []
    for season in seasons:
        y0 = 2000 + int(season[:2])
        day = 0
        for _ in range(1):          # una vuelta (i local de j y j local de i ya son ida y vuelta)
            for i in range(teams):
                for j in range(teams):
                    if i == j:
                        continue
                    day += 1
                    date = f"{y0}-09-01T{day:05d}"          # orden cronológico por texto
                    def draw(mean):
                        if nb:   # binomial negativa por mezcla gamma-Poisson (r = 5)
                            mean = rnd.gammavariate(5, mean / 5)
                        # Poisson por inversión
                        L, k, p = 2.718281828 ** -mean, 0, 1.0
                        while True:
                            p *= rnd.random()
                            if p <= L:
                                return k
                            k += 1
                    h = {s: draw(5 if s != "goals" else 1.5) for s in mp.STATS}
                    a = {s: draw(4 if s != "goals" else 1.1) for s in mp.STATS}
                    out.append({"div": "WLF", "season": season, "date": date,
                                "home": str(i), "away": str(j), "h": h, "a": a})
    return out


def test_dispersion_detecta_sobredispersion():
    pois = mw.history(_league(nb=False))
    over = mw.history(_league(nb=True))
    dp, _ = mw.fit_dispersion(pois)
    do, det = mw.fit_dispersion(over)
    # Con gamma-Poisson r=5 la r estimada es finita y pequeña; con Poisson, enorme o sin sobredispersión
    assert do[("corners", "team")] < 15
    assert dp.get(("corners", "team")) is None or dp[("corners", "team")] > do[("corners", "team")] * 3
    assert det["corners_team"]["by_div"]["WLF"]["n"] > 0


def test_lineas_centradas_y_goles_fijos():
    ms = _league(nb=False)
    lines = mw.fit_lines(ms)
    assert lines["goals"] == mw.GOAL_LINES
    rates = mw.line_rates(ms, lines)
    for s in ("corners", "shots"):
        vals = list(rates[s]["team"].values())
        assert min(vals) >= 0.15 and max(vals) <= 0.85 and len(vals) >= 3


def test_backtest_produce_metricas_por_liga_y_todas():
    ms = _league(nb=False)
    hist = mw.history(ms)
    lines = mw.fit_lines(ms)
    disp, _ = mw.fit_dispersion(hist)
    rows, met = mw.backtest(hist, lines, disp, seasons=("2526",))
    assert rows and "goals_total|WLF" in met and "goals_total|ALL" in met
    assert all(r[1] == "WLF" and r[0] == "2526" for r in rows)


def test_history_mp_v1_sin_cambios():
    # La temporada anterior de otra división de fdco sigue contando con el History por defecto
    h = mp.History([])
    assert "SP1" in h.prev_divs and "SP2" in h.prev_divs
    assert mw.history([]).prev_divs == frozenset(mw.WOMEN_DIVS.values())
