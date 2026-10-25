"""is_friendly: torneos de verano fuera de 853 (casos reales de team_stats, 25-oct-2026)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rebuild_elo import is_friendly, ocho_ligas_ids  # noqa: E402

# ids reales de team_stats: Arsenal 57, Dortmund 4, Bayern 5, Leipzig 721, Middlesbrough 343,
# Wrexham 404, Benfica 1903, Villarreal 94, Como 7397
OCHO = {"57", "4", "5", "721", "343", "404", "1903", "94", "7397"}


def _m(match_id, date, tournament=None, tournament_id=None):
    return {"match_id": match_id, "date": date, "tournament": tournament,
            "tournament_id": tournament_id}


def test_853_sigue_siendo_amistoso_sin_contexto():
    assert is_friendly(_m("OTHER_SF_1", "2026-03-20T19:00:00+00:00", "Club Friendly Games", 853))


def test_emirates_cup_arsenal_dortmund():
    m = _m("OTHER_SF_16435283", "2026-08-09T15:00:00+00:00", "Emirates Cup")
    assert is_friendly(m, OCHO, (57, 4))


def test_telekom_cup_bayern_leipzig():
    m = _m("OTHER_SF_16242805", "2026-08-15T14:00:00+00:00", "Telekom Cup")
    assert is_friendly(m, OCHO, ("5", "721"))


def test_como_cup_con_un_rival_de_fuera():
    m = _m("OTHER_SF_16654343", "2026-07-29T18:00:00+00:00", "Como Cup")
    assert is_friendly(m, OCHO, ("sf_853576", "94"))   # Al Ula – Villarreal


def test_efl_cup_se_conserva():
    m = _m("OTHER_SF_16392868", "2026-08-07T18:45:00+00:00", "EFL Cup")
    assert not is_friendly(m, OCHO, ("343", "404"))


def test_previa_uefa_se_conserva():
    m = _m("OTHER_SF_16581588", "2026-08-06T19:00:00+00:00", "UEFA Europa League")
    assert not is_friendly(m, OCHO, ("1903", "sf_2353"))   # Benfica – Hearts
    m = _m("OTHER_SF_16260286", "2026-08-12T19:00:00+00:00", "UEFA Super Cup")
    assert not is_friendly(m, OCHO, ("57", "4"))


def test_ligas_y_copas_oficiales_en_agosto():
    for t in ("Ligue 2", "2. Bundesliga", "Coppa Italia", "Johan Cruijff Schaal", "Supertaça",
              "Franz Beckenbauer Supercup", "VriendenLoterij Eredivisie", "Liga Portugal Betclic",
              "Eurojackpot KNVB Beker", "Championship", "LaLiga 2", "Trophée des Champions",
              "Community Shield"):
        assert not is_friendly(_m("OTHER_SF_9", "2026-08-10T18:00:00+00:00", t), OCHO, ("94",)), t


def test_liga_de_un_club_uefa_de_otro_pais_se_conserva():
    # LASK – rival austriaco, 31-jul: ninguno es de las 8 ligas
    m = _m("OTHER_SF_16392087", "2026-07-31T17:30:00+00:00", "Austrian Bundesliga")
    assert not is_friendly(m, OCHO, ("2016", "sf_2052"))


def test_fuera_de_junio_agosto_no_se_toca():
    m = _m("OTHER_SF_9", "2026-10-10T18:00:00+00:00", "Telekom Cup")
    assert not is_friendly(m, OCHO, ("5", "721"))


def test_sin_torneo_y_football_data_se_conservan():
    # Los antiguos sin torneo guardado (Wrexham–Leeds 25-jul) no los decide esta función.
    assert not is_friendly(_m("OTHER_SF_16274080", "2026-07-25T14:00:00+00:00", None), OCHO, ("404",))
    assert not is_friendly(_m("565824", "2026-08-15T13:30:00Z", "Emirates Cup"), OCHO, ("57",))


def test_nombre_que_empieza_como_liga_pero_es_de_verano():
    m = _m("OTHER_SF_9", "2026-07-20T12:00:00+00:00", "Premier League Asia Trophy")
    assert is_friendly(m, OCHO, ("57",))


def test_ocho_ligas_ids():
    ts = [{"_id": "94", "team_id": 94, "league": "PD"}, {"_id": "sf_2052", "league": ""},
          {"_id": "2016", "team_id": 2016, "league": "CL"}]
    assert ocho_ligas_ids(ts) == {"94"}
