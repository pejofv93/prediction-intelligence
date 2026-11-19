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
    assert c["evaluados"] == 2 and set(rows) == {"modelo_vs_liga", "modelo_vs_copa"}
    assert rows["modelo_vs_liga"] and rows["modelo_vs_liga"][0][1] == "ES"
    # el primer partido no tiene copas anteriores: sin referencia de copa; el segundo sí
    keys_copa = {r[7] for r in rows["modelo_vs_copa"]}
    assert len(keys_copa) == 1 and next(iter(keys_copa)).startswith("2425_ES_2025-01-15")
    assert mp.market_metrics(rows["modelo_vs_liga"])


def test_etiquetas_del_backtest_sin_estrella():
    met = {"corners_team|ALL": {"market": "corners_team", "div": "ALL", "ci_label": mp.LABEL_EDGE},
           "corners_total|ALL": {"market": "corners_total", "div": "ALL", "ci_label": mp.LABEL_REF},
           "corners_team|EN": {"market": "corners_team", "div": "EN", "ci_label": mp.LABEL_EDGE}}
    assert mc.labels_from_backtest(met) == {"corners_team": "medido en backtest", "corners_total": mp.LABEL_REF}


def test_diagnostico_de_amarillas():
    docs = [{"stats": {"home": {"yellows": 0, "fouls": 9}, "away": {"yellows": None, "fouls": 12, "reds": None,
                                                                    "corners": 4, "n_fields": 12}}},
            {"stats": {"home": {"yellows": 3}, "away": {"yellows": None, "n_fields": 0}}}]
    d = mc.yellow_diag(docs)
    assert d["sin_amarillas"] == 2 and d["sin_amarillas_con_faltas"] == 1 and d["sin_amarillas_campos_11+"] == 1
    assert d["con_amarillas"] == 2 and d["con_amarillas_igual_0"] == 1


def test_roja_ausente_cuenta_como_cero():
    doc = {"goals_home": 0, "goals_away": 0,
           "stats": {"home": {"corners": 4, "yellows": 2, "shots_on": 3, "shots_off": 2, "shots_blocked": 1, "fouls": 10},
                     "away": {"corners": 5, "yellows": None, "shots_on": 1, "shots_off": 2, "shots_blocked": 0, "fouls": 12}}}
    s = mc.cup_stats(doc)
    assert s["h"]["cards"] == 2            # roja ausente = 0
    assert s["a"]["cards"] is None         # sin amarillas no se inventa nada


# ── Directo: resultado, filas y estrellas ───────────────────────────────────────

def _hl_doc(**kw):
    d = {"status": "done", "finished": True, "date": "2026-12-16T20:00:00Z", "goals_home": 2, "goals_away": 0,
         "stats": {"home": {"corners": 6, "yellows": 2, "shots_on": 5, "shots_off": 4, "shots_blocked": 3,
                            "fouls": 11},
                   "away": {"corners": 3, "yellows": None, "reds": None, "shots_on": 2, "shots_off": 3,
                            "shots_blocked": 1, "fouls": 9}}}
    d.update(kw)
    return d


def test_resultado_de_copa_desde_hl_team_stats():
    r = mc.cup_result(_hl_doc())
    assert r["date"] == "2026-12-16" and r["h"]["goals"] == 2 and r["a"]["goals"] == 0
    assert r["h"]["cards"] == 2                 # roja ausente con amarillas presentes = 0
    assert r["a"]["cards"] is None              # amarilla ausente: sin dato, no se pone a 0
    assert mc.cup_result(_hl_doc(status="pending")) is None
    assert mc.cup_result(_hl_doc(goals_home=None)) is None
    assert mc.cup_result(None) is None


def test_filas_de_graduacion_por_pais():
    g = {"status": "graduada", "season": "2627", "country": "ES", "match_id": "ES_x",
         "lines": [{"market": "corners_team", "line": 4.5, "y": 1, "p_league": 0.5, "p_model": 0.6}]}
    assert mc.rows_from_cup_grades([g, {**g, "status": "anulada"}]) == [
        ("2627", "ES", "corners_team", 4.5, 1, 0.5, 0.6, "ES_x")]


def _live(market, n, ci):
    return {f"{market}|ALL": {"market": market, "div": "ALL", "n_matches": n, "ci_label": ci},
            f"{market}|ES": {"market": market, "div": "ES", "n_matches": n, "ci_label": mp.LABEL_EDGE}}


def test_estrella_solo_con_50_cruces_y_ic_en_directo():
    bt = {"corners_team": mc.LABEL_BACKTEST, "goals_total": mp.LABEL_REF}
    assert mc.final_labels(bt, None) == bt
    assert mc.final_labels(bt, _live("corners_team", 49, mp.LABEL_EDGE))["corners_team"] == mc.LABEL_BACKTEST
    assert mc.final_labels(bt, _live("corners_team", 50, mp.LABEL_EDGE))["corners_team"] == mp.LABEL_EDGE
    # 80 cruces pero el IC en directo no queda bajo 0: se queda "medido en backtest"
    assert mc.final_labels(bt, _live("corners_team", 80, mp.LABEL_REF))["corners_team"] == mc.LABEL_BACKTEST
    # "solo referencia" en el backtest no sube a estrella por el directo
    assert mc.final_labels(bt, _live("goals_total", 90, mp.LABEL_EDGE))["goals_total"] == mp.LABEL_REF
    assert mc.live_counts(_live("corners_team", 12, mp.LABEL_REF)) == {"corners_team": 12}
