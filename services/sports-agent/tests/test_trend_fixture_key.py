"""Un envío por partido: el mismo partido con dos match_id (CL_SF_* y football-data) y
nombres distintos por fuente cuenta como uno, por fecha + ids canónicos. Sin Firestore."""
from analyzers import trend_finder as tf

ROMA_SF = {"match_id": "CL_SF_16938872", "league": "CL", "match_date": "2026-10-14 19:00",
           "home_team": "AS Roma", "away_team": "Real Madrid", "home_team_id": 100, "away_team_id": 86}
ROMA_FD = {"match_id": "575352", "league": "CL", "match_date": "2026-10-14T19:00:00Z",
           "home_team": "AS Roma", "away_team": "Real Madrid CF", "home_team_id": 100, "away_team_id": 86}


def _c(fx, market, score=1.0):
    return {**fx, "market": market, "pattern_type": "model", "team": None, "team_id": None, "score": score}


def test_los_dos_ids_son_un_solo_partido():
    assert tf._fixture_key(ROMA_SF) == tf._fixture_key(ROMA_FD) == "2026-10-14|id|100|86"
    groups = tf._group_by_fixture([_c(ROMA_SF, "btts"), _c(ROMA_FD, "btts", 2.0), _c(ROMA_FD, "dnb")])
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert sorted(ck[0] for ck in g["by_candidate_key"]) == ["btts", "dnb"]


def test_enviado_con_un_id_no_se_repite_con_el_otro():
    sent = {tf._fixture_key(ROMA_SF): {"markets_sent": ["btts", "dnb"]}}
    fx = {**ROMA_FD, "candidates": [_c(ROMA_FD, "btts"), _c(ROMA_FD, "dnb")]}
    keep, skipped = tf._split_already_sent([fx], sent)
    assert keep == [] and len(skipped) == 1


def test_registro_viejo_por_nombres_sigue_valiendo():
    old_key = "2026-10-14|real madrid|roma"   # physical_fixture_key, clave de antes
    assert old_key in tf._fixture_lookup_keys(ROMA_SF)
    fx = {**ROMA_SF, "candidates": [_c(ROMA_SF, "btts"), _c(ROMA_SF, "double_chance")]}
    keep, _ = tf._split_already_sent([fx], {old_key: {"markets_sent": ["btts"]}})
    assert keep and keep[0]["new_markets"] == ["double_chance"]


def test_sin_ids_cae_a_nombres_y_agrupa_con_el_que_los_tiene():
    sin_ids = {**ROMA_SF, "home_team_id": None, "away_team_id": None}
    assert tf._fixture_key(sin_ids) == "2026-10-14|real madrid|roma"
    groups = tf._group_by_fixture([_c(ROMA_SF, "btts"), _c(sin_ids, "dnb")])
    assert len(groups) == 1
