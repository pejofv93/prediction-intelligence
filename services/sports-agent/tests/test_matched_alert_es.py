"""Alertas matched: casa o exchange sin licencia en España → solo referencia, sin stakes."""
from matched.alerts import alert_cut_reason, format_alert
from matched.models import MatchedSignal


def _sig(back="pinnacle", lay="betfair_ex_eu", **kw):
    base = dict(
        signal_id="s1", signal_type="surebet", sport_key="soccer_epl", event_id="e1",
        commence_time="2099-01-01T20:00:00Z", home_team="Arsenal", away_team="Chelsea",
        selection="Arsenal", back_bookmaker=back, back_odds=2.10, lay_bookmaker=lay,
        lay_odds=2.06, commission=0.02, confidence="high", lay_age_seconds=30,
        back_age_seconds=30, qualifying_rating=1.5, freebet_snr_rating=70.0,
        lay_stake_per_100=101.0, liability_per_100=107.0, profit_per_100=1.5,
        detected_at="2099-01-01T10:00:00Z", expires_at="2099-01-01T20:00:00Z",
    )
    base.update(kw)
    return MatchedSignal(**base)


def test_casa_y_exchange_sin_licencia_solo_referencia():
    text = format_alert(_sig())
    assert text.startswith("ℹ️ SOLO REFERENCIA (surebet +1.50%) — no apostable desde España")
    assert "casa sin licencia en España (pinnacle)" in text
    assert "exchange sin licencia en España" in text and "(betfair_ex_eu)" in text
    assert "Sin instrucciones de apuesta" in text
    # nada que se lea como instrucción: ni BACK/LAY con stake ni beneficio garantizado
    assert "→" not in text and "BACK " not in text and "LAY " not in text
    assert "garantizado" not in text and "€" not in text


def test_marca_con_version_es_se_nombra():
    text = format_alert(_sig(back="williamhill", signal_type="coverage", qualifying_rating=-0.8))
    assert "SOLO REFERENCIA (cobertura, peaje -0.80%)" in text
    assert "Existe William Hill ES (williamhill.es)" in text


def test_solo_el_lay_sin_licencia_tambien_es_referencia():
    text = format_alert(_sig(back="bwin_es"))
    assert "SOLO REFERENCIA" in text
    assert "casa sin licencia" not in text
    assert "exchange sin licencia en España" in text


def test_casa_es_y_betfair_es_formato_apostable():
    text = format_alert(_sig(back="Bwin ES", lay="betfair_es_exchange"))
    assert "SOLO REFERENCIA" not in text
    assert text.startswith("🟢 SUREBET +1.50%")
    assert "BACK Bwin ES @2.10" in text


def test_solo_referencia_se_guarda_sin_enviar():
    # 28-nov: sin licencia en España (back o lay) → no se envía; con casa ES y betfair.es, sí
    assert alert_cut_reason(_sig(back="bwin_es", lay="betfair_es_exchange")) is None
    assert alert_cut_reason(_sig()) == "reference_only"
    assert alert_cut_reason(_sig(back="bwin_es")) == "reference_only"                  # lay .com
    assert alert_cut_reason(_sig(lay="betfair_es_exchange")) == "reference_only"       # back sin licencia
    cov = dict(signal_type="coverage", qualifying_rating=-0.8)
    assert alert_cut_reason(_sig(**cov)) == "reference_only"
    assert alert_cut_reason(_sig(back="bwin_es", lay="betfair_es_exchange", **cov)) is None


def test_los_demas_cortes_siguen_primero():
    # una señal que ya no pasaba el umbral sigue contando por su motivo de siempre
    assert alert_cut_reason(_sig(confidence="medium")) == "surebet_not_high"
    assert alert_cut_reason(_sig(qualifying_rating=0.1)) == "surebet_rating"


def test_el_escaner_guarda_pero_no_alerta_solo_referencia(monkeypatch):
    from datetime import datetime, timezone
    from matched import scanner

    writes = {}

    class Doc:
        def __init__(self, i):
            self.i = i

        def set(self, d, merge=False):
            writes[self.i] = d

    class Col:
        def stream(self):
            return []

        def document(self, i):
            return Doc(i)

    monkeypatch.setattr(scanner, "col", lambda name: Col())
    ref, es = _sig(signal_id="ref"), _sig(signal_id="es", back="bwin_es", lay="betfair_es_exchange")
    written, to_alert, stats = scanner._persist([ref, es], datetime(2098, 12, 31, tzinfo=timezone.utc))
    assert written == 2 and set(writes) == {"ref", "es"} and writes["ref"]["alerted"] is False
    assert [s.signal_id for s in to_alert] == ["es"] and stats["cut"] == {"reference_only": 1}
