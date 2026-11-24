"""
services/sports-agent/analyzers/tennis_preview.py

Tenis top en Tendencias (tp-v1) — modelo, referencia y backtest walk-forward. Puro: sin
Firestore, sin red, sin escribir nada. Datos: partidos planos de analyzers/tennis_top.py
(flat_matches de tennis_tournaments, solo categorías top). Sin cuotas.

Modelo tp-v1 (plan del 24-nov-2026):
 1. Probabilidad de ganar: Elo general + Elo por superficie (K = 250/(n+5)^0,4), mezclados con
    peso de la superficie n_s/(n_s+25) (n_s = partidos del jugador en esa superficie). Se lee
    como probabilidad al mejor de 3; al mejor de 5 sale de la cadena de Markov.
 2. Factores por jugador y superficie en los 24 meses anteriores (solo partidos válidos),
    ajustados por rival (3 pasadas) y encogidos hacia la media del circuito con 8 partidos
    ficticios:
      δ «tendencia a mantener el saque» = juegos por set (el tie-break cuenta 13) por encima de la
        media; el nivel del partido suma los dos δ;
      γ «fuerza en juegos» = proporción de juegos ganados por encima de 0,5; la del partido es
        0,5 + γA − γB.
 3. Probabilidad de mantener el saque: logit(hA) = c + d/2, logit(hB) = c − d/2.
      c = c0 + κ·(δA + δB) y d = (1 − 0,3)·λ·d(Elo) + 0,3·d(juegos), donde d(Elo) es la asimetría
        que da al mejor de 3 la probabilidad del Elo y d(juegos) la que da la proporción de
        juegos 0,5 + γA − γB. c0 y λ, por circuito y superficie, se calibran en la ventana para
        que el modelo reproduzca, con el Elo previo de cada partido, la media de juegos por set y
        la de |juegos A − juegos B| por set; κ convierte juegos por set en nivel de saque.
 4. Cadena de Markov (analyzers/tennis_markov.py) → juegos totales, nº de sets, hándicap de
    juegos del favorito (por ranking), algún tie-break y juegos de cada jugador (estos, solo
    referencia).
Alternativa: regresión de juegos totales sobre |p − 0,5| (Elo), arcilla y hierba, por circuito
y best_of, con la distribución empírica de los residuos (solo juegos totales).
Referencia: frecuencia de cada línea por circuito × superficie × best_of en los 24 meses
previos (si hay menos de 30 partidos, circuito × best_of en todas las superficies).

Muestra: un jugador con menos de 8 partidos válidos en la superficie en los 24 meses → «muestra
insuficiente» (se calcula, no se resalta; el backtest no lo evalúa).
Etiqueta: «ventaja medida» solo si el IC95 por partido de Brier(modelo) − Brier(referencia)
queda entero bajo 0 (mismo criterio que mp-v1: analyzers/match_preview.market_metrics).
"""
from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Iterable, Optional

import numpy as np

from analyzers import tennis_markov as tm

MODEL_VERSION = "tp-v1"
WINDOW_DAYS = 730             # 24 meses
SHRINK_K = 8.0
MIN_SURFACE = 8
MIN_REF = 30
MIN_REG = 30
ELO_INIT = 1500.0
SURF_HALF = 25.0
W_GAMES = 0.3
ITERS = 3
ELO_STATUSES = ("completed", "retired")

LINES: dict[str, dict[str, tuple[float, ...]]] = {
    "ATP-bo3": {"total_games": (20.5, 21.5, 22.5, 23.5), "sets": (2.5,),
                "hcp_fav": (2.5, 3.5, 4.5), "tiebreak": (0.5,)},
    "ATP-bo5": {"total_games": (36.5, 37.5, 38.5, 39.5), "sets": (3.5, 4.5),
                "hcp_fav": (2.5, 3.5, 4.5), "tiebreak": (0.5,)},
    "WTA-bo3": {"total_games": (19.5, 20.5, 21.5, 22.5), "sets": (2.5,),
                "hcp_fav": (2.5, 3.5, 4.5), "tiebreak": (0.5,)},
}
MARKETS = ("total_games", "sets", "hcp_fav", "tiebreak")
LABEL_EDGE, LABEL_REF, LABEL_LOW_SAMPLE = "ventaja medida", "solo referencia", "muestra insuficiente"


