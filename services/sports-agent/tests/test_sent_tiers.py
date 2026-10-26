"""ROI por nivel del diario desde alerts_sent (sports_sent): 1u por envío, cuota del primer envío."""
from datetime import datetime, timezone
from unittest.mock import MagicMock


def _doc(d):
    m = MagicMock()
    m.to_dict.return_value = d
    return m


def _stats(monkeypatch, docs):
    import shared.model_health as mh
    monkeypatch.setattr(mh, "iter_all", lambda *a, **k: [_doc(d) for d in docs])
    return mh.sent_tier_stats()


def test_nivel_y_cuota_del_primer_envio(monkeypatch):
    t0 = datetime(2026, 10, 22, 22, tzinfo=timezone.utc)
    st = _stats(monkeypatch, [
        # nivel por ev del primer envío (0.25 → fuerte) aunque el último fuera 0.10; cuota 3.0
        {"sport": "football", "ev": 0.25, "odds": 3.0, "last_ev": 0.10, "last_odds": 1.5,
         "resends": 2, "correct": True, "sent_at": t0},
        {"sport": "football", "ev": 0.15, "odds": 2.0, "correct": False,
         "sent_at": datetime(2026, 10, 23, tzinfo=timezone.utc)},
        {"sport": "football", "ev": 0.15, "odds": 2.0, "correct": None, "result": "push"},  # push
        {"sport": "football", "ev": 0.10, "odds": 2.0, "correct": None},  # pendiente
        {"sport": "football", "ev": 0.05, "odds": 2.0, "correct": True},  # sin nivel
        {"sport": "tennis", "ev": 0.30, "odds": 2.0, "correct": None},  # tenis sin graduar
    ])
    assert st["fuerte"] == {"sent": 1, "graded": 1, "wins": 1, "pnl": 2.0}
    assert st["detectada"] == {"sent": 2, "graded": 1, "wins": 0, "pnl": -1.0}
    assert st["moderada"] == {"sent": 1, "graded": 0, "wins": 0, "pnl": 0.0}
    assert st["tennis_ungraded"] == 1
    assert st["since"] == t0


def _report(tier_stats):
    from shared.model_health import format_daily_report
    return format_daily_report({}, {}, tier_stats=tier_stats)


def test_sin_datos_hasta_20_graduadas_por_nivel():
    zero = {"sent": 0, "graded": 0, "wins": 0, "pnl": 0.0}
    txt = _report({
        "fuerte": {"sent": 25, "graded": 20, "wins": 10, "pnl": 2.0},
        "detectada": {"sent": 12, "graded": 4, "wins": 1, "pnl": -2.0},
        "moderada": zero, "tennis_ungraded": 0,
        "since": datetime(2026, 10, 22, tzinfo=timezone.utc),
    })
    assert "Enviadas por nivel desde 22/10/2026" in txt
    assert "🔥 Fuertes: 25 enviadas | Win rate: 50% | ROI: +10% (20 graduadas)" in txt
    assert "✅ Detectadas: 12 enviadas | Sin datos (4/20)" in txt
    assert "📊 Moderadas: 0 enviadas | Sin datos (0/20)" in txt
    assert "Baloncesto fuera; tenis sale sin graduar." in txt


def test_tenis_enviado_se_cuenta_y_sin_envios_no_hay_bloque():
    zero = {"sent": 0, "graded": 0, "wins": 0, "pnl": 0.0}
    txt = _report({"fuerte": {**zero, "sent": 1}, "detectada": zero, "moderada": zero,
                   "tennis_ungraded": 3, "since": None})
    assert "Baloncesto fuera; tenis sin graduar (3 enviadas)." in txt
    assert "Enviadas por nivel" not in _report({"fuerte": zero, "detectada": zero, "moderada": zero})
