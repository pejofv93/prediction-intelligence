"""Informe diario: saldo en unidades y cuadrado con los movimientos de Polymarket (como el semanal)."""
from shared.model_health import format_daily_report
from shared.shadow_engine import calculate_metrics


def _m(**kw):
    base = {"current_bankroll": -316.49, "closed_trades": 1800, "pnl_sports": -77.25, "pnl_poly": -233.08,
            "pnl_poly_movement": -56.16, "n_poly_movement": 80, "n_poly": 878, "n_poly_alerted": 941,
            "markets_poly_alerted": 704, "pending_poly_alerted": 12}
    return {**base, **kw}


def test_saldo_en_u_con_movimientos():
    txt = format_daily_report({}, _m())
    assert "💰 Bankroll virtual: -316.49u (50u iniciales -366.49u P&L)" in txt
    assert "⚽ Sports: -77.25u | 🔮 Polymarket: -233.08u | 📉 Movimientos Polymarket: -56.16u" in txt
    assert "878 operaciones virtuales cerradas" in txt
    assert "941 envíos graduados de 704 mercados, 12 pendientes" in txt
    assert "€" not in txt
    assert "Movimientos" not in format_daily_report({}, _m(n_poly_movement=0))


def test_metricas_separan_los_movimientos():
    trades = [{"source": "sports", "result": "win", "pnl_virtual": 2.0, "virtual_stake": 1.0},
              {"source": "polymarket_movement", "result": "loss", "pnl_virtual": -1.5, "virtual_stake": 1.5}]
    import shared.shadow_engine as se
    orig = se.poly_alerted_metrics
    se.poly_alerted_metrics = lambda *a, **k: {"roi": 0.0, "win_rate": 0.0, "n": 0, "wins": 0, "pnl": 0.0,
                                               "pending": 0, "markets": 0, "by_direction": {}}
    try:
        m = calculate_metrics(trades)
    finally:
        se.poly_alerted_metrics = orig
    assert m["pnl_poly_movement"] == -1.5 and m["n_poly_movement"] == 1
    assert m["current_bankroll"] == 50.5


def test_nombres_que_no_se_confunden():
    pred = {"total": 492, "pending": 10, "resolved": 482, "correct": 200, "incorrect": 282,
            "synthetic": 0, "real_odds": 492}
    txt = format_daily_report({}, _m(n_sports=966), pred_stats=pred)
    assert "📋 Predicciones Sports recientes (últimas 500): 492" in txt
    assert "966 operaciones virtuales cerradas" in txt and "Señales sports" not in txt
