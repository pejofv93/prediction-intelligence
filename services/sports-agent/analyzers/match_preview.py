"""
services/sports-agent/analyzers/match_preview.py

Ficha de partido de Tendencias (mp-v1) — Paso 2: cálculo. Puro: sin Firestore, sin red, sin
escribir nada. Lo llamará el job del paso 3. Aislado del feed actual, de trend_finder y de
Polymarket.

Modelo (el mismo del backtest del 8-nov, sesión de diseño aprobada):
  λ (local)     = media de la liga en casa  × ataque del local    × defensa del visitante
  μ (visitante) = media de la liga fuera    × ataque del visitante × defensa del local
  Ataque/defensa de cada equipo: media ponderada de (lo suyo / media de la liga en esa sede),
  partido a partido, con todas las sedes; pesos temporada actual 1.0, anterior en la misma
  división 0.5, anterior en otra división (ascendido/descendido) 0.25; encogido hacia 1 con
  K partidos ficticios de equipo medio (K = 8).
  Media de la liga: lo jugado esta temporada antes del partido; con menos de 40 partidos, la
  temporada anterior completa de la misma división.
  Probabilidad de cada línea "más de X": binomial negativa con la r ajustada en 24/25 donde hay
  sobredispersión (córners y remates por equipo; totales de córners, remates, faltas y
  tarjetas), Poisson en el resto (goles, a puerta, faltas y tarjetas por equipo).
  Referencia "media de la liga": frecuencia de la línea en la liga (actual 1.0 + anterior
  0.5), del lado del equipo para las líneas por equipo.

Etiqueta por mercado y liga: "ventaja medida" solo si en el backtest walk-forward el Brier del
modelo menos el de la liga tiene el IC95 entero por debajo de 0 (IC por partido: las líneas de
un mismo partido no son independientes). Si no, "solo referencia". Un equipo con menos de
MIN_CUR partidos de la temporada actual → "muestra insuficiente": se calcula, no se resalta.
"""
import math
from collections import defaultdict
from typing import Iterable, Optional

from scipy.stats import nbinom, poisson

from collectors.fdco_team_matches import LOWER_DIVS, MAIN_DIVS

MODEL_VERSION = "mp-v1"
SHRINK_K = 8.0
W_CUR, W_PREV_SAME, W_PREV_OTHER = 1.0, 0.5, 0.25
MIN_CUR = 5
MIN_LEAGUE_MATCHES = 40
Z95 = 1.96

STATS = ("goals", "corners", "cards", "shots", "sot", "fouls")
LINES: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {   # (por equipo, total)
    "goals":   ((0.5, 1.5, 2.5),           (1.5, 2.5, 3.5)),
    "corners": ((3.5, 4.5, 5.5, 6.5),      (8.5, 9.5, 10.5, 11.5)),
    "cards":   ((0.5, 1.5, 2.5, 3.5),      (3.5, 4.5, 5.5)),
    "shots":   ((8.5, 10.5, 12.5, 14.5),   (20.5, 22.5, 24.5, 26.5)),
    "sot":     ((2.5, 3.5, 4.5, 5.5),      (6.5, 7.5, 8.5, 9.5)),
    "fouls":   ((9.5, 10.5, 11.5, 12.5, 13.5), (20.5, 22.5, 24.5)),
}
# r de la binomial negativa (método de momentos, temporada 24/25, 8 ligas). Sin entrada = Poisson.
DISPERSION: dict[tuple[str, str], float] = {
    ("corners", "team"): 17.03, ("shots", "team"): 30.34,
    ("corners", "total"): 309.7, ("shots", "total"): 170.3,
    ("fouls", "total"): 1011.0, ("cards", "total"): 34.40,
}
LABEL_EDGE, LABEL_REF, LABEL_LOW_SAMPLE = "ventaja medida", "solo referencia", "muestra insuficiente"


def market_key(stat: str, scope: str) -> str:
    return f"{stat}_{scope}"          # goals_team, corners_total…


