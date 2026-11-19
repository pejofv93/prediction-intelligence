"""
services/sports-agent/analyzers/match_preview_nt.py

Ficha de partido para selecciones masculinas (mp-n1) — de momento SOLO el backtest. Puro: sin
Firestore, sin red, sin escribir nada. Datos: hl_team_stats (grupo "nt") de Highlightly.

Modelo (plan aprobado):
  λ (lado i) = base(sede, tipo) × ataque_i × defensa_rival
  base(sede, tipo) = media global de la ventana × factor de sede (casa / fuera / neutral)
                     × factor de competición (oficial / amistoso); factores encogidos hacia 1.
  Ataque/defensa ajustados por rival: media ponderada de (lo suyo / (base × índice del rival)),
  en ITERATIONS pasadas; ventana de WINDOW_DAYS (24 meses) con decaimiento exponencial
  (vida media HALF_LIFE_DAYS); encogido hacia 1 con K = 8 partidos ficticios de equipo medio.
  Mínimo MIN_MATCHES partidos con esa estadística en la ventana para los DOS equipos.
  Probabilidad de "más de X": binomial negativa con r ajustada en el tramo de ajuste (antes de
  FIT_END) donde hay sobredispersión; Poisson en el resto. Líneas también del tramo de ajuste.
Sede: Highlightly no guarda si el campo es neutral → fases finales (Euro, Copa América,
  Mundial) = neutral; el resto = casa/fuera (aproximación: anfitriones y amistosos en campo
  neutral quedan mal clasificados).
Referencias: "competición" = frecuencia de la línea en la ventana con el mismo tipo de
  competición y la misma sede (mínimo MIN_REF valores; si no, todo); "global" = toda la ventana.
IC95 por bootstrap sobre partidos (las líneas de un mismo partido no son independientes).

Datos de Highlightly: SOLO estadísticas. Sin cuotas; el motor de apuestas no lee nada de aquí.
"""
import bisect
import math
from collections import defaultdict
from datetime import date as _date
from typing import Iterable, Iterator, Optional

import numpy as np

from analyzers import match_preview as mp
from analyzers import match_preview_women as mw

MODEL_VERSION = "mp-n1"
WINDOW_DAYS = 730
HALF_LIFE_DAYS = 365.0
SHRINK_K = 8.0
FACTOR_K = 20.0          # pseudo-observaciones para los factores de sede y competición
MIN_MATCHES = 6
ITERATIONS = 3
MIN_REF = 40
FIT_END = "2023-01-01"   # antes: ajuste de r y líneas; desde aquí: backtest
BOOT_REPS = 2000

# id de liga de Highlightly → grupo de competición
NT_GROUPS = {28016: "WCQ_EU", 29718: "WCQ_SA", 817744: "EUROQ", 5039: "UNL",
             4188: "EURO", 8443: "COPA", 1635: "MUNDIAL", 9294: "AMIST"}
NEUTRAL = {4188, 8443, 1635}
FRIENDLY = {9294}
GROUP_NAMES = {"WCQ_EU": "Clasif. Mundial Europa", "WCQ_SA": "Clasif. Mundial Sudamérica",
               "EUROQ": "Clasif. Euro", "UNL": "Nations League", "EURO": "Euro",
               "COPA": "Copa América", "MUNDIAL": "Mundial", "AMIST": "Amistosos", "OTRA": "Otra"}
FIT, BT = "FIT", "BT"    # "temporada" de cada partido: tramo de ajuste o de backtest


def _ord(d: str) -> int:
    return _date.fromisoformat(d[:10]).toordinal()


