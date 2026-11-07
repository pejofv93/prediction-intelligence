"""fdco_team_matches: filas equipo-partido, paradas derivadas, ponderación por temporada,
ajuste a la media y muestra mínima. Sin red ni Firestore."""
from datetime import date

import pytest

from collectors import fdco_team_matches as m


def _csv_row(home="Alpha", away="Beta", d="02/11/2026", fthg=2, ftag=1, hs=12, as_=9,
             hst=5, ast=4, hf=11, af=13, hc=6, ac=3, hy=2, ay=3, hr=0, ar=1, hxg="1.4", axg="0.9"):
    return {"Date": d, "HomeTeam": home, "AwayTeam": away, "FTHG": fthg, "FTAG": ftag,
            "HTHG": 1, "HTAG": 0, "HS": hs, "AS": as_, "HST": hst, "AST": ast, "HF": hf, "AF": af,
            "HC": hc, "AC": ac, "HY": hy, "AY": ay, "HR": hr, "AR": ar, "HxG": hxg, "AxG": axg}


def test_dos_filas_con_a_favor_y_en_contra():
    home, away = m.team_rows_from_csv_row(_csv_row(), "SP2", "2627")
    assert (home["side"], home["team"], home["opponent"]) == ("home", "Alpha", "Beta")
    assert (away["side"], away["team"], away["opponent"]) == ("away", "Beta", "Alpha")
    assert home["shots_for"] == 12 and home["shots_against"] == 9
    assert away["corners_for"] == 3 and away["corners_against"] == 6
    assert home["cards_for"] == 2 and away["cards_for"] == 4  # 3 amarillas + 1 roja
    assert home["corners_total"] == away["corners_total"] == 9
    assert home["cards_total"] == 6 and home["fouls_total"] == 24
    assert home["match_date"] == "2026-11-02"
    assert home["xg_for"] == 1.4 and away["xg_for"] == 0.9


def test_paradas_derivadas_marcadas():
    home, away = m.team_rows_from_csv_row(_csv_row(fthg=2, ftag=1, hst=5, ast=4), "SP1", "2627")
    # paradas del local = a puerta del visitante (4) − goles encajados (1)
    assert home["saves_derived"] == 3
    assert away["saves_derived"] == 3  # 5 − 2
    assert home["saves_source"] == away["saves_source"] == "derivada"
    assert not home["own_goal_probable"]


def test_parada_negativa_es_autogol_y_se_queda_en_cero():
    home, _ = m.team_rows_from_csv_row(_csv_row(ftag=2, ast=1), "E0", "2627")
    assert home["saves_derived"] == 0
    assert home["own_goal_probable"]


def test_partido_sin_stats_se_descarta():
    row = _csv_row()
    row["HC"] = ""
    assert m.team_rows_from_csv_row(row, "SP1", "2627") == []


def test_f2_no_tiene_faltas():
    home, _ = m.team_rows_from_csv_row(_csv_row(), "F2", "2526")
    assert home["fouls_for"] is None and home["fouls_total"] is None
    assert home["fouls_note"] == "free_kicks_conceded_not_fouls"


def test_convencion_tarjetas_inglaterra():
    eng, _ = m.team_rows_from_csv_row(_csv_row(), "E0", "2627")
    esp, _ = m.team_rows_from_csv_row(_csv_row(), "SP1", "2627")
    assert eng["cards_convention"] == "second_yellow_excluded_from_yellows"
    assert esp["cards_convention"] == "second_yellow_counts_yellow_plus_red"


def test_temporadas():
    assert m.season_code(2027) == "2627"
    assert m.current_season_end_year(date(2026, 11, 7)) == 2027
    assert m.current_season_end_year(date(2027, 5, 20)) == 2027
    assert m.current_season_end_year(date(2027, 7, 1)) == 2028


def test_line_key_sin_puntos():
    assert m.line_key(3.5) == "over_3_5"
    assert m.line_key(10.5) == "over_10_5"


def test_doc_id_estable_y_hash_cambia_con_los_datos():
    a, _ = m.team_rows_from_csv_row(_csv_row(), "SP2", "2627")
    b, _ = m.team_rows_from_csv_row(_csv_row(hc=7), "SP2", "2627")
    assert m.doc_id_for(a) == m.doc_id_for(b) == "SP2_2627_2026-11-02_alpha_beta_alpha"
    assert m.row_hash(a) != m.row_hash(b)


# ── Porcentajes ─────────────────────────────────────────────────────────────

def _rows(div, season, team, corners_list, opp="Rival", side="home"):
    out = []
    for i, c in enumerate(corners_list):
        d = f"{(i % 28) + 1:02d}/0{(i // 28) + 1}/2026"
        if side == "home":
            r = m.team_rows_from_csv_row(_csv_row(home=team, away=f"{opp}{i}", d=d, hc=c), div, season)[0]
        else:
            r = m.team_rows_from_csv_row(_csv_row(home=f"{opp}{i}", away=team, d=d, ac=c), div, season)[1]
        out.append(r)
    return out


def _cell(doc, market="corners_for", line="over_4_5", split="all"):
    return doc["markets"][market]["lines"][line][split]


