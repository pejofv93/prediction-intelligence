"""Total exacto y Margen de victoria en sombra: se generan y gradúan, no se envían ni cuentan."""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

from analyzers import trend_finder, trend_variants  # noqa: E402
from analyzers.trend_daily import build_daily_summary  # noqa: E402
from analyzers.trend_weekly import counts_in_feed, shadow_market_lines  # noqa: E402


def _cand(market):
    return {"market": market, "pattern_type": "model", "team": "A/B", "team_id": None,
            "threshold": 2, "model_prob": 0.3, "sample": None, "rate": 1.2,
            "label": market, "detail": "", "line": market, "score": 1.0}


def test_exact_total_va_a_sombra(monkeypatch):
    monkeypatch.setattr(trend_finder, "_read_team_stats", lambda _id: {})
    monkeypatch.setattr(trend_finder, "_read_corner_stats", lambda *_: {})
    monkeypatch.setattr(trend_finder, "_model_candidates", lambda e: [_cand("exact_total"), _cand("dnb"), _cand("win_margin")])
    monkeypatch.setattr(trend_variants, "active_rules", lambda: {})
    monkeypatch.setattr(trend_variants, "variant_candidates", lambda *a: [])
    shadow = []
    enriched = {"match_id": "m1", "league": "PD", "home_team": "A", "away_team": "B",
                "match_date": "2026-10-26"}
    out = asyncio.run(trend_finder._fixture_candidates(enriched, shadow))
    assert [c["market"] for c in out] == ["dnb"]
    assert [(c["market"], c["rule_id"], c["match_id"]) for c in shadow] == [
        ("exact_total", "exact_total.v1", "m1"), ("win_margin", "win_margin.v1", "m1")]


def test_fuera_del_acierto_incluso_lo_enviado_antes():
    assert not counts_in_feed({"market": "exact_total", "shadow": False})
    assert not counts_in_feed({"market": "win_margin", "shadow": False})
    assert not counts_in_feed({"market": "btts", "shadow": True})
    assert counts_in_feed({"market": "btts", "shadow": False})


def test_lineas_aparte_con_objetivo():
    rows = ([{"market": "exact_total", "result": "hit"}] * 6 + [{"market": "exact_total", "result": "miss"}] * 30
            + [{"market": "exact_total", "result": "hit", "shadow": True}])
    lines = shadow_market_lines(rows, [{"market": "exact_total"}] * 13)
    assert lines[-2] == "Total exacto (obj. 28%): en sombra 1/1 (100%) · enviadas antes 6/36 (17%) · 13 pend."
    assert lines[-1] == "Margen de victoria (obj. 40%): en sombra — · enviadas antes — · 0 pend."


def test_diario_no_lo_cuenta():
    rows = [{"market": "btts", "result": "hit"}, {"market": "exact_total", "result": "miss"}]
    feed = [r for r in rows if counts_in_feed(r)]
    text = build_daily_summary(feed, [], [], {}, None, datetime(2026, 10, 26, tzinfo=timezone.utc),
                               shadow_market_lines(rows, []))
    assert "Acierto total: 1/1 (100%)" in text
    assert "Total exacto (obj. 28%): en sombra — · enviadas antes 0/1 (0%) · 0 pend." in text
