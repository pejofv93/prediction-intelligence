"""El 1X2 en sombra (alerts_sent type=sports_shadow) se gradúa y se mide al cierre igual que
lo enviado: ambas pasadas leen los dos tipos."""
import asyncio

import learner.closing_clv as cc
import learner.learning_engine as le
import shared.firestore_client as fc


def _capture(monkeypatch):
    seen = []

    def fake_iter_all(name, where=None, **k):
        seen.append((name, where))
        return []

    monkeypatch.setattr(fc, "iter_all", fake_iter_all)
    return seen


def test_graduacion_lee_enviadas_y_sombra(monkeypatch):
    seen = _capture(monkeypatch)
    asyncio.run(le.grade_sent_alerts())
    assert ("alerts_sent", ("type", "in", ["sports_sent", "sports_shadow"])) in seen


def test_cierre_lee_enviadas_y_sombra(monkeypatch):
    seen = _capture(monkeypatch)
    asyncio.run(cc.run_closing_clv(dry_run=True))
    assert ("alerts_sent", ("type", "in", ["sports_sent", "sports_shadow"])) in seen
