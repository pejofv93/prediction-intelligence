"""Fuentes de props de jugadores (26-nov): understat apagado y goleadores de football-data con
caché diaria por liga, reintento tras 429 y código HTTP en el aviso."""
import asyncio
import io
import json
import logging
import urllib.error

import pytest

from analyzers import player_props as pp

SCORERS = {"scorers": [{"player": {"name": "Ana Pérez"}, "team": {"shortName": "X"},
                        "goals": 12, "playedMatches": 12, "assists": 3}]}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pp._FDORG_CACHE.clear()
    monkeypatch.setattr(pp, "FOOTBALL_API_KEY", "k")
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(pp.asyncio, "sleep", fake_sleep)
    return sleeps


def _urlopen_seq(monkeypatch, seq):
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(json.dumps(item).encode())
    monkeypatch.setattr(pp.urllib.request, "urlopen", fake)
    return calls


def test_una_peticion_por_liga_y_dia(monkeypatch):
    calls = _urlopen_seq(monkeypatch, [SCORERS])
    a = asyncio.run(pp._fetch_fdorg_scorers("PL"))
    b = asyncio.run(pp._fetch_fdorg_scorers("PL"))
    assert len(calls) == 1 and a == b and len(a) == 1


def test_429_espera_lo_que_pide_y_reintenta_una_vez(monkeypatch, _clean, caplog):
    calls = _urlopen_seq(monkeypatch, [_http_error(429, {"X-RequestCounter-Reset": "23"}), SCORERS])
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(pp._fetch_fdorg_scorers("SA"))
    assert len(calls) == 2 and len(out) == 1 and _clean == [23.0]
    assert "HTTP 429" in caplog.text


def test_fallo_deja_el_codigo_y_no_se_repite_en_la_pasada(monkeypatch, caplog):
    calls = _urlopen_seq(monkeypatch, [_http_error(403)])
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(pp._fetch_fdorg_scorers("BL1")) == {}
        assert asyncio.run(pp._fetch_fdorg_scorers("BL1")) == {}
    assert len(calls) == 1
    assert "fetch falló HTTP 403" in caplog.text


def test_espera_acotada():
    assert pp._retry_wait({"Retry-After": "500"}) == pp._FDORG_MAX_WAIT
    assert pp._retry_wait({}) == 60.0


def test_understat_apagado_no_se_llama(monkeypatch):
    assert pp._UNDERSTAT_ENABLED is False
    called = []

    async def boom(league):
        called.append(league)
        return {}
    monkeypatch.setattr(pp, "_fetch_understat_players", boom)
    monkeypatch.setattr(pp, "_fetch_fdorg_scorers", boom)
    import sys
    import types
    fake_fs = types.ModuleType("shared.firestore_client")
    fake_fs.col = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "shared.firestore_client", fake_fs)
    out = asyncio.run(pp.generate_player_props_signals(
        {"match_id": "1", "home_team": "A", "away_team": "B", "league": "PL"}))
    assert out == [] and called == ["PL"]   # solo football-data
