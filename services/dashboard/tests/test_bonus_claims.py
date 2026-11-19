"""Registro de bonos: estados, patas con P&L y CRUD sobre un Firestore falso en memoria."""
import itertools

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import bonus_claims as bc


# ── Firestore falso ──────────────────────────────────────────────────────────────

class _Snap:
    def __init__(self, id_, data):
        self.id, self._data = id_, data
        self.exists = data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _Doc:
    def __init__(self, store, id_):
        self._store, self.id = store, id_

    def get(self):
        return _Snap(self.id, self._store.get(self.id))

    def set(self, data):
        self._store[self.id] = dict(data)

    def delete(self):
        self._store.pop(self.id, None)


class _Col:
    _ids = itertools.count(1)

    def __init__(self):
        self.store = {}

    def document(self, id_=None):
        return _Doc(self.store, id_ or f"auto{next(self._ids)}")

    def stream(self):
        return [_Snap(k, v) for k, v in list(self.store.items())]


@pytest.fixture
def fs(monkeypatch):
    cols = {}
    monkeypatch.setattr(bc, "_col", lambda name: cols.setdefault(name, _Col()))
    return cols


@pytest.fixture
def client(fs):
    app = FastAPI()
    app.include_router(bc.router, prefix="/api")
    return TestClient(app)


# ── Lógica pura ──────────────────────────────────────────────────────────────────

def test_transiciones():
    bc.check_transition("pendiente", "calificante_colocada")
    bc.check_transition("pendiente", "freebet_disponible")          # saltar hacia delante
    bc.check_transition("freebet_colocada", "completado")
    bc.check_transition("rollover_en_curso", "limitado")
    with pytest.raises(ValueError):
        bc.check_transition("freebet_colocada", "pendiente")        # hacia atrás
    with pytest.raises(ValueError):
        bc.check_transition("completado", "rollover_en_curso")      # final
    with pytest.raises(ValueError):
        bc.check_transition("caducado", "cancelado")
    with pytest.raises(ValueError):
        bc.check_transition("pendiente", "inventado")
    bc.check_transition("completado", "freebet_colocada", force=True)


def test_pnl_de_patas():
    assert bc.leg_pnl({"kind": "back", "stake": 10, "odds": 2.5, "result": "ganada"}) == 15.0
    assert bc.leg_pnl({"kind": "back", "stake": 10, "odds": 2.5, "result": "perdida"}) == -10.0
    assert bc.leg_pnl({"kind": "freebet_snr", "stake": 10, "odds": 5, "result": "ganada"}) == 40.0
    assert bc.leg_pnl({"kind": "freebet_snr", "stake": 10, "odds": 5, "result": "perdida"}) == 0.0
    assert bc.leg_pnl({"kind": "freebet_sr", "stake": 10, "odds": 5, "result": "ganada"}) == 50.0
    # lay 'ganada' = ganó el lay (la selección no salió): stake·(1−c)
    assert bc.leg_pnl({"kind": "lay", "stake": 10, "odds": 3, "commission": 0.05, "result": "ganada"}) == 9.5
    assert bc.leg_pnl({"kind": "lay", "stake": 10, "odds": 3, "result": "perdida"}) == -20.0
    assert bc.leg_pnl({"kind": "exchange_back", "stake": 10, "odds": 3, "commission": 0.05,
                       "result": "ganada"}) == 19.0
    assert bc.leg_pnl({"kind": "back", "stake": 10, "odds": 2, "result": "nula"}) == 0.0
    assert bc.leg_pnl({"kind": "back", "stake": 10, "odds": 2}) is None


def test_reglas_de_patas():
    with pytest.raises(ValueError, match="misma casa"):
        bc.normalize_leg({"role": "cobertura", "kind": "back", "bookmaker": "Bwin ES",
                          "odds": 2, "stake": 10}, "bwin_es")
    with pytest.raises(ValueError, match="licencia"):
        bc.normalize_leg({"kind": "back", "bookmaker": "unibet", "odds": 2, "stake": 10}, "bwin_es")
    with pytest.raises(ValueError, match="betfair.es"):
        bc.normalize_leg({"kind": "lay", "bookmaker": "betfair_ex_eu", "odds": 2, "stake": 10}, "bwin_es")
    lay = bc.normalize_leg({"kind": "lay", "odds": 3, "stake": 10}, "bwin_es")
    assert lay["bookmaker"] == "betfair_es_exchange" and lay["liability"] == 20.0
    assert lay["commission"] == 0.05
    # el calificante sí va en la casa del bono
    q = bc.normalize_leg({"role": "calificante", "kind": "back", "bookmaker": "Bwin ES",
                          "odds": 2, "stake": 10}, "bwin_es")
    assert q["bookmaker"] == "bwin_es"


