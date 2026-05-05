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
        bankroll_current, roi_total, roi_sports, win_rate, closed_trades, streak,
        poly_buy_yes_correct, poly_buy_yes_total,
        poly_buy_no_correct, poly_buy_no_total,
        poly_best_market, poly_best_edge,

    weights_before / weights_after: dicts con claves poisson, elo, form, h2h.
    bankroll_metrics: resultado de shadow_engine.calculate_metrics() (opcional).
    Devuelve el mensaje listo para enviar por Telegram (Markdown).
    """
    week = week_stats.get("week", "N/A")
    total = int(week_stats.get("predictions_total", 0))
    correct = int(week_stats.get("predictions_correct", 0))
    failed = total - correct
    accuracy = float(week_stats.get("accuracy", 0.0))
    roi_sports = float(week_stats.get("roi_sports", 0.0))

    best_match = week_stats.get("best_match", "N/A")
    best_edge = float(week_stats.get("best_edge", 0.0))
    worst_match = week_stats.get("worst_match", "N/A")
    worst_edge = float(week_stats.get("worst_edge", 0.0))

    # Accuracy por liga (solo con datos)
    accuracy_by_league = week_stats.get("accuracy_by_league", {})
    league_parts: list[str] = []
    for lg in ["PL", "PD", "BL1", "SA", "FL1", "CL"]:
        if lg in accuracy_by_league:
            acc_val = accuracy_by_league[lg]
            try:
                acc_f = float(acc_val) if not isinstance(acc_val, dict) else float(acc_val.get("accuracy", 0))
            except (TypeError, ValueError):
                continue
            if acc_f > 0:
                league_parts.append(f"{lg} {acc_f:.0%}")
    league_line = " | ".join(league_parts) if league_parts else "—"

    # Polymarket
    poly_total = int(week_stats.get("poly_total", 0))
    poly_buy_yes_correct = int(week_stats.get("poly_buy_yes_correct", 0))
    poly_buy_yes_total = int(week_stats.get("poly_buy_yes_total", 0))
    poly_buy_no_correct = int(week_stats.get("poly_buy_no_correct", 0))
    poly_buy_no_total = int(week_stats.get("poly_buy_no_total", 0))
    poly_best_market = week_stats.get("poly_best_market", "—")
    poly_best_edge = float(week_stats.get("poly_best_edge", 0.0))

    buy_yes_pct = round(poly_buy_yes_correct / poly_buy_yes_total * 100) if poly_buy_yes_total > 0 else 0
    buy_no_pct = round(poly_buy_no_correct / poly_buy_no_total * 100) if poly_buy_no_total > 0 else 0

    # Modelo — pesos
    weight_keys = ["poisson", "elo", "form", "h2h"]
    up_keys: list[str] = []
    down_keys: list[str] = []
    for k in weight_keys:
        before = float(weights_before.get(k, 0.0))
        after = float(weights_after.get(k, 0.0))
        if after > before + 0.001:
            up_keys.append(k)
        elif after < before - 0.001:
            down_keys.append(k)

    up_str = ", ".join(up_keys) if up_keys else "—"
    down_str = ", ".join(down_keys) if down_keys else "—"

    conf_values: list[float] = []
    for k in weight_keys:
        v = weights_after.get(k)
        if v is not None:
            try:
                conf_values.append(float(v))
            except (TypeError, ValueError):
                pass
    avg_conf = sum(conf_values) / len(conf_values) if conf_values else 0.0

    bankroll_current = float(week_stats.get("bankroll_current", 50.0))
    roi_total = float(week_stats.get("roi_total", 0.0))
    win_rate = float(week_stats.get("win_rate", 0.0))
    closed_trades = int(week_stats.get("closed_trades", 0))

    lines: list[str] = [
        f"📊 REPORTE SEMANAL — Semana {week}",
        "",
        "⚽ SPORTS:",
        f"Señales: {total} | ✅ {correct} | ❌ {failed}",
        f"Win rate: {accuracy:.0%} | ROI: {roi_sports:+.1%}",
    ]

    if best_match and best_match != "N/A":
        lines.append(f"Mejor señal: {best_match} +{best_edge:.0%} ✅")
    if worst_match and worst_match != "N/A":
        lines.append(f"Peor señal: {worst_match} +{worst_edge:.0%} ❌")
    if league_line and league_line != "—":
        lines.append(f"Por liga: {league_line}")

    lines += [
        "",
        "🔮 POLYMARKET:",
        f"Mercados resueltos: {poly_total}",
        f"BUY\\_YES: {poly_buy_yes_correct}/{poly_buy_yes_total} ({buy_yes_pct}%)",
        f"BUY\\_NO: {poly_buy_no_correct}/{poly_buy_no_total} ({buy_no_pct}%)",
    ]
    if poly_best_market and poly_best_market != "—":
        lines.append(f"Mejor: {poly_best_market} +{poly_best_edge:.0%} ✅")

    lines += [
        "",
        "🧠 MODELO:",
        f"Pesos actualizados: {up_str} ↑ {down_str} ↓",
        f"Confianza media: {avg_conf:.0%}",
        "Próxima mejora automática: lunes",
    ]

    if closed_trades > 0:
        lines += [
            "",
            "💰 Bankroll virtual:",
            f"Saldo: {bankroll_current:.2f}u | ROI total: {roi_total:+.1%} | Win rate: {win_rate:.0%}",
        ]

    return "\n".join(lines)
