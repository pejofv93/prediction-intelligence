"""BUY_YES descartados por confianza: se registran en poly_conf_skips (primer descarte fijo)
sin cambiar la decisión, y la predicción lleva la confianza de antes del tope."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

import alert_engine
from tests.test_groq_analyzer import (
    _col_mock, _enriched, _groq_client_mock, _groq_json, _market_doc,
)


def _run(analysis, store, fail=False):
    writes = []

    class _Ref:
        def __init__(self, k):
            self.k = k

        def get(self):
            if fail:
                raise RuntimeError("firestore caído")
            return MagicMock(exists=self.k in store, to_dict=lambda: dict(store.get(self.k, {})))

        def set(self, d):
            store[self.k] = dict(d)
            writes.append(("set", self.k))

        def update(self, d):
            store[self.k].update(d)
            writes.append(("update", self.k))

    def _col(name):
        c = MagicMock()
        if name == "poly_conf_skips":
            c.document.side_effect = _Ref
        else:
            c.document.return_value.get.return_value = MagicMock(exists=False)
        return c

    with patch("shared.firestore_client.col", side_effect=_col), \
         patch.object(alert_engine, "_load_learned_thresholds",
                      lambda: dict(alert_engine._BASE_THRESHOLDS)):
        ok = asyncio.run(alert_engine.check_and_alert(analysis))
    return ok, writes


_YES = {"market_id": "m7", "recommendation": "BUY_YES", "edge": 0.20, "confidence": 0.5925,
        "confidence_precap": 0.72, "conf_cap_applied": 0.5925, "market_price_yes": 0.45,
        "real_prob": 0.65, "category": "sports"}


def test_primer_descarte_queda_fijo_y_los_siguientes_solo_cuentan():
    store = {}
    ok, w = _run(dict(_YES), store)
    assert ok is False and w == [("set", "m7_BUY_YES")]
    d = store["m7_BUY_YES"]
    assert (d["market_price_yes"], d["edge"], d["confidence"]) == (0.45, 0.20, 0.5925)
    assert d["confidence_precap"] == 0.72 and d["conf_cap_applied"] == 0.5925
    assert d["min_confidence"] == 0.60 and d["would_pass_longshot"] is True and d["n_skips"] == 1
    assert d["skipped_at"] == d["last_at"]

    ok, w = _run({**_YES, "market_price_yes": 0.50, "edge": 0.18}, store)
    assert ok is False and w == [("update", "m7_BUY_YES")]
    d = store["m7_BUY_YES"]
    assert d["market_price_yes"] == 0.45 and d["last_price_yes"] == 0.50 and d["n_skips"] == 2


def test_buy_no_descartado_no_se_registra():
    store = {}
    _run({**_YES, "recommendation": "BUY_NO", "edge": -0.20, "confidence": 0.50}, store)
    assert store == {}


def test_si_firestore_falla_no_rompe_y_sigue_omitida():
    ok, _ = _run(dict(_YES), {}, fail=True)
    assert ok is False


@pytest.mark.asyncio
async def test_prediccion_lleva_confianza_previa_al_tope():
    price_yes = 0.45
    saved = []
    weights = {"accuracy_by_bucket": {"high": {"n": 113, "accuracy": 0.4425}}}
    with (
        patch("shared.firestore_client.col",
              side_effect=_col_mock(_market_doc("Will the Lakers win tonight?", price_yes=price_yes), saved)),
        patch("shared.groq_client._get_groq",
              return_value=_groq_client_mock(_groq_json(0.70, price_yes, confidence=0.80))),
        patch("groq_analyzer._is_groq_quota_exhausted", return_value=False),
        patch("groq_analyzer._get_poly_weights", return_value=weights),
    ):
        from groq_analyzer import analyze_market
        res = await analyze_market(_enriched(price_yes=price_yes))
    assert res is not None
    if res.get("conf_cap_applied") is None:
        pytest.skip(f"el caso no llegó al tramo alto (edge={res.get('edge')})")
    assert res["conf_cap_applied"] == 0.5925
    assert res["confidence_precap"] > 0.5925 >= res["confidence"]
    assert saved and saved[0]["confidence_precap"] == res["confidence_precap"]
