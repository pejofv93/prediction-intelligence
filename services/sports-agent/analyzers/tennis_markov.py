"""
services/sports-agent/analyzers/tennis_markov.py

Cadena de Markov de un partido de tenis (tp-v1): punto → juego → set (con tie-break) → partido al
mejor de 3 o de 5. Puro y exacto (programación dinámica, sin simulación aleatoria).

Entrada: probabilidad de que A y B mantengan su saque (ha, hb). La probabilidad de punto al saque
de cada uno sale de invertir la fórmula del juego; solo hace falta para el tie-break.
Reglas: set a 6 con 2 de ventaja, 7-5, tie-break a 7 puntos (con 2 de ventaja) en el 6-6 de
TODOS los sets. El super tie-break del set decisivo de los Grand Slam (a 10, desde 2022) se
trata como uno a 7: tennis-data lo anota 7-6 y cuenta como un juego, igual que aquí. Los sets
largos sin tie-break (Roland Garros antes de 2022) quedan fuera del modelo.
Orden del saque: alternan por juego; en el tie-break saca primero quien tocaba en el juego 13 y
luego de dos en dos; el set siguiente lo empieza quien no empezó el tie-break, o sea, el primero
en sacar cambia si el set tuvo un número impar de juegos. Quién saca primero en el partido es un
sorteo: se promedian los dos casos.
"""
from __future__ import annotations

import math
from collections import defaultdict
from functools import lru_cache

_ROUND = 5          # redondeo de las probabilidades de saque para la caché


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def hold_from_point(p: float) -> float:
    """P(mantener el saque) con probabilidad p de ganar cada punto al saque."""
    q = 1 - p
    if p <= 0:
        return 0.0
    if q <= 0:
        return 1.0
    deuce = p * p / (p * p + q * q)
    return p ** 4 * (1 + 4 * q + 10 * q * q) + 20 * p ** 3 * q ** 3 * deuce