def line_key(line: float) -> str:
    return "over_" + str(line).replace(".", "_")


def prev_season(season: str) -> str:
    """'2627' → '2526'."""
    a = int(season[:2]) - 1
    return f"{a:02d}{a + 1:02d}"


# ── Datos: filas equipo-partido (fdco_team_matches) → partidos ──────────────────

def matches_from_team_rows(rows: Iterable[dict]) -> list[dict]:
    """Filas de fdco_team_matches (una por equipo) → un registro por partido con lo del local
    (h) y lo del visitante (a). Solo hace falta la fila del local."""
    out = []
    for r in rows:
        if r.get("side") != "home":
            continue
        out.append({
            "div": r["div"], "season": r["season"], "date": r["match_date"],
            "home": r["team"], "away": r["opponent"],
            "h": {s: r.get(f"{s}_for") for s in STATS},
            "a": {s: r.get(f"{s}_against") for s in STATS},
        })
    return out


class History:
    """Índices sobre los partidos ya jugados (temporada actual y anterior, 8 ligas + segundas)."""

    def __init__(self, matches: list[dict]):
        self.by_ds: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.by_team_season: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for m in matches:
            self.by_ds[(m["div"], m["season"])].append(m)
        for v in self.by_ds.values():
            v.sort(key=lambda m: m["date"])
            for m in v:
                self.by_team_season[(m["home"], m["season"])].append(m)
                self.by_team_season[(m["away"], m["season"])].append(m)
        self._lm: dict = {}
        self._lv: dict = {}

    # Medias de la liga por partido (local, visitante)
    def league_means(self, div: str, season: str, before: Optional[str]) -> dict[str, tuple]:
        key = (div, season, before)
        if key in self._lm:
            return self._lm[key]
        ms = [m for m in self.by_ds.get((div, season), []) if before is None or m["date"] < before]
        if len(ms) < MIN_LEAGUE_MATCHES and self.by_ds.get((div, prev_season(season))):
            ms = self.by_ds[(div, prev_season(season))]
        out = {}
        for s in STATS:
            h = [m["h"][s] for m in ms if m["h"][s] is not None]
            a = [m["a"][s] for m in ms if m["a"][s] is not None]
            out[s] = (sum(h) / len(h) if h else None, sum(a) / len(a) if a else None)
        self._lm[key] = out
        return out

    def team_history(self, team: str, div: str, season: str, before: str) -> list[tuple[float, dict, str]]:
        """[(peso, partido, 'h'|'a')]: actual (solo esta división, antes de `before`) 1.0;
        anterior misma división 0.5; anterior en otra división 0.25."""
        hist = []
        for m in self.by_team_season.get((team, season), []):
            if m["div"] == div and m["date"] < before:
                hist.append((W_CUR, m, "h" if m["home"] == team else "a"))
        for m in self.by_team_season.get((team, prev_season(season)), []):
            if m["div"] in MAIN_DIVS or m["div"] in LOWER_DIVS:
                w = W_PREV_SAME if m["div"] == div else W_PREV_OTHER
                hist.append((w, m, "h" if m["home"] == team else "a"))
        return hist

    def indices(self, hist, stat: str, before: str, season: str, k: float = SHRINK_K) -> tuple[float, float, float]:
        """(ataque, defensa, n efectivo) relativos a la media de la liga y la sede de cada
        partido, encogidos hacia 1 con k partidos ficticios de equipo medio."""
        sa = sd = sw = 0.0
        for w, m, v in hist:
            o = "a" if v == "h" else "h"
            yf, ya = m[v][stat], m[o][stat]
            if yf is None or ya is None:
                continue
            lh, la = self.league_means(m["div"], m["season"], before if m["season"] == season else None)[stat]
            if not lh or not la:
                continue
            ref_for, ref_against = (lh, la) if v == "h" else (la, lh)
            sa += w * yf / ref_for
            sd += w * ya / ref_against
            sw += w
        return (sa + k) / (sw + k), (sd + k) / (sw + k), sw

    def league_line_rate(self, div: str, season: str, before: str, stat: str, side: Optional[str],
                         line: float) -> tuple[Optional[float], float]:
        """Frecuencia de "más de line" en la liga (actual antes de `before` 1.0 + anterior 0.5).
        side 'h'/'a' = de ese lado; None = total del partido. Devuelve (p, n ponderado)."""
        vals = self._line_values(div, season, before, stat, side)
        n = sum(w for w, _ in vals)
        hits = sum(w for w, y in vals if y > line)
        return (hits / n if n else None), n

    def _line_values(self, div, season, before, stat, side) -> list[tuple[float, float]]:
        key = (div, season, before, stat, side)
        if key not in self._lv:
            if len(self._lv) > 256:        # el backtest va partido a partido: basta un caché corto
                self._lv.clear()
            vals = []
            sources = ((W_CUR, [m for m in self.by_ds.get((div, season), []) if m["date"] < before]),
                       (W_PREV_SAME, self.by_ds.get((div, prev_season(season)), [])))
            for w, ms in sources:
                for m in ms:
                    if side:
                        y = m[side][stat]
                    else:
                        y = None if m["h"][stat] is None or m["a"][stat] is None else m["h"][stat] + m["a"][stat]
                    if y is not None:
                        vals.append((w, y))
            self._lv[key] = vals
        return self._lv[key]


