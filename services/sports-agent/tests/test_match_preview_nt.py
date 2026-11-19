"""mp-n1 (selecciones): adaptador, índices walk-forward, referencias, bootstrap y script de backtest."""
import copy
import importlib.util
import os
import random
from datetime import date, timedelta

from analyzers import match_preview as mp
from analyzers import match_preview_nt as mn


def _side(c, f=10):
    return {"corners": c, "fouls": f, "yellows": 1, "reds": 0, "shots_on": 4, "shots_off": 5, "shots_blocked": 2}


def _doc(lid, d, h, a, gh=1, ga=0, stats=True, **kw):
    doc = {"group": "nt", "league_id": lid, "season": 2024, "date": d, "finished": True,
           "home_team_id": h, "away_team_id": a, "goals_home": gh, "goals_away": ga,
           "status": "done" if stats else "pending"}
    if stats:
        doc["stats"] = {"home": _side(6), "away": _side(3), "complete": True}
    doc.update(kw)
    return doc


def test_adaptador_sede_tipo_y_tramo():
    docs = [_doc(28016, "2022-03-01", 1, 2),
            _doc(9294, "2023-06-01", 1, 2, stats=False),
            _doc(1635, "2026-06-20", 1, 2),
            _doc(5039, "2024-09-01", 1, 2, gh=None),                 # sin marcador: fuera
            _doc(5039, "2024-09-02", 1, 2, finished=False),          # sin terminar: fuera
            _doc(121626, "2024-09-03", 1, 2, group="women")]         # otro grupo: fuera
    ms = mn.matches_from_hl(docs, fit_end="2023-01-01")
    assert [m["div"] for m in ms] == ["WCQ_EU", "AMIST", "MUNDIAL"]
    assert [m["season"] for m in ms] == [mn.FIT, mn.BT, mn.BT]
    assert [m["neutral"] for m in ms] == [False, False, True]
    assert [m["friendly"] for m in ms] == [False, True, False]
    assert ms[0]["h"] == {"corners": 6, "fouls": 10, "sot": 4, "shots": 11, "cards": 1, "goals": 1}
    assert ms[1]["h"]["corners"] is None and ms[1]["h"]["goals"] == 1
    cov = mn.coverage(ms)
    assert cov[("AMIST", "2023")]["matches"] == 1 and cov[("AMIST", "2023")]["corners"] == 0


def _poisson(rnd, mean):
    L, k, p = 2.718281828 ** -mean, 0, 1.0
    while True:
        p *= rnd.random()
        if p <= L:
            return k
        k += 1