@lru_cache(maxsize=100_000)
def point_from_hold(h: float) -> float:
    """Inversa de hold_from_point (creciente) por bisección."""
    lo, hi = 1e-6, 1 - 1e-6
    for _ in range(50):
        mid = (lo + hi) / 2
        if hold_from_point(mid) < h:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _tb_a_serves(k: int) -> bool:
    """¿Saca A el punto k (desde 0) de un tie-break que empieza A? A, BB, AA, BB…"""
    return k == 0 or ((k - 1) // 2) % 2 == 1


def tiebreak_win(pa: float, pb: float) -> float:
    """P(A gana el tie-break a 7) si A saca primero. pa = P(A gana punto con su saque),
    pb = P(B gana punto con su saque)."""
    probs = {(0, 0): 1.0}
    win = 0.0
    at66 = 0.0
    for k in range(12):
        nxt: dict = defaultdict(float)
        for (a, b), pr in probs.items():
            if a + b != k:
                continue
            pw = pa if _tb_a_serves(k) else 1 - pb
            for na, nb, q in ((a + 1, b, pw), (a, b + 1, 1 - pw)):
                if na == 7:
                    win += pr * q
                elif nb == 7:
                    pass
                elif na == 6 and nb == 6:
                    at66 += pr * q
                else:
                    nxt[(na, nb)] += pr * q
        probs = nxt
    # Desde 6-6, por parejas de puntos (saca A y luego B): gana quien se lleve las dos.
    w2, l2 = pa * (1 - pb), (1 - pa) * pb
    return win + (at66 * w2 / (w2 + l2) if w2 + l2 > 0 else at66 * 0.5)


@lru_cache(maxsize=200_000)
def set_dist(ha: float, hb: float, a_first: bool) -> tuple[tuple[int, int, float], ...]:
    """Marcadores finales del set con su probabilidad: ((juegos A, juegos B, p), …).
    Con 6-6 → tie-break (anotado 7-6 / 6-7)."""
    probs = {(0, 0): 1.0}
    out: dict = defaultdict(float)
    for n in range(12):
        nxt: dict = defaultdict(float)
        a_serves = (n % 2 == 0) == a_first
        for (a, b), pr in probs.items():
            pw = ha if a_serves else 1 - hb
            for na, nb, q in ((a + 1, b, pw), (a, b + 1, 1 - pw)):
                if (na >= 6 or nb >= 6) and abs(na - nb) >= 2:
                    out[(na, nb)] += pr * q
                else:
                    nxt[(na, nb)] += pr * q
        probs = nxt
    p66 = probs.get((6, 6), 0.0)
    if p66:
        pa, pb = point_from_hold(ha), point_from_hold(hb)
        # El tie-break lo empieza quien sacaría el juego 13 (n=12, par): el que empezó el set.
        p_tb = tiebreak_win(pa, pb) if a_first else 1 - tiebreak_win(pb, pa)
        out[(7, 6)] += p66 * p_tb
        out[(6, 7)] += p66 * (1 - p_tb)
    return tuple((a, b, p) for (a, b), p in sorted(out.items()))


def _r(x: float) -> float:
    return round(min(max(x, 1e-4), 1 - 1e-4), _ROUND)


def match_dist(ha: float, hb: float, best_of: int = 3) -> dict:
    """
    Distribuciones del partido: p_a (gana A), total {juegos: p}, diff {juegos A − juegos B: p},
    sets {nº de sets: p}, p_tiebreak (al menos un tie-break), exp_games_a / exp_games_b.
    """
    ha, hb = _r(ha), _r(hb)
    need = best_of // 2 + 1
    total: dict = defaultdict(float)
    diff: dict = defaultdict(float)
    sets: dict = defaultdict(float)
    p_a = p_notb = ea = eb = 0.0
    for first in (True, False):
        # estado (sets A, sets B, ¿empieza A el set?) → [masa, total{}, diff{}, masa sin tb, eA, eB]
        states = {(0, 0, first): [0.5, {0: 0.5}, {0: 0.5}, 0.5, 0.0, 0.0]}
        for played in range(2 * need - 1):
            nxt: dict = {}
            for (sa, sb, af), (m, tot, dif, ntb, xa, xb) in states.items():
                if sa + sb != played:
                    continue
                for ga, gb, q in set_dist(ha, hb, af):
                    na, nb = (sa + 1, sb) if ga > gb else (sa, sb + 1)
                    naf = (not af) if (ga + gb) % 2 else af
                    st = nxt.get((na, nb, naf))
                    if st is None:
                        st = nxt[(na, nb, naf)] = [0.0, defaultdict(float), defaultdict(float), 0.0, 0.0, 0.0]
                    st[0] += m * q
                    for t, pt in tot.items():
                        st[1][t + ga + gb] += pt * q
                    for d, pd in dif.items():
                        st[2][d + ga - gb] += pd * q
                    if max(ga, gb) != 7 or min(ga, gb) != 6:
                        st[3] += ntb * q
                    st[4] += (xa + m * ga) * q
                    st[5] += (xb + m * gb) * q
            states = {}
            for key, st in nxt.items():
                sa, sb, _ = key
                if sa == need or sb == need:
                    m, tot, dif, ntb, xa, xb = st
                    p_a += m if sa == need else 0.0
                    sets[sa + sb] += m
                    for t, pt in tot.items():
                        total[t] += pt
                    for d, pd in dif.items():
                        diff[d] += pd
                    p_notb += ntb
                    ea += xa
                    eb += xb
                else:
                    states[key] = st
    return {"p_a": p_a, "total": dict(total), "diff": dict(diff), "sets": dict(sets),
            "p_tiebreak": 1 - p_notb, "exp_games_a": ea, "exp_games_b": eb}


def p_over(dist: dict, line: float) -> float:
    return sum(p for v, p in dist.items() if v > line)


@lru_cache(maxsize=200_000)
def set_summary(ha: float, hb: float, a_first: bool) -> tuple[float, float, float, float, float]:
    """(P(A gana el set y cambia quién empieza el siguiente), P(A gana y no cambia),
    P(B gana y cambia), P(B gana y no cambia), juegos esperados del set)."""
    out = [0.0, 0.0, 0.0, 0.0, 0.0]
    for ga, gb, p in set_dist(ha, hb, a_first):
        odd = (ga + gb) % 2 == 1
        out[(0 if ga > gb else 2) + (0 if odd else 1)] += p
        out[4] += (ga + gb) * p
    return tuple(out)


def match_win(ha: float, hb: float, best_of: int = 3) -> float:
    """Solo P(gana A): rápido, para resolver la asimetría del saque."""
    ha, hb = _r(ha), _r(hb)
    need = best_of // 2 + 1

    @lru_cache(maxsize=None)
    def f(sa: int, sb: int, af: bool) -> float:
        if sa == need:
            return 1.0
        if sb == need:
            return 0.0
        wc, wn, lc, ln, _ = set_summary(ha, hb, af)
        return (wc * f(sa + 1, sb, not af) + wn * f(sa + 1, sb, af)
                + lc * f(sa, sb + 1, not af) + ln * f(sa, sb + 1, af))

    return 0.5 * (f(0, 0, True) + f(0, 0, False))


def games_share(ha: float, hb: float) -> float:
    """Proporción esperada de juegos de A en un set (media de los dos órdenes de saque)."""
    ha, hb = _r(ha), _r(hb)
    num = den = 0.0
    for af in (True, False):
        for ga, gb, p in set_dist(ha, hb, af):
            num += ga * p
            den += (ga + gb) * p
    return num / den


def games_per_set(ha: float, hb: float) -> float:
    ha, hb = _r(ha), _r(hb)
    return 0.5 * (set_summary(ha, hb, True)[4] + set_summary(ha, hb, False)[4])


def abs_diff_per_set(ha: float, hb: float) -> float:
    """|juegos A − juegos B| esperado en un set (media de los dos órdenes de saque)."""
    ha, hb = _r(ha), _r(hb)
    return 0.5 * sum(abs(ga - gb) * p for af in (True, False) for ga, gb, p in set_dist(ha, hb, af))
