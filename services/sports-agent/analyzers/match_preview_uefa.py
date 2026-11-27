"""
services/sports-agent/analyzers/match_preview_uefa.py
Europa (Champions League, Europa League, Conference League) con el modelo mp-e1. Puro: sin Firestore,
sin red, no escribe nada (lo usan scripts/ops/uefa_backtest.py y las fichas de Europa).

Datos:
  - Partidos europeos: hl_team_stats grupo "uefa" (Highlightly; todos los partidos de UEFA_SEASONS,
    previas incluidas). Mismas cuentas que copas (mc.cup_stats): roja ausente con el resto presente = 0;
    amarilla ausente = sin dato (las líneas de tarjetas de ese lado no cuentan).
  - Fuerza de cada equipo en su liga: el History de mp-v1 con los CSV de football-data.co.uk de las 8
    ligas, las segundas y EUROPE_DIVS (B1, T1, G1, SC0; cero peticiones a Highlightly).
  - Id de Highlightly → nombre de football-data: por id (votos sobre hl_matches, mc.hl_id_names) y, si
    no está, por parecido único ≥ MIN_SIM con los equipos de las ligas con CSV de esa temporada.

Modelo mp-e1, por estadística (walk-forward: cada partido solo con lo jugado ANTES de su fecha):
  log λ_local = log(media de la competición en casa) + log(ataque del local) + log(defensa del visitante)
                + β_ataque[liga del local] + β_defensa[liga del visitante]
  y lo simétrico para el visitante. Ataque/defensa = índices de mp-v1 en su liga (relativos a la media
  de esa liga, encogidos hacia 1). Un lado sin datos de liga (liga sin CSV o menos de MIN_EFF partidos
  ponderados en esa estadística) cuenta como equipo medio de la liga OTHER. β: regresión de Poisson con
  desplazamiento y ridge (encogida hacia 0 con RIDGE), con los partidos europeos anteriores; con menos de
  MIN_FIT partidos, β = 0. "mp-v1 sin factor" = lo mismo con β = 0.
  Media de la competición (y su frecuencia por línea, la referencia principal): lo jugado esta temporada
  en esa competición antes del partido (peso 1.0) + la temporada anterior (0.5); con menos de MIN_COMP
  partidos, las tres competiciones juntas con los mismos pesos.
Etiquetas: IC95 por bootstrap agrupado por partido (las líneas de un partido no son independientes).
  "Medido en backtest" por mercado (todas las competiciones) solo si el IC95 de (mp-e1 − frecuencia de la
  competición) queda entero bajo 0; por competición solo con ≥ MIN_COMP_EVAL partidos evaluables.
  Estrella ("ventaja medida", star_labels, desde el 27-nov): el mismo IC95 entero bajo 0 en todo el
  periodo (y en las temporadas limpias juntas, sin DESIGN_SEASONS) Y, en RECENT_SEASONS juntas, el
  modelo no peor que la referencia (diferencia media ≤ 0). Antes: regla de
  copas (50 graduados en directo); mc.final_labels la sigue aplicando encima (nunca quita una ⭐).
"""
import math
from collections import Counter, defaultdict
from typing import Callable, Iterable, Optional

import numpy as np

from analyzers import match_preview as mp
from analyzers import match_preview_cups as mc
from collectors.fdco_team_matches import EUROPE_DIVS, LOWER_DIVS, MAIN_DIVS