def matches_from_hl(docs: Iterable[dict], fit_end: str = FIT_END) -> list[dict]:
    """Docs de hl_team_stats (grupo nt) → partidos. Solo terminados, con marcador y fecha.
    Estadística que falte = None (no cuenta para esa estadística)."""
    out = []
    for d in docs:
        if d.get("group", "nt") != "nt" or not d.get("finished") or not d.get("date"):
            continue
        gh, ga = d.get("goals_home"), d.get("goals_away")
        if gh is None or ga is None or d.get("home_team_id") is None or d.get("away_team_id") is None:
            continue
        lid = d.get("league_id")
        st = d.get("stats") if d.get("status") == "done" else None
        h, a = mw._side(st, "home"), mw._side(st, "away")
        h["goals"], a["goals"] = gh, ga
        dt = d["date"][:10]
        out.append({"div": NT_GROUPS.get(lid, "OTRA"), "season": FIT if dt < fit_end else BT, "date": dt,
                    "ord": _ord(dt), "home": str(d["home_team_id"]), "away": str(d["away_team_id"]),
                    "neutral": lid in NEUTRAL, "friendly": lid in FRIENDLY, "h": h, "a": a})
    out.sort(key=lambda m: m["date"])
    return out


def coverage(matches: list[dict]) -> dict:
    """{(grupo, año): {matches, con cada estadística en los dos lados}} — solo recuentos."""
    out: dict = defaultdict(lambda: {"matches": 0, **{s: 0 for s in mp.STATS}})
    for m in matches:
        c = out[(m["div"], m["date"][:4])]
        c["matches"] += 1
        for s in mp.STATS:
            if m["h"][s] is not None and m["a"][s] is not None:
                c[s] += 1
    return dict(out)


def _roles(m: dict) -> tuple[str, str]:
    return ("N", "N") if m["neutral"] else ("H", "A")


class Ratings:
    """Índices de todos los equipos con lo jugado en la ventana ANTES de `day` (ordinal)."""

    def __init__(self, matches: list[dict], stat: str, day: int, k: float = SHRINK_K,
                 window: int = WINDOW_DAYS, half_life: float = HALF_LIFE_DAYS, iterations: int = ITERATIONS):
        self.stat = stat
        rows = []
        for m in matches:
            if m["ord"] >= day:
                break                                  # matches va ordenado por fecha
            if m["ord"] < day - window or m["h"][stat] is None or m["a"][stat] is None:
                continue
            w = 0.5 ** ((day - m["ord"]) / half_life)
            rh, ra = _roles(m)
            rows.append((w, m["home"], m["away"], rh, ra, m["friendly"], m["h"][stat], m["a"][stat]))
        self.rows = rows
        self._cache: dict = {}
        self.count: dict[str, int] = defaultdict(int)
        for _, h, a, *_ in rows:
            self.count[h] += 1
            self.count[a] += 1
        sw = sum(2 * r[0] for r in rows)
        self.overall = sum(r[0] * (r[6] + r[7]) for r in rows) / sw if sw else None
        self.f_role = {"H": 1.0, "A": 1.0, "N": 1.0}
        self.f_type = {True: 1.0, False: 1.0}
        if not self.overall:
            self.att, self.dfn = {}, {}
            return
        role_acc, type_acc = defaultdict(lambda: [0.0, 0.0]), defaultdict(lambda: [0.0, 0.0])
        for w, _, _, rh, ra, fr, yh, ya in rows:
            for role, y in ((rh, yh), (ra, ya)):
                role_acc[role][0] += w * y
                role_acc[role][1] += w
                type_acc[fr][0] += w * y
                type_acc[fr][1] += w
        o = self.overall
        for role, (sy, sw_) in role_acc.items():
            self.f_role[role] = (sy + FACTOR_K * o) / (sw_ + FACTOR_K) / o
        for fr, (sy, sw_) in type_acc.items():
            self.f_type[fr] = (sy + FACTOR_K * o) / (sw_ + FACTOR_K) / o
        att: dict[str, float] = defaultdict(lambda: 1.0)
        dfn: dict[str, float] = defaultdict(lambda: 1.0)
        for _ in range(iterations):
            sa, sd, sw2 = defaultdict(float), defaultdict(float), defaultdict(float)
            for w, h, a, rh, ra, fr, yh, ya in rows:
                bh, ba = self.base(rh, fr), self.base(ra, fr)
                if not bh or not ba:
                    continue
                sa[h] += w * yh / (bh * dfn[a])
                sa[a] += w * ya / (ba * dfn[h])
                sd[h] += w * ya / (ba * att[a])
                sd[a] += w * yh / (bh * att[h])
                sw2[h] += w
                sw2[a] += w
            att = defaultdict(lambda: 1.0, {t: (sa[t] + k) / (sw2[t] + k) for t in sw2})
            dfn = defaultdict(lambda: 1.0, {t: (sd[t] + k) / (sw2[t] + k) for t in sw2})
        self.att, self.dfn = att, dfn

    def base(self, role: str, friendly: bool) -> Optional[float]:
        if not self.overall:
            return None
        return self.overall * self.f_role[role] * self.f_type[friendly]

    def expected(self, m: dict) -> Optional[tuple[float, float]]:
        """(λ local, μ visitante) o None si falta media o algún equipo no llega al mínimo."""
        if not self.overall or self.count.get(m["home"], 0) < MIN_MATCHES or self.count.get(m["away"], 0) < MIN_MATCHES:
            return None
        rh, ra = _roles(m)
        return (self.base(rh, m["friendly"]) * self.att[m["home"]] * self.dfn[m["away"]],
                self.base(ra, m["friendly"]) * self.att[m["away"]] * self.dfn[m["home"]])

    def _vals(self, role: Optional[str], friendly: Optional[bool]) -> list:
        """Valores ordenados de la ventana. role: 'H'/'A'/'N' = de ese lado; ANY = cualquier lado;
        None = total del partido. friendly None = todas las competiciones."""
        key = (role, friendly)
        if key not in self._cache:
            vals = []
            for _, _, _, rh, ra, fr, yh, ya in self.rows:
                if friendly is not None and fr != friendly:
                    continue
                if role is None:
                    vals.append(yh + ya)
                else:
                    if role in (ANY, rh):
                        vals.append(yh)
                    if role in (ANY, ra):
                        vals.append(ya)
            self._cache[key] = sorted(vals)
        return self._cache[key]

    def ref_rate(self, line: float, role: Optional[str], friendly: Optional[bool]) -> Optional[float]:
        """Frecuencia de "más de line" en la ventana (sin decaimiento). Con menos de MIN_REF
        valores en (sede, tipo), los de cualquier sede y competición."""
        vals = self._vals(role, friendly)
        if len(vals) < MIN_REF:
            vals = self._vals(None if role is None else ANY, None)
        if not vals:
            return None
        return (len(vals) - bisect.bisect_right(vals, line)) / len(vals)