def test_ponderacion_y_muestra_visible():
    cur = _rows("SP1", "2627", "Alpha", [6] * 6)                   # 6/6 > 4.5
    prev = _rows("SP1", "2526", "Alpha", [6] * 5 + [2] * 5)         # 5/10
    docs = m.build_rate_docs({"SP1": cur}, {"SP1": prev}, "2627", "2526")
    doc = docs["SP1_alpha"]
    c = _cell(doc)
    assert (c["n_cur"], c["hits_cur"], c["n_prev"], c["hits_prev"]) == (6, 6, 10, 5)
    assert c["n_w"] == pytest.approx(6 + 10 * 0.5)
    assert c["hits_w"] == pytest.approx(6 + 5 * 0.5)
    assert c["p_raw"] == pytest.approx(8.5 / 11, abs=1e-4)
    assert doc["previous_weight"] == 0.5 and not doc["previous_other_div"]


def test_ajuste_a_la_media_de_liga():
    alpha = _rows("SP1", "2627", "Alpha", [6] * 5)                  # 5/5
    beta = _rows("SP1", "2627", "Beta", [2] * 15, opp="Otro")       # 0/15
    docs = m.build_rate_docs({"SP1": alpha + beta}, {}, "2627", "2526")
    c = _cell(docs["SP1_alpha"])
    assert c["p_league"] == pytest.approx(5 / 20)
    assert c["p_adj"] == pytest.approx((5 + 8 * 0.25) / (5 + 8), abs=1e-4)
    assert c["p_adj"] < c["p_raw"] == 1.0


def test_ascendido_pesa_025_y_se_marca():
    cur = _rows("E0", "2627", "Coventry", [6] * 9)
    prev_lower = _rows("E1", "2526", "Coventry", [6] * 20)
    docs = m.build_rate_docs({"E0": cur}, {"E0": [], "E1": prev_lower}, "2627", "2526")
    doc = docs["E0_coventry"]
    assert doc["previous_div"] == "E1" and doc["previous_other_div"]
    assert doc["previous_weight"] == 0.25
    assert _cell(doc)["n_w"] == pytest.approx(9 + 20 * 0.25)


def test_sin_temporada_anterior():
    docs = m.build_rate_docs({"SP2": _rows("SP2", "2627", "Eldense", [6] * 11)}, {}, "2627", "2526")
    doc = docs["SP2_eldense"]
    assert doc["previous_div"] is None and doc["matches_previous"] == 0


def test_muestra_minima():
    # all: n_pond >= 10 y >= 5 actuales
    docs = m.build_rate_docs({"SP1": _rows("SP1", "2627", "Alpha", [6] * 4)},
                             {"SP1": _rows("SP1", "2526", "Alpha", [6] * 30)}, "2627", "2526")
    assert not _cell(docs["SP1_alpha"])["meets_min_sample"]       # 4 actuales < 5
    docs = m.build_rate_docs({"SP1": _rows("SP1", "2627", "Alpha", [6] * 5)},
                             {"SP1": _rows("SP1", "2526", "Alpha", [6] * 10)}, "2627", "2526")
    assert _cell(docs["SP1_alpha"])["meets_min_sample"]           # 5 + 10·0.5 = 10
    # home/away: n_pond >= 8
    assert _cell(docs["SP1_alpha"], split="home")["meets_min_sample"]
    assert not _cell(docs["SP1_alpha"], split="away")["meets_min_sample"]  # 0 fuera


def test_lados_casa_fuera():
    home = _rows("SP1", "2627", "Alpha", [6] * 4, side="home")
    away = _rows("SP1", "2627", "Alpha", [2] * 3, side="away")
    c_home = _cell(m.build_rate_docs({"SP1": home + away}, {}, "2627", "2526")["SP1_alpha"], split="home")
    c_away = _cell(m.build_rate_docs({"SP1": home + away}, {}, "2627", "2526")["SP1_alpha"], split="away")
    assert (c_home["n_cur"], c_home["hits_cur"]) == (4, 4)
    assert (c_away["n_cur"], c_away["hits_cur"]) == (3, 0)


def test_paradas_llevan_fuente_derivada_en_porcentajes():
    docs = m.build_rate_docs({"SP1": _rows("SP1", "2627", "Alpha", [6] * 3)}, {}, "2627", "2526")
    assert docs["SP1_alpha"]["markets"]["saves_derived"]["source"] == "derivada"
    assert docs["SP1_alpha"]["markets"]["corners_for"]["source"] == "football-data.co.uk"


def test_faltas_f2_no_cuentan_en_la_muestra():
    cur = _rows("F1", "2627", "Troyes", [6] * 9)
    prev = _rows("F2", "2526", "Troyes", [6] * 20)
    doc = m.build_rate_docs({"F1": cur}, {"F2": prev}, "2627", "2526")["F1_troyes"]
    fouls = doc["markets"]["fouls_for"]["lines"]["over_9_5"]["all"]
    corners = _cell(doc)
    assert fouls["n_prev"] == 0 and corners["n_prev"] == 20


def test_run_rechaza_seasons_invalido():
    import asyncio
    res = asyncio.run(m.run_fdco_team_matches(seasons="todas"))
    assert res["ok"] is False
