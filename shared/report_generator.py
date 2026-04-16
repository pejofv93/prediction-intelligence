"""
Generador de reporte semanal en formato Markdown para Telegram.
En shared/ para que telegram-bot pueda importarlo sin dependencias cruzadas.
"""


def generate_weekly_report(
    week_stats: dict,
    weights_before: dict,
    weights_after: dict,
) -> str:
    """
    Genera string Markdown formateado para Telegram.

    week_stats keys:
        week, predictions_total, predictions_correct, accuracy,
        accuracy_by_league, best_match, best_edge, best_result,
        worst_match, worst_edge, worst_error, poly_total, poly_alerts,
        poly_avg_edge, prev_week_accuracy

    weights_before / weights_after: dicts con claves poisson, elo, form, h2h.
    Devuelve el mensaje listo para enviar por Telegram (Markdown).
    """
    week = week_stats.get("week", "N/A")
    total = week_stats.get("predictions_total", 0)
    correct = week_stats.get("predictions_correct", 0)
    accuracy = week_stats.get("accuracy", 0.0)
    prev_accuracy = week_stats.get("prev_week_accuracy")

    best_match = week_stats.get("best_match", "N/A")
    best_edge = week_stats.get("best_edge", 0.0)
    best_result = week_stats.get("best_result", "N/A")
    worst_match = week_stats.get("worst_match", "N/A")
    worst_edge = week_stats.get("worst_edge", 0.0)
    worst_error = week_stats.get("worst_error", "N/A")

    poly_total = week_stats.get("poly_total", 0)
    poly_alerts = week_stats.get("poly_alerts", 0)
    poly_avg_edge = week_stats.get("poly_avg_edge", 0.0)

    # Delta respecto a semana anterior
    if prev_accuracy is not None:
        delta = accuracy - prev_accuracy
        delta_str = f"{delta:+.1%}"
    else:
        delta_str = "N/A (primera semana)"

    # Lineas de ajuste de pesos
    weight_keys = ["poisson", "elo", "form", "h2h"]
    weight_lines = []
    for k in weight_keys:
        before = weights_before.get(k, 0.0)
        after = weights_after.get(k, 0.0)
        if after > before:
            arrow = "▲"
        elif after < before:
            arrow = "▼"
        else:
            arrow = "─"
        weight_lines.append(f"  {k}: {before:.2f} → {after:.2f} {arrow}")

    weights_block = "\n".join(weight_lines)

    # Construir mensaje segun formato del spec
    if total == 0:
        # Semana sin predicciones
        return (
            f"📈 REPORTE SEMANAL — Semana {week}\n\n"
            "🏟 Sports Agent:\n"
            "  Sin predicciones esta semana.\n\n"
            f"🔮 Polymarket:\n"
            f"  Mercados analizados: {poly_total} | Alertas: {poly_alerts}\n"
            f"  Edge medio detectado: +{poly_avg_edge:.1%}"
        )

    lines = [
        f"📈 REPORTE SEMANAL — Semana {week}",
        "",
        "🏟 Sports Agent:",
        f"  Predicciones: {total} | Correctas: {correct} | Accuracy: {accuracy:.1%}",
        f"  Variación vs semana anterior: {delta_str}",
        "",
        "⚙️ Ajustes de pesos aplicados:",
        weights_block,
    ]

    if best_match and best_match != "N/A":
        lines += [
            "",
            f"🏆 Mejor señal: {best_match} (edge {best_edge:+.1%}, {best_result})",
        ]

    if worst_match and worst_match != "N/A":
        lines.append(f"❌ Peor señal: {worst_match} (edge {worst_edge:+.1%}, error: {worst_error})")

    lines += [
        "",
        "🔮 Polymarket:",
        f"  Mercados analizados: {poly_total} | Alertas: {poly_alerts}",
        f"  Edge medio detectado: +{poly_avg_edge:.1%}",
    ]

    return "\n".join(lines)