ANY = "*"


def predictions(matches: list[dict], period: Optional[str] = None, k: float = SHRINK_K) -> Iterator[dict]:
    """Walk-forward (generador): λ y μ de cada partido y estadística con lo anterior a su fecha.
    Los índices se calculan una vez por día y estadística; cada predicción lleva los del día (rt)
    para las referencias. period: solo los partidos de ese tramo (FIT/BT); None = todos."""
    by_day: dict[int, list[dict]] = defaultdict(list)
    for m in matches:
        if period is None or m["season"] == period:
            by_day[m["ord"]].append(m)
    for day in sorted(by_day):
        for s in mp.STATS:
            todays = [m for m in by_day[day] if m["h"][s] is not None and m["a"][s] is not None]
            if not todays:
                continue
            rt = Ratings(matches, s, day, k)
            for m in todays:
                e = rt.expected(m)
                if e is not None:
                    yield {"m": m, "stat": s, "lam": e[0], "mu": e[1], "rt": rt}


def fit_dispersion(preds: Iterable[dict], period: str = FIT) -> tuple[dict, dict]:
    """r = Σλ² / Σ((y − λ)² − λ) por (estadística, alcance) en el tramo de ajuste. None = Poisson."""
    acc = defaultdict(lambda: [0, 0.0, 0.0])
    for p in preds:
        m = p["m"]
        if m["season"] != period:
            continue
        s = p["stat"]
        for scope, lam, y in (("team", p["lam"], m["h"][s]), ("team", p["mu"], m["a"][s]),
                              ("total", p["lam"] + p["mu"], m["h"][s] + m["a"][s])):
            a = acc[(s, scope)]
            a[0] += 1
            a[1] += lam * lam
            a[2] += (y - lam) ** 2 - lam
    r_of = lambda a: round(a[1] / a[2], 2) if a[0] and a[2] > 0 else None
    disp = {key: r_of(a) for key, a in acc.items() if r_of(a) is not None}
    detail = {f"{s}_{sc}": {"n": a[0], "r": r_of(a)} for (s, sc), a in sorted(acc.items())}
    return disp, detail


