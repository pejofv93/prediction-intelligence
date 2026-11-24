"""Tenis tp-v1: cadena de Markov punto → juego → set → partido (exacta, sin azar)."""
import math
import random

import pytest

from analyzers import tennis_markov as tm


def test_juego_y_su_inversa():
    assert tm.hold_from_point(0.5) == pytest.approx(0.5)
    assert tm.hold_from_point(0.65) == pytest.approx(0.8296, abs=1e-4)
    for h in (0.55, 0.7, 0.85, 0.95):
        assert tm.hold_from_point(tm.point_from_hold(h)) == pytest.approx(h, abs=1e-9)


def test_tie_break_simetrico_y_orden_de_saque():
    assert tm.tiebreak_win(0.65, 0.65) == pytest.approx(0.5)
    assert tm.tiebreak_win(0.7, 0.6) > 0.5 > tm.tiebreak_win(0.6, 0.7)
    assert [tm._tb_a_serves(k) for k in range(7)] == [True, False, False, True, True, False, False]


def test_set_marcadores_posibles_y_suma_uno():
    d = tm.set_dist(0.8, 0.7, True)
    scores = {(a, b) for a, b, _ in d}
    assert scores <= {(6, i) for i in range(5)} | {(i, 6) for i in range(5)} | {(7, 5), (5, 7), (7, 6), (6, 7)}
    assert sum(p for *_, p in d) == pytest.approx(1)


@pytest.mark.parametrize("bo,n_sets", [(3, (2, 3)), (5, (3, 4, 5))])
def test_partido_simetrico(bo, n_sets):
    d = tm.match_dist(0.8, 0.8, bo)
    assert d["p_a"] == pytest.approx(0.5)
    assert set(d["sets"]) == set(n_sets) and sum(d["sets"].values()) == pytest.approx(1)
    assert sum(d["total"].values()) == pytest.approx(1) and sum(d["diff"].values()) == pytest.approx(1)
    assert sum(x * p for x, p in d["diff"].items()) == pytest.approx(0, abs=1e-9)
    assert d["exp_games_a"] == pytest.approx(d["exp_games_b"])
    assert 0 < d["p_tiebreak"] < 1


def test_favorito_gana_mas_al_mejor_de_5_y_juegos_coherentes():
    d3, d5 = tm.match_dist(0.85, 0.7, 3), tm.match_dist(0.85, 0.7, 5)
    assert 0.5 < d3["p_a"] < d5["p_a"] < 1
    assert d3["p_a"] == pytest.approx(tm.match_win(0.85, 0.7, 3))
    exp_tot = sum(t * p for t, p in d3["total"].items())
    assert exp_tot == pytest.approx(d3["exp_games_a"] + d3["exp_games_b"])
    assert sum(x * p for x, p in d3["diff"].items()) == pytest.approx(d3["exp_games_a"] - d3["exp_games_b"])
    assert tm.p_over(d3["total"], 0.5) == pytest.approx(1)


def test_coincide_con_simulacion_monte_carlo():
    """La programación dinámica cuadra con una simulación punto a punto independiente."""
    rng = random.Random(3)
    ha, hb = 0.78, 0.66
    pa, pb = tm.point_from_hold(ha), tm.point_from_hold(hb)

    def game(p):
        a = b = 0
        while True:
            if rng.random() < p:
                a += 1
            else:
                b += 1
            if a >= 4 and a - b >= 2:
                return True
            if b >= 4 and b - a >= 2:
                return False

    def tiebreak(a_first):
        a = b = 0
        k = 0
        while True:
            a_srv = tm._tb_a_serves(k) == a_first
            win = rng.random() < pa if a_srv else rng.random() >= pb
            a, b = (a + 1, b) if win else (a, b + 1)
            k += 1
            if (a >= 7 or b >= 7) and abs(a - b) >= 2:
                return a > b

    n, wins, tot, tbs = 20000, 0, 0, 0
    for i in range(n):
        first = i % 2 == 0
        sa = sb = 0
        any_tb = False
        while sa < 2 and sb < 2:
            a = b = 0
            g = 0
            while True:
                if a == 6 and b == 6:
                    any_tb = True
                    w = tiebreak(first)
                    a, b = (7, 6) if w else (6, 7)
                    break
                a_srv = (g % 2 == 0) == first
                w = game(pa) if a_srv else not game(pb)
                a, b = (a + 1, b) if w else (a, b + 1)
                g += 1
                if (a >= 6 or b >= 6) and abs(a - b) >= 2:
                    break
            tot += a + b
            sa, sb = (sa + 1, sb) if a > b else (sa, sb + 1)
            if (a + b) % 2:
                first = not first
        wins += sa == 2
        tbs += any_tb
    d = tm.match_dist(ha, hb, 3)
    se = math.sqrt(0.25 / n)
    assert abs(wins / n - d["p_a"]) < 4 * se
    assert abs(tbs / n - d["p_tiebreak"]) < 4 * se
    assert abs(tot / n - sum(t * p for t, p in d["total"].items())) < 0.15
