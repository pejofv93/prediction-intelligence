"""/send-alert: solo 200 si el mensaje llegó a Telegram; 409 si no se envía por
duplicado o por tarde; 502 si Telegram lo rechaza; 400 si el texto viene vacío.
El envío a Telegram (send_message) y los slots de Firestore están simulados."""
import pytest
from fastapi.testclient import TestClient

import alert_manager
import main
import shared.match_timing

HEADERS = {"x-cloud-token": "test-token"}
_POLY = {"market_id": "m1", "recommendation": "BUY_YES", "edge": 0.2, "question": "¿X?"}
_SPORTS = {"sport": "football", "home_team": "A", "away_team": "B", "edge": 0.1,
           "odds": 2.0, "match_date": "2099-01-01T20:00:00Z"}


@pytest.fixture
def bot(monkeypatch):
    """Estado simulado: qué devuelve Telegram y qué slots se liberan."""
    state = {"telegram_ok": True, "messages": [], "released": []}

    async def _send(text, *a, **k):
        state["messages"].append(text)
        return state["telegram_ok"]

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(alert_manager, "send_message", _send)
    monkeypatch.setattr(alert_manager.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(alert_manager, "_format_poly_alert", lambda a: "poly")
    monkeypatch.setattr(alert_manager, "_format_alert_unified", lambda a: "sports")
    monkeypatch.setattr(alert_manager, "_record_sports_sent", lambda p: None)
    monkeypatch.setattr(alert_manager, "_claim_alert_slot", lambda key, t: True)
    monkeypatch.setattr(alert_manager, "_claim_match_slot", lambda p: (True, None))
    monkeypatch.setattr(alert_manager, "_release_alert_slot",
                        lambda key: state["released"].append(key))
    monkeypatch.setattr(shared.match_timing, "signal_is_too_late", lambda d: False)
    state["client"] = TestClient(main.app)
    return state


def _post(bot, alert_type, data):
    return bot["client"].post("/send-alert", json={"type": alert_type, "data": data},
                              headers=HEADERS)


def test_texto_enviado_200_sent_true(bot):
    r = _post(bot, "trend", {"text": "hola"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "sent": True, "reason": "sent"}
    assert bot["messages"] == ["hola"]


def test_poly_enviada_200_sent_true(bot):
    r = _post(bot, "polymarket", dict(_POLY))
    assert r.status_code == 200 and r.json()["sent"] is True
    assert bot["released"] == []


def test_poly_duplicada_409(bot, monkeypatch):
    monkeypatch.setattr(alert_manager, "_claim_alert_slot", lambda key, t: False)
    r = _post(bot, "polymarket", dict(_POLY))
    assert r.status_code == 409
    assert r.json() == {"ok": False, "sent": False, "reason": "duplicate"}
    assert bot["messages"] == []


def test_sports_duplicada_por_slot_de_partido_409(bot, monkeypatch):
    monkeypatch.setattr(alert_manager, "_claim_match_slot", lambda p: (False, None))
    r = _post(bot, "sports", dict(_SPORTS))
    assert r.status_code == 409 and r.json()["reason"] == "duplicate"
    assert bot["messages"] == []


def test_sports_tarde_409(bot, monkeypatch):
    monkeypatch.setattr(shared.match_timing, "signal_is_too_late", lambda d: True)
    r = _post(bot, "sports", dict(_SPORTS))
    assert r.status_code == 409 and r.json()["reason"] == "too_late"
    assert bot["messages"] == []


def test_telegram_rechaza_texto_502(bot):
    bot["telegram_ok"] = False
    r = _post(bot, "polymarket_resolution", {"text": "resuelto"})
    assert r.status_code == 502
    assert r.json() == {"ok": False, "sent": False, "reason": "rejected"}


def test_telegram_rechaza_poly_502_y_libera_slot(bot):
    bot["telegram_ok"] = False
    r = _post(bot, "polymarket", dict(_POLY))
    assert r.status_code == 502 and r.json()["reason"] == "rejected"
    assert len(bot["released"]) == 1


def test_telegram_rechaza_sports_502(bot):
    bot["telegram_ok"] = False
    r = _post(bot, "sports", dict(_SPORTS))
    assert r.status_code == 502 and r.json()["reason"] == "rejected"


def test_texto_vacio_400(bot):
    r = _post(bot, "matched", {"text": ""})
    assert r.status_code == 400 and r.json()["reason"] == "empty"
    assert bot["messages"] == []