def group_key(tour: str, best_of: int) -> str:
    return f"{tour.upper()}-bo{best_of}"


def days_before(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) - timedelta(days=days)).isoformat()


# ── Elo ───────────────────────────────────────────────────────────────────────

def _k(n: int) -> float:
    return 250.0 / (n + 5) ** 0.4


class Elo:
    """Elo general y por superficie (claves de jugador '{tour}|{nombre}')."""

    def __init__(self):
        self.r: dict[str, float] = {}
        self.n: dict[str, int] = {}
        self.rs: dict[tuple[str, str], float] = {}
        self.ns: dict[tuple[str, str], int] = {}

    def rating(self, p: str, surface: str) -> float:
        ns = self.ns.get((p, surface), 0)
        w = ns / (ns + SURF_HALF)
        return w * self.rs.get((p, surface), ELO_INIT) + (1 - w) * self.r.get(p, ELO_INIT)

    def prob(self, a: str, b: str, surface: str) -> float:
        return 1 / (1 + 10 ** ((self.rating(b, surface) - self.rating(a, surface)) / 400))

    def update(self, w: str, l: str, surface: str) -> None:
        for rat, cnt, kw, kl in ((self.r, self.n, w, l), (self.rs, self.ns, (w, surface), (l, surface))):
            rw, rl = rat.get(kw, ELO_INIT), rat.get(kl, ELO_INIT)
            e = 1 / (1 + 10 ** ((rl - rw) / 400))
            rat[kw] = rw + _k(cnt.get(kw, 0)) * (1 - e)
            rat[kl] = rl - _k(cnt.get(kl, 0)) * (1 - e)
            cnt[kw] = cnt.get(kw, 0) + 1
            cnt[kl] = cnt.get(kl, 0) + 1


# ── Tablas (c, d) de la cadena de Markov ──────────────────────────────────────

C_GRID = np.round(np.arange(0.0, 3.5001, 0.1), 4)
D_GRID = np.round(np.arange(0.0, 6.0001, 0.1), 4)
_TABLES: dict[str, np.ndarray] = {}


def tables() -> dict[str, np.ndarray]:
    """P(gana A al mejor de 3), proporción de juegos de A, juegos por set y |juegos A − juegos B|
    por set en la rejilla c × d (d ≥ 0; con d < 0 se usa la simetría). Los juegos por set son
    simétricos en c (cambiar c por −c solo cambia quién gana cada juego), así que basta c ≥ 0."""
    if not _TABLES:
        shape = (len(C_GRID), len(D_GRID))
        pw, sh, gp, ad = (np.zeros(shape) for _ in range(4))
        for i, c in enumerate(C_GRID):
            for j, d in enumerate(D_GRID):
                ha, hb = tm.sigmoid(c + d / 2), tm.sigmoid(c - d / 2)
                pw[i, j] = tm.match_win(ha, hb, 3)
                sh[i, j] = tm.games_share(ha, hb)
                gp[i, j] = tm.games_per_set(ha, hb)
                ad[i, j] = tm.abs_diff_per_set(ha, hb)
        # Monótonas en d (evita empates numéricos en np.interp)
        pw = np.maximum.accumulate(pw, axis=1)
        sh = np.maximum.accumulate(sh, axis=1)
        _TABLES.update(pwin=pw, share=sh, gps=gp, absd=ad)
    return _TABLES


def _row(name: str, c: float) -> np.ndarray:
    t = tables()[name]
    x = float(np.clip(c, C_GRID[0], C_GRID[-1]))
    i = min(int((x - C_GRID[0]) / 0.1), len(C_GRID) - 2)
    f = (x - C_GRID[i]) / 0.1
    return t[i] * (1 - f) + t[i + 1] * f


