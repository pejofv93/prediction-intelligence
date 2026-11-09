"""match_preview (ficha de partido mp-v1, paso 2): modelo ataque × defensa con media de la liga
por sede, encogimiento, pesos por temporada, Poisson / binomial negativa, etiquetas por mercado
y liga con IC por partido. Liga INVENTADA; sin red ni Firestore. La fidelidad al backtest del
8-nov (194.718 filas, Brier idéntico en los 12 mercados con k=6) se comprobó aparte con los CSV."""
import math

import pytest

from analyzers import match_preview as mp

BASE = {"goals": 1, "corners": 5, "cards": 2, "shots": 12, "sot": 4, "fouls": 11}


def _m(div, season, date, home, away, h=None, a=None):
    return {"div": div, "season": season, "date": date, "home": home, "away": away,
            "h": dict(BASE, **(h or {})), "a": dict(BASE, **(a or {}))}


def _league(div="SP1", season="2627", teams=("A", "B", "C", "D"), rounds=4, start=1, strong=None):
    """Todos contra todos `rounds` veces, un partido por día. `strong` remata el doble."""
    out, day = [], start
    for _ in range(rounds):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                hs = {"shots": 24} if h == strong else {}
                as_ = {"shots": 24} if a == strong else {}
                out.append(_m(div, season, f"2026-{8 + day // 28:02d}-{1 + day % 28:02d}", h, a, hs, as_))
                day += 1
    return out


def test_prev_season():
    assert mp.prev_season("2627") == "2526"
    assert mp.prev_season("1011") == "0910"


def test_matches_from_team_rows_usa_la_fila_del_local():
    rows = [
        {"side": "home", "div": "SP1", "season": "2627", "match_date": "2026-09-01", "team": "A", "opponent": "B",
         **{f"{s}_for": 1 for s in mp.STATS}, **{f"{s}_against": 2 for s in mp.STATS}},
        {"side": "away", "div": "SP1", "season": "2627", "match_date": "2026-09-01", "team": "B", "opponent": "A"},
    ]
    ms = mp.matches_from_team_rows(rows)
    assert len(ms) == 1 and ms[0]["home"] == "A" and ms[0]["h"]["shots"] == 1 and ms[0]["a"]["shots"] == 2


def test_liga_plana_da_la_media_de_la_liga():
    h = mp.History(_league())
    out = mp.predict_match(h, "SP1", "2627", "2026-12-31", "A", "B")
    s = out["stats"]["shots"]
    assert s["exp_home"] == pytest.approx(12) and s["exp_away"] == pytest.approx(12)
    assert out["teams"]["home"]["attack"]["shots"] == pytest.approx(1.0)


def test_equipo_fuerte_sube_pero_encogido_hacia_la_media():
    h = mp.History(_league(strong="A"))
    out = mp.predict_match(h, "SP1", "2627", "2026-12-31", "A", "B")
    att = out["teams"]["home"]["attack"]["shots"]
    assert 1.0 < att < 24 / ((24 * 6 + 12 * 30) / 36)        # por debajo del ataque bruto
    # la defensa de B no cambia por A; λ = media local × ataque A × defensa B
    s = out["stats"]["shots"]
    assert s["exp_home"] > s["exp_away"]


def test_encogimiento_k():
    h = mp.History([])
    hist = [(1.0, _m("SP1", "2627", "2026-09-01", "A", "B", {"goals": 2}), "h")]
    h.by_ds[("SP1", "2627")] = [_m("SP1", "2627", "2026-09-0%d" % i, "X", "Y") for i in range(1, 10)]
    att, dfn, n = h.indices(hist, "goals", "2026-12-31", "2627", k=8)
    assert n == 1.0 and att == pytest.approx((2 + 8) / 9) and dfn == pytest.approx(1.0)


def test_pesos_por_temporada_y_division_y_sin_futuro():
    ms = _league("SP1", "2627") + _league("SP1", "2526", start=1) + _league("SP2", "2526", teams=("A", "Z", "Y"), start=1)
    h = mp.History(ms)
    hist = h.team_history("A", "SP1", "2627", "2026-09-05")
    weights = sorted({w for w, *_ in hist})
    assert weights == [0.25, 0.5, 1.0]
    assert all(m["date"] < "2026-09-05" for w, m, _ in hist if w == 1.0)


def test_media_de_liga_usa_la_anterior_con_pocos_partidos():
    prev = _league("SP1", "2526", rounds=4)                       # 48 partidos, goles 1
    cur = [_m("SP1", "2627", "2026-08-15", "A", "B", {"goals": 5})]
    h = mp.History(prev + cur)
    lh, _ = h.league_means("SP1", "2627", "2026-12-31")["goals"]
    assert lh == pytest.approx(1.0)                                # 1 partido < 40 → temporada anterior


