"""Regla de propiedad en resultados de CL: el CL_SF_* no se escribe si football-data ya
tiene el mismo partido en match_results (salvo señal pendiente). Sin red ni Firestore."""
from collectors.allsports_uefa import results_owned_by_footballdata


def _sf(mid, home, away, gh, ga, league="CL", day="2026-10-20"):
    return {"match_id": mid, "home_team": home, "away_team": away, "goals_home": gh,
            "goals_away": ga, "league": league, "match_date": f"{day} 19:00"}


def _fd(mid, home, away, gh, ga, day="2026-10-20"):
    return {"match_id": mid, "home_team": home, "away_team": away, "goals_home": gh,
            "goals_away": ga, "league": "CL", "match_date": f"{day}T19:00:00Z"}


FD = [_fd("575363", "Liverpool FC", "Villarreal CF", 4, 3),
      _fd("575361", "Manchester City FC", "PAE AEK", 5, 1),
      _fd("575339", "Como 1907", "RB Leipzig", 4, 1, day="2026-09-10")]


def test_cede_por_nombre_y_por_marcador_mas_un_equipo():
    keep, ceded = results_owned_by_footballdata(
        [_sf("CL_SF_1", "Liverpool FC", "Villarreal", 4, 3),
         _sf("CL_SF_2", "Manchester City", "AEK Athens", 5, 1),
         _sf("CL_SF_3", "Como", "RB Leipzig", 4, 1, day="2026-09-10")], FD, set())
    assert [m["match_id"] for m in ceded] == ["CL_SF_1", "CL_SF_2", "CL_SF_3"]
    assert keep == []


def test_sin_gemelo_o_otra_fecha_se_escribe():
    keep, ceded = results_owned_by_footballdata(
        [_sf("CL_SF_4", "Real Madrid", "PSV Eindhoven", 2, 0),
         _sf("CL_SF_5", "Liverpool FC", "Villarreal", 4, 3, day="2026-10-21")], FD, set())
    assert [m["match_id"] for m in keep] == ["CL_SF_4", "CL_SF_5"] and ceded == []


def test_un_equipo_con_otro_marcador_no_es_gemelo():
    keep, _ = results_owned_by_footballdata([_sf("CL_SF_6", "Manchester City", "AEK Athens", 1, 1)], FD, set())
    assert [m["match_id"] for m in keep] == ["CL_SF_6"]


def test_senal_pendiente_se_escribe_igual():
    keep, ceded = results_owned_by_footballdata(
        [_sf("CL_SF_1", "Liverpool FC", "Villarreal", 4, 3)], FD, {"CL_SF_1"})
    assert [m["match_id"] for m in keep] == ["CL_SF_1"] and ceded == []


def test_el_y_ecl_no_se_tocan():
    keep, ceded = results_owned_by_footballdata(
        [_sf("EL_SF_7", "Liverpool FC", "Villarreal", 4, 3, league="EL")], FD, set())
    assert [m["match_id"] for m in keep] == ["EL_SF_7"] and ceded == []


def test_los_cl_sf_de_match_results_no_cuentan_como_football_data():
    sf_viejo = {**_sf("CL_SF_9", "Liverpool FC", "Villarreal", 4, 3), "match_date": "2026-10-20 19:00"}
    keep, _ = results_owned_by_footballdata([_sf("CL_SF_1", "Liverpool FC", "Villarreal", 4, 3)],
                                            [sf_viejo], set())
    assert [m["match_id"] for m in keep] == ["CL_SF_1"]
