"""Semanal de tendencias en HTML, con el formato del diario (23-nov-2026): título
«📈 Señales de tendencia», mercados con muestra suficiente a la vista y lo secundario
(por tipo, muestra pequeña, pruebas, sombra, puntuación) en un plegable."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from analyzers.trend_weekly import _zero_days_line, build_weekly_summary  # noqa: E402


def _rows(market, hits, misses, ptype="series", graded_at="2026-11-10T08:00:00"):
    return ([{"market": market, "result": "hit", "pattern_type": ptype, "graded_at": graded_at}] * hits
            + [{"market": market, "result": "miss", "pattern_type": ptype, "graded_at": graded_at}] * misses)


def _build(**kw):
    log = _rows("handicap", 99, 67) + _rows("team_goals_over", 30, 10) + _rows("corners", 3, 1, "rolling")
    week = _rows("handicap", 8, 7, graded_at="2026-11-20T08:00:00")
    variants = [{"market": "team_goals_over", "variant": "G5", "desc": "equipo 2+ >=70% y rival encaja 2+ >=50% (10)",
                 "target": 0.70, "hits": 24, "n": 30, "rate": 0.8, "v1_hits": 40, "v1_n": 60, "v1_rate": 0.67,
                 "status": "ready"}]
    runs = {f"2026-11-{d}": {"sent": 5} for d in (16, 18, 19, 20, 21, 22)}
    return build_weekly_summary(log, log[:0] + week, [{"market": "handicap"}], {"handicap": {"status": "below_target"}},
                                datetime(2026, 11, 16, tzinfo=timezone.utc), datetime(2026, 11, 22, tzinfo=timezone.utc),
                                variants, runs, ["En sombra (se gradúa, no se envía ni cuenta en el acierto)",
                                                 "Total exacto (obj. 28%): en sombra 3/10 (30%)"], {}, **kw)


def test_titulo_y_partes_visibles():
    text = _build()
    lines = text.split("\n")
    assert lines[0] == "<b>📈 Señales de tendencia · semana 16–22 nov</b>"
    assert lines[1] == "🎯 Semana <b>8/15 (53%)</b> · Total 132/210 (63%) · ⏳ Pendientes: 1"
    assert "1 sin ejecución registrada (17 nov)" in lines[2]
    assert "⚠️ <b>Hándicap</b> 99/166 (60%) · obj. 55% · semana 8/15 · 1 pend." in text
    assert "✅ <b>Goles 2+ del equipo</b> 30/40 (75%) · obj. 70%" in text
    assert "🟢 <b>LISTA</b>" in text.split("<blockquote expandable>")[0]


def test_lo_secundario_va_plegado():
    text = _build()
    visible, fold = text.split("<blockquote expandable>")
    for part in ("Por tipo", "Muestra pequeña", "Córners 3/4", "Probando", "G5: 24/30 (80%) · v1 en sombra 40/60 (67%) · 30 graduadas · cumple",
                 "En sombra", "Por puntuación del partido"):
        assert part in fold and part not in visible, part
    assert fold.rstrip().endswith("<i>Tendencia estadística, no consejo de inversión.</i>")
    assert "&lt;30 graduadas" in fold


def test_dias_sin_ejecucion_con_fecha():
    runs = {"2026-11-16": {"sent": 0}, "2026-11-18": {"sent": 3}}
    line = _zero_days_line(runs, datetime(2026, 11, 16), datetime(2026, 11, 18))
    assert line == "Días sin mensajes: 1 de 2 · 1 sin ejecución registrada (17 nov)"


def test_ambos_marcan_enviadas_siguen_en_el_total():
    """23-nov: Ambos marcan en sombra, pero sus 166 enviadas siguen en el acierto total; no sale
    en la lista de mercados, sino en el plegable «En sombra» con «enviadas antes»."""
    from analyzers.trend_weekly import counts_in_feed, shadow_market_lines
    sent = _rows("btts", 99, 67)
    new_shadow = [{"market": "btts", "result": "hit", "shadow": True, "rule_id": "btts.v1"}] * 3
    other = _rows("team_goals_over", 30, 10)
    all_rows = sent + new_shadow + other
    feed = [r for r in all_rows if counts_in_feed(r)]
    text = build_weekly_summary(feed, [], [], {}, datetime(2026, 11, 23, tzinfo=timezone.utc),
                                datetime(2026, 11, 29, tzinfo=timezone.utc), None, {},
                                shadow_market_lines(all_rows, []), None)
    visible, fold = text.split("<blockquote expandable>")
    assert "Total 129/206 (63%)" in visible
    assert "Ambos marcan" not in visible
    assert "Ambos marcan (obj. 70%): en sombra 3/3 (100%) · enviadas antes 99/166 (60%) · 0 pend." in fold
