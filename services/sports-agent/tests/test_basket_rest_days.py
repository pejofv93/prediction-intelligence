"""Descanso y back-to-back respecto a la fecha del partido en hora local de la liga (EE. UU.
en NBA), no respecto a la hora del análisis. Partidos reales de oct-2026. Sin red."""
from datetime import date

from analyzers.basketball_analyzer import _game_local_date, _rest_days


def _m(dt):
    return {"match_date": dt[:10], "match_datetime": dt}


def test_fecha_local_nba_es_la_de_eeuu():
    # 02:00 UTC del 31 = 22:00 ET del 30
    assert _game_local_date("2026-10-31T02:00Z", "NBA") == date(2026, 10, 30)
    assert _game_local_date("2026-10-30T23:00Z", "NBA") == date(2026, 10, 30)
    assert _game_local_date("2026-10-31T19:00Z", "NBA") == date(2026, 10, 31)


def test_nba_sin_hora_es_ambigua():
    assert _game_local_date("2026-10-29", "NBA") is None


def test_euroliga_fecha_utc():
    assert _game_local_date("2026-10-29", "EUROLEAGUE") == date(2026, 10, 29)
    assert _game_local_date("2026-10-29 19:45:00", "EUROLEAGUE") == date(2026, 10, 29)


def test_warriors_lakers_no_es_back_to_back():
    # Warriors–Lakers 31-oct 02:00Z (30-oct ET). Lakers jugó en LAC el 29-oct 02:30Z (28 ET)
    # y Warriors en MIN el 29-oct 00:00Z (28 ET): dos días de descanso, no B2B.
    game = _game_local_date("2026-10-31T02:00Z", "NBA")
    lakers = [_m("2026-10-29T02:30Z"), _m("2026-10-28T03:00Z"), _m("2026-10-25T21:00Z")]
    warriors = [_m("2026-10-29T00:00Z"), _m("2026-10-27T02:00Z")]
    assert _rest_days(lakers, game, "NBA") == 2
    assert _rest_days(warriors, game, "NBA") == 2


def test_back_to_back_real():
    # Hornets: en Washington el 30-oct 23:00Z (30 ET) y en casa ante 76ers el 31-oct 22:00Z (31 ET)
    game = _game_local_date("2026-10-31T22:00Z", "NBA")
    assert _rest_days([_m("2026-10-30T23:00Z"), _m("2026-10-28T23:00Z")], game, "NBA") == 1


def test_ignora_partidos_posteriores_o_sin_hora():
    game = _game_local_date("2026-10-31T02:00Z", "NBA")
    assert _rest_days([{"match_date": "2026-10-29"}], game, "NBA") is None   # historial antiguo
    assert _rest_days([_m("2026-11-01T00:00Z")], game, "NBA") is None
    assert _rest_days([_m("2026-10-29T02:30Z")], None, "NBA") is None
