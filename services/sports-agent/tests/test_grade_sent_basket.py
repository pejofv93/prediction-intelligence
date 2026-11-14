"""grade_sent_alerts: el baloncesto registrado en alerts_sent copia el resultado de su doc."""
import asyncio
from unittest.mock import MagicMock

import learner.learning_engine as le


def _doc(doc_id, d):
    m = MagicMock()
    m.id = doc_id
    m.to_dict.return_value = d
    return m


def test_baloncesto_copia_resultado_de_predictions(monkeypatch):
    sent = [_doc("s1", {"type": "sports_sent", "sport": "basketball", "prediction_id": "g1_h2h", "result": None}),
            _doc("s2", {"type": "sports_sent", "sport": "basketball", "prediction_id": "g2_h2h", "result": None}),
            _doc("s3", {"type": "sports_sent", "sport": "basketball", "prediction_id": "g3_h2h", "result": None})]
    preds = {"g1_h2h": {"correct": True}, "g2_h2h": {"correct": None}, "g3_h2h": {"push": True}}
    writes = {}

    def col(name):
        c = MagicMock()

        def document(doc_id):
            ref = MagicMock()
            snap = MagicMock()
            snap.exists = doc_id in preds
            snap.to_dict.return_value = preds.get(doc_id)
            ref.get.return_value = snap
            ref.update.side_effect = lambda d: writes.setdefault(doc_id, d)
            return ref
        c.document.side_effect = document
        return c

    import shared.firestore_client as fc
    monkeypatch.setattr(fc, "iter_all", lambda *a, **k: sent)
    monkeypatch.setattr(le, "col", col)
    summary = asyncio.run(le.grade_sent_alerts())
    assert writes["s1"]["result"] == "win" and writes["s1"]["correct"] is True
    assert writes["s3"]["result"] == "push" and writes["s3"]["correct"] is None
    assert "s2" not in writes                         # su partido aún sin resultado
    assert summary["graduados"] == 2 and summary["aciertos"] == 1 and summary["sin_fuente"] == 0
