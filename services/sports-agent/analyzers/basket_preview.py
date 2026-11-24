"""
services/sports-agent/analyzers/basket_preview.py

Baloncesto en Tendencias (bp-v1) — Fase 0: modelo por equipo y estadística. Puro: sin Firestore,
sin red, sin escribir nada. Lo usa scripts/ops/basket_backtest.py (walk-forward por jornada) y,
más adelante, las fichas. Aislado del motor de apuestas, de trend_finder y de Polymarket.

Partidos de entrada (games_from_docs convierte eur_games y las colecciones con su misma forma):
  {league, season, date, group (jornada), home, away, ot (prórrogas),
   h: {pts, reb, ast, fg3m, poss}, a: {…}}            poss = posesiones estimadas del equipo

Modelo (por 100 posesiones, ajustado por rival, ritmo, casa/fuera y prórroga):
  Liga (antes del corte; temporada actual peso 1, anterior 0,5):
    R_casa[s], R_fuera[s] = estadística s por 100 posesiones del local y del visitante
    P = posesiones por equipo en 40 minutos (las de un partido con prórroga × 40/(40+5·prórrogas))
    tasa_prórroga = prórrogas medias por partido
  Equipo (mismos pesos, encogido hacia 1 con K = 8 partidos ficticios de equipo medio):
    ataque[s]  = media de (lo suyo por 100 / R de su sede)
    defensa[s] = media de (lo del rival por 100 / R de la sede del rival)
    ritmo      = media de (posesiones en 40 min del partido / P)
  Partido:
    posesiones = P × ritmo_local × ritmo_visitante × (1 + tasa_prórroga × 5/40)
    media_local[s]     = posesiones × R_casa[s]/100  × ataque_local[s]     × defensa_visitante[s]
    media_visitante[s] = posesiones × R_fuera[s]/100 × ataque_visitante[s] × defensa_local[s]
    total = las dos sumadas (los puntos con prórroga, como gradúan las casas)
  P(más de x,5) = normal con la desviación de los residuos de las jornadas anteriores de la liga
  (walk-forward); sin residuos suficientes, la desviación de la liga.

Referencias (las dos «medias» con las que se compara el modelo):
  media de la liga  = frecuencia de «más de x,5» en la liga hasta la fecha (actual 1 + anterior
                      0,5), del lado del equipo en las líneas por equipo.
  media del equipo  = frecuencia de «más de x,5» en los partidos del equipo hasta la fecha (mismos
                      pesos), con 2 partidos ficticios a la frecuencia de la liga para no dar 0 ni 1.
                      En el total, los partidos de los dos equipos juntos.

Líneas: fijas por liga y mercado, percentiles 25/50/75 de la liga redondeados a x,5 (fixed_lines),
calculadas con la temporada de calentamiento del backtest. Cuando se aprueben se copian a
FIXED_LINES y dejan de recalcularse.

Etiquetas: las de mp-v1 (analyzers.match_preview.market_metrics): «ventaja medida» solo si el IC95
por partido de (Brier modelo − Brier referencia) queda entero por debajo de 0.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Optional

MODEL_VERSION = "bp-v1"
SHRINK_K = 8.0
W_CUR, W_PREV = 1.0, 0.5
MIN_CUR = 5                  # partidos de la temporada actual de cada equipo para predecir
MIN_RESID = 60               # residuos de la liga para usar su desviación en vez de la bruta
TEAM_REF_PRIOR = 2.0         # partidos ficticios a la frecuencia de la liga en la media del equipo
REG_MIN, OT_MIN = 40.0, 5.0
QUANTILES = (0.25, 0.5, 0.75)

STATS = ("pts", "reb", "ast", "fg3m")
SCOPES = ("team", "total")
STAT_NAMES = {"pts": "puntos", "reb": "rebotes", "ast": "asistencias", "fg3m": "triples"}

# Líneas aprobadas por liga y mercado ({liga: {mercado: (l25, l50, l75)}}). Vacío hasta revisar
# el backtest: mientras tanto, fixed_lines con la temporada de calentamiento.
FIXED_LINES: dict[str, dict[str, tuple[float, ...]]] = {}


def market_key(stat: str, scope: str) -> str:
    return f"{stat}_{scope}"


def prev_season(season: str) -> str:
    """'E2025' → 'E2024'; 'N2025' → 'N2024'; '2025' → '2024'."""
    head = season.rstrip("0123456789")
    return f"{head}{int(season[len(head):]) - 1}"


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_over(line: float, mean: float, sd: float) -> float:
    """P(Y > line) con Y entera y línea x,5: la línea ya hace de corrección de continuidad."""
    if sd <= 0:
        return 1.0 if mean > line else 0.0
    return 1.0 - norm_cdf((line - mean) / sd)


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _side(d: dict) -> Optional[dict]:
    out = {s: _num((d or {}).get(s)) for s in STATS}
    out["poss"] = _num((d or {}).get("poss_est"))
    if any(v is None for v in out.values()) or out["poss"] <= 0:
        return None
    return out


def games_from_docs(docs: Iterable[dict], league: str) -> list[dict]:
    """Docs con la forma de eur_games (season, date, home_code, away_code, ot_periods, phase,
    round, home/away con pts, reb, ast, fg3m y poss_est) → partidos del modelo. Fuera los que
    no traen las cuatro estadísticas y las posesiones de los dos equipos."""
    out = []
    for d in docs:
        h, a = _side(d.get("home")), _side(d.get("away"))
        if h is None or a is None or not d.get("date") or not d.get("season"):
            continue
        rnd = d.get("round")
        group = (f"{d['season']}_{d.get('phase') or ''}_{rnd}" if rnd is not None
                 else f"{d['season']}_{d['date']}")
        out.append({"league": league, "season": d["season"], "date": str(d["date"])[:10],
                    "group": group, "home": d.get("home_code"), "away": d.get("away_code"),
                    "ot": int(d.get("ot_periods") or 0), "h": h, "a": a})
    return [g for g in out if g["home"] and g["away"]]


def reg_factor(ot: int) -> float:
    """Fracción de 40 minutos sobre los minutos jugados: 1 sin prórroga, 40/45 con una."""
    return REG_MIN / (REG_MIN + OT_MIN * ot)


def game_poss(g: dict) -> float:
    """Posesiones por equipo del partido, llevadas a 40 minutos."""
    return (g["h"]["poss"] + g["a"]["poss"]) / 2 * reg_factor(g["ot"])


def stat_value(g: dict, stat: str, side: Optional[str]) -> float:
    return g[side][stat] if side else g["h"][stat] + g["a"][stat]


def _round_half(x: float) -> float:
    return math.floor(x) + 0.5


def quantile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    if not s:
        raise ValueError("sin valores")
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def fixed_lines(games: list[dict], quantiles: tuple[float, ...] = QUANTILES) -> dict[str, tuple[float, ...]]:
    """Líneas por mercado: percentiles de la liga redondeados a x,5 (las de equipo, con los dos
    lados juntos). Sin repetidas y en orden."""
    out = {}
    for s in STATS:
        team = [g[side][s] for g in games for side in ("h", "a")]
        total = [stat_value(g, s, None) for g in games]
        for scope, vals in (("team", team), ("total", total)):
            if vals:
                out[market_key(s, scope)] = tuple(sorted({_round_half(quantile(vals, q)) for q in quantiles}))
    return out


def lines_for(league: str, warmup_games: list[dict]) -> dict[str, tuple[float, ...]]:
    """Las aprobadas (FIXED_LINES) si existen; si no, las de la temporada de calentamiento."""
    return FIXED_LINES.get(league) or fixed_lines(warmup_games)


class History:
    """Índices sobre los partidos ya jugados de cada liga (temporada actual y anterior)."""

    def __init__(self, games: list[dict]):
        self.by_ls: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.by_team: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for g in games:
            self.by_ls[(g["league"], g["season"])].append(g)
        for v in self.by_ls.values():
            v.sort(key=lambda g: g["date"])
            for g in v:
                self.by_team[(g["league"], g["home"], g["season"])].append(g)
                self.by_team[(g["league"], g["away"], g["season"])].append(g)
        self._lm: dict = {}

    def _weighted(self, league: str, season: str, before: str) -> list[tuple[float, dict]]:
        cur = [(W_CUR, g) for g in self.by_ls.get((league, season), []) if g["date"] < before]
        prev = [(W_PREV, g) for g in self.by_ls.get((league, prev_season(season)), [])]
        return cur + prev

    def league_means(self, league: str, season: str, before: str) -> Optional[dict]:
        """{'pace', 'ot_rate', 'rate': {s: (por 100 en casa, por 100 fuera)}} o None sin partidos."""
        key = (league, season, before)
        if key in self._lm:
            return self._lm[key]
        ws = self._weighted(league, season, before)
        sw = sum(w for w, _ in ws)
        if not sw:
            self._lm[key] = None
            return None
        ph = sum(w * g["h"]["poss"] for w, g in ws)
        pa = sum(w * g["a"]["poss"] for w, g in ws)
        out = {"pace": sum(w * game_poss(g) for w, g in ws) / sw,
               "ot_rate": sum(w * g["ot"] for w, g in ws) / sw,
               "rate": {s: (100 * sum(w * g["h"][s] for w, g in ws) / ph,
                            100 * sum(w * g["a"][s] for w, g in ws) / pa) for s in STATS},
               "n": sw}
        if len(self._lm) > 512:
            self._lm.clear()
        self._lm[key] = out
        return out

    def team_games(self, league: str, team: str, season: str, before: str) -> list[tuple[float, dict, str]]:
        """[(peso, partido, 'h'|'a')]: actual antes de `before` 1,0; anterior entera 0,5."""
        out = [(W_CUR, g, "h" if g["home"] == team else "a")
               for g in self.by_team.get((league, team, season), []) if g["date"] < before]
        out += [(W_PREV, g, "h" if g["home"] == team else "a")
                for g in self.by_team.get((league, team, prev_season(season)), [])]
        return out

    @staticmethod
    def n_current(tg: list[tuple[float, dict, str]]) -> int:
        return sum(1 for w, *_ in tg if w == W_CUR)

    def team_indices(self, tg: list[tuple[float, dict, str]], lm: dict, k: float = SHRINK_K) -> dict:
        """{'att': {s}, 'def': {s}, 'pace'} relativos a la liga, encogidos hacia 1."""
        sa = {s: 0.0 for s in STATS}
        sd = {s: 0.0 for s in STATS}
        sp = sw = 0.0
        for w, g, v in tg:
            o = "a" if v == "h" else "h"
            iv, io = (0, 1) if v == "h" else (1, 0)
            for s in STATS:
                sa[s] += w * (100 * g[v][s] / g[v]["poss"]) / lm["rate"][s][iv]
                sd[s] += w * (100 * g[o][s] / g[o]["poss"]) / lm["rate"][s][io]
            sp += w * game_poss(g) / lm["pace"]
            sw += w
        return {"att": {s: (sa[s] + k) / (sw + k) for s in STATS},
                "def": {s: (sd[s] + k) / (sw + k) for s in STATS},
                "pace": (sp + k) / (sw + k)}

    def league_sd(self, league: str, season: str, before: str, stat: str, scope: str) -> Optional[float]:
        """Desviación bruta de la liga (lado del equipo con los dos lados juntos, o total)."""
        vals = []
        for w, g in self._weighted(league, season, before):
            if scope == "team":
                vals += [(w, g["h"][stat]), (w, g["a"][stat])]
            else:
                vals.append((w, stat_value(g, stat, None)))
        sw = sum(w for w, _ in vals)
        if sw <= 0:
            return None
        m = sum(w * y for w, y in vals) / sw
        return math.sqrt(sum(w * (y - m) ** 2 for w, y in vals) / sw)

    def league_rate(self, league: str, season: str, before: str, stat: str, side: Optional[str],
                    line: float) -> Optional[float]:
        ws = self._weighted(league, season, before)
        sw = sum(w for w, _ in ws)
        if not sw:
            return None
        return sum(w for w, g in ws if stat_value(g, stat, side) > line) / sw

    @staticmethod
    def team_rate(tgs: list[list[tuple[float, dict, str]]], stat: str, total: bool, line: float,
                  p_league: float) -> float:
        """Frecuencia de «más de line» en los partidos del equipo (o de los dos, en el total),
        con TEAM_REF_PRIOR partidos ficticios a la frecuencia de la liga."""
        hits = sw = 0.0
        for tg in tgs:
            for w, g, v in tg:
                y = stat_value(g, stat, None if total else v)
                hits += w * (y > line)
                sw += w
        return (hits + TEAM_REF_PRIOR * p_league) / (sw + TEAM_REF_PRIOR)


def expected(hist: History, league: str, season: str, before: str, home: str, away: str,
             k: float = SHRINK_K) -> Optional[dict]:
    """Medias del modelo: {'poss', 'h': {s}, 'a': {s}, 'total': {s}, 'n_cur': (local, visitante)}."""
    lm = hist.league_means(league, season, before)
    if lm is None:
        return None
    th = hist.team_games(league, home, season, before)
    ta = hist.team_games(league, away, season, before)
    ih, ia = hist.team_indices(th, lm, k), hist.team_indices(ta, lm, k)
    poss = lm["pace"] * ih["pace"] * ia["pace"] * (1 + lm["ot_rate"] * OT_MIN / REG_MIN)
    h = {s: poss * lm["rate"][s][0] / 100 * ih["att"][s] * ia["def"][s] for s in STATS}
    a = {s: poss * lm["rate"][s][1] / 100 * ia["att"][s] * ih["def"][s] for s in STATS}
    return {"poss": poss, "h": h, "a": a, "total": {s: h[s] + a[s] for s in STATS},
            "n_cur": (hist.n_current(th), hist.n_current(ta)), "tg": (th, ta)}


class Residuals:
    """Residuos (real − media del modelo) por liga, estadística y ámbito, en orden de jornada.
    Cuentan los de la temporada actual y la anterior (como las medias)."""

    def __init__(self):
        self._r: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)

    def add(self, league: str, season: str, stat: str, scope: str, r: float) -> None:
        self._r[(league, season, stat, scope)].append(r)

    def sd(self, league: str, season: str, stat: str, scope: str) -> Optional[float]:
        vals = self._r.get((league, season, stat, scope), []) + \
            self._r.get((league, prev_season(season), stat, scope), [])
        if len(vals) < MIN_RESID:
            return None
        return math.sqrt(sum(x * x for x in vals) / len(vals))


def _groups(games: list[dict]) -> list[list[dict]]:
    """Partidos agrupados por jornada, en orden de su primer día."""
    by: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        by[g["group"]].append(g)
    return sorted(by.values(), key=lambda gs: min(g["date"] for g in gs))


def backtest_rows(hist: History, league: str, seasons: Iterable[str], lines: dict[str, tuple],
                  k: float = SHRINK_K, counts: Optional[dict] = None,
                  resid: Optional[Residuals] = None) -> dict[str, list[tuple]]:
    """
    Walk-forward por jornada: cada jornada se predice solo con lo jugado antes de su primer día;
    sus residuos entran después. Filas (season, liga, mercado, línea, y, p_ref, p_modelo, partido)
    con la forma de match_preview.backtest_rows, en dos listas: 'vs_liga' y 'vs_equipo'.
    Los dos equipos con al menos MIN_CUR partidos de la temporada antes del corte.
    """
    counts = counts if counts is not None else defaultdict(int)
    resid = resid or Residuals()
    out = {"vs_liga": [], "vs_equipo": []}
    seasons = list(seasons)
    # Los residuos de la temporada anterior a la primera evaluada también cuentan (calentamiento)
    warm = [prev_season(seasons[0])] if seasons else []
    for season in warm + seasons:
        evaluate = season in seasons
        for grp in _groups(hist.by_ls.get((league, season), [])):
            cutoff = min(g["date"] for g in grp)
            done = []
            for g in grp:
                e = expected(hist, league, season, cutoff, g["home"], g["away"], k)
                if e is None:
                    counts["sin_liga"] += int(evaluate)
                    continue
                if min(e["n_cur"]) < MIN_CUR:
                    counts["pocos_partidos"] += int(evaluate)
                    done.append((g, e, False))
                    continue
                done.append((g, e, evaluate))
                if not evaluate:
                    continue
                counts["evaluados"] += 1
                mk = f"{g['group']}_{g['home']}_{g['away']}_{g['date']}"
                for s in STATS:
                    for scope in SCOPES:
                        lns = lines.get(market_key(s, scope)) or ()
                        sd = resid.sd(league, season, s, scope) or \
                            hist.league_sd(league, season, cutoff, s, scope)
                        if not sd:
                            continue
                        sides = ("h", "a") if scope == "team" else (None,)
                        for side in sides:
                            mean = e[side][s] if side else e["total"][s]
                            y = stat_value(g, s, side)
                            for ln in lns:
                                pl = hist.league_rate(league, season, cutoff, s, side, ln)
                                if pl is None:
                                    continue
                                if side:
                                    tgs = [e["tg"][0 if side == "h" else 1]]
                                else:
                                    tgs = list(e["tg"])
                                pt = History.team_rate(tgs, s, side is None, ln, pl)
                                pm = p_over(ln, mean, sd)
                                key = market_key(s, scope)
                                out["vs_liga"].append((season, league, key, ln, int(y > ln), pl, pm, mk))
                                out["vs_equipo"].append((season, league, key, ln, int(y > ln), pt, pm, mk))
            for g, e, _ in done:          # la jornada ya jugada: sus residuos, para las siguientes
                for s in STATS:
                    resid.add(league, season, s, "team", g["h"][s] - e["h"][s])
                    resid.add(league, season, s, "team", g["a"][s] - e["a"][s])
                    resid.add(league, season, s, "total", stat_value(g, s, None) - e["total"][s])
    return out


def mean_bias(hist: History, league: str, seasons: Iterable[str], k: float = SHRINK_K) -> dict[str, float]:
    """Sesgo medio (real − modelo) del total por estadística en las temporadas dadas: control de
    que el ajuste de prórroga y ritmo no desplaza las medias. Walk-forward como el backtest."""
    acc: dict[str, list[float]] = defaultdict(list)
    for season in seasons:
        for grp in _groups(hist.by_ls.get((league, season), [])):
            cutoff = min(g["date"] for g in grp)
            for g in grp:
                e = expected(hist, league, season, cutoff, g["home"], g["away"], k)
                if e is None or min(e["n_cur"]) < MIN_CUR:
                    continue
                for s in STATS:
                    acc[s].append(stat_value(g, s, None) - e["total"][s])
    return {s: round(sum(v) / len(v), 3) for s, v in acc.items() if v}
