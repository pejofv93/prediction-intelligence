"""1X2 de fútbol en sombra (SPORTS_FOOTBALL_1X2_SHADOW, 23-nov-2026): se registra en
alerts_sent como sports_shadow y no se envía; el resto de Sports sale igual."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import alert_manager
import main
import shared.firestore_client as fc
import shared.match_timing
from shared.clv_summary import TITLE_SHADOW, report_lines, split_by_type, summarize
from shared.config import is_football_1x2

HEADERS = {"x-cloud-token": "test-token"}
_F1X2 = {"sport": "football", "market_type": "h2h", "match_id": "m1", "team_to_back": "A",
         "home_team": "A", "away_team": "B", "odds": 2.0, "ev": 0.12, "edge": 0.1,
         "match_date": "2099-01-01T20:00:00Z"}


@pytest.fixture
def bot(monkeypatch):
    state = {"messages": [], "records": [], "claims": 0}

    async def _send(text, *a, **k):
        state["messages"].append(text)
        return True

    async def _no_sleep(*a, **k):
        return None

    def _claim(p):
        state["claims"] += 1
        return True, None

    monkeypatch.setattr(alert_manager, "SPORTS_FOOTBALL_1X2_SHADOW", True)
    monkeypatch.setattr(alert_manager, "send_message", _send)
    monkeypatch.setattr(alert_manager.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(alert_manager, "_format_alert_unified", lambda a: "sports")
    monkeypatch.setattr(alert_manager, "_apply_tier_adjustment", lambda p: (p, ""))
    monkeypatch.setattr(alert_manager, "_segment_history_line", lambda p, h: "")
    monkeypatch.setattr(alert_manager, "_segment_history_doc", lambda: None)
    monkeypatch.setattr(alert_manager, "_record_sports_sent",
                        lambda p, shadow=False: state["records"].append((p.get("market_type"), shadow)))
    monkeypatch.setattr(alert_manager, "_claim_match_slot", _claim)
    monkeypatch.setattr(alert_manager, "_claim_alert_slot", lambda key, t: True)
    monkeypatch.setattr(shared.match_timing, "signal_is_too_late", lambda d: False)
    state["client"] = TestClient(main.app)
    return state


def test_1x2_futbol_se_registra_en_sombra_y_no_se_envia(bot):
    r = bot["client"].post("/send-alert", json={"type": "sports", "data": dict(_F1X2)}, headers=HEADERS)
    assert r.status_code == 202
    assert r.json() == {"ok": False, "sent": False, "reason": "shadow"}
    assert bot["messages"] == [] and bot["claims"] == 0
    assert bot["records"] == [("h2h", True)]


@pytest.mark.parametrize("data", [
    {**_F1X2, "market_type": "totals"},
    {**_F1X2, "sport": "basketball"},
    {**_F1X2, "sport": "tennis"},
])
def test_resto_de_sports_se_envia_igual(bot, data):
    r = bot["client"].post("/send-alert", json={"type": "sports", "data": data}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["sent"] is True
    assert bot["messages"] == ["sports"] and bot["records"][0][1] is False


def test_con_la_sombra_apagada_el_1x2_sale(bot, monkeypatch):
    monkeypatch.setattr(alert_manager, "SPORTS_FOOTBALL_1X2_SHADOW", False)
    r = bot["client"].post("/send-alert", json={"type": "sports", "data": dict(_F1X2)}, headers=HEADERS)
    assert r.status_code == 200 and bot["messages"] == ["sports"]


def test_is_football_1x2():
    assert is_football_1x2({}) and is_football_1x2({"sport": "soccer", "market_type": "1X2"})
    assert not is_football_1x2({"sport": "football", "market_type": "btts"})
    assert not is_football_1x2({"sport": "nba", "market_type": "h2h"})


class _Ref:
    def __init__(self, store, exists):
        self.store, self.exists = store, exists

    def get(self):
        return type("S", (), {"exists": self.exists, "to_dict": lambda s: {"resends": 0}})()

    def set(self, data):
        self.store.append(("set", data))

    def update(self, data):
        self.store.append(("update", data))


def _record(monkeypatch, exists, shadow):
    store = []
    monkeypatch.setattr(fc, "col", lambda name: type("C", (), {
        "document": lambda s, _id: _Ref(store, exists)})())
    alert_manager._record_sports_sent(dict(_F1X2), shadow=shadow)
    return store


def test_registro_en_sombra(monkeypatch):
    (op, doc), = _record(monkeypatch, exists=False, shadow=True)
    assert op == "set" and doc["type"] == "sports_shadow" and doc["status"] == "shadow"
    assert doc["result"] is None and doc["odds"] == 2.0
    assert _record(monkeypatch, exists=True, shadow=True) == []          # re-evaluación: no toca
    (op, doc), = _record(monkeypatch, exists=False, shadow=False)
    assert doc["type"] == "sports_sent" and doc["status"] == "sent"


def test_aviso_de_cambio_de_cuota_no_sale_para_1x2_en_sombra(monkeypatch):
    sent = []

    async def _send(text, *a, **k):
        sent.append(text)
        return True

    preds = [{"match_id": "m1", "sport": "football", "market_type": "h2h", "odds": 2.0,
              "calculated_prob": 0.6, "match_date": "2099-01-01T20:00:00Z", "home_team": "A",
              "away_team": "B", "team_to_back": "A"}]
    docs = [type("D", (), {"id": p["match_id"], "to_dict": lambda s, p=p: dict(p)})() for p in preds]

    class _Q:
        def where(self, *a, **k): return self
        def limit(self, *a): return self
        def stream(self): return iter(docs)

    monkeypatch.setattr(fc, "col", lambda name: _Q())
    monkeypatch.setattr(alert_manager, "send_message", _send)
    monkeypatch.setattr(shared.match_timing, "signal_is_too_late", lambda d: False)
    monkeypatch.setattr(alert_manager, "SPORTS_FOOTBALL_1X2_SHADOW", True)
    assert asyncio.run(alert_manager.check_pending_odds_changes({"m1": 1.7})) == 0
    assert sent == []


def test_informe_semanal_separa_enviadas_y_sombra():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 30, tzinfo=timezone.utc)
    base = {"sport": "football", "market_type": "h2h", "sent_at": now, "close_source": "fdco-media-cierre"}
    docs = [{**base, "type": "sports_sent", "clv_close": 0.02},
            {**base, "type": "sports_shadow", "clv_close": -0.03},
            {**base, "type": "sports_shadow", "clv_close": -0.01}]
    sent, shadow = split_by_type(docs)
    assert len(sent) == 1 and len(shadow) == 2
    lines = report_lines(summarize(shadow, now), TITLE_SHADOW)
    assert lines[0] == TITLE_SHADOW and "n=2" in lines[2]