def p_over(line: float, mean: float, r: Optional[float]) -> float:
    k = math.floor(line)
    if r is None:
        return float(1 - poisson.cdf(k, mean))
    return float(1 - nbinom.cdf(k, r, r / (r + mean)))


def market_prob_over25(odds: Optional[dict]) -> Optional[float]:
    """Probabilidad de más de 2,5 goles que da la cuota media, sin margen (dos vías)."""
    o, u = (odds or {}).get("over_2_5"), (odds or {}).get("under_2_5")
    if not o or not u:
        return None
    return round((1 / o) / (1 / o + 1 / u), 4)


# ── Ficha de un partido ─────────────────────────────────────────────────────────

def predict_match(hist: History, div: str, season: str, match_date: str, home: str, away: str,
                  labels: Optional[dict[tuple[str, str], str]] = None,
                  odds: Optional[dict] = None, k: float = SHRINK_K) -> dict:
    """
    Ficha del partido con lo jugado ANTES de match_date (fecha ISO del partido).
    labels: {(mercado, div): "ventaja medida" | "solo referencia"} (de match_preview_metrics);
            lo que no esté = "solo referencia".
    """
    labels = labels or {}
    hh = hist.team_history(home, div, season, match_date)
    ha = hist.team_history(away, div, season, match_date)
    used = [m for _, m, _ in hh + ha if m["season"] == season]
    teams = {}
    for side, name, h in (("home", home, hh), ("away", away, ha)):
        n_cur = sum(1 for w, *_ in h if w == W_CUR)
        teams[side] = {"team": name, "n_cur": n_cur, "n_eff": round(sum(w for w, *_ in h), 2),
                       "insufficient": n_cur < MIN_CUR, "attack": {}, "defence": {}}
    low_sample = teams["home"]["insufficient"] or teams["away"]["insufficient"]

    means = hist.league_means(div, season, match_date)
    stats: dict = {}
    for s in STATS:
        lh, la = means[s]
        if not lh or not la:
            stats[s] = {"available": False, "reason": "sin media de liga"}
            continue
        ah, dh, nh = hist.indices(hh, s, match_date, season, k)
        aa, da, na = hist.indices(ha, s, match_date, season, k)
        teams["home"]["attack"][s], teams["home"]["defence"][s] = round(ah, 4), round(dh, 4)
        teams["away"]["attack"][s], teams["away"]["defence"][s] = round(aa, 4), round(da, 4)
        lam, mu = lh * ah * da, la * aa * dh
        team_lines, total_lines = LINES[s]
        block = {"available": True, "league_mean_home": round(lh, 3), "league_mean_away": round(la, 3),
                 "exp_home": round(lam, 3), "exp_away": round(mu, 3), "exp_total": round(lam + mu, 3),
                 "n_eff_home": round(nh, 2), "n_eff_away": round(na, 2), "lines": {}}
        for scope, side, mean, lines in (("team", "h", lam, team_lines), ("team", "a", mu, team_lines),
                                         ("total", None, lam + mu, total_lines)):
            mk = market_key(s, scope)
            label = labels.get((mk, div), LABEL_REF)
            r = DISPERSION.get((s, scope))
            out = {}
            for ln in lines:
                pm = p_over(ln, mean, r)
                pl, nl = hist.league_line_rate(div, season, match_date, s, side, ln)
                out[line_key(ln)] = {
                    "line": ln, "p_model": round(pm, 4),
                    "p_league": None if pl is None else round(pl, 4),
                    "diff_vs_league": None if pl is None else round(pm - pl, 4),
                    "league_n": round(nl, 1),
                }
            block["lines"][{"h": "home", "a": "away", None: "total"}[side]] = {
                "market": mk, "label": LABEL_LOW_SAMPLE if low_sample else label,
                "market_label": label, "highlight": (label == LABEL_EDGE and not low_sample),
                "distribution": "binomial negativa" if r else "Poisson", "r": r, "lines": out,
            }
        stats[s] = block

    p_mkt = market_prob_over25(odds)
    if p_mkt is not None and stats["goals"].get("available"):
        stats["goals"]["lines"]["total"]["lines"]["over_2_5"]["p_market"] = p_mkt

    return {
        "model_version": MODEL_VERSION,
        "params": {"k": k, "weights": [W_CUR, W_PREV_SAME, W_PREV_OTHER], "min_cur": MIN_CUR,
                   "min_league_matches": MIN_LEAGUE_MATCHES,
                   "dispersion": {f"{a}_{b}": v for (a, b), v in DISPERSION.items()}},
        "div": div, "season": season, "match_date": match_date,
        "data_cutoff": max((m["date"] for m in used), default=None),
        "low_sample": low_sample, "teams": teams, "stats": stats,
    }


