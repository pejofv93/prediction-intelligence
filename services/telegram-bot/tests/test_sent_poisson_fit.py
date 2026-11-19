"""alerts_sent guarda la versión del ajuste Poisson de la predicción enviada."""
import alert_manager
import shared.firestore_client as fc


class _Ref:
    def __init__(self, store):
        self.store = store

    def get(self):
        return type("S", (), {"exists": False, "to_dict": lambda s: {}})()

    def set(self, data):
        self.store.append(data)


class _Col:
    def __init__(self, store):
        self.store = store

    def document(self, _id):
        return _Ref(self.store)


def _record(monkeypatch, pred):
    store = []
    monkeypatch.setattr(fc, "col", lambda name: _Col(store))
    alert_manager._record_sports_sent(pred)
    return store[0]


def test_guarda_poisson_fit(monkeypatch):
    doc = _record(monkeypatch, {"match_id": "m1", "team_to_back": "A", "odds": 2.0, "ev": 0.1,
                                "sport": "football", "poisson_fit": "lbfgs-l2-v1"})
    assert doc["poisson_fit"] == "lbfgs-l2-v1"


def test_sin_poisson_queda_none(monkeypatch):
    doc = _record(monkeypatch, {"match_id": "m2", "team_to_back": "B", "odds": 2.0, "ev": 0.1,
                                "sport": "tennis"})
    assert doc["poisson_fit"] is None


def test_guarda_min_ev_aplicado(monkeypatch):
    doc = _record(monkeypatch, {"match_id": "m3", "team_to_back": "C", "odds": 2.0, "ev": 0.1,
                                "sport": "football", "min_ev": 0.096})
    assert doc["min_ev"] == 0.096
    doc = _record(monkeypatch, {"match_id": "m4", "team_to_back": "D", "odds": 2.0, "ev": 0.1,
                                "sport": "tennis"})
    assert doc["min_ev"] is None
