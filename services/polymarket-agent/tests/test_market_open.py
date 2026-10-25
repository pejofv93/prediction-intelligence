"""
Mercados cerrados: market_closed_reason (estado real de Gamma) y el corte en
analyze_market antes de gastar LLM. Caso real: gpt-oss alertó el 61% de las veces
sobre mercados ya cerrados (partidos terminados, endDate días después).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scanner import _parse_gamma_ts, market_closed_reason

_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
_OPEN = {"closed": False, "active": True, "acceptingOrders": True, "endDate": "2026-10-31T23:55:00Z"}


def test_open_market_has_no_reason():
    assert market_closed_reason(dict(_OPEN), _NOW) is None


def test_closed_flag_even_if_end_date_in_future():
    # World Series: eliminado → closed=True con endDate a 31-oct.
    raw = dict(_OPEN, closed=True, acceptingOrders=False, closedTime="2026-10-11 05:55:09+00",
               umaResolutionStatus="resolved")
    assert market_closed_reason(raw, _NOW) == "closed"


def test_not_accepting_orders():
    assert market_closed_reason(dict(_OPEN, acceptingOrders=False), _NOW) == "not_accepting_orders"


def test_uma_proposed_counts_as_not_open():
    assert market_closed_reason(dict(_OPEN, umaResolutionStatus="proposed"), _NOW) == "resolution_proposed"


def test_game_started_is_not_open():
    raw = dict(_OPEN, gameStartTime="2026-10-15 11:30:00+00")
    assert market_closed_reason(raw, _NOW) == "event_started"


def test_game_in_future_is_open():
    raw = dict(_OPEN, gameStartTime="2026-10-15 18:00:00+00")
    assert market_closed_reason(raw, _NOW) is None


def test_missing_accepting_orders_is_not_closed():
    raw = {"closed": False, "active": True, "endDate": "2026-10-31T23:55:00Z"}
    assert market_closed_reason(raw, _NOW) is None


def test_parse_gamma_ts_formats():
    assert _parse_gamma_ts("2026-10-11 13:30:00+00") == datetime(2026, 10, 11, 13, 30, tzinfo=timezone.utc)
    assert _parse_gamma_ts("2026-10-26T03:59:00Z") == datetime(2026, 10, 26, 3, 59, tzinfo=timezone.utc)
    assert _parse_gamma_ts(None) is None
    assert _parse_gamma_ts("basura") is None


async def test_analyze_market_skips_closed_market_before_llm(monkeypatch):
    import scanner
    from tests.test_groq_analyzer import _col_mock, _enriched, _market_doc

    async def _closed(market_id):
        return {"open": False, "reason": "closed", "closed_time": "2026-10-11", "game_start_time": None}

    monkeypatch.setattr(scanner, "fetch_market_state", _closed)
    saved: list[dict] = []
    with (
        patch("shared.firestore_client.col", side_effect=_col_mock(_market_doc("Will the peace treaty be signed by end of 2026?"), saved)),
        patch("shared.groq_client._get_groq") as groq,
        patch("groq_analyzer._is_groq_quota_exhausted", return_value=False),
    ):
        from groq_analyzer import analyze_market
        result = await analyze_market(_enriched())
    assert result is None
    groq.assert_not_called()


async def test_analyze_market_skips_when_gamma_unavailable(monkeypatch):
    import scanner
    from tests.test_groq_analyzer import _col_mock, _enriched, _market_doc

    async def _down(market_id):
        return None

    monkeypatch.setattr(scanner, "fetch_market_state", _down)
    with (
        patch("shared.firestore_client.col", side_effect=_col_mock(_market_doc("Will the peace treaty be signed by end of 2026?"), [])),
        patch("shared.groq_client._get_groq") as groq,
        patch("groq_analyzer._is_groq_quota_exhausted", return_value=False),
    ):
        from groq_analyzer import analyze_market
        assert await analyze_market(_enriched()) is None
    groq.assert_not_called()


async def test_gamma_streak_alerts_once_and_recovers(monkeypatch):
    """3 ciclos caídos seguidos → un aviso; el 4º no repite; al volver, aviso de recuperación."""
    import scanner
    store: dict = {}

    class _Ref:
        def get(self):
            m = type("S", (), {})()
            m.exists = bool(store)
            m.to_dict = lambda: dict(store)
            return m

        def set(self, data, merge=False):
            store.update(data)

    monkeypatch.setattr(scanner, "col", lambda name: type("C", (), {"document": lambda self, d: _Ref()})())
    sent: list[str] = []

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent.append(json["data"]["text"])

    monkeypatch.setattr(scanner.httpx, "AsyncClient", _Client)
    import shared.config as cfg
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_URL", "http://bot", raising=False)

    for i in range(4):
        scanner.note_gamma_call(False)
        r = await scanner.record_gamma_cycle("analyze")
        assert r["streak"] == i + 1
    assert len(sent) == 1 and "no responde" in sent[0]

    scanner.note_gamma_call(True)
    r = await scanner.record_gamma_cycle("analyze")
    assert r["streak"] == 0 and len(sent) == 2 and "vuelve" in sent[1]


async def test_gamma_cycle_without_calls_is_noop():
    import scanner
    scanner.reset_gamma_calls()
    assert (await scanner.record_gamma_cycle("analyze"))["streak"] is None
