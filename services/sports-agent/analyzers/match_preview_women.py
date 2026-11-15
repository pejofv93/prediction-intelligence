"""
services/sports-agent/analyzers/match_preview_women.py

Ficha de partido para fútbol femenino (mp-w1): el mismo cálculo que mp-v1 (match_preview) con
datos de Highlightly (hl_team_stats, grupo "women"). Puro: sin Firestore, sin red.

Qué cambia frente a mp-v1:
  - Ligas: Liga F, WSL, Frauen-Bundesliga, Serie A femenina y D1 francesa. La Champions femenina
    queda fuera (mezcla equipos de ligas distintas: no hay media de liga con sentido).
  - Fuente única: medias de liga e índices de cada equipo salen de Highlightly, así que el
    modelo es coherente aunque Highlightly no cuente igual que football-data. Equipos por id.
  - Media de cada liga recalculada con sus datos (History de mp-v1, temporada anterior solo de
    estas ligas: no hay segundas divisiones femeninas cargadas).
  - Dispersión (r de la binomial negativa) recalculada con 2023/24 y 2024/25, partido a partido
    frente a lo que esperaba el modelo: r = Σλ² / Σ((y − λ)² − λ); sin sobredispersión, Poisson.
  - Líneas fijadas con 2023/24 y 2024/25 (percentiles 30-70), antes de mirar el backtest.
    Goles con líneas fijas (incluyen 3,5 por equipo y 4,5 en total).
Igual que mp-v1: K = 8, pesos 1 / 0,5, mínimo de 5 partidos de la temporada actual.

Datos de Highlightly: SOLO estadísticas. Sin cuotas; el motor de apuestas no lee nada de aquí.
"""
import math
from collections import defaultdict
from typing import Iterable, Optional

from analyzers import match_preview as mp

MODEL_VERSION = "mp-w1"
# id de liga de Highlightly → código propio
WOMEN_DIVS = {121626: "WLF", 38228: "WSL", 70566: "WFBL", 119073: "WSA", 55248: "WD1"}
DIV_NAMES = {"WLF": "Liga F", "WSL": "WSL", "WFBL": "Frauen-Bundesliga", "WSA": "Serie A fem.",
             "WD1": "D1 Arkema"}
FIT_SEASONS = ("2324", "2425")          # dispersión y líneas
BACKTEST_SEASONS = ("2526", "2627")
GOAL_LINES = ((0.5, 1.5, 2.5, 3.5), (1.5, 2.5, 3.5, 4.5))
LINE_PCTS = (0.30, 0.40, 0.50, 0.60, 0.70)   # P(más de la línea) objetivo


def season_code(hl_season) -> Optional[str]:
    """Temporada de Highlightly (2025) → '2526'."""
    try:
        y = int(hl_season)
    except (TypeError, ValueError):
        return None
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def _side(stats: Optional[dict], side: str) -> dict:
    """Estadísticas de un lado en el formato de mp-v1 (sin goles)."""
    t = (stats or {}).get(side) or {}

    def g(k):
        v = t.get(k)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    on, off, blk, y, r = g("shots_on"), g("shots_off"), g("shots_blocked"), g("yellows"), g("reds")
    return {"corners": g("corners"), "fouls": g("fouls"), "sot": on,
            "shots": None if None in (on, off, blk) else on + off + blk,
            "cards": None if y is None else y + (r or 0)}


def matches_from_hl(docs: Iterable[dict]) -> list[dict]:
    """Docs de hl_team_stats (grupo women) → partidos de mp-v1. Solo terminados, con marcador,
    de las 5 ligas. Una estadística que falte queda en None (no cuenta para esa estadística)."""
    out = []
    for d in docs:
        div = WOMEN_DIVS.get(d.get("league_id"))
        season = season_code(d.get("season"))
        if not div or not season or not d.get("finished") or not d.get("date"):
            continue
        gh, ga = d.get("goals_home"), d.get("goals_away")
        if gh is None or ga is None or d.get("home_team_id") is None or d.get("away_team_id") is None:
            continue
        st = d.get("stats") if d.get("status") == "done" else None
        h, a = _side(st, "home"), _side(st, "away")
        h["goals"], a["goals"] = gh, ga
        out.append({"div": div, "season": season, "date": d["date"],
                    "home": str(d["home_team_id"]), "away": str(d["away_team_id"]), "h": h, "a": a})
    return out


def history(matches: list[dict]) -> mp.History:
    return mp.History(matches, prev_divs=WOMEN_DIVS.values())


def coverage(matches: list[dict]) -> dict:
    """{(div, season): {matches, con estadísticas por campo}} — solo recuentos."""
    out: dict = defaultdict(lambda: {"matches": 0, **{s: 0 for s in mp.STATS}})
    for m in matches:
        c = out[(m["div"], m["season"])]
        c["matches"] += 1
        for s in mp.STATS:
            if m["h"][s] is not None and m["a"][s] is not None:
                c[s] += 1
    return dict(out)