# ── Backtest walk-forward y etiquetas por mercado y liga ───────────────────────

def backtest_rows(hist: History, seasons: Iterable[str], divs: Iterable[str] = tuple(MAIN_DIVS),
                  k: float = SHRINK_K) -> list[tuple]:
    """Cada partido de `seasons` predicho solo con lo anterior (los dos equipos con al menos
    MIN_CUR partidos de la temporada actual). Fila por línea:
    (season, div, mercado, línea, y(0/1), p_liga, p_modelo, match_key)."""
    rows = []
    for season in seasons:
        for div in divs:
            for m in hist.by_ds.get((div, season), []):
                d = m["date"]
                hh = hist.team_history(m["home"], div, season, d)
                ha = hist.team_history(m["away"], div, season, d)
                if sum(1 for w, *_ in hh if w == W_CUR) < MIN_CUR or sum(1 for w, *_ in ha if w == W_CUR) < MIN_CUR:
                    continue
                mk_id = f"{season}_{div}_{d}_{m['home']}"
                means = hist.league_means(div, season, d)
                for s in STATS:
                    lh, la = means[s]
                    if m["h"][s] is None or m["a"][s] is None or not lh or not la:
                        continue
                    ah, dh, _ = hist.indices(hh, s, d, season, k)
                    aa, da, _ = hist.indices(ha, s, d, season, k)
                    lam, mu = lh * ah * da, la * aa * dh
                    team_lines, total_lines = LINES[s]
                    for side, mean in (("h", lam), ("a", mu)):
                        for ln in team_lines:
                            pl, _ = hist.league_line_rate(div, season, d, s, side, ln)
                            if pl is None:
                                continue
                            rows.append((season, div, market_key(s, "team"), ln, int(m[side][s] > ln), pl,
                                         p_over(ln, mean, DISPERSION.get((s, "team"))), mk_id))
                    yt = m["h"][s] + m["a"][s]
                    for ln in total_lines:
                        pl, _ = hist.league_line_rate(div, season, d, s, None, ln)
                        if pl is None:
                            continue
                        rows.append((season, div, market_key(s, "total"), ln, int(yt > ln), pl,
                                     p_over(ln, lam + mu, DISPERSION.get((s, "total"))), mk_id))
    return rows


