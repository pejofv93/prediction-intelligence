"""
Nivel de una señal de Sports: 🔥 fuerte, ✅ detectada, 📊 moderada. Un solo significado en
la alerta, en alerts_sent, en el historial por segmento y en el informe diario.

Manda el nivel que calculó el generador y viaja en la señal (campo intensity): es el que
se ve en el canal. En 1X2 de fútbol, value_bet_engine lo decide con EV, confianza y cuota
(🔥 EV > 0,20 y confianza > 0,80 y cuota < 5; ✅ EV > 0,12 y confianza > 0,70 y cuota < 6);
en mercados alternativos, football_markets sobre el edge (> 0,15 / > 0,08).

Si la señal no lo trae (otros generadores, o envíos registrados antes del 14-nov-2026, que
no lo guardaban), se reconstruye sobre el EV con los cortes del motor: > 0,15 fuerte,
> 0,08 detectada, el resto moderada.
"""
EMOJI_TO_TIER = {"🔥": "fuerte", "✅": "detectada", "📊": "moderada"}
TIER_STRONG_EV = 0.15
TIER_DETECTED_EV = 0.08


def emoji_from_ev(ev) -> str:
    try:
        ev = float(ev or 0)
    except (TypeError, ValueError):
        ev = 0.0
    if ev > TIER_STRONG_EV:
        return "🔥"
    if ev > TIER_DETECTED_EV:
        return "✅"
    return "📊"


def shown_emoji(intensity, ev) -> str:
    """El nivel que trae la señal; si no, el reconstruido sobre el EV."""
    e = str(intensity or "")
    return e if e in EMOJI_TO_TIER else emoji_from_ev(ev)


def tier_of(intensity, ev) -> str:
    return EMOJI_TO_TIER[shown_emoji(intensity, ev)]