MODEL_VERSION = "mp-e1"
COMPS = ("CL", "EL", "ECL")
COMP_NAMES = {"CL": "Champions League", "EL": "Europa League", "ECL": "Conference League"}
OTHER = "OTRA"                       # liga de los lados sin datos de liga
ALL_DIVS = (*MAIN_DIVS, *LOWER_DIVS, *EUROPE_DIVS)
MIN_EFF = 5.0                        # partidos de liga ponderados para contar como "con datos"
MIN_COMP = 40                        # partidos de la competición para su propia media
MIN_FIT = 60                         # partidos europeos anteriores para ajustar β
RIDGE = 20.0
NEWTON_ITERS = 30
MIN_COMP_EVAL = 300                  # partidos evaluables para etiquetar por competición
N_BOOT = 1000
EVAL_SEASONS = ("2122", "2223", "2324", "2425", "2526", "2627")    # 2020/21 solo calienta
# Temporadas europeas usadas para diseñar mp-e1: ninguna. El modelo y sus parámetros (4e2c31e, 24-nov)
# se escribieron antes de cargar los partidos europeos (25-nov) y no se han tocado desde entonces; el
# único backtest visto (25-nov) no cambió nada. Si un ajuste futuro se decide mirando una temporada,
# va aquí y sale de las temporadas limpias.
DESIGN_SEASONS: tuple[str, ...] = ()
# Temporadas recientes (decisión del usuario, 27-nov): con ellas juntas, el modelo no puede salir peor
# que la referencia (diferencia media de Brier por partido ≤ 0) para llevar ⭐.
RECENT_SEASONS: tuple[str, ...] = ("2526", "2627")
MIN_SIM = 0.80
COVERAGES = ("ambos", "un_lado")
VARIANTS = ("e1_vs_comp", "v1_vs_comp", "e1_vs_v1")
LABEL_BACKTEST = mc.LABEL_BACKTEST


# ── Datos ────────────────────────────────────────────────────────────────────────

def team_divs_all(hist: mp.History) -> dict[tuple[str, str], str]:
    """{(equipo, temporada): división} con todas las ligas con CSV (la principal gana a la segunda)."""
    out: dict = {}
    for (div, season), ms in hist.by_ds.items():
        for m in ms:
            for t in (m["home"], m["away"]):
                if (t, season) not in out or div in MAIN_DIVS or div in EUROPE_DIVS:
                    out[(t, season)] = div
    return out


def _best_unique(name: str, cands: Iterable[str], similarity, min_sim: float) -> Optional[str]:
    scored = sorted(((similarity(name or "", c), c) for c in cands), reverse=True)
    if not scored or scored[0][0] < min_sim or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1]


def make_resolver(ids: dict, tdiv: dict[tuple[str, str], str], similarity,
                  min_sim: float = MIN_SIM) -> Callable[[object, str, str], Optional[str]]:
    """resolve(id de Highlightly, nombre de Highlightly, temporada) → nombre de football-data o None.
    Primero por id (si ese equipo tiene liga esa temporada o la anterior); si no, por parecido único."""
    by_season: dict[str, set] = defaultdict(set)
    for t, s in tdiv:
        by_season[s].add(t)
    cache: dict = {}

    def resolve(hl_id, hl_name: str, season: str) -> Optional[str]:
        cands = by_season.get(season, set()) | by_season.get(mp.prev_season(season), set())
        n = ids.get(hl_id, ids.get(str(hl_id))) if hl_id is not None else None
        if n in cands:
            return n
        key = (hl_name, season)
        if key not in cache:
            cache[key] = _best_unique(hl_name, cands, similarity, min_sim) if hl_name else None
        return cache[key]

    return resolve


def uefa_matches(docs: Iterable[dict], resolve, counts: Counter) -> list[dict]:
    """Docs de hl_team_stats (grupo uefa) → partidos utilizables por fecha. Un equipo sin emparejar se
    queda con nombre None (cuenta como "sin datos"). Cuenta los descartes en counts."""
    out = []
    for d in docs:
        counts["uefa_leidos"] += 1
        comp = d.get("comp")
        if comp not in COMPS:
            counts["descartado_sin_competicion"] += 1
            continue
        if d.get("status") != "done" or not d.get("finished", True):
            counts["descartado_sin_detalle"] += 1
            continue
        st = mc.cup_stats(d)
        if st is None:
            counts["descartado_sin_estadisticas"] += 1
            continue
        date = (d.get("date") or "")[:10]
        if len(date) != 10:
            counts["descartado_sin_fecha"] += 1
            continue
        season = mc.season_of(date)
        home = resolve(d.get("home_team_id"), d.get("home_team") or "", season)
        away = resolve(d.get("away_team_id"), d.get("away_team") or "", season)
        counts["equipos_emparejados"] += (home is not None) + (away is not None)
        out.append({"comp": comp, "season": season, "date": date, "key": f"{comp}_{date}_{d.get('match_id')}",
                    "home": home, "away": away, "h": st["h"], "a": st["a"]})
    out.sort(key=lambda m: (m["date"], m["key"]))
    counts["uefa_utilizables"] = len(out)
    return out


