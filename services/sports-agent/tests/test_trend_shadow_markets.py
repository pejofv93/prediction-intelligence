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
    assert counts_in_feed({"market": "btts", "shadow": False})   # sombra desde el 23-nov: lo enviado sigue contando
    assert not counts_in_feed({"market": "btts", "shadow": True})
    assert not counts_in_feed({"market": "team_goals_over", "shadow": True})
    assert counts_in_feed({"market": "team_goals_over", "shadow": False})


def test_lineas_aparte_con_objetivo():
    rows = ([{"market": "exact_total", "result": "hit"}] * 6 + [{"market": "exact_total", "result": "miss"}] * 30
            + [{"market": "exact_total", "result": "hit", "shadow": True}])
    lines = shadow_market_lines(rows, [{"market": "exact_total"}] * 13)
    assert lines[-2] == "Total exacto (obj. 28%): en sombra 1/1 (100%) · enviadas antes 6/36 (17%) · 13 pend."
    assert lines[-1] == "Margen de victoria (obj. 40%): en sombra — · enviadas antes — · 0 pend."


def test_diario_no_lo_cuenta():
    rows = [{"market": "team_goals_over", "result": "hit"}, {"market": "exact_total", "result": "miss"}]
    feed = [r for r in rows if counts_in_feed(r)]
    text = build_daily_summary(feed, [], [], {}, None, datetime(2026, 10, 26, tzinfo=timezone.utc),
                               shadow_market_lines(rows, []))
    assert "🎯 Acierto <b>1/1 (100%)</b>" in text
    assert "Total exacto (obj. 28%): en sombra — · enviadas antes 0/1 (0%) · 0 pend." in text