def test_binomial_negativa_tiene_colas_mas_gordas():
    assert mp.p_over(8.5, 5.0, 17.03) > mp.p_over(8.5, 5.0, None)
    assert mp.p_over(0.5, 1.0, None) == pytest.approx(1 - math.exp(-1))


def test_muestra_insuficiente_y_etiquetas():
    h = mp.History(_league(rounds=1))                              # 3 partidos por equipo... 6 en total
    labels = {("shots_team", "SP1"): mp.LABEL_EDGE}
    out = mp.predict_match(h, "SP1", "2627", "2026-08-05", "A", "B", labels=labels)
    assert out["low_sample"] is True
    team = out["stats"]["shots"]["lines"]["home"]
    assert team["label"] == mp.LABEL_LOW_SAMPLE and team["market_label"] == mp.LABEL_EDGE and team["highlight"] is False

    h = mp.History(_league(rounds=4))
    out = mp.predict_match(h, "SP1", "2627", "2026-12-31", "A", "B", labels=labels,
                           odds={"over_2_5": 2.0, "under_2_5": 1.8})
    assert out["low_sample"] is False
    team = out["stats"]["shots"]["lines"]["home"]
    assert team["label"] == mp.LABEL_EDGE and team["highlight"] is True and team["distribution"] == "binomial negativa"
    tot = out["stats"]["shots"]["lines"]["total"]
    assert tot["label"] == mp.LABEL_REF and tot["highlight"] is False
    line = out["stats"]["goals"]["lines"]["total"]["lines"]["over_2_5"]
    assert line["p_market"] == pytest.approx((1 / 2.0) / (1 / 2.0 + 1 / 1.8), abs=1e-4)
    assert set(line) >= {"p_model", "p_league", "diff_vs_league", "league_n"}
    assert out["data_cutoff"] < "2026-12-31" and out["model_version"] == "mp-v1"


def _row(match, y, pl, pm, market="shots_team", div="SP1", season="2627", line=10.5):
    return (season, div, market, line, y, pl, pm, match)


def test_metricas_ventaja_medida_solo_si_el_ic_no_toca_cero():
    # el modelo acierta más que la liga en todos los partidos → IC entero < 0
    good = [_row(f"m{i}", 1, 0.5, 0.8) for i in range(40)] + [_row(f"n{i}", 0, 0.5, 0.2) for i in range(40)]
    met = mp.market_metrics(good)
    assert met["shots_team|SP1"]["label"] == mp.LABEL_EDGE and met["shots_team|SP1"]["ci_hi"] < 0
    assert met["shots_team|ALL"]["calibration"]
    # mitad mejor, mitad peor → el IC toca 0
    mixed = [_row(f"m{i}", 1, 0.5, 0.8) for i in range(40)] + [_row(f"n{i}", 0, 0.5, 0.8) for i in range(40)]
    assert mp.market_metrics(mixed)["shots_team|SP1"]["label"] == mp.LABEL_REF
    assert mp.labels_from_metrics(met) == {("shots_team", "SP1"): mp.LABEL_EDGE}


def test_regla_prudente_exige_tambien_el_mercado_en_conjunto():
    # SP1 gana con claridad; SP2 pierde con más partidos → en conjunto no hay ventaja
    sp1 = [_row(f"a{i}", 1, 0.5, 0.8) for i in range(30)]
    sp2 = [_row(f"b{i}", 1, 0.5, 0.1, div="SP2") for i in range(60)]
    met = mp.market_metrics(sp1 + sp2)
    assert met["shots_team|SP1"]["ci_label"] == mp.LABEL_EDGE
    assert met["shots_team|ALL"]["ci_label"] == mp.LABEL_REF
    assert met["shots_team|SP1"]["label"] == mp.LABEL_REF
    assert mp.labels_from_metrics(met) == {("shots_team", "SP1"): mp.LABEL_REF, ("shots_team", "SP2"): mp.LABEL_REF}


def test_ic_por_partido_no_por_linea():
    # 4 líneas del mismo partido cuentan como UN partido
    rows = [_row("m1", 1, 0.5, 0.8, line=ln) for ln in (8.5, 10.5, 12.5, 14.5)]
    met = mp.market_metrics(rows)["shots_team|SP1"]
    assert met["n_matches"] == 1 and met["n_lines"] == 4 and met["label"] == mp.LABEL_REF


def test_backtest_salta_equipos_con_menos_de_5_partidos():
    h = mp.History(_league(rounds=4))
    rows = mp.backtest_rows(h, ("2627",), divs=("SP1",))
    keys = {r[-1] for r in rows}
    assert rows and len(keys) < 48                                 # los primeros partidos no se predicen
    assert {r[2] for r in rows} == {mp.market_key(s, sc) for s in mp.STATS for sc in ("team", "total")}