# ── Factor de liga: Poisson con desplazamiento + ridge ───────────────────────────

def fit_league_factors(obs: list[tuple], ridge: float = RIDGE, iters: int = NEWTON_ITERS,
                       start: Optional[dict] = None) -> dict:
    """obs: [(y, desplazamiento, liga que ataca, liga que defiende)] → {("att"|"def", liga): β}.
    Newton con penalización ridge (encogida hacia 0); start = β de un ajuste anterior (arranque en
    caliente)."""
    if not obs:
        return {}
    leagues = sorted({o[2] for o in obs} | {o[3] for o in obs})
    idx = {lg: i for i, lg in enumerate(leagues)}
    n_l = len(leagues)
    p = 2 * n_l
    y = np.array([o[0] for o in obs], dtype=float)
    off = np.array([o[1] for o in obs], dtype=float)
    ia = np.array([idx[o[2]] for o in obs])
    idd = np.array([n_l + idx[o[3]] for o in obs])
    beta = np.zeros(p)
    for lg, i in idx.items():
        if start:
            beta[i] = start.get(("att", lg), 0.0)
            beta[n_l + i] = start.get(("def", lg), 0.0)
    for _ in range(iters):
        mu = np.exp(np.clip(off + beta[ia] + beta[idd], -30, 30))
        r = y - mu
        g = np.bincount(ia, r, p) + np.bincount(idd, r, p) - ridge * beta
        h = (np.bincount(ia * p + ia, mu, p * p) + np.bincount(idd * p + idd, mu, p * p)
             + np.bincount(ia * p + idd, mu, p * p) + np.bincount(idd * p + ia, mu, p * p)).reshape(p, p)
        h += ridge * np.eye(p)
        step = np.linalg.solve(h, g)
        beta += step
        if float(np.max(np.abs(step))) < 1e-9:
            break
    return {**{("att", lg): float(beta[i]) for lg, i in idx.items()},
            **{("def", lg): float(beta[n_l + i]) for lg, i in idx.items()}}


# ── Motor walk-forward ───────────────────────────────────────────────────────────

