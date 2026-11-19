"""Alertas de valor de Sports: casa sin licencia en España → solo referencia, sin Kelly."""
import alert_manager

_PRED = {"sport": "football", "league": "PD", "home_team": "Betis", "away_team": "Sevilla",
         "market_type": "h2h", "selection": "Betis", "odds": 2.4, "edge": 0.08, "ev": 0.09,
         "confidence": 0.7, "kelly_fraction": 0.03, "signals": {"poisson": 0.5}}


def test_sin_casa_en_la_senal_sale_como_siempre():
    text = alert_manager._format_alert_unified(dict(_PRED))
    assert "Casa:" not in text
    assert "🧮 Kelly: 3.0% del bankroll" in text
    assert "solo referencia" not in text


def test_casa_sin_licencia_solo_referencia_sin_kelly():
    text = alert_manager._format_alert_unified(dict(_PRED, bookmaker="unibet"))
    assert "🏦 Casa: unibet — ⚠️ solo referencia: casa sin licencia en España" in text
    assert "Kelly" not in text
    assert "Sin stake sugerido: cuota de referencia, no apostable desde España" in text


def test_marca_generica_con_version_es():
    text = alert_manager._format_alert_unified(dict(_PRED, bookmaker="bet365"))
    assert "solo referencia" in text
    assert "Existe Bet365 ES" in text


def test_pinnacle_sin_version_es():
    text = alert_manager._format_alert_unified(dict(_PRED, bookmaker="pinnacle"))
    assert "solo referencia" in text and "Existe" not in text


def test_casa_es_se_muestra_y_mantiene_kelly():
    text = alert_manager._format_alert_unified(dict(_PRED, bookmaker="bwin_es"))
    assert "🏦 Casa: Bwin ES" in text
    assert "solo referencia" not in text
    assert "🧮 Kelly: 3.0% del bankroll" in text
