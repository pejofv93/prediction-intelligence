"""POLY_CATEGORY_BIAS_ENABLED: apagado no toca real_prob ni deja que el aprendizaje
reescriba llm_bias_by_category; encendido se comporta como antes."""
from unittest.mock import MagicMock, patch

import pytest

from .test_groq_analyzer import _col_mock, _enriched, _groq_client_mock, _groq_json, _market_doc

PRICE = 0.50
BIAS = {"economy": {"n": 40, "bias": 0.10}}


async def _analyze(monkeypatch, enabled, weights):
    import groq_analyzer
    monkeypatch.setattr(groq_analyzer, "POLY_CATEGORY_BIAS_ENABLED", enabled)
    market = _market_doc(question="Will the Fed cut interest rates in December 2026?",
                         price_yes=PRICE, volume_24h=60_000, slug="fed-cut-dec-2026")
    saved = []
    em = _enriched(price_yes=PRICE, news_trend="POSITIVE",
                   news_headlines=["Fed officials signal a December cut"])
    with (
        patch("shared.firestore_client.col", side_effect=_col_mock(market, saved)),
        patch("shared.groq_client._get_groq",
              return_value=_groq_client_mock(_groq_json(0.62, PRICE, recommendation="BUY_YES"))),
        patch("groq_analyzer._is_groq_quota_exhausted", return_value=False),
        patch("groq_analyzer._get_poly_weights", return_value=weights),
    ):
        return await groq_analyzer.analyze_market(em)


@pytest.mark.asyncio
async def test_off_no_aplica_y_registra(monkeypatch):
    base = await _analyze(monkeypatch, False, {})
    off = await _analyze(monkeypatch, False, {"llm_bias_by_category": {"economy": {"n": 40, "bias": 0.30}},
                                              "llm_bias_by_category_measured": BIAS})
    assert base["category"] == "economy"
    assert off["real_prob"] == base["real_prob"] and off["edge"] == base["edge"]
    sh = off["category_bias_shadow"]
    assert sh["bias"] == 0.10 and sh["n"] == 40          # el medido, no el congelado
    assert sh["real_prob_if_on"] == round(sh["real_prob"] - 0.10, 4)
    assert base["category_bias_shadow"] is None


@pytest.mark.asyncio
async def test_on_aplica_como_antes(monkeypatch):
    base = await _analyze(monkeypatch, True, {})
    on = await _analyze(monkeypatch, True, {"llm_bias_by_category": BIAS})
    assert on["real_prob"] < base["real_prob"]
    assert on["category_bias_shadow"] is None


def _learn(monkeypatch, enabled):
    import poly_learning_engine as ple
    monkeypatch.setattr(ple, "POLY_CATEGORY_BIAS_ENABLED", enabled)
    trades = [{"source": "polymarket", "category": "sports", "selection": "BUY_YES",
               "result": "loss", "edge": 0.2, "confidence": 0.7,
               "signal_data": {"real_prob": 0.7}} for _ in range(6)]
    monkeypatch.setattr(ple, "_load_resolved_trades", lambda: trades)
    monkeypatch.setattr(ple, "_load_current_weights", lambda: {
        "version": 171, "min_edge": 0.1, "min_confidence": 0.6, "buy_yes_min_edge": 0.1,
        "buy_yes_min_confidence": 0.6, "buy_no_min_edge": 0.1, "buy_no_min_confidence": 0.6,
        "llm_bias_by_category": {"sports": {"n": 43, "bias": 0.2574}}})
    written = []
    monkeypatch.setattr(ple, "col", lambda n: MagicMock(
        document=lambda k: MagicMock(set=lambda d: written.append(d))))
    ple.run_poly_learning()
    return written[0]


def test_learner_off_congela(monkeypatch):
    doc = _learn(monkeypatch, False)
    assert doc["llm_bias_by_category"] == {"sports": {"n": 43, "bias": 0.2574}}
    assert doc["llm_bias_by_category_measured"] == {"sports": {"n": 6, "bias": 0.7}}
    assert doc["category_bias_enabled"] is False


def test_learner_on_reescribe(monkeypatch):
    doc = _learn(monkeypatch, True)
    assert doc["llm_bias_by_category"] == {"sports": {"n": 6, "bias": 0.7}}
    assert doc["category_bias_enabled"] is True


@pytest.mark.asyncio
async def test_prediccion_lleva_version_del_categorizador(monkeypatch):
    r = await _analyze(monkeypatch, False, {})
    assert r["categorizer_version"] == 2
