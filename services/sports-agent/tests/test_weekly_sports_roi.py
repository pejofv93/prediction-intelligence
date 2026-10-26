"""Cabecera del semanal de Sports: ROI de la semana (shadow_trades cerrados en ella), acumulado etiquetado."""
from datetime import datetime, timezone

from shared.report_generator import generate_weekly_report
from shared.shadow_engine import period_metrics

W0 = datetime(2026, 10, 19, tzinfo=timezone.utc)
W1 = datetime(2026, 10, 26, tzinfo=timezone.utc)


def test_period_metrics_solo_la_semana_y_la_fuente():
    trades = [
        {"source": "sports", "result": "win", "pnl_virtual": 1.5, "virtual_stake": 1.0,
         "closed_at": datetime(2026, 10, 20, tzinfo=timezone.utc)},
        {"source": "sports", "result": "loss", "pnl_virtual": -1.0, "virtual_stake": 1.0,
         "closed_at": "2026-10-25T23:59:00+00:00"},
        {"source": "sports", "result": "loss", "pnl_virtual": -1.0, "virtual_stake": 1.0,
         "closed_at": W1},  # lunes 00:00: semana siguiente
        {"source": "sports", "result": "pending", "closed_at": datetime(2026, 10, 21, tzinfo=timezone.utc)},
        {"source": "polymarket", "result": "loss", "pnl_virtual": -2.0, "virtual_stake": 2.0,
         "closed_at": datetime(2026, 10, 21, tzinfo=timezone.utc)},
    ]
    m = period_metrics(trades, "sports", W0, W1)
    assert m["n"] == 2 and m["wins"] == 1
    assert m["roi"] == 0.25 and m["win_rate"] == 0.5


def _report(stats):
    return generate_weekly_report({"week": "2026-W43", **stats}, {}, {})


def test_cabecera_con_roi_de_la_semana_y_acumulado_etiquetado():
    txt = _report({"accuracy": 0.2879, "roi_sports_week": -0.2267, "n_sports_week": 66,
                   "roi_sports": -0.0202, "closed_trades": 571, "roi_total": 0.05})
    assert "Win rate: 29% | ROI semana: -22.7% (66 cerradas)" in txt
    assert "⚽ Sports (acumulado): -2.0%" in txt
    assert "ROI acumulado total: +5.0%" in txt


def test_cabecera_sin_cerradas_en_la_semana():
    assert "ROI semana: — (0 cerradas)" in _report({"accuracy": 0.0})
