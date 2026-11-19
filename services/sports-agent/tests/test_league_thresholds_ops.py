"""scripts/ops/league_thresholds.py replica la fusión config + Firestore del generador."""
import importlib.util
import os

from analyzers import value_bet_engine as vbe
from shared.config import LEAGUE_MIN_EDGE, SPORTS_MIN_EDGE

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_spec = importlib.util.spec_from_file_location(
    "league_thresholds", os.path.join(_ROOT, "scripts", "ops", "league_thresholds.py"))
lt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lt)


def test_mapa_de_nombres_igual_al_del_generador():
    assert lt.LEAGUE_NAME_TO_CODE == vbe._LEAGUE_NAME_TO_CODE


def test_firestore_h2h_pisa_a_la_config():
    eff = lt.effective_thresholds({"Serie A": {"h2h": 0.08, "totals": 0.096},
                                   "Liga rara": {"h2h": 0.5}, "Ligue 1": "basura"})
    assert eff["SA"] == 0.08
    assert eff["FL1"] == LEAGUE_MIN_EDGE["FL1"]
    assert "Liga rara" not in eff
    assert lt.effective_thresholds(None) == LEAGUE_MIN_EDGE


def test_envio_por_debajo_de_la_config():
    eff = lt.effective_thresholds({"Serie A": {"h2h": 0.08}})
    assert lt.below_config(0.085, "SA", eff) == {
        "league": "SA", "ev": 0.085, "min_config": 0.096, "min_efectivo": 0.08}
    assert lt.below_config(0.10, "SA", eff) is None
    assert lt.below_config(0.07, "XYZ", eff)["min_efectivo"] == SPORTS_MIN_EDGE