class Engine:
    """Estado del walk-forward: partidos europeos ya jugados (con sus desplazamientos) y β por
    estadística. add() solo con partidos de fechas ya cerradas; refit() antes de predecir."""

    def __init__(self, hist: mp.History, tdiv: dict, k: float = mp.SHRINK_K, ridge: float = RIDGE,
                 min_fit: Optional[int] = None):
        self.hist, self.tdiv, self.k, self.ridge = hist, tdiv, k, ridge
        self.min_fit = MIN_FIT if min_fit is None else min_fit
        self.by_cs: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.by_season: dict[str, list[dict]] = defaultdict(list)
        self.obs: dict[str, list[tuple]] = defaultdict(list)
        self.beta: dict[str, dict] = {}
        self.n_fit = 0

    # Fuerza de un lado en su liga
    def side(self, team: Optional[str], season: str, date: str) -> dict:
        """{'has_data', 'n_eff', 'n_cur', 'div', 'league': {stat: liga}, 'att': {stat}, 'def': {stat}}"""
        div = None
        if team:
            div = self.tdiv.get((team, season)) or self.tdiv.get((team, mp.prev_season(season)))
        out = {"team": team, "div": div, "has_data": False, "n_eff": 0.0, "n_cur": 0,
               "league": {}, "att": {}, "def": {}}
        hh = self.hist.team_history(team, div, season, date) if div else []
        out["n_eff"] = round(sum(w for w, *_ in hh), 2)
        out["n_cur"] = sum(1 for w, *_ in hh if w == mp.W_CUR)
        out["has_data"] = out["n_eff"] >= MIN_EFF
        for s in mp.STATS:
            a = d = 1.0
            lg = OTHER
            if out["has_data"]:
                a_, d_, n_s = self.hist.indices(hh, s, date, season, self.k)
                if n_s >= MIN_EFF:
                    a, d, lg = a_, d_, div
            out["att"][s], out["def"][s], out["league"][s] = a, d, lg
        return out

    # Ventana de la competición: (peso, partido)
    def window(self, comp: str, season: str) -> list[tuple[float, dict]]:
        prev = mp.prev_season(season)
        win = ([(mp.W_CUR, m) for m in self.by_cs.get((comp, season), [])]
               + [(mp.W_PREV_SAME, m) for m in self.by_cs.get((comp, prev), [])])
        if len(win) >= MIN_COMP:
            return win
        return ([(mp.W_CUR, m) for m in self.by_season.get(season, [])]
                + [(mp.W_PREV_SAME, m) for m in self.by_season.get(prev, [])])

    @staticmethod
    def comp_means(win: list[tuple[float, dict]], s: str) -> tuple[Optional[float], Optional[float]]:
        out = []
        for side in ("h", "a"):
            num = den = 0.0
            for w, m in win:
                v = m[side][s]
                if v is not None:
                    num += w * v
                    den += w
            out.append(num / den if den else None)
        return out[0], out[1]

    @staticmethod
    def comp_line_rate(win: list[tuple[float, dict]], s: str, side: Optional[str], line: float) -> Optional[float]:
        num = den = 0.0
        for w, m in win:
            if side:
                y = m[side][s]
            else:
                y = None if m["h"][s] is None or m["a"][s] is None else m["h"][s] + m["a"][s]
            if y is not None:
                den += w
                num += w * (y > line)
        return num / den if den else None

    def refit(self) -> None:
        n = len({m["key"] for ms in self.by_season.values() for m in ms})
        self.n_fit = n
        if n < self.min_fit:
            self.beta = {}
            return
        self.beta = {s: fit_league_factors(self.obs[s], self.ridge, start=self.beta.get(s)) for s in mp.STATS}

    def factor(self, s: str, att_league: str, def_league: str) -> float:
        b = self.beta.get(s) or {}
        return math.exp(b.get(("att", att_league), 0.0) + b.get(("def", def_league), 0.0))

    def expected(self, comp: str, season: str, date: str, home: Optional[str], away: Optional[str]) -> dict:
        """Esperados por estadística con y sin factor de liga, con la ventana de la competición."""
        sh, sa = self.side(home, season, date), self.side(away, season, date)
        win = self.window(comp, season)
        stats = {}
        for s in mp.STATS:
            mh, ma = self.comp_means(win, s)
            if not mh or not ma:
                stats[s] = None
                continue
            lam_v1 = mh * sh["att"][s] * sa["def"][s]
            mu_v1 = ma * sa["att"][s] * sh["def"][s]
            stats[s] = {"mh": mh, "ma": ma, "lam_v1": lam_v1, "mu_v1": mu_v1,
                        "lam": lam_v1 * self.factor(s, sh["league"][s], sa["league"][s]),
                        "mu": mu_v1 * self.factor(s, sa["league"][s], sh["league"][s]),
                        "lh": sh["league"][s], "la": sa["league"][s]}
        return {"home": sh, "away": sa, "win": win, "stats": stats}

    def add(self, m: dict, exp: dict) -> None:
        """Partido ya jugado → ventanas y observaciones del ajuste (desplazamiento sin factor)."""
        self.by_cs[(m["comp"], m["season"])].append(m)
        self.by_season[m["season"]].append(m)
        for s, e in exp["stats"].items():
            if not e:
                continue
            if m["h"][s] is not None and e["lam_v1"] > 0:
                self.obs[s].append((m["h"][s], math.log(e["lam_v1"]), e["lh"], e["la"]))
            if m["a"][s] is not None and e["mu_v1"] > 0:
                self.obs[s].append((m["a"][s], math.log(e["mu_v1"]), e["la"], e["lh"]))


def coverage(exp: dict) -> Optional[str]:
    n = exp["home"]["has_data"] + exp["away"]["has_data"]
    return {2: "ambos", 1: "un_lado"}.get(n)


# ── Backtest ────────────────────────────────────────────────────────────────────

