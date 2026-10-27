"""Contexto de eliminatoria UEFA: parse_event lo guarda y los escritores lo copian, sin
cambiar goals_*/winner (marcador final). Sin red ni Firestore."""
import asyncio

from collectors import allsports_uefa, firestore_writer
from collectors.allsports_uefa import parse_event


def _ev(home, away, status_code=100, round_info=None, **extra):
    return {"id": 99, "startTimestamp": 1786000000,
            "homeTeam": {"id": 1, "name": "A"}, "awayTeam": {"id": 2, "name": "B"},
            "status": {"type": "finished", "code": status_code},
            "homeScore": home, "awayScore": away,
            "roundInfo": round_info or {"round": 636, "name": "Playoff round", "cupRoundType": 4},
            **extra}


def test_partido_a_90_sin_prorroga():
    m = parse_event(_ev({"current": 2, "period1": 1, "period2": 1},
                        {"current": 1, "period1": 0, "period2": 1},
                        previousLegEventId=98, winnerCode=1, aggregatedWinnerCode=1), "CL")
    assert (m["goals_home"], m["goals_away"]) == (2, 1)
    assert (m["goals_home_90"], m["goals_away_90"]) == (2, 1)
    assert m["went_extra_time"] is False
    assert (m["round"], m["round_num"], m["cup_round_type"]) == ("Playoff round", 636, 4)
    assert (m["previous_leg_event_id"], m["winner_code"], m["aggregated_winner_code"]) == (98, 1, 1)


def test_prorroga_y_penaltis_no_tocan_goals():
    m = parse_event(_ev({"current": 2, "normaltime": 1, "extra1": 1, "penalties": 4},
                        {"current": 2, "normaltime": 1, "extra2": 1, "penalties": 3},
                        status_code=120), "EL")
    assert (m["goals_home"], m["goals_away"]) == (2, 2)          # sin cambios: marcador final
    assert (m["goals_home_90"], m["goals_away_90"]) == (1, 1)
    assert m["went_extra_time"] is True
    assert (m["penalties_home"], m["penalties_away"]) == (4, 3)


def test_prorroga_sin_ningun_campo_conocido_no_guarda_el_final_como_90():
    # Ni normaltime, ni extra1/2, ni code 110: las partes no suman el final → vacío.
    m = parse_event(_ev({"current": 3, "period1": 1, "period2": 1},
                        {"current": 2, "period1": 0, "period2": 1}, status_code=100), "CL")
    assert m["goals_home_90"] is None and m["goals_away_90"] is None


def test_sin_partes_no_se_puede_comprobar():
    m = parse_event(_ev({"current": 1}, {"current": 0}), "CL")
    assert m["goals_home_90"] is None


def test_prorroga_sin_normaltime_deja_90_desconocido():
    m = parse_event(_ev({"current": 3}, {"current": 2}, status_code=110), "ECL")
    assert m["went_extra_time"] is True
    assert m["goals_home_90"] is None and m["goals_away_90"] is None


def test_programado_sin_contexto_de_resultado():
    ev = _ev({}, {})
    ev["status"] = {"type": "notstarted", "code": 0}
    m = parse_event(ev, "CL")
    assert m["went_extra_time"] is None and m["goals_home_90"] is None
    assert m["round"] == "Playoff round"


def test_match_results_guarda_ids_y_contexto_solo_uefa(monkeypatch):
    written = {}

    class _Doc:
        def __init__(self, i): self.i = i
        def set(self, d): written[self.i] = d

    monkeypatch.setattr(firestore_writer, "col", lambda name: type("C", (), {"document": lambda s, i: _Doc(i)})())
    uefa = {**parse_event(_ev({"current": 1, "period1": 1, "period2": 0},
                                {"current": 0, "period1": 0, "period2": 0}), "CL"),
            "home_team_id": 86, "away_team_id": "sf_2"}
    fd = {"match_id": "123", "home_team": "X", "away_team": "Y", "league": "PL",
          "goals_home": 1, "goals_away": 1, "date": "2026-10-20", "home_team_id": 5, "round": "R"}
    asyncio.run(firestore_writer.update_finished_matches([uefa, fd]))
    u = written["CL_SF_99"]
    assert (u["home_team_id"], u["away_team_id"], u["source"]) == (86, "sf_2", "allsports_uefa")
    assert (u["goals_home_90"], u["went_extra_time"], u["round"], u["winner"]) == (1, False, "Playoff round", "H")
    assert set(written["123"]) == {"match_id", "home_team", "away_team", "league", "goals_home",
                                   "goals_away", "winner", "match_date", "season"}
    assert allsports_uefa  # módulo importado sin red