def test_duplicadas_fuera_de_todo():
    from analyzers.trend_weekly import not_duplicate
    rows = [{"market": "handicap", "result": "hit"},
            {"market": "handicap", "result": "hit", "duplicate_of": "575352_handicap"},
            {"market": "handicap", "result": "miss", "shadow": True, "rule_id": "handicap.H5",
             "duplicate_of": "x"}]
    kept = [r for r in rows if not_duplicate(r)]
    assert kept == [rows[0]]
    text = build_daily_summary([r for r in kept if counts_in_feed(r)], [], [], {}, None,
                               datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert "🎯 Acierto <b>1/1 (100%)</b>" in text


def test_diario_html_resumen_mercados_y_plegado():
    from analyzers.trend_daily import build_daily_summary as b
    hits = [{"market": "double_chance", "result": "hit"}] * 40 + [{"market": "double_chance", "result": "miss"}] * 10
    low = [{"market": "handicap", "result": "hit"}] * 30 + [{"market": "handicap", "result": "miss"}] * 25
    thin = [{"market": "corners", "result": "hit"}] * 3
    cal = {"double_chance": {"status": "at_fixed"}, "handicap": {"status": "below_target"}}
    text = b(hits + low + thin, hits[:2], [{"market": "handicap"}], cal, {"sent": 4},
             datetime(2026, 11, 20, tzinfo=timezone.utc), ["", "En sombra (se gradúa)", "Total exacto 1/2"], {})
    first, second, third = text.splitlines()[:3]
    assert first == "<b>📈 Señales de tendencia · Viernes 20 nov</b>"
    assert "Enviados ayer: 4" in second and "Pendientes: 1" in second
    assert "✅ <b>Doble oportunidad</b> 40/50 (80%)" in text and "ayer 2/2" in text
    assert "⚠️ <b>Hándicap</b> 30/55 (55%)" in text and "1 pend." in text
    folded = text[text.index("<blockquote expandable>"):text.index("</blockquote>")]
    assert "Muestra pequeña" in folded and "Córners 3/3" in folded and "En sombra" in folded
    assert "&lt;" in folded and "RESUMEN DEL DÍA" not in text


def test_linea_descartados_por_ya_enviados():
    from analyzers.trend_daily import _sends_detail
    assert _sends_detail({"sent": 0, "skipped_already_sent": 0}) == "Descartados por ya enviados: 0"
    assert _sends_detail({"skipped_already_sent": 3, "new_market_repeats": 2}) == \
        "Descartados por ya enviados: 3 · repetidos por mercado nuevo: 2"
    assert _sends_detail(None) is None


def test_graduadas_ayer_cuenta_tambien_las_de_sombra_aparte():
    from analyzers.trend_daily import build_daily_summary as b
    text = b([], [], [], {}, {"sent": 0}, datetime(2026, 11, 21, tzinfo=timezone.utc), day_shadow=8)
    assert "Graduadas ayer: 0 (0 aciertos · 0 fallos · 0 anuladas) · en sombra: 8" in text
    assert "en sombra:" not in b([], [], [], {}, None, datetime(2026, 11, 21, tzinfo=timezone.utc))


def test_ambos_marcan_en_sombra_sin_duplicar_su_v1(monkeypatch):
    """Ambos marcan (23-nov): no se envía; su v1 ya va a la sombra como variante, así que el
    candidato normal no se copia otra vez (mismo rule_id, contaría dos veces)."""
    monkeypatch.setattr(trend_finder, "_read_team_stats", lambda _id: {})
    monkeypatch.setattr(trend_finder, "_read_corner_stats", lambda *_: {})
    btts = {**_cand("btts"), "pattern_type": "series", "rate": 0.8}
    monkeypatch.setattr(trend_finder, "_model_candidates", lambda e: [btts, _cand("dnb")])
    monkeypatch.setattr(trend_variants, "active_rules", lambda: {})
    monkeypatch.setattr(trend_variants, "variant_candidates", lambda *a: [
        {**btts, "rule_id": "btts.v1"}, {**btts, "rule_id": "btts.B6"}])
    shadow = []
    enriched = {"match_id": "m1", "league": "PD", "home_team": "A", "away_team": "B",
                "match_date": "2026-11-24"}
    out = asyncio.run(trend_finder._fixture_candidates(enriched, shadow))
    assert [c["market"] for c in out] == ["dnb"]
    assert sorted(c["rule_id"] for c in shadow) == ["btts.B6", "btts.v1"]


def test_sombra_de_ambos_marcan_solo_su_v1():
    rows = ([{"market": "btts", "result": "hit"}] * 3 + [{"market": "btts", "result": "miss"}]
            + [{"market": "btts", "result": "hit", "shadow": True, "rule_id": "btts.v1"}] * 2
            + [{"market": "btts", "result": "miss", "shadow": True, "rule_id": "btts.B6"}] * 5)
    line = [x for x in shadow_market_lines(rows, []) if x.startswith("Ambos marcan")][0]
    assert line == "Ambos marcan (obj. 70%): en sombra 2/2 (100%) · enviadas antes 3/4 (75%) · 0 pend."


def test_etiqueta_de_variante_dice_por_que_no_decide():
    from analyzers.trend_weekly import _decision_state
    base = {"target": 0.70, "status": "testing", "v1_n": 127, "v1_rate": 0.63}
    assert _decision_state({**base, "n": 12, "rate": 0.8}, 30) == "12/30 para decidir"
    assert _decision_state({**base, "n": 59, "rate": 40 / 59}, 30) == "59 graduadas (mín. 30) · por debajo del 70%"
    assert _decision_state({**base, "n": 40, "rate": 0.72, "v1_rate": 0.75}, 30) ==         "40 graduadas (mín. 30) · no mejora a v1"
    assert _decision_state({**base, "n": 40, "rate": 0.8, "v1_n": 20}, 30) == "40 graduadas · v1 en sombra 20/30"
    assert _decision_state({**base, "n": 40, "rate": 0.8, "status": "ready"}, 30) == "40 graduadas · cumple"
