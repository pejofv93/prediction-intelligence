"""Exportación de la web de Tendencias: etiqueta de fiabilidad, formato compacto y comparación.
Ejecutar: python -m pytest trends_web/tests -q   (sin red ni credenciales)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import export_rates as er  # noqa: E402
from collectors import fdco_team_matches as fd  # noqa: E402


def _row(team, opp, side, date, corners, cards=1, fouls=11, shots=12, sot=4, ga=1, div="SP1", season="2627"):
    key = "_".join([date] + sorted([team, opp]))   # las dos filas de un partido comparten clave
    return {"div": div, "season": season, "match_date": date, "match_key": key,
            "team": team, "team_slug": fd.slugify(team), "opponent": opp, "side": side,
            "corners_for": corners, "cards_for": cards, "fouls_for": fouls, "shots_for": shots,
            "sot_for": sot, "saves_derived": 3, "corners_total": corners + 4, "cards_total": cards + 2,
            "fouls_total": fouls + 10, "shots_total": shots + 10, "sot_total": sot + 3,
            "goals_against": ga, "cards_convention": "second_yellow_counts_yellow_plus_red"}


def test_fiabilidad():
    assert er.reliability({"meets_min_sample": False, "p_adj": 0.9, "p_league": 0.5}) == "insuficiente"
    assert er.reliability({"meets_min_sample": True, "p_adj": 0.53, "p_league": 0.5}) == "en_linea"
    assert er.reliability({"meets_min_sample": True, "p_adj": 0.56, "p_league": 0.5}) == "encima"
    assert er.reliability({"meets_min_sample": True, "p_adj": 0.40, "p_league": 0.5}) == "debajo"
    assert er.reliability({"meets_min_sample": True, "p_adj": 0.5, "p_league": None}) == "sin_liga"


def _docs():
    cur = {"SP1": [_row("A", "B", "home" if i % 2 else "away", f"2026-09-{i + 1:02d}", corners=7 if i < 9 else 3)
                   for i in range(12)]
                  + [_row("B", "A", "away" if i % 2 else "home", f"2026-09-{i + 1:02d}", corners=4)
                     for i in range(12)]}
    prev = {"SP1": [_row("A", "B", "home", f"2025-09-{i + 1:02d}", corners=6, season="2526") for i in range(6)]}
    return fd.build_rate_docs(cur, prev, "2627", "2526"), cur


def test_formato_compacto_y_celdas():
    docs, cur = _docs()
    data = er.compact(docs, cur, "2026-11-09T12:00Z")
    lg = data["leagues"]["SP1"]
    assert lg["name"] == "LaLiga" and lg["matches"] == 12 and lg["data_through"] == "2026-09-12"
    a = next(t for t in lg["teams"] if t["team"] == "A")
    assert a["mc"] == 12 and a["mp"] == 6 and a["prev_div"] == "SP1" and a["prev_w"] == 0.5
    # córners +3,5 (primera línea), lado "all": la celda trae p_adj, p_liga, muestra y etiqueta
    p_adj, p_liga, n_cur, n_prev, n_w, label, hits_cur = a["r"]["corners_for"][0][0]
    src = docs["SP1_a"]["markets"]["corners_for"]["lines"]["over_3_5"]["all"]
    assert (p_adj, p_liga, n_cur, n_prev) == (round(src["p_adj"], 3), round(src["p_league"], 3), 12, 6)
    assert n_w == 15.0 and label == er.reliability(src)
    # aciertos de esta temporada para el modo "solo esta temporada": 9 de 12 con más de 3,5 córners
    assert hits_cur == src["hits_cur"] == 9
    # las 11 pestañas, con sus líneas legibles
    keys = [m["key"] for m in data["markets"]]
    assert keys == [k for k, _, _ in er.MARKET_LABELS] and data["markets"][0]["lines"][0] == "3,5"
    assert next(m for m in data["markets"] if m["key"] == "saves_derived")["source"] == "derivada"
    # ligas sin datos salen vacías, no se inventan
    assert data["leagues"]["E0"]["teams"] == [] and data["leagues"]["E0"]["data_through"] is None


def test_comparacion_detecta_igual_y_distinto():
    import compare_rates as cr
    docs, _ = _docs()
    mine = docs["SP1_a"]
    assert cr.compare_doc(mine, mine)[1] == 0
    import copy
    stored = copy.deepcopy(mine)
    stored["markets"]["corners_for"]["lines"]["over_3_5"]["all"]["p_adj"] += 0.01
    cells, diff, gap, ex = cr.compare_doc(mine, stored)
    assert diff == 1 and abs(gap - 0.01) < 1e-9 and ex == ["SP1·A·corners_for·over_3_5·all"]