def _world(seed=3, teams=12, days=500, strong=("0", "1", "2")):
    """Selecciones sintéticas: 3 equipos con el triple de córners. Un partido cada ~3 días."""
    rnd = random.Random(seed)
    out, d0 = [], date(2021, 1, 1)
    for i in range(days // 3):
        h, a = rnd.sample(range(teams), 2)
        lid = rnd.choice([28016, 5039, 9294, 1635])
        neutral = lid in mn.NEUTRAL
        def side(t, home):
            base = 6.0 if str(t) in strong else 2.0
            base *= 1.5 if home and not neutral else 1.0
            return {"corners": _poisson(rnd, base), "fouls": _poisson(rnd, 12), "yellows": _poisson(rnd, 2),
                    "reds": 0, "shots_on": _poisson(rnd, 4), "shots_off": _poisson(rnd, 5),
                    "shots_blocked": _poisson(rnd, 2)}
        out.append({"group": "nt", "league_id": lid, "date": (d0 + timedelta(days=3 * i)).isoformat(),
                    "finished": True, "home_team_id": h, "away_team_id": a,
                    "goals_home": _poisson(rnd, 1.3), "goals_away": _poisson(rnd, 1.0), "status": "done",
                    "stats": {"home": side(h, True), "away": side(a, False), "complete": True}})
    return out


def test_walk_forward_no_mira_al_futuro():
    ms = mn.matches_from_hl(_world(), fit_end="2021-09-01")
    cut = ms[len(ms) // 2]["date"]
    rt = mn.Ratings(ms, "corners", mn._ord(cut))
    future = copy.deepcopy(ms)
    for m in future:
        if m["date"] >= cut:
            m["h"]["corners"] = 99
            m["a"]["corners"] = 0
    rt2 = mn.Ratings(future, "corners", mn._ord(cut))
    assert rt.overall == rt2.overall and dict(rt.att) == dict(rt2.att)
    assert all(r[6] != 99 for r in rt2.rows)


def test_minimo_de_partidos_y_ajuste_por_rival():
    ms = mn.matches_from_hl(_world(days=1500), fit_end="2021-09-01")
    rt = mn.Ratings(ms, "corners", ms[-1]["ord"] + 1)
    assert rt.att["0"] > 1.5 and rt.att["5"] < 0.9          # fuertes y flojos separados
    assert rt.f_role["H"] > rt.f_role["A"]
    early = mn.Ratings(ms, "corners", ms[3]["ord"])          # 3 partidos jugados: nadie llega a 6
    assert all(early.expected(m) is None for m in ms[3:10])


def test_referencias_y_respaldo():
    ms = mn.matches_from_hl(_world(), fit_end="2021-09-01")
    rt = mn.Ratings(ms, "corners", ms[-1]["ord"] + 1)
    p_all = rt.ref_rate(3.5, mn.ANY, None)
    assert 0 < p_all < 1
    # Pocos neutrales amistosos (nunca hay: los amistosos no son neutrales) → respaldo a todo
    assert rt.ref_rate(3.5, "N", True) == p_all
    assert rt.ref_rate(7.5, None, None) is not None


def test_backtest_modelo_gana_cuando_hay_senal():
    ms = mn.matches_from_hl(_world(days=1500), fit_end="2022-01-01")
    disp, det = mn.fit_dispersion(mn.predictions(ms, period=mn.FIT))
    assert det["corners_team"]["n"] > 0
    from analyzers import match_preview_women as mw
    lines = mw.fit_lines(ms, seasons=(mn.FIT,))
    rows = mn.backtest_rows(mn.predictions(ms, period=mn.BT), lines, disp)
    comp, glob = rows["modelo_vs_competicion"], rows["modelo_vs_global"]
    assert comp and glob and all(r[0] == mn.BT for r in comp)
    met = mn.metrics(comp, reps=300)
    v = met["corners_team|ALL"]
    assert v["delta"] < 0 and v["ci_hi"] < 0 and mn.verdict(v) == "mejor el modelo"
    assert "ci_lo_normal" in v and v["ci_lo"] <= v["delta"] <= v["ci_hi"]
    # Sin señal (faltas iguales para todos): no debe salir "mejor el modelo" por sistema
    assert mn.verdict(met["fouls_team|ALL"]) != "mejor el modelo" or met["fouls_team|ALL"]["delta"] > -0.01


def test_boot_ci():
    rows = [("BT", "G", "m", 0.5, i % 2, 0.5, 0.5, f"k{i}") for i in range(50)]
    lo, hi = mn.boot_ci(rows, reps=200)
    assert lo == 0 and hi == 0
    assert mn.boot_ci(rows[:1]) == (None, None)


def test_script_solo_agregados(monkeypatch, capsys):
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "ops", "nt_backtest.py")
    spec = importlib.util.spec_from_file_location("nt_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    docs = _world(days=1200)
    for d in docs:      # ids que no deben aparecer en el log
        d["home_team_id"] = f"SECRETO{d['home_team_id']}"
        d["away_team_id"] = f"SECRETO{d['away_team_id']}"
        d["home_team"], d["away_team"] = "Ruritania", "Freedonia"
    monkeypatch.setattr(mod, "read_docs", lambda: docs)
    monkeypatch.setattr(mod, "FIT_END", "2022-01-01")
    monkeypatch.setattr(mn, "BOOT_REPS", 200)
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "modelo_vs_competicion" in out and "corners_team" in out
    assert "SECRETO" not in out and "Ruritania" not in out and "Freedonia" not in out
