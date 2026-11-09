"""Aviso de salud: los dos grupos contra el acierto esperado por cuota; Polymarket con margen
según la muestra en vez de los umbrales fijos 35 % / 45 %."""
import shared.model_health as mh


def _sig(n_ok, n, odds):
    return [{"correct": i < n_ok, "odds": odds} for i in range(n)]


def test_margen_segun_muestra():
    assert mh._min_gap(0.5, 20, True) == round(1.645 * (0.25 / 20) ** 0.5, 4)   # ~18 puntos
    assert mh._min_gap(0.95, 20, True) == 0.10                                # el ruido es menor que 10
    assert mh._min_gap(0.5, 20, False) == 0.10                                # Sports: fijo


def test_poly_buy_no_caro_que_acierta_la_mitad_avisa():
    # Precio del lado comprado 0.80 (cuota 1.25): espera 80 %, acierta 50 % → antes "SALUDABLE"
    g = mh._evaluate_group(_sig(10, 20, 1.25), vs_expected=True, sample_aware=True)
    assert g["expected_win_rate"] == 0.8 and g["status"] == "POR DEBAJO DE CUOTA" and g["degraded"]


def test_poly_longshot_que_acierta_lo_esperado_no_avisa():
    # Cuota 4.0: espera 25 %, acierta 30 % → antes "CRITICO" (< 35 %)
    g = mh._evaluate_group(_sig(6, 20, 4.0), vs_expected=True, sample_aware=True)
    assert g["status"] == "SALUDABLE" and not g["degraded"]


def test_poly_ruido_de_muestra_no_avisa_pero_un_hundimiento_si():
    # Cuota 2.0 (espera 50 %): 35 % son 15 puntos, dentro del ruido con n=20 (margen ~18)
    assert not mh._evaluate_group(_sig(7, 20, 2.0), vs_expected=True, sample_aware=True)["degraded"]
    # 30 % son 20 puntos: avisa
    assert mh._evaluate_group(_sig(6, 20, 2.0), vs_expected=True, sample_aware=True)["degraded"]


def test_sports_sigue_con_10_puntos():
    g = mh._evaluate_group(_sig(8, 20, 2.0), vs_expected=True)   # 40 % frente a 50 %
    assert g["min_gap"] == 0.10 and g["degraded"]


def test_check_model_health_usa_cuota_en_los_dos(monkeypatch):
    monkeypatch.setattr(mh, "col", lambda name: type("C", (), {"document": lambda s, d: type(
        "D", (), {"set": lambda s, v: None})()})())
    h = mh.check_model_health(sports_signals=_sig(8, 20, 2.0), poly_signals=_sig(6, 20, 4.0))
    assert h["groups"]["sports"]["status"] == "POR DEBAJO DE CUOTA"
    assert h["groups"]["polymarket"]["status"] == "SALUDABLE"
    assert h["groups"]["polymarket"]["min_gap"] > 0.10


def test_aviso_muestra_el_margen():
    txt = mh.format_health_alert("polymarket", {"n": 20, "win_rate_last_20": 0.5, "status": "POR DEBAJO DE CUOTA",
                                                "expected_win_rate": 0.8, "min_gap": 0.10})
    assert "esperado por cuota" in txt and "10 puntos por debajo" in txt