def backtest_rows(preds: Iterable[dict], lines: dict, dispersion: dict, period: str = BT) -> dict[str, list[tuple]]:
    """Filas (season, grupo, mercado, línea, y, p_ref, p_modelo, match_key) del tramo de
    backtest, con dos referencias: 'competicion' (mismo tipo y sede) y 'global' (toda la ventana)."""
    out = {"modelo_vs_competicion": [], "modelo_vs_global": []}
    for p in preds:
        m, s, rt = p["m"], p["stat"], p["rt"]
        if m["season"] != period:
            continue
        key = f"{m['date']}_{m['home']}_{m['away']}"
        rh, ra = _roles(m)
        team_lines, total_lines = lines[s]
        sides = (("h", rh, p["lam"]), ("a", ra, p["mu"]))
        for side, role, mean in sides:
            for ln in team_lines:
                pm = mp.p_over(ln, mean, dispersion.get((s, "team")))
                y = int(m[side][s] > ln)
                for name, pr in (("modelo_vs_competicion", rt.ref_rate(ln, role, m["friendly"])),
                                 ("modelo_vs_global", rt.ref_rate(ln, ANY, None))):
                    if pr is not None:
                        out[name].append((m["season"], m["div"], mp.market_key(s, "team"), ln, y, pr, pm, key))
        yt = m["h"][s] + m["a"][s]
        for ln in total_lines:
            pm = mp.p_over(ln, p["lam"] + p["mu"], dispersion.get((s, "total")))
            for name, pr in (("modelo_vs_competicion", rt.ref_rate(ln, None, m["friendly"])),
                             ("modelo_vs_global", rt.ref_rate(ln, None, None))):
                if pr is not None:
                    out[name].append((m["season"], m["div"], mp.market_key(s, "total"), ln, int(yt > ln), pr, pm, key))
    return out


def boot_ci(sub: list[tuple], reps: Optional[int] = None, seed: int = 7) -> tuple[Optional[float], Optional[float]]:
    """IC95 percentil por bootstrap de la diferencia de Brier (modelo − referencia), remuestreando
    PARTIDOS (media por partido de sus líneas)."""
    per = defaultdict(list)
    for r in sub:
        y, pr, pm = r[4], r[5], r[6]
        per[r[7]].append((pm - y) ** 2 - (pr - y) ** 2)
    d = np.array([sum(v) / len(v) for v in per.values()])
    if len(d) < 2:
        return None, None
    reps = reps or BOOT_REPS
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, len(d), size=(reps, len(d)))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def metrics(rows: list[tuple], reps: Optional[int] = None) -> dict[str, dict]:
    """mp.market_metrics con el IC95 por bootstrap (el normal queda en ci_lo_normal/ci_hi_normal)
    y las etiquetas recalculadas con él (celda y mercado en todos los grupos)."""
    met = mp.market_metrics(rows)
    groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in rows:
        groups[(r[2], r[1])].append(r)
        groups[(r[2], "ALL")].append(r)
    for key, v in met.items():
        lo, hi = boot_ci(groups[(v["market"], v["div"])], reps)
        v["ci_lo_normal"], v["ci_hi_normal"] = v["ci_lo"], v["ci_hi"]
        v["ci_lo"] = None if lo is None else round(lo, 5)
        v["ci_hi"] = None if hi is None else round(hi, 5)
        v["ci_label"] = mp.LABEL_EDGE if hi is not None and hi < 0 else mp.LABEL_REF
    for v in met.values():
        whole = met.get(f"{v['market']}|ALL", v)
        v["label"] = mp.LABEL_EDGE if v["ci_label"] == mp.LABEL_EDGE and whole["ci_label"] == mp.LABEL_EDGE \
            else mp.LABEL_REF
    return met


def verdict(v: dict) -> str:
    if v["ci_hi"] is not None and v["ci_hi"] < 0:
        return "mejor el modelo"
    if v["ci_lo"] is not None and v["ci_lo"] > 0:
        return "peor el modelo"
    return "sin diferencia clara"