# ── CRUD ─────────────────────────────────────────────────────────────────────────

def _new(client, **kw):
    body = {"bookmaker": "Bwin ES", "title": "Freebet bienvenida", "bonus_kind": "freebet_snr",
            "amount": 20} | kw
    return client.post("/api/bonus-claims", json=body)


def test_alta_lista_y_flujo_completo(client, fs):
    r = _new(client, legs=[
        {"role": "calificante", "kind": "back", "bookmaker": "bwin_es", "odds": 2.0, "stake": 10},
        {"role": "cobertura", "kind": "lay", "odds": 2.04, "stake": 10.05},
    ])
    assert r.status_code == 200, r.text
    claim = r.json()
    cid = claim["id"]
    assert claim["bookmaker"] == "bwin_es" and claim["status"] == "pendiente"
    assert claim["pending_legs"] == 2 and claim["pnl"] == 0.0
    assert fs[bc.CLAIMS].store[cid]["bookmaker"] == "bwin_es"

    for st in ("calificante_colocada", "esperando_freebet", "freebet_disponible", "freebet_colocada"):
        r = client.patch(f"/api/bonus-claims/{cid}", json={"status": st})
        assert r.status_code == 200, r.text

    legs = claim["legs"]
    legs[0]["result"] = "perdida"     # −10
    legs[1]["result"] = "ganada"      # 10.05·0.95 = 9.55
    r = client.patch(f"/api/bonus-claims/{cid}", json={"legs": legs, "status": "completado"})
    body = r.json()
    assert body["status"] == "completado"
    assert body["pnl"] == -0.45 and body["pending_legs"] == 0
    assert [h["status"] for h in body["status_history"]][-1] == "completado"

    r = client.get("/api/bonus-claims")
    assert r.json()["count"] == 1 and r.json()["pnl_total"] == -0.45
    assert r.json()["by_status"] == {"completado": 1}
    assert client.get("/api/bonus-claims?status=pendiente").json()["count"] == 0


def test_transicion_invalida_400_y_force(client):
    cid = _new(client, status="freebet_colocada").json()["id"]
    r = client.patch(f"/api/bonus-claims/{cid}", json={"status": "pendiente"})
    assert r.status_code == 400
    r = client.patch(f"/api/bonus-claims/{cid}", json={"status": "pendiente", "force": True})
    assert r.status_code == 200 and r.json()["status_history"][-1]["forced"] is True


def test_alta_rechaza_casa_sin_licencia(client):
    assert _new(client, bookmaker="Unibet").status_code == 400
    assert _new(client, bookmaker="betfair_es_exchange").status_code == 400   # el exchange no da bonos aquí
    assert _new(client, bonus_kind="raro").status_code == 400


def test_get_y_delete(client):
    cid = _new(client).json()["id"]
    assert client.get(f"/api/bonus-claims/{cid}").json()["title"] == "Freebet bienvenida"
    assert client.delete(f"/api/bonus-claims/{cid}").json()["deleted"] is True
    assert client.get(f"/api/bonus-claims/{cid}").status_code == 404
    assert client.patch("/api/bonus-claims/nada", json={"status": "cancelado"}).status_code == 404


def test_estado_de_cuenta_por_casa(client, fs):
    r = client.get("/api/matched-bookmakers")
    books = {b["slug"]: b for b in r.json()["bookmakers"]}
    assert books["bwin_es"]["account_status"] == "sin_cuenta"
    r = client.put("/api/matched-bookmakers/bwin_es", json={"account_status": "limitada", "notes": "stake máx 5 €"})
    assert r.status_code == 200
    assert fs[bc.BOOKMAKERS].store["bwin_es"]["account_status"] == "limitada"
    books = {b["slug"]: b for b in client.get("/api/matched-bookmakers").json()["bookmakers"]}
    assert books["bwin_es"]["account_status"] == "limitada"
    assert client.put("/api/matched-bookmakers/unibet", json={"account_status": "activa"}).status_code == 404
    assert client.put("/api/matched-bookmakers/bwin_es", json={"account_status": "rara"}).status_code == 400