def _d_for(name: str, c: float, target):
    """Asimetría d que da `target` (P de ganar o proporción de juegos) con el nivel c."""
    row = _row(name, c)
    t = np.asarray(target, dtype=float)
    sign = np.where(t >= 0.5, 1.0, -1.0)
    up = np.clip(np.where(t >= 0.5, t, 1 - t), 0.5, row[-1])
    return sign * np.interp(up, row, D_GRID)


def d_from_p(c: float, p):
    return _d_for("pwin", c, p)


def d_from_share(c: float, s):
    return _d_for("share", c, s)


def gps_at(c: float, d) -> np.ndarray:
    return np.interp(np.abs(np.asarray(d, dtype=float)), D_GRID, _row("gps", c))


def absd_at(c: float, d) -> np.ndarray:
    return np.interp(np.abs(np.asarray(d, dtype=float)), D_GRID, _row("absd", c))


LAMBDA_RANGE = (0.5, 3.0)
KAPPA_MAX = 3.0


def _level_for(g0: float, p: np.ndarray, lam: float) -> float:
    """c0 con el que la media de juegos por set del modelo (asimetría λ·d(Elo)) es g0."""
    def mean_gps(c):
        return float(np.mean(gps_at(c, lam * d_from_p(c, p))))

    lo, hi = float(C_GRID[0]), float(C_GRID[-1])
    if mean_gps(lo) >= g0:
        return lo
    if mean_gps(hi) <= g0:
        return hi
    for _ in range(22):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if mean_gps(mid) < g0 else (lo, mid)
    return (lo + hi) / 2


def calibrate_level(g0: float, absd0: float, p_pre: list[float]) -> tuple[float, float, float]:
    """
    (c0, κ, λ) de un circuito y superficie con los partidos válidos de la ventana:
      λ escala la asimetría que da el Elo (el Elo encoge las diferencias) y c0 es el nivel de
      saque; se eligen para que el modelo reproduzca, con el Elo previo de cada partido, la media
      observada de juegos por set (g0) y de |juegos A − juegos B| por set (absd0).
      κ = dc/d(juegos por set) en c0 (con tope KAPPA_MAX: cerca de c = 0 la pendiente se anula).
    """
    p = np.array([max(x, 1 - x) for x in p_pre] or [0.5])

    def mean_absd(lam):
        c = _level_for(g0, p, lam)
        return float(np.mean(absd_at(c, lam * d_from_p(c, p))))

    lo, hi = LAMBDA_RANGE
    if mean_absd(lo) >= absd0:
        lam = lo
    elif mean_absd(hi) <= absd0:
        lam = hi
    else:
        for _ in range(18):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if mean_absd(mid) < absd0 else (lo, mid)
        lam = (lo + hi) / 2
    c0 = _level_for(g0, p, lam)
    a, b = max(c0 - 0.1, float(C_GRID[0])), min(c0 + 0.1, float(C_GRID[-1]))
    ga = float(np.mean(gps_at(a, lam * d_from_p(a, p))))
    gb = float(np.mean(gps_at(b, lam * d_from_p(b, p))))
    slope = (gb - ga) / (b - a)
    kappa = min(1 / slope, KAPPA_MAX) if slope > 1e-6 else 0.0
    if c0 <= float(C_GRID[0]) + 1e-9:
        kappa = 0.0          # en c = 0 los juegos por set no responden al nivel: sin ajuste por δ
    return c0, kappa, lam


# ── Factores por jugador y superficie ─────────────────────────────────────────

def fit_factors(ms: list[dict], k: float = SHRINK_K, iters: int = ITERS) -> dict:
    """δ (juegos por set) y γ (proporción de juegos) por jugador, ajustados por rival y
    encogidos hacia 0 con k partidos ficticios. ms: partidos válidos de un circuito y superficie."""
    sets = sum(m["n_sets"] for m in ms)
    g0 = sum(m["total_games"] for m in ms) / sets if sets else 10.0
    absd0 = sum(abs(a - b) for m in ms for a, b in m["sets"]) / sets if sets else 3.0
    by_p: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for m in ms:
        if not m["n_sets"] or not m["total_games"]:
            continue
        g = m["total_games"] / m["n_sets"]
        sw = m["w_games"] / m["total_games"]
        by_p[m["w"]].append((m["l"], g, sw))
        by_p[m["l"]].append((m["w"], g, 1 - sw))
    delta: dict[str, float] = {}
    gamma: dict[str, float] = {}
    for _ in range(iters):
        delta = {p: sum(g - g0 - delta.get(o, 0.0) for o, g, _ in lst) / (len(lst) + k) for p, lst in by_p.items()}
        gamma = {p: sum(s - 0.5 + gamma.get(o, 0.0) for o, _, s in lst) / (len(lst) + k) for p, lst in by_p.items()}
    return {"g0": g0, "absd0": absd0, "delta": delta, "gamma": gamma, "n": {p: len(v) for p, v in by_p.items()}}


