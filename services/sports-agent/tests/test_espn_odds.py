"""Cuotas ESPN del scoreboard NBA: formato actual (moneyline/pointSpread, DraftKings) y el
anterior (homeTeamOdds.moneyLine). Muestra real SAC @ NO del 2026-10-29. Sin red."""
from analyzers.basketball_analyzer import _build_event_from_espn_odds
from collectors.api_sports_client import _parse_espn_odds

NUEVO = [{
    "details": "NO -5.5", "overUnder": 237.5, "spread": -5.5,
    "homeTeamOdds": {"favorite": True}, "awayTeamOdds": {"favorite": False},
    "moneyline": {"home": {"close": {"odds": "-218"}, "open": {"odds": "-192"}},
                  "away": {"close": {"odds": "+180"}, "open": {"odds": "+160"}}},
    "pointSpread": {"home": {"close": {"line": "-5.5", "odds": "-115"}},
                    "away": {"close": {"line": "+5.5", "odds": "-105"}}},
}]


def test_formato_nuevo():
    assert _parse_espn_odds(NUEVO) == {"home_ml": -218, "away_ml": 180, "spread": "-5.5", "total": 237.5}


def test_visitante_favorito_lleva_signo_del_local():
    odds = [{**NUEVO[0], "details": "NO -5.5",
             "pointSpread": {"home": {"close": {"line": "+3.5"}}, "away": {"close": {"line": "-3.5"}}}}]
    assert _parse_espn_odds(odds)["spread"] == "+3.5"


def test_sin_close_usa_open_y_even():
    odds = [{"overUnder": 220, "moneyline": {"home": {"open": {"odds": "EVEN"}},
                                             "away": {"close": {"odds": "-120"}}}}]
    assert _parse_espn_odds(odds) == {"home_ml": 100, "away_ml": -120, "spread": None, "total": 220}


def test_formato_anterior_sigue_valiendo():
    odds = [{"details": "-4.5", "overUnder": 218.5,
             "homeTeamOdds": {"moneyLine": -190}, "awayTeamOdds": {"moneyLine": 160}}]
    assert _parse_espn_odds(odds) == {"home_ml": -190, "away_ml": 160, "spread": "-4.5", "total": 218.5}


def test_sin_cuotas():
    assert _parse_espn_odds([]) is None
    assert _parse_espn_odds([{"details": "NO -5.5"}]) is None


def test_el_analizador_construye_el_evento():
    ev = _build_event_from_espn_odds("New Orleans Pelicans", "Sacramento Kings", _parse_espn_odds(NUEVO))
    mk = {m["key"]: m for m in ev["bookmakers"][0]["markets"]} if "bookmakers" in ev else None
    assert mk and mk["h2h"]["outcomes"][0]["price"] < mk["h2h"]["outcomes"][1]["price"]
    assert mk["spreads"]["outcomes"][0]["point"] == -5.5
    assert mk["totals"]["outcomes"][0]["point"] == 237.5