def _paired_ci(sub: list[tuple]) -> tuple[float, float, float, int]:
    """Diferencia media de Brier (modelo − liga) con IC95 normal sobre la media POR PARTIDO."""
    per = defaultdict(list)
    for r in sub:
        y, pl, pm = r[4], r[5], r[6]
        per[r[7]].append((pm - y) ** 2 - (pl - y) ** 2)
    d = [sum(v) / len(v) for v in per.values()]
    n = len(d)
    m = sum(d) / n
    if n < 2:
        return m, float("-inf"), float("inf"), n
    se = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1) / n)
    return m, m - Z95 * se, m + Z95 * se, n


def _calibration(sub: list[tuple], bins: int = 10) -> list[dict]:
    acc = defaultdict(lambda: [0, 0.0, 0])
    for r in sub:
        b = min(bins - 1, int(r[6] * bins))
        acc[b][0] += 1
        acc[b][1] += r[6]
        acc[b][2] += r[4]
    return [{"bin": f"{b * 10}-{b * 10 + 10}", "n": n, "predicted": round(sp / n, 4), "observed": round(sy / n, 4)}
            for b, (n, sp, sy) in sorted(acc.items())]


def market_metrics(rows: list[tuple]) -> dict[str, dict]:
    """
    Métricas por mercado y liga (clave "{mercado}|{div}") y por mercado en todas las ligas
    ("{mercado}|ALL"): partidos, Brier de la liga y del modelo, diferencia con IC95 por
    partido, etiqueta del IC de la celda (ci_label), etiqueta final (label: la celda y el mercado
    en todas las ligas con ✔) y calibración en 10 tramos (solo en ALL).
    """
    groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in rows:
        groups[(r[2], r[1])].append(r)
        groups[(r[2], "ALL")].append(r)
    out = {}
    for (mk, div), sub in sorted(groups.items()):
        bl = sum((r[5] - r[4]) ** 2 for r in sub) / len(sub)
        bm = sum((r[6] - r[4]) ** 2 for r in sub) / len(sub)
        m, lo, hi, n = _paired_ci(sub)
        out[f"{mk}|{div}"] = {
            "market": mk, "div": div, "n_matches": n, "n_lines": len(sub),
            "brier_league": round(bl, 5), "brier_model": round(bm, 5),
            "bss_vs_league": round(1 - bm / bl, 4) if bl else None,
            "delta": round(m, 5), "ci_lo": round(lo, 5) if math.isfinite(lo) else None,
            "ci_hi": round(hi, 5) if math.isfinite(hi) else None,
            "ci_label": LABEL_EDGE if hi < 0 else LABEL_REF,
            **({"calibration": _calibration(sub)} if div == "ALL" else {}),
        }
    # Regla prudente (96 celdas al 95 % → 2-3 ✔ por azar): una liga solo lleva "ventaja medida"
    # si su IC queda entero bajo cero Y el del mercado en todas las ligas también.
    for v in out.values():
        whole = out.get(f"{v['market']}|ALL", v)
        v["label"] = LABEL_EDGE if v["ci_label"] == LABEL_EDGE and whole["ci_label"] == LABEL_EDGE else LABEL_REF
    return out


def labels_from_metrics(metrics: dict[str, dict]) -> dict[tuple[str, str], str]:
    """{(mercado, div): etiqueta} para predict_match, solo con las celdas por liga."""
    return {(v["market"], v["div"]): v["label"] for v in metrics.values() if v["div"] != "ALL"}
