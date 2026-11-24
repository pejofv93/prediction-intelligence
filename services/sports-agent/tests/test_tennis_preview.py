"""Tenis tp-v1: Elo, factores encogidos, calibración, referencia, regresión y backtest
walk-forward por torneo (datos y jugadores inventados; sin red ni Firestore)."""
import pytest

from analyzers import match_preview as mp
from analyzers import tennis_preview as tp
from analyzers import tennis_top as tt
from tests.tennis_fake import make_docs


@pytest.fixture(scope="module")
def matches():
    return tt.flat_matches(make_docs(weeks=110, n_players=20, per_tournament=14))


def _m(w, l, games, date="2024-01-01", tour="atp", surface="Hard", bo=3, wrank=1, lrank=2, status="completed"):
    sets = [tuple(s) for s in games]
    return {"tid": "t", "tour": tour, "surface": surface, "best_of": bo, "date": date, "start": date,
            "w": f"{tour}|{w}", "l": f"{tour}|{l}", "wrank": wrank, "lrank": lrank, "sets": sets,
            "w_games": sum(a for a, _ in sets), "l_games": sum(b for _, b in sets),
            "total_games": sum(a + b for a, b in sets), "n_sets": len(sets),
            "tiebreaks": sum(1 for a, b in sets if max(a, b) == 7 and min(a, b) == 6),
            "status": status, "valid": status == "completed"}


def test_elo_general_y_por_superficie():
    e = tp.Elo()
    for _ in range(10):
        e.update("atp|A", "atp|B", "Clay")
        e.update("atp|B", "atp|A", "Hard")
    assert e.prob("atp|A", "atp|B", "Clay") > 0.5 > e.prob("atp|A", "atp|B", "Hard")
    # Sin hierba: solo el general (alterno, cerca de 0,5; el último partido lo ganó B)
    assert e.prob("atp|A", "atp|B", "Grass") == pytest.approx(1 / (1 + 10 ** ((e.r["atp|B"] - e.r["atp|A"]) / 400)))
    assert e.prob("atp|A", "atp|B", "Hard") + e.prob("atp|B", "atp|A", "Hard") == pytest.approx(1)


def test_factores_encogidos_y_ajustados_por_rival():
    ms = [_m("A", "B", [(7, 6), (7, 6)]) for _ in range(4)] + [_m("C", "D", [(6, 1), (6, 2)]) for _ in range(4)]
    f = tp.fit_factors(ms, k=8)
    assert f["g0"] == pytest.approx((26 * 4 + 15 * 4) / 16)
    assert f["delta"]["atp|A"] > 0 > f["delta"]["atp|C"]
    assert f["gamma"]["atp|C"] > f["gamma"]["atp|A"] > 0 > f["gamma"]["atp|B"]
    # Encogidos: con 4 partidos y k=8, menos de la mitad de lo observado
    assert abs(f["delta"]["atp|A"]) < (13 - f["g0"]) / 2
    assert f["n"]["atp|A"] == 4 and f["absd0"] == pytest.approx((1 * 8 + 5 * 4 + 4 * 4) / 16)


def test_valores_de_mercado_y_favorito_por_ranking():
    m = _m("A", "B", [(6, 4), (3, 6), (7, 6)], wrank=20, lrank=5)
    v = tp.outcome_values(m)
    assert v == {"total_games": 32, "sets": 3, "hcp_fav": 16 - 16, "tiebreak": 1}
    m2 = _m("A", "B", [(6, 1), (6, 2)], wrank=5, lrank=20)
    assert tp.outcome_values(m2)["hcp_fav"] == 9
    assert tp.outcome_values(_m("A", "B", [(6, 1)], wrank=None))["hcp_fav"] is None


def test_calibracion_reproduce_juegos_por_set(matches):
    elo, win = tp.build_state(matches, "2023-12-01")
    for tour in ("atp", "wta"):
        f = win.factors(tour, "Hard")
        c0, kappa, lam = win.level(tour, "Hard")
        p = [x for m, x in zip(win.ms, win.pre) if m["tour"] == tour and m["surface"] == "Hard"]
        import numpy as np
        pp = np.array([max(x, 1 - x) for x in p])
        mean = float(np.mean(tp.gps_at(c0, lam * tp.d_from_p(c0, pp))))
        if tp.C_GRID[0] < c0 < tp.C_GRID[-1]:
            assert mean == pytest.approx(f["g0"], abs=0.02)
        assert 0 <= kappa <= tp.KAPPA_MAX and tp.LAMBDA_RANGE[0] <= lam <= tp.LAMBDA_RANGE[1]


