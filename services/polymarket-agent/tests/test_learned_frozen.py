"""POLY_LEARNED_PARAMS_FROZEN: el aprendizaje mide en *_measured sin cambiar lo aplicado y
alert_engine usa los umbrales base aunque el acierto pase del 50 %."""
from unittest.mock import MagicMock

V171 = {
    "version": 171, "min_edge": 0.115, "min_confidence": 0.9,
    "buy_yes_min_edge": 0.115, "buy_yes_min_confidence": 0.9,
    "buy_no_min_edge": 0.10, "buy_no_min_confidence": 0.9, "buy_no_sports_min_edge": 0.15,
    "by_category": {"sports": {"total": 43, "wins": 21, "accuracy": 0.4884}},
    "accuracy_by_bucket": {"high": {"n": 113, "accuracy": 0.4425}},
    "llm_bias_by_category": {"sports": {"n": 43, "bias": 0.2574}},
    "accuracy_overall": 0.436, "sample_size": 172,
}


def _trades():
    t = []
    for i in range(30):  # 80 % de acierto, todos sports, edge alto
        t.append({"source": "polymarket", "category": "sports", "selection": "BUY_YES",
                  "result": "win" if i % 5 else "loss", "edge": 0.2, "confidence": 0.7,
                  "signal_data": {"real_prob": 0.7}})
    return t


def _learn(monkeypatch, frozen, current):
    import poly_learning_engine as ple
    monkeypatch.setattr(ple, "POLY_LEARNED_PARAMS_FROZEN", frozen)
    monkeypatch.setattr(ple, "_load_resolved_trades", _trades)
    monkeypatch.setattr(ple, "_load_current_weights", lambda: dict(current))
    written = []
    monkeypatch.setattr(ple, "col", lambda n: MagicMock(
        document=lambda k: MagicMock(set=lambda d: written.append(d))))
    ple.run_poly_learning()
    return written[0]


def test_congelado_no_cambia_lo_aplicado(monkeypatch):
    doc = _learn(monkeypatch, True, V171)
    import poly_learning_engine as ple
    for k in ple._FROZEN_KEYS:
        assert doc[k] == V171[k], k
        assert f"{k}_measured" in doc
    assert doc["by_category_measured"]["sports"]["total"] == 30
    assert doc["accuracy_by_bucket_measured"]["high"]["n"] == 30
    assert doc["buy_yes_min_edge_measured"] != V171["buy_yes_min_edge"]
    assert doc["learned_params_frozen"] is True
    # segunda noche: sigue igual lo aplicado y la medición parte de lo medido
    doc2 = _learn(monkeypatch, True, doc)
    for k in ple._FROZEN_KEYS:
        assert doc2[k] == V171[k], k


def test_descongelado_como_antes(monkeypatch):
    doc = _learn(monkeypatch, False, V171)
    assert doc["by_category"]["sports"]["total"] == 30
    assert doc["accuracy_by_bucket"]["high"]["n"] == 30
    assert "by_category_measured" not in doc


def _thresholds(monkeypatch, frozen, acc):
    import alert_engine as ae
    import shared.firestore_client as fc
    monkeypatch.setattr(ae, "POLY_LEARNED_PARAMS_FROZEN", frozen)
    monkeypatch.setattr(ae, "_LEARNED_THRESHOLDS", None)
    data = {**V171, "accuracy_overall": acc}
    snap = MagicMock(exists=True, to_dict=lambda: data)
    monkeypatch.setattr(fc, "col", lambda n: MagicMock(document=lambda k: MagicMock(get=lambda: snap)))
    return ae._load_learned_thresholds()


def test_umbrales_dormidos_aunque_acierto_pase_50(monkeypatch):
    import alert_engine as ae
    assert _thresholds(monkeypatch, True, 0.62) == ae._BASE_THRESHOLDS
    assert _thresholds(monkeypatch, False, 0.62) != ae._BASE_THRESHOLDS   # control: se activarían
