"""Réplica de Sports v1 (fútbol 1X2) con los CSV públicos de football-data.co.uk.

La usa replica_fidelity.py (workflow sports-replica-fidelity, solo lectura) para medir cuánto se
parece al modelo real. Mismo cálculo que v1 en lo principal:
  - Poisson: fit_attack_defense de v1 (mismo Nelder-Mead, mismo punto de partida y opciones;
    aquí con la verosimilitud vectorizada) sobre los 20 últimos partidos de cada equipo.
  - Confianza: 1 − desviación de las señales del ensemble (Poisson, forma, ELO, h2h).
  - ELO 1500, K 32, ventaja local 100; forma = calculate_form_score de los 10 últimos.
Diferencias conocidas: solo partidos de liga del CSV (v1 usa football-data.org, con copas), y
faltan algunos filtros de contexto (movimiento de cuotas, top-6, clasificación, descanso).
"""
import csv
import io
from collections import defaultdict, deque
from datetime import datetime

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln

FDCO_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
# liga de v1 (football-data.org) → división de football-data.co.uk
V1_DIVS = {"PL": "E0", "PD": "SP1", "BL1": "D1", "SA": "I1", "FL1": "F1", "DED": "N1", "PPL": "P1",
           "ELC": "E1"}
RAW_KEEP, MIN_FIT = 20, 3
ELO0, K, HA = 1500.0, 32.0, 100.0
COLD = {"home_advantage": 0.25, "default_attack": 1.2, "default_defense": 1.0}


def parse_csv(text: str, div: str, season: str) -> list[dict]:
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if not r.get("HomeTeam") or r.get("FTHG") in (None, ""):
            continue
        d = r.get("Date") or ""
        try:
            dt = datetime.strptime(d, "%d/%m/%Y" if len(d) == 10 else "%d/%m/%y")
        except ValueError:
            continue
        out.append({"div": div, "season": season, "date": dt.date().isoformat(), "time": r.get("Time") or "",
                    "home": r["HomeTeam"].strip(), "away": r["AwayTeam"].strip(),
                    "gh": int(r["FTHG"]), "ga": int(r["FTAG"])})
    return out


def res_of(m: dict, team: str) -> str:
    gf, gc = (m["gh"], m["ga"]) if m["home"] == team else (m["ga"], m["gh"])
    return "W" if gf > gc else ("L" if gf < gc else "D")


def form_score(results: list[str]) -> float:
    """calculate_form_score de v1 (stats_processor)."""
    if not results:
        return 50.0
    pts = {"W": 3, "D": 1, "L": 0}
    a = sum(pts[r] / (i + 1) for i, r in enumerate(results))
    mx = sum(3 / (i + 1) for i in range(len(results)))
    return a / mx * 100.0


def fit_nelder_mead(matches: list[dict]) -> dict:
    """fit_attack_defense de v1 tal cual corre hoy (Nelder-Mead, maxiter 2000)."""
    count: dict[str, int] = {}
    for m in matches:
        for t in (m["home"], m["away"]):
            count[t] = count.get(t, 0) + 1
    teams = sorted(count)
    n = len(teams)
    idx = {t: i for i, t in enumerate(teams)}
    hi = np.array([idx[m["home"]] for m in matches])
    ai = np.array([idx[m["away"]] for m in matches])
    gh = np.array([m["gh"] for m in matches], dtype=float)
    ga = np.array([m["ga"] for m in matches], dtype=float)
    cgh, cga = gammaln(gh + 1), gammaln(ga + 1)
    x0 = np.zeros(2 * n + 1)
    x0[:n] = np.log(COLD["default_attack"])
    x0[n:2 * n] = np.log(COLD["default_defense"])
    x0[-1] = np.log(1.0 + COLD["home_advantage"])

    def nll(p):
        att, de, hm = np.exp(p[:n]), np.exp(p[n:2 * n]), np.exp(p[-1])
        lam = np.maximum(att[hi] * de[ai] * hm, 1e-6)
        mu = np.maximum(att[ai] * de[hi], 1e-6)
        return -float(((gh * np.log(lam) - lam - cgh) + (ga * np.log(mu) - mu - cga)).sum())

    r = minimize(nll, x0, method="Nelder-Mead", options={"maxiter": 2000, "xatol": 1e-5, "fatol": 1e-5})
    att, de = np.exp(r.x[:n]), np.exp(r.x[n:2 * n])
    out = {"home_advantage": max(0.0, float(np.exp(r.x[-1])) - 1.0)}
    for t in teams:
        i = idx[t]
        out[t] = ({"attack": COLD["default_attack"], "defense": COLD["default_defense"]} if count[t] < MIN_FIT
                  else {"attack": float(max(att[i], 0.1)), "defense": float(max(de[i], 0.1))})
    return out


