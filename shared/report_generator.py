"""
Generador de reporte semanal en formato Markdown para Telegram.
En shared/ para que telegram-bot pueda importarlo sin dependencias cruzadas.
"""


def generate_weekly_report(
    week_stats: dict,
    weights_before: dict,
    weights_after: dict,
    bankroll_metrics: dict | None = None,
) -> str:
    """
    Genera string Markdown formateado para Telegram.

    week_stats keys:
        week, predictions_total, predictions_correct, accuracy,
        accuracy_by_league, best_match, best_edge, best_result,
        worst_match, worst_edge, worst_error, poly_total, poly_alerts,
        poly_avg_edge, prev_week_accuracy,
        bankroll_current, roi_total, roi_sports, win_rate, closed_trades, streak

    weights_before / weights_after: dicts con claves poisson, elo, form, h2h.
    bankroll_metrics: resultado de shadow_engine.calculate_metrics() (opcional).
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

    bankroll_current = week_stats.get("bankroll_current", 50.0)
    roi_total = week_stats.get("roi_total", 0.0)
    roi_sports = week_stats.get("roi_sports", 0.0)
    win_rate = week_stats.get("win_rate", 0.0)
    closed_trades = week_stats.get("closed_trades", 0)
    streak = week_stats.get("streak", 0)

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

    # Accuracy por liga (solo ligas con al menos 1 prediccion)
    accuracy_by_league = week_stats.get("accuracy_by_league", {})
    league_lines = []
    for lg, acc in sorted(accuracy_by_league.items()):
        try:
            acc_f = float(acc)
        except (TypeError, ValueError):
            continue
        if acc_f > 0:
            league_lines.append(f"  {lg}: {acc_f:.1%}")

    # Construir mensaje segun formato del spec
    if total == 0:
        lines = [
            f"📈 REPORTE SEMANAL — Semana {week}",
            "",
            "🏟 Sports Agent:",
            "  Sin predicciones esta semana.",
        ]
        if league_lines:
            lines += ["", "📊 Accuracy por liga:"] + league_lines
        lines += [
            "",
            "🔮 Polymarket:",
            f"  Mercados analizados: {poly_total} | Alertas: {poly_alerts}",
            f"  Edge medio detectado: +{poly_avg_edge:.1%}",
        ]
        if closed_trades > 0:
            streak_str = f"+{streak}" if streak > 0 else str(streak)
            lines += [
                "",
                "💰 Bankroll virtual:",
                f"  Saldo: {bankroll_current:.2f}u | ROI: {roi_total:+.1%}",
                f"  Win rate: {win_rate:.1%} ({closed_trades} cerradas) | Racha: {streak_str}",
            ]
        return "\n".join(lines)

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

    if league_lines:
        lines += ["", "📊 Accuracy por liga:"] + league_lines

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

    if closed_trades > 0:
        streak_str = f"+{streak}" if streak > 0 else str(streak)
        lines += [
            "",
            "💰 Bankroll virtual:",
            f"  Saldo: {bankroll_current:.2f}u | ROI total: {roi_total:+.1%} | Sports: {roi_sports:+.1%}",
            f"  Win rate: {win_rate:.1%} ({closed_trades} trades cerradas) | Racha: {streak_str}",
        ]

    return "\n".join(lines)
