"""Historial NBA desde el schedule ESPN: solo partidos terminados. Un partido en juego ya
tiene marcador > 0 (Clippers–Lakers 38-56 guardado a mitad del partido). Sin red."""
from collectors.api_sports_client import _espn_completed


def _comp(name, state, completed):
    return {"status": {"type": {"name": name, "state": state, "completed": completed}}}


def test_final():
    assert _espn_completed(_comp("STATUS_FINAL", "post", True))


def test_en_juego_y_descanso_no():
    assert not _espn_completed(_comp("STATUS_IN_PROGRESS", "in", False))
    assert not _espn_completed(_comp("STATUS_HALFTIME", "in", False))


def test_programado_no():
    assert not _espn_completed(_comp("STATUS_SCHEDULED", "pre", False))


def test_sin_status_decide_el_marcador():
    assert _espn_completed({})
