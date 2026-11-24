"""mp-e1 (Europa): factor de liga, emparejado de equipos, walk-forward, IC95 bootstrap y ficha.
Datos INVENTADOS (equipos y ligas de mentira): nada de Highlightly ni de football-data. Sin red."""
import math
import random
from collections import Counter
from datetime import date, timedelta

from analyzers import match_preview as mp
from analyzers import match_preview_uefa as mu
from collectors.highlightly_players import team_similarity

BASE = {"goals": 1.4, "corners": 5.0, "cards": 2.0, "shots": 12.0, "sot": 4.0, "fouls": 11.0}
FACTOR = {"SP1": 1.5, "B1": 0.65}           # lo que rinde cada liga en Europa frente a su liga


def _poisson(rng, lam):
    x, p, s = 0, math.exp(-lam), math.exp(-lam)
    u = rng.random()
    while u > s and x < 200:
        x += 1
        p *= lam / x
        s += p
    return x


def _world(seed=3):
    """Dos ligas (SP1 fuerte en Europa, B1 débil), 8 equipos cada una, 2020/21 y 2021/22, y partidos
    europeos entre ellas en las dos temporadas."""
    rng = random.Random(seed)
    teams = {"SP1": [f"Iberia {i}" for i in range(8)], "B1": [f"Flandia {i}" for i in range(8)]}
    strength = {t: 0.75 + 0.5 * rng.random() for ts in teams.values() for t in ts}
    league = []
    for season, y0 in (("2021", 2020), ("2122", 2021)):
        for div, ts in teams.items():
            pairs = [(h, a) for h in ts for a in ts if h != a]
            for i, (h, a) in enumerate(pairs):
                d = (date(y0, 8, 10) + timedelta(days=4 * i)).isoformat()
                gen = {side: {s: _poisson(rng, BASE[s] * strength[x] / strength[o]) for s in mp.STATS}
                       for side, x, o in (("h", h, a), ("a", a, h))}
                league.append({"div": div, "season": season, "date": d, "home": h, "away": a, **gen})
    euro = []
    k = 0
    for season, y0 in (("2021", 2020), ("2122", 2021)):
        for j in range(90):
            h, a = teams["SP1"][j % 8], teams["B1"][(j * 3) % 8]
            if j % 2:
                h, a = a, h
            dh, da = ("SP1", "B1") if h.startswith("Iberia") else ("B1", "SP1")
            d = (date(y0, 9, 15) + timedelta(days=2 * j)).isoformat()
            gen = {side: {s: _poisson(rng, BASE[s] * strength[x] / strength[o] * FACTOR[lx] / FACTOR[lo])
                          for s in mp.STATS}
                   for side, x, o, lx, lo in (("h", h, a, dh, da), ("a", a, h, da, dh))}
            euro.append({"comp": "CL" if j % 3 else "EL", "season": season, "date": d,
                         "key": f"E{k}", "home": h, "away": a, **gen})
            k += 1
    return league, euro


def _engine(league, **kw):
    hist = mp.History(league, prev_divs=mu.ALL_DIVS)
    return mu.Engine(hist, mu.team_divs_all(hist), **kw)


def test_factor_de_liga_recupera_el_signo():
    rng = random.Random(1)
    obs = []
    for _ in range(1500):
        la, ld = rng.choice(["A", "B"]), rng.choice(["A", "B"])
        eta = math.log(5) + {"A": 0.3, "B": -0.3}[la] + {"A": -0.2, "B": 0.2}[ld]
        obs.append((_poisson(rng, math.exp(eta)), math.log(5), la, ld))
    b = mu.fit_league_factors(obs, ridge=5)
    assert b[("att", "A")] - b[("att", "B")] > 0.45
    assert b[("def", "B")] - b[("def", "A")] > 0.25
    # Ridge fuerte → todo cerca de 0
    b2 = mu.fit_league_factors(obs, ridge=1e7)
    assert max(abs(v) for v in b2.values()) < 0.01
    assert mu.fit_league_factors([]) == {}


def test_resolver_por_id_y_por_parecido_unico():
    tdiv = {("Club Brugia", "2526"): "B1", ("Real Iberia", "2526"): "SP1", ("Sporting Iberia", "2526"): "SP1",
            ("Sporting Lusia", "2526"): "P1"}
    res = mu.make_resolver({77: "Real Iberia"}, tdiv, team_similarity)
    assert res(77, "Cualquier nombre", "2526") == "Real Iberia"
    assert res(78, "Club Brugia KV", "2526") == "Club Brugia"          # sin id: parecido único
    assert res(79, "Equipo Lejano", "2526") is None
    assert res(77, "Real Iberia", "2627") == "Real Iberia"             # temporada nueva sin CSV: la anterior


def _doc(mid, comp="CL", status="done", day="2025-10-01", hid=1, aid=2, home="Real Iberia", away="Equipo Lejano"):
    side = {"corners": 5, "fouls": 10, "yellows": 2, "reds": None, "shots_on": 4, "shots_off": 5,
            "shots_blocked": 2}
    return {"match_id": mid, "comp": comp, "status": status, "finished": True, "date": day,
            "home_team_id": hid, "away_team_id": aid, "home_team": home, "away_team": away,
            "goals_home": 2, "goals_away": 0, "stats": {"home": dict(side), "away": dict(side)}}