def backtest_rows(engine: Engine, matches: list[dict], counts: Counter,
                  seasons: Optional[Iterable[str]] = None) -> dict[str, list[tuple]]:
    """Walk-forward por fecha. Filas {"{variante}|{cobertura}": [(temporada, competición, mercado,
    línea, y, p_referencia, p_modelo, clave del partido)]}, variantes: e1_vs_comp (mp-e1 frente a la
    frecuencia de la competición), v1_vs_comp (mp-v1 sin factor frente a la competición), e1_vs_v1."""
    seasons = set(EVAL_SEASONS if seasons is None else seasons)
    rows: dict[str, list[tuple]] = {f"{v}|{c}": [] for v in VARIANTS for c in COVERAGES}
    by_date: dict[str, list[dict]] = defaultdict(list)
    for m in matches:
        by_date[m["date"]].append(m)
    for date in sorted(by_date):
        engine.refit()
        day = []
        for m in by_date[date]:
            exp = engine.expected(m["comp"], m["season"], date, m["home"], m["away"])
            day.append((m, exp))
            if m["season"] not in seasons:
                counts["calentamiento"] += 1
                continue
            cov = coverage(exp)
            if cov is None:
                counts["sin_datos_ningun_lado"] += 1
                continue
            counts[f"evaluados_{cov}"] += 1
            counts[f"evaluados_{m['comp']}"] += 1
            if not engine.beta:
                counts["evaluados_sin_factor_aun"] += 1
            _rows_for(rows, m, exp, engine, cov)
        for m, exp in day:
            engine.add(m, exp)
    return rows


def _rows_for(rows, m, exp, engine: Engine, cov: str) -> None:
    for s in mp.STATS:
        e = exp["stats"][s]
        if not e:
            continue
        team_lines, total_lines = mp.LINES[s]
        r = mp.DISPERSION.get((s, "team"))
        for side, mean, mean_v1 in (("h", e["lam"], e["lam_v1"]), ("a", e["mu"], e["mu_v1"])):
            if m[side][s] is None:
                continue
            for ln in team_lines:
                pc = engine.comp_line_rate(exp["win"], s, side, ln)
                _add(rows, cov, m, mp.market_key(s, "team"), ln, int(m[side][s] > ln), pc,
                     mp.p_over(ln, mean, r), mp.p_over(ln, mean_v1, r))
        if m["h"][s] is None or m["a"][s] is None:
            continue
        r = mp.DISPERSION.get((s, "total"))
        yt = m["h"][s] + m["a"][s]
        for ln in total_lines:
            pc = engine.comp_line_rate(exp["win"], s, None, ln)
            _add(rows, cov, m, mp.market_key(s, "total"), ln, int(yt > ln), pc,
                 mp.p_over(ln, e["lam"] + e["mu"], r), mp.p_over(ln, e["lam_v1"] + e["mu_v1"], r))


def _add(rows, cov, m, mk, ln, y, p_comp, p_e1, p_v1) -> None:
    base = (m["season"], m["comp"], mk, ln, y)
    if p_comp is not None:
        rows[f"e1_vs_comp|{cov}"].append((*base, p_comp, p_e1, m["key"]))
        rows[f"v1_vs_comp|{cov}"].append((*base, p_comp, p_v1, m["key"]))
    rows[f"e1_vs_v1|{cov}"].append((*base, p_v1, p_e1, m["key"]))


