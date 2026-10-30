"""Totales y hándicap de baloncesto: se gradúan con el marcador final, la línea sale de la
selección y van por la vía legacy (sin pesos). Partidos reales de oct-2026. Sin red."""
import asyncio

from collectors import basketball_collector as bc
from learner.learning_engine import (_basket_score_in_window, _grade_with_score,
                                     _is_basket_score_market)


def _p(market, sel, league="NBA", **kw):
    return {"sport": "basketball", "league": league, "market_type": market, "selection": sel,
            "home_team": "Washington Wizards", "away_team": "Charlotte Hornets", **kw}


def test_totales():
    assert _grade_with_score(_p("totals", "Over 223.5"), 120, 110)["correct"] is True
    assert _grade_with_score(_p("totals", "Over 223.5"), 110, 110)["correct"] is False
    assert _grade_with_score(_p("totals", "Under 230.5"), 100, 101)["correct"] is True


def test_totales_linea_entera_es_push():
    assert _grade_with_score(_p("totals", "Over 220"), 110, 110)["correct"] is None


def test_handicap_local():
    # Wizards -1.5: tienen que ganar por 2 o más
    assert _grade_with_score(_p("spread", "Washington Wizards -1.5"), 105, 103)["correct"] is True
    assert _grade_with_score(_p("spread", "Washington Wizards -1.5"), 104, 103)["correct"] is False
    # Wizards +4.5: cubren perdiendo por 4
    assert _grade_with_score(_p("spread", "Washington Wizards +4.5"), 100, 104)["correct"] is True
    assert _grade_with_score(_p("spread", "Washington Wizards +4.5"), 100, 105)["correct"] is False


def test_futbol_sin_line_sigue_sin_graduarse():
    p = {"sport": "football", "league": "PL", "market_type": "totals", "selection": "Over 2.5"}
    assert _grade_with_score(p, 3, 1) is None


def test_solo_totales_y_handicap_de_basket():
    assert _is_basket_score_market(_p("totals", "Over 223.5"))
    assert _is_basket_score_market(_p("spread", "Washington Wizards -1.5", league="EUROLEAGUE"))
    assert not _is_basket_score_market(_p("h2h", "Washington Wizards"))
    assert not _is_basket_score_market({"sport": "football", "league": "PL", "market_type": "totals"})


def test_solo_partidos_desde_el_30_oct():
    # Las enviadas anteriores van por backfill; las no enviadas anteriores no se tocan.
    assert _basket_score_in_window(_p("totals", "Over 223.5", match_date="2026-10-30T23:00Z"))
    assert _basket_score_in_window(_p("totals", "Over 165.5", match_date="2026-11-04 19:45:00"))
    assert not _basket_score_in_window(_p("totals", "Over 165.5", match_date="2026-10-29T18:30:00.000Z"))
    assert not _basket_score_in_window(_p("totals", "Over 165.5"))


def test_marcador_en_la_orientacion_de_la_prediccion(monkeypatch):
    async def fake(league, date10):
        return [{"match_date": "2026-10-29", "home_team_name": "LA Clippers",
                 "away_team_name": "Los Angeles Lakers", "goals_home": 87.0, "goals_away": 103.0}]
    monkeypatch.setattr(bc, "_basket_finished_for_league", fake)
    run = asyncio.run
    assert run(bc.get_basketball_score_by_teams("NBA", "LA Clippers", "Los Angeles Lakers", "2026-10-29")) == (87, 103)
    assert run(bc.get_basketball_score_by_teams("NBA", "Los Angeles Lakers", "LA Clippers", "2026-10-29")) == (103, 87)
    assert run(bc.get_basketball_result_by_teams("NBA", "LA Clippers", "Los Angeles Lakers", "2026-10-29")) == "AWAY_WIN"
    assert run(bc.get_basketball_score_by_teams("NBA", "LA Clippers", "Los Angeles Lakers", "2026-10-28")) is None


def test_moneyline_de_basket_tambien_es_basket():
    from learner.learning_engine import _is_basketball
    assert _is_basketball(_p("h2h", "Washington Wizards"))
    assert _is_basketball({"league": "EUROLEAGUE", "market_type": "h2h"})
    assert not _is_basketball({"sport": "football", "league": "PL", "market_type": "h2h"})
