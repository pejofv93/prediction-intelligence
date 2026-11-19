"""Mínimo de EV por liga (19-nov-2026): max(config, Firestore), calculado tras rellenar la liga."""
import inspect

from analyzers import value_bet_engine as vbe


def test_merge_no_baja_nunca_la_config():
    base = {"SA": 0.096, "PL": 0.072}
    out = vbe.merge_backtest_thresholds(base, {"Serie A": {"h2h": 0.08}, "Premier League": {"h2h": 0.08}})
    assert out == {"SA": 0.096, "PL": 0.08}
    assert vbe.merge_backtest_thresholds(base, None) == base
    # liga del doc sin entrada en la config → max con SPORTS_MIN_EDGE
    out = vbe.merge_backtest_thresholds({}, {"Ligue 1": {"h2h": 0.05}})
    assert out == {"FL1": vbe.SPORTS_MIN_EDGE}


def test_minimo_se_calcula_despues_de_rellenar_la_liga():
    src = inspect.getsource(vbe.generate_signal)
    fill = src.index('league = league or um.get("league", "")')
    first = src.index("_min_edge = _load_backtest_thresholds()")
    assert first > fill


def test_min_ev_va_en_la_prediccion_enviada():
    src = inspect.getsource(vbe.generate_signal)
    assert src.count('"min_ev": round(float(_min_edge), 4)') >= 2      # 1X2 y totales
