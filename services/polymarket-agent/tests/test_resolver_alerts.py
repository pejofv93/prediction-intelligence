"""Resolver: gradúa lo ENVIADO (alerts_sent), no la recomendación actual."""
from polymarket_resolver import _alert_selection, _determine_outcome, _trade_result


def test_selection_prefers_recommendation_then_direction():
    assert _alert_selection({"recommendation": "BUY_NO", "direction": "BUY_YES"}) == "BUY_NO"
    assert _alert_selection({"direction": "BUY_YES"}) == "BUY_YES"


def test_selection_from_alert_key_sign_when_no_direction():
    assert _alert_selection({"alert_key": "1895140_0.12"}) == "BUY_YES"
    assert _alert_selection({"alert_key": "1066081_-0.74"}) == "BUY_NO"
    assert _alert_selection({"alert_key": "raro"}) is None


def test_grading():
    assert _trade_result("BUY_NO", _determine_outcome(["0.0005", "0.9995"])) == "win"
    assert _trade_result("BUY_YES", _determine_outcome(["0.0005", "0.9995"])) == "loss"
    assert _determine_outcome(["0.5", "0.5"]) is None
