"""GET /api/matched-signals y /api/matched-bonuses marcan 'solo referencia' las casas sin licencia ES."""
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import matched


class _Snap:
    def __init__(self, i, d):
        self.id, self._d = i, d

    def to_dict(self):
        return dict(self._d)


class _Col:
    def __init__(self, docs):
        self.docs = docs

    def stream(self):
        return [_Snap(str(i), d) for i, d in enumerate(self.docs)]

    def limit(self, n):
        return self

    def document(self, i):
        return types.SimpleNamespace(set=lambda d: None)


_SIG = {"signal_type": "coverage", "home_team": "A", "away_team": "B", "selection": "A",
        "back_odds": 2.0, "lay_odds": 2.04, "confidence": "high", "qualifying_rating": -0.5,
        "freebet_snr_rating": 70.0, "expires_at": "2999-01-01T00:00:00Z"}


@pytest.fixture
def client(monkeypatch):
    cols = {
        "matched_signals": _Col([
            _SIG | {"back_bookmaker": "pinnacle", "lay_bookmaker": "betfair_ex_eu"},
            _SIG | {"back_bookmaker": "bwin_es", "lay_bookmaker": "betfair_es_exchange",
                    "freebet_snr_rating": 10.0},
        ]),
        "matched_bonuses": _Col([{"bookmaker": "Codere", "type": "freebet_snr", "amount": 10,
                                  "min_odds": 1.5, "active": True}]),
    }
    fake = types.ModuleType("shared.firestore_client")
    fake.col = lambda name: cols[name]
    monkeypatch.setitem(sys.modules, "shared.firestore_client", fake)
    app = FastAPI()
    app.include_router(matched.router, prefix="/api")
    return TestClient(app)


def test_senales_marcadas(client):
    sigs = client.get("/api/matched-signals").json()["signals"]
    by_book = {s["back_bookmaker"]: s for s in sigs}
    assert "casa sin licencia en España (pinnacle)" in by_book["pinnacle"]["reference_only"]
    assert by_book["bwin_es"]["reference_only"] is None


def test_jugada_de_bono_con_lay_com_es_referencia(client):
    bonuses = client.get("/api/matched-bonuses").json()["bonuses"]
    play = bonuses[0]["play"]
    assert play["lay_bookmaker"] == "betfair_ex_eu"        # la de mayor SNR
    assert "exchange sin licencia en España" in play["reference_only"]
