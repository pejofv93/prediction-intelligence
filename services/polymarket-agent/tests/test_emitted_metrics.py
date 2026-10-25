"""Emitido real desde alerts_sent (1u por alerta, precio del envío)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock


def _doc(d):
    m = MagicMock()
    m.to_dict.return_value = d
    return m


def test_poly_alerted_metrics(monkeypatch):
    import shared.shadow_engine as se
    w1 = datetime(2026, 10, 6, tzinfo=timezone.utc)
    docs = [
        # BUY_NO a mp_yes 0.75 → cuota 4.0 → win +3u
        {"status": "sent", "market_id": "1", "direction": "BUY_NO", "last_price": 0.75,
         "result": "win", "resolved_at": w1},
        # BUY_YES a 0.5 → loss −1u
        {"status": "sent_archived", "market_id": "2", "recommendation": "BUY_YES",
         "market_price_yes": 0.5, "result": "loss", "resolved_at": w1},
        # sin dirección: signo del edge del alert_key → BUY_YES, win a 0.5 → +1u
        {"status": "sent", "market_id": "3", "alert_key": "3_0.12", "last_price": 0.5,
         "result": "win", "resolved_at": datetime(2026, 9, 1, tzinfo=timezone.utc)},
        {"status": "sent", "market_id": "4", "direction": "BUY_NO", "last_price": 0.4},  # pendiente
        {"status": None, "market_id": "5", "result": "win"},  # log legacy: no cuenta
    ]
    monkeypatch.setattr(se, "iter_all", lambda *a, **k: [_doc(d) for d in docs])
    m = se.poly_alerted_metrics()
    assert m["n"] == 3 and m["wins"] == 2 and m["pending"] == 1
    assert m["pnl"] == 3.0 and m["roi"] == 1.0
    assert m["by_direction"]["BUY_NO"] == {"n": 1, "wins": 1, "pnl": 3.0, "roi": 3.0}
    wk = se.poly_alerted_metrics(resolved_from=datetime(2026, 10, 5, tzinfo=timezone.utc),
                                 resolved_to=datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert wk["n"] == 2 and wk["pending"] == 0
