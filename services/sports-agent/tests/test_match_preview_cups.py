"""Backtest de copas con mp-v1: mapeo de equipos, filtros y filas walk-forward (sin red)."""
from collections import Counter
from datetime import date, timedelta

from analyzers import match_preview as mp
from analyzers import match_preview_cups as mc
from collectors.highlightly_players import team_similarity


def test_season_of():
    assert mc.season_of("2025-01-08") == "2425" and mc.season_of("2025-08-20") == "2526"


def test_cup_stats_suma_tarjetas_y_remates():
    doc = {"goals_home": 2, "goals_away": 1,
           "stats": {"home": {"corners": 6, "yellows": 2, "reds": 1, "shots_on": 5, "shots_off": 4,
                              "shots_blocked": 3, "fouls": 11},
                     "away": {"corners": 3, "yellows": 1, "reds": 0, "shots_on": 2, "shots_off": 3,
                              "shots_blocked": None, "fouls": 9}}}
    s = mc.cup_stats(doc)
    assert s["h"] == {"goals": 2, "corners": 6, "cards": 3, "shots": 12, "sot": 5, "fouls": 11}
    assert s["a"]["shots"] is None and s["a"]["cards"] == 1
    assert mc.cup_stats({"goals_home": None, "goals_away": 1, "stats": doc["stats"]}) is None


def test_ids_por_mayoria_con_la_fila_del_csv():
    csv = {("SP1", "2025-10-01"): [{"home": "Real Madrid", "away": "Ath Madrid"}],
           ("SP1", "2025-10-08"): [{"home": "Ath Madrid", "away": "Betis"}]}
    hl = [{"div": "SP1", "date": "2025-10-01", "home_team": "Real Madrid", "home_team_id": 1,
           "away_team": "Atletico Madrid", "away_team_id": 2},
          {"div": "SP1", "date": "2025-10-08", "home_team": "Atletico Madrid", "home_team_id": 2,
           "away_team": "Real Betis", "away_team_id": 3},
          {"div": "SP1", "date": "2025-10-09", "home_team": "X", "home_team_id": 9,
           "away_team": "Y", "away_team_id": 8}]
    ids = mc.hl_id_names(hl, csv, team_similarity, min_score=0.5)
    assert ids[1] == "Real Madrid" and ids[2] == "Ath Madrid" and ids[3] == "Betis" and 9 not in ids


def _league(div="SP1", season="2425", teams=("A", "B", "C", "D"), rounds=6, start=date(2024, 8, 15)):
    ms, d = [], start
    for _ in range(rounds):
        for i, h in enumerate(teams):
            a = teams[(i + 1) % len(teams)]
            ms.append({"div": div, "season": season, "date": d.isoformat(), "home": h, "away": a,
                       "h": {"goals": 2, "corners": 6, "cards": 2, "shots": 13, "sot": 5, "fouls": 11},
                       "a": {"goals": 1, "corners": 4, "cards": 2, "shots": 10, "sot": 3, "fouls": 12}})
            d += timedelta(days=1)
    return ms


def _cup_doc(i, lid=122477, home=1, away=2, day="2025-01-08", **kw):
    return {"match_id": i, "league_id": lid, "status": "done", "finished": True, "date": day,
            "home_team_id": home, "away_team_id": away, "goals_home": 1, "goals_away": 1,
            "stats": {s: {"corners": 7, "yellows": 3, "reds": 0, "shots_on": 6, "shots_off": 5,
                          "shots_blocked": 2, "fouls": 14} for s in ("home", "away")}, **kw}


def test_filtros_y_filas_walk_forward():
    hist = mp.History(_league() + _league(div="SP2", teams=("E", "F", "G", "H")))
    tdiv = mc.team_divs(hist)
    ids = {1: "A", 2: "B", 3: "E", 4: "Z"}
    docs = [_cup_doc(1), _cup_doc(2, day="2025-01-15", home=2, away=1),
            _cup_doc(3, home=1, away=3),                       # SP1 contra SP2 → fuera
            _cup_doc(4, lid=473940),                           # supercopa → fuera
            _cup_doc(5, home=1, away=4),                       # equipo sin liga → fuera
            _cup_doc(6, status="empty")]
    c = Counter()
    cups = mc.cup_matches(docs, ids, tdiv, c)
    assert len(cups) == 2 and c["descartado_distinta_division"] == 1 and c["descartado_supercopa"] == 1
    assert c["descartado_equipo_sin_liga_esa_temporada"] == 1 and c["descartado_sin_detalle"] == 1
    rows = mc.backtest_cup_rows(hist, cups, c)
    assert c["evaluados"] == 2
    assert rows["modelo_vs_liga"] and rows["modelo_vs_liga"][0][1] == "ES"
    # el primer partido no tiene copas anteriores: sin referencia de copa; el segundo sí
    keys_copa = {r[7] for r in rows["modelo_vs_copa"]}
    assert len(keys_copa) == 1 and next(iter(keys_copa)).startswith("2425_ES_2025-01-15")
    # factor de copa: en el primer partido vale 1 (filas iguales), en el segundo ya no
    first = [r for r in rows["factor_vs_liga"] if "2025-01-08" in r[7]]
    same = [r for r in rows["modelo_vs_liga"] if "2025-01-08" in r[7]]
    assert [r[6] for r in first] == [r[6] for r in same]
    assert c["factor_copa"]["corners_h"] > 1          # 7 córners en copa frente a ~6 esperados
    assert mp.market_metrics(rows["modelo_vs_liga"])