def test_ficha_coherente_y_muestra(matches):
    elo, win = tp.build_state(matches, "2023-12-01")
    a, b = "atp|InventadoATP00 A.", "atp|InventadoATP01 B."
    r = tp.predict(elo, win, a, b, "atp", "Hard", 3)
    assert r["model_version"] == "tp-v1" and r["group"] == "ATP-bo3"
    ls = r["lines"]
    assert set(ls) == set(tp.LINES["ATP-bo3"])
    tot = [ls["total_games"][x] for x in tp.LINES["ATP-bo3"]["total_games"]]
    assert tot == sorted(tot, reverse=True) and all(0 < x < 1 for x in tot)
    assert ls["hcp_fav"][2.5] >= ls["hcp_fav"][3.5] >= ls["hcp_fav"][4.5]
    assert r["exp_games_a"] + r["exp_games_b"] == pytest.approx(r["exp_total_games"], abs=0.02)
    # Al mejor de 5 el mismo favorito gana más
    r5 = tp.predict(elo, win, a, b, "atp", "Hard", 5)
    assert abs(r5["p_win_a"] - 0.5) >= abs(r["p_win_a"] - 0.5)
    assert set(r5["lines"]["sets"]) == {3.5, 4.5}
    # Jugador sin partidos → muestra insuficiente
    r0 = tp.predict(elo, win, a, "atp|Desconocido Z.", "atp", "Hard", 3)
    assert r0["low_sample"] and r0["n_surface"][1] == 0


def test_referencia_por_circuito_superficie_y_best_of():
    ms = [_m("A", "B", [(6, 4), (6, 4)]) for _ in range(40)] + [_m("A", "B", [(7, 6), (6, 7), (7, 6)])
                                                             for _ in range(10)]
    win = tp.Window(ms, [0.6] * len(ms), "2024-06-01")
    p, n = win.ref_rate("atp", "Hard", 3, "total_games", 22.5)
    assert n == 50 and p == pytest.approx(10 / 50)
    assert win.ref_rate("atp", "Hard", 3, "tiebreak", 0.5)[0] == pytest.approx(0.2)
    # Pocos en la superficie → circuito × best_of con todas las superficies
    ms2 = ms + [_m("A", "B", [(6, 0), (6, 0)], surface="Grass")]
    win2 = tp.Window(ms2, [0.6] * len(ms2), "2024-06-01")
    assert win2.ref_rate("atp", "Grass", 3, "total_games", 22.5)[1] == 51
    assert win2.ref_rate("wta", "Hard", 3, "total_games", 22.5) == (None, 0)


def test_regresion_juegos_totales():
    ms = ([_m("A", "B", [(6, 4), (7, 5)]) for _ in range(30)]
          + [_m("A", "B", [(6, 0), (6, 1)]) for _ in range(30)])
    pre = [0.55] * 30 + [0.95] * 30                         # parejos → más juegos
    win = tp.Window(ms, pre, "2024-06-01")
    assert win.reg_p_over("atp", 3, "Hard", 0.52, 15.5) > 0.9
    assert win.reg_p_over("atp", 3, "Hard", 0.97, 15.5) < 0.1
    assert tp.Window(ms[:5], pre[:5], "x").reg_p_over("atp", 3, "Hard", 0.5, 15.5) is None


def test_backtest_sin_mirar_el_futuro(matches):
    rows, rows_reg, cnt = tp.backtest(matches, "2023-09-01")
    assert cnt["torneos"] > 0 and cnt["evaluados"] > 0 and cnt["no_validos"] > 0
    assert rows and rows_reg and {r[2] for r in rows_reg} == {"total_games"}
    assert {r[1] for r in rows} == {"ATP-bo3", "ATP-bo5", "WTA-bo3"}
    assert all(0 <= r[5] <= 1 and 0 <= r[6] <= 1 and r[4] in (0, 1) for r in rows)
    # Cambiar el resultado de un partido evaluado no cambia su propia predicción.
    key = rows[0][7]
    tid, idx = key.split("#")
    ms = sorted(matches, key=lambda m: (m["date"], m["tid"]))
    target = ms[int(idx)]
    altered = [dict(m, sets=[(0, 6), (0, 6)], w_games=0, l_games=12, total_games=12, n_sets=2, tiebreaks=0)
               if m is target else m for m in matches]
    rows2, _, _ = tp.backtest(altered, "2023-09-01")
    p1 = {(r[2], r[3]): r[6] for r in rows if r[7] == key}
    p2 = {(r[2], r[3]): r[6] for r in rows2 if r[7] == key}
    assert p1 == p2
    met = mp.market_metrics(rows)
    assert {"total_games|ALL", "sets|ALL", "hcp_fav|ALL", "tiebreak|ALL"} <= set(met)
    assert met["hcp_fav|ALL"]["label"] in (tp.LABEL_EDGE, tp.LABEL_REF)
