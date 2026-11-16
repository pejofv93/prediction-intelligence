"""Versión del ajuste Poisson guardada en cada predicción y en la alerta (separar antes/después)."""
from analyzers.value_bet_engine import _build_alert_payload
from enrichers import poisson_model as pm


def test_version_y_fecha_del_cambio():
    assert pm.FIT_VERSION == "lbfgs-l2-v1"
    assert pm.FIT_VERSION_PREVIOUS == "nelder-mead-2000"
    assert pm.FIT_VERSION_SINCE == "2026-11-16"


def test_la_alerta_lleva_la_version_de_la_prediccion():
    pred = {"match_id": "m1", "market_type": "h2h", "ev": 0.1, "poisson_fit": pm.FIT_VERSION}
    assert _build_alert_payload(pred, {"match_id": "m1"})["poisson_fit"] == "lbfgs-l2-v1"
    assert _build_alert_payload({"match_id": "m2", "ev": 0.1}, {}).get("poisson_fit") is None
