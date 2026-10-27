"""Rotación de keys del escáner matched (_choose_keys): sin red ni Firestore."""
from datetime import datetime, timedelta, timezone

from matched.scanner import _choose_keys

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _cand(*keys):
    prio = {"tennis": 0, "soccer": 1, "basketball": 2}
    return sorted(((prio[k.split("_")[0]], k) for k in keys), key=lambda t: (t[0], t[1]))


def test_nunca_escaneadas_ganan_a_las_recientes():
    cand = _cand("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_france_ligue_one")
    counts = {k: 10 for _, k in cand}
    last = {"soccer_epl": NOW - timedelta(hours=24), "soccer_france_ligue_one": NOW - timedelta(hours=24)}
    keys, rows = _choose_keys(cand, counts, last, NOW)
    assert set(keys) == {"soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_epl"}
    by = {r["key"]: r for r in rows}
    assert by["soccer_france_ligue_one"]["reason"] == "tope de keys"
    assert by["soccer_spain_la_liga"]["score"] == 30.0


def test_tope_de_tenis():
    cand = _cand("tennis_atp_paris", "tennis_wta_a", "tennis_wta_b", "soccer_epl")
    counts = {"tennis_atp_paris": 40, "tennis_wta_a": 30, "tennis_wta_b": 30, "soccer_epl": 2}
    keys, rows = _choose_keys(cand, counts, {}, NOW)
    assert sum(k.startswith("tennis_") for k in keys) == 2
    assert "soccer_epl" in keys
    assert {r["key"]: r for r in rows}["tennis_wta_b"]["reason"] == "tope de tenis"


def test_sin_partidos_fuera_y_fallo_parcial_cuenta_uno():
    cand = _cand("soccer_epl", "soccer_italy_serie_a", "basketball_nba")
    counts = {"soccer_epl": 0, "soccer_italy_serie_a": None, "basketball_nba": 5}
    keys, rows = _choose_keys(cand, counts, {}, NOW)
    assert keys == ["basketball_nba", "soccer_italy_serie_a"]
    assert {r["key"]: r for r in rows}["soccer_epl"]["reason"] == "sin partidos en 72h"


def test_fallo_total_vuelve_al_orden_fijo():
    cand = _cand("tennis_atp_paris", "soccer_epl", "soccer_spain_la_liga", "basketball_nba")
    keys, rows = _choose_keys(cand, {k: None for _, k in cand}, {}, NOW)
    assert keys == ["tennis_atp_paris", "soccer_epl", "soccer_spain_la_liga"]
    assert all("orden fijo" in r["reason"] for r in rows)
