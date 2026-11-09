"""accuracy_by_confidence: el resultado exacto no suma en ningún tramo; el resto, igual que antes."""
import pytest

from learner.learning_engine import _confidence_bucket


@pytest.mark.parametrize("mt", ["correct_score", "CORRECT_SCORE", "Correct_Score", " correct_score "])
@pytest.mark.parametrize("conf", [0.66, 0.75, 0.85, 0.95, 0.99, 1.0])
def test_resultado_exacto_no_cuenta_en_ningun_tramo(mt, conf):
    assert _confidence_bucket({"market_type": mt, "confidence": conf}) is None


@pytest.mark.parametrize("conf,esperado", [
    (0.64, None), (0.65, "65_70"), (0.699, "65_70"), (0.70, "70_80"), (0.80, "80_90"),
    (0.90, "90_99"), (0.99, "90_99"), (1.0, "90_99"), (None, None),
])
def test_tramos_sin_cambios_para_el_resto(conf, esperado):
    assert _confidence_bucket({"market_type": "h2h", "confidence": conf}) == esperado
    assert _confidence_bucket({"confidence": conf}) == esperado          # sin market_type = h2h
    assert _confidence_bucket({"market_type": "TOTALS", "confidence": conf}) == esperado