# ── Valores de cada mercado en un partido jugado ──────────────────────────────

def favorite_is_winner(m: dict) -> Optional[bool]:
    """Favorito = mejor ranking (número más bajo). None si falta alguno o empatan."""
    wr, lr = m.get("wrank"), m.get("lrank")
    if not wr or not lr or wr == lr:
        return None
    return wr < lr


def outcome_values(m: dict) -> dict[str, Optional[float]]:
    fav_w = favorite_is_winner(m)
    diff = None if fav_w is None else (m["w_games"] - m["l_games"]) * (1 if fav_w else -1)
    return {"total_games": m["total_games"], "sets": m["n_sets"], "hcp_fav": diff,
            "tiebreak": m["tiebreaks"]}


# ── Ventana de 24 meses: factores, nivel, referencia y regresión ──────────────

class Window:
    """Lo que se sabe antes de `cutoff`: partidos válidos de los 24 meses previos con el Elo
    previo de cada uno (pre: P(ganador) antes del partido)."""

    def __init__(self, ms: list[dict], pre: list[float], cutoff: str):
        self.cutoff = cutoff
        self.ms, self.pre = ms, pre
        self._f: dict = {}
        self._lvl: dict = {}
        self._ref: dict = {}
        self._reg: dict = {}

    def factors(self, tour: str, surface: str) -> dict:
        key = (tour, surface)
        if key not in self._f:
            self._f[key] = fit_factors([m for m in self.ms if m["tour"] == tour and m["surface"] == surface])
        return self._f[key]

    def level(self, tour: str, surface: str) -> tuple[float, float, float]:
        key = (tour, surface)
        if key not in self._lvl:
            f = self.factors(tour, surface)
            p = [x for m, x in zip(self.ms, self.pre) if m["tour"] == tour and m["surface"] == surface]
            self._lvl[key] = calibrate_level(f["g0"], f["absd0"], p)
        return self._lvl[key]

    def _values(self, tour: str, surface: Optional[str], bo: int, market: str) -> np.ndarray:
        key = (tour, surface, bo, market)
        if key not in self._ref:
            vals = [outcome_values(m)[market] for m in self.ms
                    if m["tour"] == tour and m["best_of"] == bo and (surface is None or m["surface"] == surface)]
            self._ref[key] = np.array([v for v in vals if v is not None], dtype=float)
        return self._ref[key]

    def ref_rate(self, tour: str, surface: str, bo: int, market: str, line: float) -> tuple[Optional[float], int]:
        """Frecuencia de «más de line» (circuito × superficie × best_of; si hay menos de MIN_REF,
        circuito × best_of). Devuelve (p, n)."""
        v = self._values(tour, surface, bo, market)
        if len(v) < MIN_REF:
            v = self._values(tour, None, bo, market)
        if not len(v):
            return None, 0
        return float(np.mean(v > line)), len(v)

    def regression(self, tour: str, bo: int):
        """(β, residuos) de juegos totales ~ 1 + |p−0,5| + arcilla + hierba; None si no hay muestra."""
        key = (tour, bo)
        if key not in self._reg:
            rows = [(m, p) for m, p in zip(self.ms, self.pre) if m["tour"] == tour and m["best_of"] == bo]
            if len(rows) < MIN_REG:
                self._reg[key] = None
            else:
                x = np.array([_reg_x(p, m["surface"]) for m, p in rows])
                y = np.array([m["total_games"] for m, _ in rows], dtype=float)
                beta, *_ = np.linalg.lstsq(x, y, rcond=None)
                self._reg[key] = (beta, y - x @ beta)
        return self._reg[key]

    def reg_p_over(self, tour: str, bo: int, surface: str, p: float, line: float) -> Optional[float]:
        fit = self.regression(tour, bo)
        if fit is None:
            return None
        beta, res = fit
        pred = float(np.array(_reg_x(p, surface)) @ beta)
        return float(np.mean(pred + res > line))

    def n_surface(self, player: str, tour: str, surface: str) -> int:
        return self.factors(tour, surface)["n"].get(player, 0)


