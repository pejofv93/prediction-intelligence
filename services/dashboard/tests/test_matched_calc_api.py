"""POST /api/matched-calc y GET /api/es-bookmakers (solo el router, sin Basic Auth)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import matched_es


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(matched_es.router, prefix="/api")
    return TestClient(app)


def test_lista_de_casas(client):
    r = client.get("/api/es-bookmakers")
    assert r.status_code == 200
    body = r.json()
    slugs = {b["slug"] for b in body["bookmakers"]}
    assert {"bwin_es", "codere_es", "sportium", "betfair_es_exchange"} <= slugs
    assert "unibet" not in slugs
    assert body["default_commission"] == 0.05


def test_calificante_dutching(client):
    r = client.post("/api/matched-calc", json={
        "outcomes": ["1", "X", "2"], "bonus_kind": "qualifying", "bonus_outcome": "1",
        "bonus_bookmaker": "bwin_es", "stake": 10, "odds": 2.5,
        "legs": [{"outcome": "X", "kind": "back", "odds": 3.4, "bookmaker": "codere_es"},
                 {"outcome": "2", "kind": "back", "odds": 4.0, "bookmaker": "betway_es"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert [l["stake"] for l in body["legs"]] == [7.35, 6.25]
    assert body["profit_exact"] == pytest.approx(25 * (1 - 1 / 2.5 - 1 / 3.4 - 1 / 4.0), abs=1e-4)
    assert len(body["steps"]) == 3


def test_lay_betfair_es_con_comision_por_defecto(client):
    r = client.post("/api/matched-calc", json={
        "outcomes": ["1", "X", "2"], "bonus_kind": "freebet_snr", "bonus_outcome": "1",
        "bonus_bookmaker": "codere_es", "stake": 10, "odds": 5.0,
        "legs": [{"outcome": "1", "kind": "lay", "odds": 5.2}],
    })
    assert r.status_code == 200
    assert r.json()["legs"][0]["stake"] == round(40 / 5.15, 2)


def test_400_cobertura_en_casa_del_bono(client):
    r = client.post("/api/matched-calc", json={
        "outcomes": ["1", "2"], "bonus_kind": "qualifying", "bonus_outcome": "1",
        "bonus_bookmaker": "bwin_es", "stake": 10, "odds": 2.0,
        "legs": [{"outcome": "2", "kind": "back", "odds": 2.1, "bookmaker": "Bwin ES"}],
    })
    assert r.status_code == 400
    assert "misma casa" in r.json()["detail"]


def test_400_exchange_sin_licencia(client):
    r = client.post("/api/matched-calc", json={
        "outcomes": ["1", "2"], "bonus_kind": "qualifying", "bonus_outcome": "1",
        "bonus_bookmaker": "bwin_es", "stake": 10, "odds": 2.0,
        "legs": [{"outcome": "1", "kind": "lay", "odds": 2.1, "bookmaker": "betfair_ex_eu"}],
    })
    assert r.status_code == 400
