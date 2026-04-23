"""
Motor de puntuacion unificada 0-100 para senales de sports y polymarket.
"""


def calculate_unified_score(
    signal: dict,
    win_rate_last_20: float = 0.5,
    days_to_close: int | None = None,
) -> int:
    """
    Calcula score 0-100 para cualquier senal (sports o polymarket).

    Componentes:
    - Edge normalizado  (0-40 pts): min(40, edge/0.30 * 40)
    - Confianza         (0-25 pts): confidence * 25
    - Kelly fraction    (0-15 pts): min(15, kelly/0.10 * 15)
    - Historical acc.   (0-20 pts): win_rate_last_20 * 20

    Factor temporal (si days_to_close esta disponible):
    - days_to_close <= 7:  time_factor = 1.0 (sin cambio)
    - 7 < days_to_close <= 30: time_factor = 0.85
    - days_to_close > 30:  time_factor = 0.70

    Returns int 0-100.
    """
    edge = abs(float(signal.get("edge") or 0))
    confidence = float(signal.get("confidence") or 0)
    kelly_raw = float(signal.get("kelly_fraction") or 0)
    kelly = kelly_raw if kelly_raw > 0 else abs(edge) / 2

    edge_pts = min(40.0, (edge / 0.30) * 40.0)
    confidence_pts = min(25.0, confidence * 25.0)
    kelly_pts = min(15.0, (kelly / 0.10) * 15.0)
    history_pts = min(20.0, win_rate_last_20 * 20.0)

    raw = edge_pts + confidence_pts + kelly_pts + history_pts

    if days_to_close is not None:
        if days_to_close <= 7:
            time_factor = 1.0
        elif days_to_close <= 30:
            time_factor = 0.85
        else:
            time_factor = 0.70
        raw = raw * time_factor

    return max(0, min(100, int(round(raw))))


def score_label(score: int) -> str:
    """Devuelve el label textual para el score."""
    if score >= 80:
        return "🔥 SEÑAL FUERTE"
    elif score >= 60:
        return "✅ SEÑAL DETECTADA"
    elif score >= 40:
        return "📊 SEÑAL MODERADA"
    return "⚪ SEÑAL DÉBIL"