def bootstrap_ci(per_match: list[float], n_boot: int = N_BOOT, seed: int = 7) -> tuple[float, float]:
    """IC95 percentil de la media por bootstrap de partidos."""
    arr = np.asarray(per_match, dtype=float)
    if len(arr) < 2:
        return float("-inf"), float("inf")
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, len(arr), (n_boot, len(arr)))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def market_metrics(rows: list[tuple], n_boot: int = N_BOOT, min_comp: int = MIN_COMP_EVAL) -> dict[str, dict]:
    """Por mercado y competición ("{mercado}|{comp}") y por mercado en las tres ("{mercado}|ALL"):
    partidos, Brier de referencia y de modelo, diferencia media por partido con IC95 bootstrap y
    etiqueta del IC. Una competición con menos de min_comp partidos no se etiqueta ("pocos partidos").
    El campo de la competición se llama "div" (como mp-v1 y copas: mc.labels_from_backtest)."""
    groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in rows:
        groups[(r[2], r[1])].append(r)
        groups[(r[2], "ALL")].append(r)
    out = {}
    for (mk, comp), sub in sorted(groups.items()):
        per = defaultdict(list)
        for r in sub:
            per[r[7]].append((r[6] - r[4]) ** 2 - (r[5] - r[4]) ** 2)
        d = [sum(v) / len(v) for v in per.values()]
        lo, hi = bootstrap_ci(d, n_boot)
        bl = sum((r[5] - r[4]) ** 2 for r in sub) / len(sub)
        bm = sum((r[6] - r[4]) ** 2 for r in sub) / len(sub)
        few = comp != "ALL" and len(d) < min_comp
        out[f"{mk}|{comp}"] = {
            "market": mk, "div": comp, "n_matches": len(d), "n_lines": len(sub),
            "brier_ref": round(bl, 5), "brier_model": round(bm, 5),
            "bss_vs_ref": round(1 - bm / bl, 4) if bl else None,
            "delta": round(sum(d) / len(d), 5),
            "ci_lo": round(lo, 5) if math.isfinite(lo) else None,
            "ci_hi": round(hi, 5) if math.isfinite(hi) else None,
            "ci_label": "pocos partidos" if few else (mp.LABEL_EDGE if hi < 0 else mp.LABEL_REF),
        }
    return out


def labels_from_backtest(metrics: dict[str, dict]) -> dict[str, str]:
    """{mercado: "medido en backtest" | "solo referencia"} con las tres competiciones juntas (como
    copas: nunca estrella desde el backtest)."""
    return mc.labels_from_backtest(metrics)


def clean_seasons(evals: Iterable[str]) -> list[str]:
    """Temporadas evaluadas que no se usaron para diseñar mp-e1 (sin DESIGN_SEASONS)."""
    return [s for s in evals if s not in DESIGN_SEASONS]


def season_metrics(rows: list[tuple], n_boot: int = N_BOOT) -> dict[str, dict[str, dict]]:
    """{temporada: market_metrics de las filas de esa temporada} (desglose del backtest)."""
    by: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    return {s: market_metrics(sub, n_boot=n_boot) for s, sub in sorted(by.items())}


def star_labels(met_all: dict[str, dict], met_clean: Optional[dict[str, dict]] = None,
                met_recent: Optional[dict[str, dict]] = None) -> dict[str, str]:
    """{mercado: "ventaja medida" | "solo referencia"} (decisión del usuario, 27-nov). ⭐ desde el
    backtest si el IC95 por partido de (Brier mp-e1 − Brier de la frecuencia de la competición) queda
    entero bajo 0 con las tres competiciones juntas en todo el periodo, también en las temporadas
    limpias juntas (met_clean; None = son las mismas) y, si se pasa met_recent (RECENT_SEASONS juntas),
    ahí el modelo no sale peor que la referencia (diferencia media ≤ 0; sin partidos = no cumple)."""
    def cell(met: dict, mk: str) -> dict:
        return met.get(f"{mk}|ALL") or {}

    out = {}
    for mk in sorted({v["market"] for v in met_all.values()}):
        ok = cell(met_all, mk).get("ci_label") == mp.LABEL_EDGE
        ok = ok and (met_clean is None or cell(met_clean, mk).get("ci_label") == mp.LABEL_EDGE)
        if met_recent is not None:
            d = cell(met_recent, mk).get("delta")
            ok = ok and d is not None and d <= 0
        out[mk] = mp.LABEL_EDGE if ok else mp.LABEL_REF
    return out


def comp_labels(metrics: dict[str, dict]) -> dict[str, dict[str, str]]:
    """{competición: {mercado: etiqueta}} solo de las competiciones con ≥ MIN_COMP_EVAL partidos."""
    out: dict = defaultdict(dict)
    for v in metrics.values():
        if v["div"] != "ALL" and v["ci_label"] != "pocos partidos":
            out[v["div"]][v["market"]] = LABEL_BACKTEST if v["ci_label"] == mp.LABEL_EDGE else mp.LABEL_REF
    return dict(out)


