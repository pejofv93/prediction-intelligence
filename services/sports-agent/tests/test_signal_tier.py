"""Un solo significado de fuerte/detectada/moderada (shared/signal_tier)."""
from analyzers.value_bet_engine import _intensity_emoji
from shared.signal_tier import emoji_from_ev, shown_emoji, tier_of


def test_cortes_del_motor_y_nivel_mostrado():
    assert [emoji_from_ev(x) for x in (0.16, 0.15, 0.09, 0.08, None)] == ["🔥", "✅", "✅", "📊", "📊"]
    assert shown_emoji("📊", 0.5) == "📊"          # manda lo que se vio
    assert tier_of(None, 0.2) == "fuerte" and tier_of("✅", 0) == "detectada"
    # el motor usa la misma función cuando la señal no trae nivel
    assert all(_intensity_emoji(x) == emoji_from_ev(x) for x in (0.01, 0.09, 0.16, 0.3))
