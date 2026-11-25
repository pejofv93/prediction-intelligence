"""bp-v1: modelo de baloncesto por 100 posesiones, líneas por percentiles y walk-forward por
jornada. Todo con partidos inventados; sin red ni Firestore."""
import copy
import random

import pytest

from analyzers import basket_preview as bp


def _side(pts, reb=35, ast=18, fg3m=10, poss=75.0):
    return {"pts": pts, "reb": reb, "ast": ast, "fg3m": fg3m, "poss": poss}


def _league(seasons=("E2024", "E2025"), n_teams=8, seed=7, ot_every=0):
    """Liga inventada: doble vuelta por temporada; el equipo T0 ataca mucho más y T1 juega
    rápido. Jornadas semanales; ot_every>0 mete una prórroga cada ot_every partidos."""
    rnd = random.Random(seed)
    teams = [f"T{i}" for i in range(n_teams)]
    att = {t: 1.0 for t in teams}
    att["T0"], att["T7"] = 1.18, 0.85
    pace = {t: 1.0 for t in teams}
    pace["T1"] = 1.12
    games, k = [], 0
    for si, season in enumerate(seasons):
        year = 2024 + si
        pairs = [(h, a) for h in teams for a in teams if h != a]
        rnd.shuffle(pairs)
        per_round = n_teams // 2
        for r in range(len(pairs) // per_round):
            day = f"{year}-{1 + r // 28:02d}-{1 + r % 28:02d}"
            for h, a in pairs[r * per_round:(r + 1) * per_round]:
                k += 1
                ot = 1 if ot_every and k % ot_every == 0 else 0
                poss = 72 * pace[h] * pace[a] * (1 + ot * 0.125)
                ph = poss * 1.10 * att[h] / att[a] ** 0.3 + rnd.gauss(0, 7)
                pa = poss * 1.04 * att[a] / att[h] ** 0.3 + rnd.gauss(0, 7)
                games.append({"league": "EUR", "season": season, "date": day,
                              "group": f"{season}_RS_{r + 1}", "home": h, "away": a, "ot": ot,
                              "h": _side(round(ph), round(36 + rnd.gauss(0, 5)), round(18 * att[h] + rnd.gauss(0, 3)),
                                         max(0, round(10 * att[h] + rnd.gauss(0, 3))), poss),
                              "a": _side(round(pa), round(33 + rnd.gauss(0, 5)), round(16 * att[a] + rnd.gauss(0, 3)),
                                         max(0, round(9 * att[a] + rnd.gauss(0, 3))), poss)})
    return games


def test_prev_season_y_normal():
    assert bp.prev_season("E2025") == "E2024"
    assert bp.prev_season("N2026") == "N2025"
    assert bp.prev_season("2025") == "2024"
    assert bp.p_over(160.5, 160.5, 10) == pytest.approx(0.5)
    assert bp.p_over(150.5, 160.5, 10) == pytest.approx(1 - bp.norm_cdf(-1))
    assert bp.p_over(150.5, 160.5, 0) == 1.0


def test_lineas_por_percentiles_redondeadas_a_media():
    games = [{"h": _side(70 + i), "a": _side(80 + i)} for i in range(21)]
    lines = bp.fixed_lines(games)
    # total = 150 + 2i, i = 0..20 → p25 = 160, p50 = 170, p75 = 180 → x,5
    assert lines["pts_total"] == (160.5, 170.5, 180.5)
    # por equipo: los dos lados juntos (70..90 y 80..100)
    team = sorted([70 + i for i in range(21)] + [80 + i for i in range(21)])
    assert lines["pts_team"] == tuple(sorted({bp._round_half(bp.quantile(team, q)) for q in bp.QUANTILES}))
    # estadística constante → una sola línea, sin repetidas
    assert lines["reb_team"] == (35.5,)


def test_lineas_aprobadas_mandan(monkeypatch):
    monkeypatch.setattr(bp, "FIXED_LINES", {"EUR": {"pts_total": (150.5,)}})
    assert bp.lines_for("EUR", []) == {"pts_total": (150.5,)}
    games = [{"h": _side(80), "a": _side(80)}]
    assert bp.lines_for("ACB", games)["pts_total"] == (160.5,)


def test_docs_de_eur_games_a_partidos():
    ok = {"season": "E2025", "date": "2025-10-02", "phase": "RS", "round": 3, "home_code": "AAA",
          "away_code": "BBB", "ot_periods": 1,
          "home": {"pts": 90, "reb": 30, "ast": 20, "fg3m": 9, "poss_est": 80.5},
          "away": {"pts": 88, "reb": 31, "ast": 15, "fg3m": 11, "poss_est": 79.5}}
    sin_poss = copy.deepcopy(ok)
    sin_poss["away"]["poss_est"] = 0
    sin_ast = copy.deepcopy(ok)
    del sin_ast["home"]["ast"]
    sin_ronda = {**copy.deepcopy(ok), "round": None}
    out = bp.games_from_docs([ok, sin_poss, sin_ast, sin_ronda], "EUR")
    assert len(out) == 2
    g = out[0]
    assert g["group"] == "E2025_RS_3" and g["ot"] == 1 and g["h"]["poss"] == 80.5
    assert out[1]["group"] == "E2025_2025-10-02"      # sin jornada: por día (NBA)
    # posesiones del partido llevadas a 40 minutos: media 80 × 40/45
    assert bp.game_poss(g) == pytest.approx(80 * 40 / 45)


def test_modelo_ve_ataque_ritmo_y_prorroga():
    games = _league(ot_every=10)
    hist = bp.History(games)
    e = bp.expected(hist, "EUR", "E2025", "2025-12-31", "T0", "T7")
    e_rev = bp.expected(hist, "EUR", "E2025", "2025-12-31", "T7", "T0")
    assert e["h"]["pts"] > e_rev["h"]["pts"]          # T0 ataca más que T7
    e_fast = bp.expected(hist, "EUR", "E2025", "2025-12-31", "T1", "T2")
    e_slow = bp.expected(hist, "EUR", "E2025", "2025-12-31", "T3", "T2")
    assert e_fast["poss"] > e_slow["poss"]           # T1 juega rápido
    lm = hist.league_means("EUR", "E2025", "2025-12-31")
    assert lm["ot_rate"] == pytest.approx(0.1, abs=0.03)
    # el ritmo de la liga se mide en 40 minutos: la prórroga no lo infla
    assert lm["pace"] == pytest.approx(72, rel=0.05)


def test_sin_historia_no_predice():
    hist = bp.History(_league(seasons=("E2025",)))
    assert bp.expected(hist, "EUR", "E2025", "2000-01-01", "T0", "T1") is None


def test_walk_forward_no_mira_el_futuro():
    games = _league()
    lines = bp.fixed_lines([g for g in games if g["season"] == "E2024"])
    rows = bp.backtest_rows(bp.History(games), "EUR", ["E2025"], lines)
    # se cambia la última jornada de E2025: las filas anteriores no se mueven
    last_group = max((g for g in games if g["season"] == "E2025"), key=lambda g: g["date"])["group"]
    games2 = copy.deepcopy(games)
    for g in games2:
        if g["group"] == last_group:
            g["h"]["pts"] += 40
    rows2 = bp.backtest_rows(bp.History(games2), "EUR", ["E2025"], lines)
    keep = [r for r in rows["vs_liga"] if not r[7].startswith(f"{last_group}_")]
    keep2 = [r for r in rows2["vs_liga"] if not r[7].startswith(f"{last_group}_")]
    assert keep and keep == keep2


def test_backtest_filas_y_metricas():
    games = _league(seasons=("E2023", "E2024", "E2025"))
    lines = bp.fixed_lines([g for g in games if g["season"] == "E2023"])
    counts = {"evaluados": 0, "pocos_partidos": 0, "sin_liga": 0}
    rows = bp.backtest_rows(bp.History(games), "EUR", ["E2024", "E2025"], lines, counts=counts)
    assert counts["evaluados"] > 0 and counts["pocos_partidos"] > 0
    assert len(rows["vs_liga"]) == len(rows["vs_equipo"]) > 0
    mk = {r[2] for r in rows["vs_liga"]}
    assert mk == {bp.market_key(s, sc) for s in bp.STATS for sc in bp.SCOPES}
    for r in rows["vs_liga"] + rows["vs_equipo"]:
        assert 0 <= r[5] <= 1 and 0 <= r[6] <= 1 and r[4] in (0, 1)
    # mismas filas en las dos listas salvo la referencia
    assert [r[:5] + r[6:] for r in rows["vs_liga"]] == [r[:5] + r[6:] for r in rows["vs_equipo"]]
    # La media del equipo nunca da 0 ni 1 (partidos ficticios de liga)
    assert all(0 < r[5] < 1 for r in rows["vs_equipo"])

    from analyzers.match_preview import market_metrics
    met = market_metrics(rows["vs_liga"])
    pts = met["pts_team|ALL"]
    # En la liga inventada los equipos difieren mucho en ataque: el modelo gana a la media de liga
    assert pts["brier_model"] < pts["brier_league"]
    assert pts["n_matches"] == counts["evaluados"]


def test_sesgo_medio_pequeno():
    games = _league(seasons=("E2024", "E2025"), ot_every=8)
    bias = bp.mean_bias(bp.History(games), "EUR", ["E2025"])
    assert set(bias) == set(bp.STATS)
    assert abs(bias["pts"]) < 6        # la prórroga entra en la media del total


# ── Etiquetas del backtest y ficha (fichas bp-v1) ───────────────────────────────

def _cell(mk, label):
    return {f"{mk}|ALL": {"market": mk, "div": "ALL", "ci_label": label},
            f"{mk}|EUR": {"market": mk, "div": "EUR", "ci_label": label}}


def test_etiquetas_ventaja_solo_si_gana_a_las_dos_medias():
    edge, ref = bp.LABEL_EDGE, bp.LABEL_REF
    liga = {**_cell("pts_team", edge), **_cell("reb_team", edge), **_cell("ast_total", ref)}
    equipo = {**_cell("pts_team", edge), **_cell("reb_team", ref), **_cell("ast_total", edge)}
    assert bp.labels_from_backtest(liga, equipo) == {"ast_total": ref, "pts_team": edge, "reb_team": ref}
    assert bp.metrics_doc_id("EUR", "backtest") == "bp-v1-EUR_backtest"


def test_ficha_con_forma_de_mp_v1():
    games = _league(seasons=("E2024", "E2025"))
    hist = bp.History(games)
    lines = bp.fixed_lines([g for g in games if g["season"] == "E2024"])
    resid = bp.residuals_until(hist, "EUR", "E2025")
    assert resid.sd("EUR", "E2025", "pts", "team") is not None
    labels = {"pts_team": bp.LABEL_EDGE, "pts_total": bp.LABEL_REF}
    p = bp.predict_game(hist, resid, "EUR", "E2025", "2026-01-01", "T0", "T7", lines, labels)
    assert not p["low_sample"] and p["data_cutoff"] < "2026-01-01"
    assert set(p["stats"]) == set(bp.STATS)
    pts = p["stats"]["pts"]
    assert pts["exp_home"] > pts["exp_away"]                     # T0 ataca más que T7
    assert pts["exp_total"] == pytest.approx(pts["exp_home"] + pts["exp_away"], abs=0.02)
    home = pts["lines"]["home"]
    assert home["market"] == "pts_team" and home["label"] == bp.LABEL_EDGE and home["highlight"]
    assert pts["lines"]["total"]["label"] == bp.LABEL_REF and not pts["lines"]["total"]["highlight"]
    assert p["stats"]["reb"]["lines"]["away"]["label"] == bp.LABEL_REF     # sin etiqueta = referencia
    fixed = [v for v in home["lines"].values() if not v.get("extra")]
    assert sorted(v["line"] for v in fixed) == list(lines["pts_team"])
    extra = [v for v in home["lines"].values() if v.get("extra")]
    assert len(extra) <= 1 and all(abs(v["p_model"] - 0.5) < 0.1 for v in extra)
    for v in home["lines"].values():
        assert 0 <= v["p_model"] <= 1 and 0 <= v["p_league"] <= 1 and 0 < v["p_team"] < 1
    # P(más) baja al subir la línea
    ps = [v["p_model"] for v in sorted(home["lines"].values(), key=lambda v: v["line"])]
    assert ps == sorted(ps, reverse=True)
    assert home["main_line"] in [v["line"] for v in home["lines"].values()]


def test_ficha_muestra_insuficiente_no_resalta():
    games = _league(seasons=("E2024", "E2025"))
    hist = bp.History(games)
    # al principio de E2025 nadie lleva 5 partidos de la temporada
    first = min(g["date"] for g in games if g["season"] == "E2025")
    p = bp.predict_game(hist, bp.Residuals(), "EUR", "E2025", first, "T0", "T1", {"pts_team": (80.5,)},
                        {"pts_team": bp.LABEL_EDGE})
    assert p["low_sample"]
    blk = p["stats"]["pts"]["lines"]["home"]
    assert blk["label"] == bp.LABEL_LOW_SAMPLE and blk["market_label"] == bp.LABEL_EDGE and not blk["highlight"]
    assert bp.predict_game(bp.History([]), bp.Residuals(), "EUR", "E2025", first, "T0", "T1", {}) is None


def test_prorroga_nba_sobre_48_minutos():
    g = {"league": "NBA", "ot": 1, "h": _side(110, poss=106.0), "a": _side(108, poss=106.0)}
    assert bp.game_poss(g) == pytest.approx(106 * 48 / 53)
    assert bp.game_poss({**g, "league": "EUR"}) == pytest.approx(106 * 40 / 45)