# ── Ficha de un partido ─────────────────────────────────────────────────────────

def predict_uefa_match(engine: Engine, comp: str, season: str, match_date: str, home: Optional[str],
                       away: Optional[str], labels: Optional[dict[str, str]] = None) -> dict:
    """Ficha con lo jugado ANTES de match_date (el motor ya con add() de lo anterior y refit()).
    Misma forma que mp.predict_match (stats → lines → home/away/total), con la frecuencia de la
    competición en el sitio de la de la liga. labels: {mercado: etiqueta} (mc.final_labels).
    Un lado sin datos de liga → "muestra insuficiente" (se calcula, no se resalta)."""
    labels = labels or {}
    exp = engine.expected(comp, season, match_date, home, away)
    low = coverage(exp) != "ambos" or exp["home"]["n_cur"] < mp.MIN_CUR or exp["away"]["n_cur"] < mp.MIN_CUR
    teams = {side: {"team": exp[side]["team"], "div": exp[side]["div"], "n_cur": exp[side]["n_cur"],
                    "n_eff": exp[side]["n_eff"], "has_data": exp[side]["has_data"],
                    "insufficient": not exp[side]["has_data"] or exp[side]["n_cur"] < mp.MIN_CUR,
                    "attack": {s: round(exp[side]["att"][s], 4) for s in mp.STATS},
                    "defence": {s: round(exp[side]["def"][s], 4) for s in mp.STATS}}
             for side in ("home", "away")}
    stats: dict = {}
    for s in mp.STATS:
        e = exp["stats"][s]
        if not e:
            stats[s] = {"available": False, "reason": "sin media de la competición"}
            continue
        lam, mu = e["lam"], e["mu"]
        team_lines, total_lines = mp.LINES[s]
        block = {"available": True, "league_mean_home": round(e["mh"], 3), "league_mean_away": round(e["ma"], 3),
                 "exp_home": round(lam, 3), "exp_away": round(mu, 3), "exp_total": round(lam + mu, 3),
                 "factor_home": round(lam / e["lam_v1"], 4) if e["lam_v1"] else None,
                 "factor_away": round(mu / e["mu_v1"], 4) if e["mu_v1"] else None, "lines": {}}
        for scope, side, mean, lines in (("team", "h", lam, team_lines), ("team", "a", mu, team_lines),
                                         ("total", None, lam + mu, total_lines)):
            mk = mp.market_key(s, scope)
            label = labels.get(mk, mp.LABEL_REF)
            r = mp.DISPERSION.get((s, scope))
            out = {}
            for ln in sorted(set(lines) | {mp.line_near_half(mean, r)}):
                pm = mp.p_over(ln, mean, r)
                pc = engine.comp_line_rate(exp["win"], s, side, ln)
                out[mp.line_key(ln)] = {"line": ln, "p_model": round(pm, 4),
                                        **({"extra": True} if ln not in lines else {}),
                                        "p_league": None if pc is None else round(pc, 4),
                                        "diff_vs_league": None if pc is None else round(pm - pc, 4),
                                        "league_n": round(sum(w for w, _ in exp["win"]), 1)}
            block["lines"][{"h": "home", "a": "away", None: "total"}[side]] = {
                "market": mk, "label": mp.LABEL_LOW_SAMPLE if low else label, "market_label": label,
                "highlight": label == mp.LABEL_EDGE and not low,
                "distribution": "binomial negativa" if r else "Poisson", "r": r, "lines": out,
                "main_line": mp.main_line(out)}
        stats[s] = block
    used = [m["date"] for w, m in exp["win"]]
    return {"model_version": MODEL_VERSION, "comp": comp, "season": season, "match_date": match_date,
            "params": {"k": engine.k, "ridge": engine.ridge, "min_eff": MIN_EFF, "min_comp": MIN_COMP,
                       "min_fit": engine.min_fit, "n_fit": engine.n_fit},
            "data_cutoff": max(used, default=None), "low_sample": low, "teams": teams, "stats": stats}