def test_uefa_matches_cuenta_descartes_y_deja_sin_nombre_al_desconocido():
    res = mu.make_resolver({1: "Real Iberia"}, {("Real Iberia", "2526"): "SP1"}, team_similarity)
    c = Counter()
    ms = mu.uefa_matches([_doc(1), _doc(2, comp=None), _doc(3, status="pending"),
                          {**_doc(4), "stats": {}}], res, c)
    assert len(ms) == 1 and ms[0]["home"] == "Real Iberia" and ms[0]["away"] is None
    assert ms[0]["h"]["cards"] == 2 and ms[0]["h"]["shots"] == 11        # roja ausente = 0; remates = 4+5+2
    assert c["descartado_sin_competicion"] == c["descartado_sin_detalle"] == c["descartado_sin_estadisticas"] == 1


def test_walk_forward_el_factor_aporta_en_el_mundo_inventado():
    league, euro = _world()
    eng = _engine(league, min_fit=40)
    counts = Counter()
    rows = mu.backtest_rows(eng, euro, counts, seasons=("2122",))
    assert counts["calentamiento"] == 90 and counts["evaluados_ambos"] == 90
    assert not rows["e1_vs_comp|un_lado"]
    # el factor que aprende va en la dirección buena
    b = eng.beta["corners"]
    assert b[("att", "SP1")] > b[("att", "B1")] and b[("def", "SP1")] < b[("def", "B1")]
    met = mu.market_metrics(rows["e1_vs_v1|ambos"], n_boot=200)
    assert met["corners_team|ALL"]["delta"] < 0 and met["shots_team|ALL"]["delta"] < 0
    assert met["corners_team|CL"]["ci_label"] == "pocos partidos"          # < 300 partidos por competición


def test_un_lado_sin_datos_va_aparte_y_sin_ninguno_no_se_evalua():
    league, euro = _world()
    euro = [dict(m) for m in euro]
    for m in euro[150:165]:
        m["away"] = None
    for m in euro[165:170]:
        m["home"] = m["away"] = None
    counts = Counter()
    rows = mu.backtest_rows(_engine(league, min_fit=40), euro, counts, seasons=("2122",))
    assert counts["evaluados_un_lado"] == 15 and counts["sin_datos_ningun_lado"] == 5
    assert {r[7] for r in rows["e1_vs_comp|un_lado"]} == {m["key"] for m in euro[150:165]}


def test_ningun_dato_del_futuro():
    """Cambiar un partido europeo posterior no cambia lo predicho antes."""
    league, euro = _world()
    a = mu.backtest_rows(_engine(league, min_fit=40), euro, Counter(), seasons=("2122",))
    euro2 = [dict(m) for m in euro]
    euro2[-1] = {**euro2[-1], "h": {s: 50 for s in mp.STATS}}
    b = mu.backtest_rows(_engine(league, min_fit=40), euro2, Counter(), seasons=("2122",))
    last = euro[-1]["key"]
    keep = lambda rs: [r for r in rs if r[7] != last]                       # noqa: E731
    assert keep(a["e1_vs_comp|ambos"]) == keep(b["e1_vs_comp|ambos"])


def test_metricas_bootstrap_y_etiquetas_como_copas():
    rows = []
    for i in range(400):
        y = i % 2
        rows.append(("2526", "CL" if i < 350 else "EL", "corners_team", 4.5, y, 0.5, 0.9 if y else 0.1, f"m{i}"))
        rows.append(("2526", "CL" if i < 350 else "EL", "goals_total", 2.5, y, 0.5, 0.5, f"m{i}"))
    met = mu.market_metrics(rows, n_boot=300)
    assert met["corners_team|ALL"]["ci_hi"] < 0 and met["corners_team|ALL"]["ci_label"] == mp.LABEL_EDGE
    assert met["corners_team|CL"]["ci_label"] == mp.LABEL_EDGE and met["corners_team|EL"]["ci_label"] == "pocos partidos"
    assert met["goals_total|ALL"]["ci_label"] == mp.LABEL_REF
    labels = mu.labels_from_backtest(met)
    assert labels == {"corners_team": "medido en backtest", "goals_total": "solo referencia"}
    assert mu.comp_labels(met) == {"CL": {"corners_team": "medido en backtest", "goals_total": "solo referencia"}}


def test_ficha_forma_de_mp_v1_y_muestra_insuficiente_si_falta_un_lado():
    league, euro = _world()
    eng = _engine(league, min_fit=40)
    mu.backtest_rows(eng, euro, Counter(), seasons=())
    eng.refit()
    labels = {"corners_team": "medido en backtest"}
    f = mu.predict_uefa_match(eng, "CL", "2122", "2022-05-30", "Iberia 1", "Flandia 2", labels=labels)
    assert f["model_version"] == "mp-e1" and not f["low_sample"]
    blk = f["stats"]["corners"]["lines"]["home"]
    assert blk["market"] == "corners_team" and blk["label"] == "medido en backtest" and not blk["highlight"]
    assert f["stats"]["corners"]["factor_home"] > 1 > f["stats"]["corners"]["factor_away"]
    assert all(v["p_league"] is not None for v in blk["lines"].values())
    g = mu.predict_uefa_match(eng, "CL", "2122", "2022-05-30", "Iberia 1", None, labels=labels)
    assert g["low_sample"] and g["teams"]["away"]["insufficient"]
    assert g["stats"]["corners"]["lines"]["home"]["label"] == mp.LABEL_LOW_SAMPLE
