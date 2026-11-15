"""Poisson de v1: el ajuste converge y da el mismo resultado aunque cambie el orden de los
partidos (con Nelder-Mead, maxiter 2000, no convergía y el redondeo movía hasta 4-5 puntos)."""
import random

import numpy as np
import pytest

from enrichers import poisson_model as pm


def _matches(n_teams=10, n=40, seed=3):
    rnd = np.random.default_rng(seed)
    att = rnd.uniform(0.7, 1.6, n_teams)
    de = rnd.uniform(0.7, 1.4, n_teams)
    out = []
    for _ in range(n):
        h, a = rnd.choice(n_teams, 2, replace=False)
        out.append({"home_team_id": f"t{h}", "away_team_id": f"t{a}",
                    "goals_home": int(rnd.poisson(att[h] * de[a] * 1.25)),
                    "goals_away": int(rnd.poisson(att[a] * de[h]))})
    return out


def _probs(ms, home="t0", away="t1"):
    p = pm.predict_match_probs(home, away, pm.fit_attack_defense(ms))
    return np.array([p["home_win"], p["draw"], p["away_win"]])


def test_mismo_resultado_con_otro_orden():
    ms = _matches()
    base = _probs(ms)
    for seed in range(5):
        sh = ms[:]
        random.Random(seed).shuffle(sh)
        assert np.allclose(_probs(sh), base, atol=2e-4)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_converge_al_minimo(seed, monkeypatch):
    """El mínimo encontrado es el mismo que el de otro optimizador desde otro punto de partida."""
    from scipy.optimize import minimize as real_minimize
    ms = _matches(seed=seed)
    ref = _probs(ms)
    calls = {}

    def other(fun, x0, jac=None, method=None, bounds=None, options=None):
        calls["n"] = calls.get("n", 0) + 1
        start = x0 + np.random.default_rng(seed).normal(0, 0.3, len(x0))
        return real_minimize(fun, start, jac=jac, method="BFGS", options={"gtol": 1e-10, "maxiter": 5000})

    monkeypatch.setattr(pm, "minimize", other)
    alt = _probs(ms)
    assert calls["n"] == 1
    assert np.allclose(alt, ref, atol=5e-4)


def test_recupera_la_ventaja_de_campo_con_muestra_grande():
    ms = _matches(n_teams=12, n=1500, seed=9)
    params = pm.fit_attack_defense(ms)
    assert abs(params["home_advantage"] - 0.25) < 0.08


def test_gradiente_analitico_correcto(monkeypatch):
    from scipy.optimize import check_grad
    captured = {}
    real = pm.minimize

    def spy(fun, x0, **kw):
        captured["fun"], captured["x0"] = fun, x0
        return real(fun, x0, **kw)

    monkeypatch.setattr(pm, "minimize", spy)
    pm.fit_attack_defense(_matches())
    f, x0 = captured["fun"], captured["x0"]
    x = x0 + np.random.default_rng(0).normal(0, 0.2, len(x0))
    err = check_grad(lambda p: f(p)[0], lambda p: f(p)[1], x)
    assert err < 1e-4 * max(1.0, np.linalg.norm(f(x)[1]))
