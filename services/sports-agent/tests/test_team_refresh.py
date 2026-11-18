"""Collect de fútbol: pedir un equipo solo si ha jugado desde su última descarga (LaLiga: 72 h)."""
from datetime import datetime, timedelta, timezone

from collectors.team_refresh import last_kickoff_by_team, needs_refresh

NOW = datetime(2026, 11, 18, 5, 30, tzinfo=timezone.utc)
H = timedelta(hours=1)


def test_sin_historial_se_pide():
    assert needs_refresh(None, None, True, NOW) == (True, "sin_historial")


def test_ha_jugado_desde_la_descarga():
    up = NOW - 30 * H
    assert needs_refresh(up, NOW - 10 * H, True, NOW) == (True, "ha_jugado")
    # descargado durante el partido (antes del final estimado): también se pide
    assert needs_refresh(NOW - 9 * H, NOW - 10 * H, True, NOW) == (True, "ha_jugado")


def test_no_ha_jugado_no_se_pide_aunque_pasen_dias():
    assert needs_refresh(NOW - 100 * H, NOW - 120 * H, True, NOW) == (False, "sin_jugar")


def test_sin_resultados_por_competicion_regla_72h():
    assert needs_refresh(NOW - 71 * H, None, True, NOW) == (False, "reciente")
    assert needs_refresh(NOW - 72 * H, None, True, NOW) == (True, "sin_resultados_72h")


def test_sin_lista_de_terminados_vuelve_a_20h():
    assert needs_refresh(NOW - 19 * H, NOW - 10 * H, False, NOW) == (False, "reciente")
    assert needs_refresh(NOW - 20 * H, None, False, NOW) == (True, "sin_lista_20h")


def test_salvaguarda_de_7_dias():
    assert needs_refresh(NOW - 7 * 24 * H, NOW - 8 * 24 * H, True, NOW) == (True, "7_dias")


def test_ultimo_partido_por_equipo():
    fin = [{"date": "2026-11-15T15:00:00Z", "home_team_id": 1, "away_team_id": 2},
           {"date": "2026-11-17T20:00:00Z", "home_team_id": 3, "away_team_id": 1},
           {"date": None, "home_team_id": 9, "away_team_id": 8}]
    last = last_kickoff_by_team(fin)
    assert last[1] == datetime(2026, 11, 17, 20, tzinfo=timezone.utc)
    assert last[2] == datetime(2026, 11, 15, 15, tzinfo=timezone.utc) and 9 not in last
