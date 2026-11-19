"""Lector de cuotas ES de odds-api.io: solo con fixtures, NINGUNA petición real."""
import asyncio

import pytest

from matched import es_odds_reader as r

# Forma real de /odds/multi (odds-api.io v3): bookmakers = {nombre: [{name, odds:[{...}]}]}
_ITEMS = [
    {
        "id": 101, "home": "Real Betis", "away": "Sevilla FC", "date": "2099-01-01T20:00:00Z",
        "league": {"name": "Spain - LaLiga", "slug": "spain-laliga"},
        "bookmakers": {
            "Bwin ES": [{"name": "ML", "odds": [{"home": "2.50", "draw": "3.30", "away": "3.00"}]}],
            "Codere ES": [{"name": "ML", "odds": [{"home": "2.40", "draw": "3.40", "away": "3.10"}]},
                          {"name": "Totals", "odds": [{"hdp": 2.5, "over": "1.9", "under": "1.9"}]}],
            "Unibet": [{"name": "ML", "odds": [{"home": "2.60", "draw": "3.60", "away": "3.40"}]}],
        },
    },
    {   # tenis: sin empate
        "id": "t9", "home": "Alcaraz C.", "away": "Sinner J.", "date": "2099-01-02T12:00:00Z",
        "league": {"slug": "atp-finals"},
        "bookmakers": {
            "Bwin ES": [{"name": "ML", "odds": [{"home": "1.90", "away": "1.95"}]}],
            "Codere ES": [{"name": "ML", "odds": [{"home": "1.85", "away": "2.05"}]}],
        },
    },
    {"id": 7, "home": "A", "away": "B", "bookmakers": {"Pinnacle": [{"name": "ML", "odds": [{"home": "2"}]}]}},
]


def test_seleccion_de_casas(monkeypatch):
    assert r.selected_bookmakers("bwin_es, Codere ES") == ["Bwin ES", "Codere ES"]
    # no ES, de pago o sin API → fuera; tope de 2
    assert r.selected_bookmakers("Unibet,bet365_es,sportium,bwin_es,codere_es,betway_es") == \
        ["Bwin ES", "Codere ES"]
    monkeypatch.setenv("ODDSAPIIO_ES_BOOKMAKERS", "winamax_es")
    assert r.selected_bookmakers() == ["Winamax ES"]
    monkeypatch.delenv("ODDSAPIIO_ES_BOOKMAKERS")
    assert r.selected_bookmakers() == []


def test_parametros_por_lotes_de_10():
    params = r.build_odds_params([str(i) for i in range(23)], ["Bwin ES", "Codere ES"])
    assert len(params) == 3
    assert params[0]["eventIds"].count(",") == 9
    assert params[0]["bookmakers"] == "Bwin ES,Codere ES" and params[0]["markets"] == "ML"


def test_parse_solo_casas_es():
    evs = r.parse_es_odds(_ITEMS)
    assert [e["event_id"] for e in evs] == ["101", "t9"]     # el de solo Pinnacle se descarta
    betis = evs[0]
    assert set(betis["prices"]) == {"bwin_es", "codere_es"}
    assert betis["prices"]["bwin_es"] == {"1": 2.5, "X": 3.3, "2": 3.0}
    assert betis["ignored"] == ["Unibet"]
    assert betis["outcomes"] == ["1", "X", "2"] and betis["league"] == "spain-laliga"
    assert evs[1]["outcomes"] == ["1", "2"]


def test_mejor_cobertura_nunca_en_la_casa_del_bono():
    betis = r.parse_es_odds(_ITEMS)[0]
    res = r.best_cover(betis, "bwin_es", "1", "freebet_snr", 10)
    # bono 1 @2.50 en Bwin ES; X y 2 solo pueden ir a Codere ES (3.40 y 3.10)
    assert [(l["outcome"], l["bookmaker"], l["odds"]) for l in res["legs"]] == \
        [("X", "codere_es", 3.4), ("2", "codere_es", 3.1)]
    assert res["profit_exact"] == pytest.approx(15 * (1 - 1 / 3.4 - 1 / 3.1), abs=1e-4)
    assert res["event"] == "Real Betis vs Sevilla FC"
    # casa del bono sin cuotas en el evento → None
    assert r.best_cover(betis, "betway_es", "1", "freebet_snr", 10) is None


def test_mejor_cobertura_tenis_dos_resultados():
    tenis = r.parse_es_odds(_ITEMS)[1]
    res = r.best_cover(tenis, "codere_es", "2", "qualifying", 10)
    assert res["legs"][0]["bookmaker"] == "bwin_es" and res["legs"][0]["outcome"] == "1"


def test_desactivado_no_hace_peticiones(monkeypatch):
    calls = []

    async def getter(path, params):
        calls.append((path, params))
        return _ITEMS

    monkeypatch.delenv("MATCHED_ES_READER_ENABLED", raising=False)
    assert asyncio.run(r.fetch_es_odds(["101"], getter=getter, bookmakers=["bwin_es"])) == []
    # activado pero sin casas ES elegidas → tampoco
    assert asyncio.run(r.fetch_es_odds(["101"], getter=getter, enabled=True, bookmakers=["Unibet"])) == []
    assert calls == []


def test_activado_con_getter_simulado():
    calls = []

    async def getter(path, params):
        calls.append((path, params))
        return _ITEMS

    evs = asyncio.run(r.fetch_es_odds(["101", "t9", "7"], getter=getter, enabled=True,
                                      bookmakers=["bwin_es", "codere_es"]))
    assert calls == [("/odds/multi", {"eventIds": "101,t9,7", "bookmakers": "Bwin ES,Codere ES",
                                      "markets": "ML"})]
    assert len(evs) == 2


def test_sin_respuesta_corta_los_lotes():
    calls = []

    async def getter(path, params):
        calls.append(params)
        return None

    evs = asyncio.run(r.fetch_es_odds([str(i) for i in range(25)], getter=getter, enabled=True,
                                      bookmakers=["bwin_es"]))
    assert evs == [] and len(calls) == 1