def _reg_x(p: float, surface: str) -> list[float]:
    return [1.0, abs(p - 0.5), float(surface == "Clay"), float(surface == "Grass")]


# ── Ficha de un partido ───────────────────────────────────────────────────────

def predict(elo: Elo, win: Window, a: str, b: str, tour: str, surface: str, best_of: int) -> dict:
    """Probabilidades de tp-v1 para A contra B (A = favorito por ranking cuando lo hay, así el
    hándicap es del favorito). Solo con lo anterior a win.cutoff."""
    f = win.factors(tour, surface)
    c0, kappa, lam = win.level(tour, surface)
    da, db = f["delta"].get(a, 0.0), f["delta"].get(b, 0.0)
    c = float(np.clip(c0 + kappa * (da + db), C_GRID[0], C_GRID[-1]))
    p_elo = elo.prob(a, b, surface)
    share = min(max(0.5 + f["gamma"].get(a, 0.0) - f["gamma"].get(b, 0.0), 0.05), 0.95)
    d = (1 - W_GAMES) * lam * float(d_from_p(c, p_elo)) + W_GAMES * float(d_from_share(c, share))
    ha, hb = tm.sigmoid(c + d / 2), tm.sigmoid(c - d / 2)
    dist = tm.match_dist(ha, hb, best_of)
    group = group_key(tour, best_of)
    lines = {}
    for market, lns in LINES.get(group, {}).items():
        src = {"total_games": dist["total"], "sets": dist["sets"], "hcp_fav": dist["diff"]}.get(market)
        lines[market] = {ln: (dist["p_tiebreak"] if market == "tiebreak" else tm.p_over(src, ln)) for ln in lns}
    n_a, n_b = win.n_surface(a, tour, surface), win.n_surface(b, tour, surface)
    return {
        "model_version": MODEL_VERSION, "group": group, "surface": surface, "best_of": best_of,
        "p_elo": round(p_elo, 4), "p_win_a": round(dist["p_a"], 4),
        "hold_a": round(ha, 4), "hold_b": round(hb, 4), "level_c": round(c, 4), "asym_d": round(d, 4), "lambda": round(lam, 3),
        "exp_total_games": round(sum(t * p for t, p in dist["total"].items()), 2),
        "exp_games_a": round(dist["exp_games_a"], 2), "exp_games_b": round(dist["exp_games_b"], 2),
        "n_surface": [n_a, n_b], "low_sample": min(n_a, n_b) < MIN_SURFACE,
        "lines": lines, "data_cutoff": win.cutoff,
    }


# ── Backtest walk-forward por torneo ──────────────────────────────────────────

def _fav_first(m: dict, elo: Elo) -> tuple[str, str, Optional[bool]]:
    fav_w = favorite_is_winner(m)
    if fav_w is None:
        fav_w = elo.prob(m["w"], m["l"], m["surface"]) >= 0.5
        return (m["w"], m["l"], None) if fav_w else (m["l"], m["w"], None)
    return (m["w"], m["l"], True) if fav_w else (m["l"], m["w"], False)