def predict(home: str, away: str, params: dict) -> tuple[float, float, float]:
    """predict_match_probs de v1: Poisson 9×9 con corrección Dixon-Coles (rho −0,13)."""
    from scipy.stats import poisson
    hp, ap = params.get(home, {}), params.get(away, {})
    ha = params.get("home_advantage", COLD["home_advantage"])
    lam = max(hp.get("attack", 1.2) * ap.get("defense", 1.0) * (1 + ha), 0.1)
    mu = max(ap.get("attack", 1.2) * hp.get("defense", 1.0), 0.1)
    rho = -0.13
    corr = np.maximum(np.array([[1 - lam * mu * rho, 1 + lam * rho], [1 + mu * rho, 1 - rho]]), 0.0)
    m = np.outer(poisson.pmf(np.arange(9), lam), poisson.pmf(np.arange(9), mu))
    m[:2, :2] *= corr
    m /= m.sum()
    return float(np.tril(m, -1).sum()), float(np.trace(m)), float(np.triu(m, 1).sum())


class Replica:
    """Recorre los partidos en orden y da, para cualquier partido, el estado justo antes."""

    def __init__(self, rows: list[dict]):
        self.rows = sorted(rows, key=lambda m: (m["date"], m["time"], m["div"], m["home"]))

    def states_before(self, wanted: dict[tuple, dict]) -> dict[tuple, dict]:
        """wanted: {(div, fecha, local, visitante): …} → estado previo de cada uno que exista."""
        hist = defaultdict(deque)
        meet = defaultdict(list)
        elo = defaultdict(lambda: ELO0)
        out = {}
        for m in self.rows:
            key = (m["div"], m["date"], m["home"], m["away"])
            if key in wanted:
                rh, ra = list(hist[m["home"]]), list(hist[m["away"]])
                mt = meet[tuple(sorted((m["home"], m["away"])))][-10:]
                out[key] = {"raw_h": rh, "raw_a": ra, "elo_h": elo[m["home"]], "elo_a": elo[m["away"]],
                            "meet": mt, "home": m["home"], "away": m["away"]}
            rec = {k: m[k] for k in ("home", "away", "gh", "ga", "date")}
            for t in (m["home"], m["away"]):
                hist[t].appendleft(rec)
                while len(hist[t]) > RAW_KEEP:
                    hist[t].pop()
            meet[tuple(sorted((m["home"], m["away"])))].append(rec)
            s = 1.0 if m["gh"] > m["ga"] else (0.0 if m["gh"] < m["ga"] else 0.5)
            eh, ea = elo[m["home"]] + HA, elo[m["away"]]
            exp = 1 / (1 + 10 ** ((ea - eh) / 400))
            elo[m["home"]] = eh + K * (s - exp) - HA
            elo[m["away"]] = ea + K * ((1 - s) - (1 - exp))
        return out


def replica_probs(st: dict) -> dict | None:
    """Probabilidades (Poisson) y confianza de cada lado, como v1. None sin datos para Poisson."""
    if len(st["raw_h"]) < MIN_FIT or len(st["raw_a"]) < MIN_FIT:
        return None
    pp = predict(st["home"], st["away"], fit_nelder_mead(st["raw_h"] + st["raw_a"]))
    elo_p = 1 / (1 + 10 ** ((st["elo_a"] - (st["elo_h"] + HA)) / 400))
    elo_suff = not (abs(st["elo_h"] - ELO0) < 1 and abs(st["elo_a"] - ELO0) < 1)
    fh = form_score([res_of(x, st["home"]) for x in st["raw_h"]][:10])
    fa = form_score([res_of(x, st["away"]) for x in st["raw_a"]][:10])
    h2h = None
    if st["meet"]:
        h2h = sum(1 if res_of(x, st["home"]) == "W" else -1 if res_of(x, st["home"]) == "L" else 0
                  for x in st["meet"]) / len(st["meet"])
    conf = {}
    for side, p, form in (("home", pp[0], fh), ("away", pp[2], fa)):
        sig = [p]
        if form != 0.0:
            sig.append(0.5 + 0.6 * (form / 100 - 0.5))
        if elo_suff:
            sig.append(elo_p if side == "home" else 1 - elo_p)
        if h2h is not None:
            sig.append((h2h + 1) / 2 if side == "home" else 1 - (h2h + 1) / 2)
        sig = [max(0.0, min(1.0, s)) for s in sig]
        conf[side] = max(0.0, 1 - float(np.std(sig)))
    return {"p": {"home": pp[0], "draw": pp[1], "away": pp[2]}, "conf": conf}