def _model_means(hist: mp.History, seasons: Iterable[str], divs: Iterable[str], k: float = mp.SHRINK_K):
    """Walk-forward: (stat, scope, λ, y) de cada partido con los dos equipos con MIN_CUR."""
    for season in seasons:
        for div in divs:
            for m in hist.by_ds.get((div, season), []):
                d = m["date"]
                hh = hist.team_history(m["home"], div, season, d)
                ha = hist.team_history(m["away"], div, season, d)
                if (sum(1 for w, *_ in hh if w == mp.W_CUR) < mp.MIN_CUR
                        or sum(1 for w, *_ in ha if w == mp.W_CUR) < mp.MIN_CUR):
                    continue
                means = hist.league_means(div, season, d)
                for s in mp.STATS:
                    lh, la = means[s]
                    if m["h"][s] is None or m["a"][s] is None or not lh or not la:
                        continue
                    ah, dh, _ = hist.indices(hh, s, d, season, k)
                    aa, da, _ = hist.indices(ha, s, d, season, k)
                    lam, mu = lh * ah * da, la * aa * dh
                    yield s, "team", lam, m["h"][s], div
                    yield s, "team", mu, m["a"][s], div
                    yield s, "total", lam + mu, m["h"][s] + m["a"][s], div


def fit_dispersion(hist: mp.History, seasons: Iterable[str] = FIT_SEASONS,
                   divs: Iterable[str] = tuple(WOMEN_DIVS.values())) -> tuple[dict, dict]:
    """(r común por (stat, scope), detalle con n, r común y r por liga). r None = Poisson."""
    acc = defaultdict(lambda: [0, 0.0, 0.0])          # n, Σλ², Σ((y−λ)² − λ)
    by_div = defaultdict(lambda: [0, 0.0, 0.0])
    for s, scope, lam, y, div in _model_means(hist, seasons, divs):
        for a in (acc[(s, scope)], by_div[(s, scope, div)]):
            a[0] += 1
            a[1] += lam * lam
            a[2] += (y - lam) ** 2 - lam

    def r_of(a):
        return round(a[1] / a[2], 2) if a[0] and a[2] > 0 else None

    disp = {key: r_of(a) for key, a in acc.items() if r_of(a) is not None}
    detail = {f"{s}_{scope}": {"n": a[0], "r": r_of(a),
                               "by_div": {div: {"n": b[0], "r": r_of(b)}
                                          for (s2, sc2, div), b in sorted(by_div.items())
                                          if (s2, sc2) == (s, scope)}}
              for (s, scope), a in sorted(acc.items())}
    return disp, detail


def _quantile(vals: list[float], p: float) -> float:
    vs = sorted(vals)
    return vs[min(len(vs) - 1, max(0, int(math.floor(p * (len(vs) - 1) + 0.5))))]


def fit_lines(matches: list[dict], seasons: Iterable[str] = FIT_SEASONS) -> dict:
    """Líneas por estadística (por equipo, total) con lo jugado en `seasons`: las x,5 cuya
    frecuencia de "más de" queda cerca del 30, 40, 50, 60 y 70 %. Goles: fijas."""
    seasons = set(seasons)
    out = {}
    for s in mp.STATS:
        if s == "goals":
            out[s] = GOAL_LINES
            continue
        team, total = [], []
        for m in matches:
            if m["season"] not in seasons or m["h"][s] is None or m["a"][s] is None:
                continue
            team += [m["h"][s], m["a"][s]]
            total.append(m["h"][s] + m["a"][s])
        pair = []
        for vals in (team, total):
            if not vals:
                pair.append(())
                continue
            # P(y > L) = p  →  L ≈ cuantil (1 − p), redondeado a x,5
            ls = sorted({math.floor(_quantile(vals, 1 - p)) + 0.5 for p in LINE_PCTS})
            pair.append(tuple(max(0.5, l) for l in ls))
        out[s] = tuple(pair)
    return out


def line_rates(matches: list[dict], lines: dict, seasons: Iterable[str] = FIT_SEASONS) -> dict:
    """Frecuencia de "más de" de cada línea en `seasons` (para comprobar que quedan centradas)."""
    seasons = set(seasons)
    out = {}
    for s, (tl, ttl) in lines.items():
        team = [y for m in matches if m["season"] in seasons and m["h"][s] is not None and m["a"][s] is not None
                for y in (m["h"][s], m["a"][s])]
        total = [m["h"][s] + m["a"][s] for m in matches
                 if m["season"] in seasons and m["h"][s] is not None and m["a"][s] is not None]
        out[s] = {"team": {l: round(sum(y > l for y in team) / len(team), 3) for l in tl} if team else {},
                  "total": {l: round(sum(y > l for y in total) / len(total), 3) for l in ttl} if total else {}}
    return out


def backtest(hist: mp.History, lines: dict, dispersion: dict,
             seasons: Iterable[str] = BACKTEST_SEASONS) -> tuple[list[tuple], dict]:
    """Filas del walk-forward y métricas por mercado y liga (misma regla que mp-v1)."""
    rows = mp.backtest_rows(hist, seasons, divs=tuple(WOMEN_DIVS.values()), lines=lines, dispersion=dispersion)
    return rows, (mp.market_metrics(rows) if rows else {})