def backtest(matches: Iterable[dict], eval_from: str, eval_to: Optional[str] = None) -> tuple[list, list, Counter]:
    """
    Cada torneo top con inicio en [eval_from, eval_to] se predice solo con los partidos de fecha
    anterior a su inicio (Elo de todo lo anterior; factores, nivel, referencia y regresión de los
    24 meses previos). Se evalúan los partidos válidos con los dos jugadores con ≥ MIN_SURFACE
    partidos en la superficie. Filas (como mp-v1): (año, grupo, mercado, línea, y, p_ref, p_modelo,
    clave del partido). Devuelve (filas tp-v1, filas de la regresión [solo juegos totales], contadores).
    """
    ms = sorted((m for m in matches if m.get("date")), key=lambda m: (m["date"], m["tid"]))
    dates = [m["date"] for m in ms]
    by_t: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(ms):
        by_t[m["tid"]].append(i)
    starts: dict[str, list[str]] = defaultdict(list)
    for tid, idx in by_t.items():
        starts[min(ms[i]["start"] or ms[i]["date"] for i in idx)].append(tid)
    elo = Elo()
    pre: dict[int, float] = {}
    rows, rows_reg = [], []
    cnt: Counter = Counter()
    j = 0
    for s in sorted(starts):
        while j < len(ms) and ms[j]["date"] < s:
            m = ms[j]
            if m["status"] in ELO_STATUSES and m["sets"]:
                pre[j] = elo.prob(m["w"], m["l"], m["surface"])
                elo.update(m["w"], m["l"], m["surface"])
            j += 1
        if s < eval_from or (eval_to and s > eval_to):
            continue
        lo = bisect.bisect_left(dates, days_before(s, WINDOW_DAYS))
        widx = [i for i in range(lo, j) if ms[i]["valid"] and i in pre]
        win = Window([ms[i] for i in widx], [pre[i] for i in widx], s)
        for tid in starts[s]:
            cnt["torneos"] += 1
            for i in by_t[tid]:
                m = ms[i]
                if not m["valid"]:
                    cnt["no_validos"] += 1
                    continue
                group = group_key(m["tour"], m["best_of"])
                if group not in LINES:
                    cnt["sin_grupo"] += 1
                    continue
                a, b, fav_known = _fav_first(m, elo)
                if min(win.n_surface(a, m["tour"], m["surface"]), win.n_surface(b, m["tour"], m["surface"])) < MIN_SURFACE:
                    cnt["muestra_insuficiente"] += 1
                    continue
                cnt["evaluados"] += 1
                pr = predict(elo, win, a, b, m["tour"], m["surface"], m["best_of"])
                vals = outcome_values(m)
                key = f"{tid}#{i}"
                for market, lns in LINES[group].items():
                    if vals[market] is None or (market == "hcp_fav" and fav_known is None):
                        cnt["sin_ranking_hcp"] += market == "hcp_fav"
                        continue
                    for ln in lns:
                        p_ref, _ = win.ref_rate(m["tour"], m["surface"], m["best_of"], market, ln)
                        if p_ref is None:
                            continue
                        y = int(vals[market] > ln)
                        rows.append((s[:4], group, market, ln, y, p_ref, pr["lines"][market][ln], key, m["surface"]))
                        if market == "total_games":
                            pg = win.reg_p_over(m["tour"], m["best_of"], m["surface"], elo.prob(a, b, m["surface"]), ln)
                            if pg is not None:
                                rows_reg.append((s[:4], group, market, ln, y, p_ref, pg, key, m["surface"]))
    return rows, rows_reg, cnt


def build_state(matches: Iterable[dict], cutoff: str) -> tuple[Elo, Window]:
    """Elo y ventana con todo lo anterior a `cutoff` (para las fichas de F3)."""
    ms = sorted((m for m in matches if m.get("date") and m["date"] < cutoff), key=lambda m: m["date"])
    elo = Elo()
    lo = days_before(cutoff, WINDOW_DAYS)
    wms, wpre = [], []
    for m in ms:
        if m["status"] in ELO_STATUSES and m["sets"]:
            p = elo.prob(m["w"], m["l"], m["surface"])
            elo.update(m["w"], m["l"], m["surface"])
            if m["valid"] and m["date"] >= lo:
                wms.append(m)
                wpre.append(p)
    return elo, Window(wms, wpre, cutoff)


def brier(rows: list[tuple], col: int) -> float:
    return sum((r[col] - r[4]) ** 2 for r in rows) / len(rows) if rows else math.nan
