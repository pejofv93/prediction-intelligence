"""Revisión mensual de niveles (shared/tier_review): solo baja etiqueta y Kelly, sin perseguir el azar."""
from datetime import datetime, timedelta, timezone

from shared import tier_review as tr

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _recs(n, hit_rate, odds=2.5, p=0.5, start=0):
    """n señales a cuota `odds` con prob del modelo `p`, acertando `hit_rate` repartido en el tiempo."""
    wins = round(n * hit_rate)
    every = n / wins if wins else n + 1
    return [{"ok": (i % every) < 1 if wins else False, "odds": odds, "p": p,
             "at": T0 + timedelta(hours=start + i)} for i in range(n)]


def test_estadisticas_y_k_observado():
    s = tr.segment_stats(_recs(100, 0.30, odds=2.5, p=0.5))
    # mercado 40 %, modelo 50 %, acierto 30 % → el modelo no ha cumplido nada de su ventaja
    assert (s["n"], s["hit"], s["expected"], s["model"]) == (100, 0.3, 0.4, 0.5)
    assert s["k_obs"] == 0.0 and s["z"] < -3 and s["both_halves"]
    assert tr.model_prob(0.25, 2.5) == 0.5


def test_benjamini_hochberg():
    assert tr.benjamini_hochberg({"a": 0.001, "b": 0.04, "c": 0.5}, q=0.10) == {"a", "b"}
    assert tr.benjamini_hochberg({"a": 0.2, "b": 0.5}) == set()


def test_propone_bajar_con_paso_maximo_y_no_persigue_el_azar():
    recs = {
        "football|h2h|fuerte": _recs(120, 0.30),          # pierde claro frente al modelo
        "football|totals|fuerte": _recs(60, 0.47),        # por debajo pero dentro del ruido
        "basketball|h2h|detectada": _recs(30, 0.10),      # muy mal pero muestra corta
    }
    rows = tr.propose(recs, {})
    a = rows["football|h2h|fuerte"]
    assert a["k_current"] == 1.0 and a["k_proposed"] == 0.9      # paso máximo 0,1
    assert rows["football|totals|fuerte"]["k_proposed"] == 1.0
    assert "basketball|h2h|detectada" not in rows                 # < MIN_N: ni se prueba


def test_si_deja_de_cumplir_vuelve_hacia_1():
    rows = tr.propose({"football|h2h|fuerte": _recs(80, 0.50)}, {"football|h2h|fuerte": 0.7})
    assert rows["football|h2h|fuerte"]["k_proposed"] == 0.8
    rows = tr.propose({}, {"football|h2h|fuerte": 0.95})
    assert rows["football|h2h|fuerte"]["k_proposed"] == 1.0


def test_ajuste_solo_baja_nivel_y_kelly():
    pred = {"sport": "football", "market_type": "h2h", "intensity": "🔥", "odds": 2.5, "ev": 0.25,
            "kelly_fraction": 0.05}
    assert tr.adjust(pred, {}) is None
    assert tr.adjust(pred, {"football|h2h|fuerte": 1.0}) is None
    adj = tr.adjust(pred, {"football|h2h|fuerte": 0.5})
    # mercado 40 %, modelo 50 % → 45 % → EV 0,125 → ✅; Kelly 0,125/1,5 = 0,0833 → tope el original
    assert adj["intensity"] == "✅" and adj["intensity_model"] == "🔥" and adj["ev_adjusted"] == 0.125
    assert adj["kelly_fraction"] == 0.05
    adj = tr.adjust({**pred, "kelly_fraction": 0.03}, {"football|h2h|fuerte": 0.2})
    # 40 % + 0,2·10 = 42 % → EV 0,05 → 📊; Kelly min(original 0,03, 0,05/1,5 = 0,033) = 0,03
    assert adj["intensity"] == "📊" and adj["ev_adjusted"] == 0.05 and adj["kelly_fraction"] == 0.03
    adj = tr.adjust({**pred, "kelly_fraction": 0.05}, {"football|h2h|fuerte": 0.2})
    assert adj["kelly_fraction"] == round(0.05 / 1.5, 4)
    # un 📊 nunca sube aunque el EV ajustado diera más
    assert tr.adjust({**pred, "intensity": "📊"}, {"football|h2h|moderada": 0.9})["intensity"] == "📊"
