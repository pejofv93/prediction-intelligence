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
        avg_signal_confidence, n_signal_confidence,

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

    # Confianza media REAL de las señales emitidas esta semana (calculada en
    # send-weekly-report sobre el campo `confidence` de cada señal, sports +
    # poly alertadas). Antes este campo promediaba los 4 pesos del ensemble
    # (poisson/elo/form/h2h) — como se normalizan para sumar ~1.0, su media
    # daba ~25% SIEMPRE sin importar la confianza real de las señales (70-91%);
    # medía el reparto del ensemble, no confianza (bug descubierto 2026-09-12).
    avg_signal_confidence = float(week_stats.get("avg_signal_confidence", 0.0))
    n_signal_confidence = int(week_stats.get("n_signal_confidence", 0))

    bankroll_current = float(week_stats.get("bankroll_current", 50.0))
    roi_total = float(week_stats.get("roi_total", 0.0))
    win_rate = float(week_stats.get("win_rate", 0.0))
    closed_trades = int(week_stats.get("closed_trades", 0))
    # Desglose sports vs polymarket (bankroll compartido, P&L separado por source)
    roi_poly_bk = float(week_stats.get("roi_poly", 0.0))
    wr_sports_bk = float(week_stats.get("win_rate_sports", 0.0))
    wr_poly_bk = float(week_stats.get("win_rate_poly", 0.0))
    n_sports_bk = int(week_stats.get("n_sports", 0))
    n_poly_bk = int(week_stats.get("n_poly", 0))
    pnl_sports_bk = float(week_stats.get("pnl_sports", 0.0))
    pnl_poly_bk = float(week_stats.get("pnl_poly", 0.0))
    # Polymarket "emitido real" (solo señales realmente alertadas) vs ledger crudo
    roi_poly_alerted_bk = float(week_stats.get("roi_poly_alerted", 0.0))
    wr_poly_alerted_bk = float(week_stats.get("win_rate_poly_alerted", 0.0))
    n_poly_alerted_bk = int(week_stats.get("n_poly_alerted", 0))

    pending = int(week_stats.get("predictions_pending", 0))
    pending_note = f" (+{pending} pendientes)" if pending > 0 else ""

    lines: list[str] = [
        f"📊 REPORTE SEMANAL — Semana {week}",
        "",
        "⚽ SPORTS:",
        f"Resueltas: {total} | ✅ {correct} | ❌ {failed}{pending_note}",
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
        # "analizados" (no "resueltos"): poly_total cuenta mercados con analyzed_at
        # en la semana, no mercados que hayan resuelto esa semana — un mercado se
        # analiza en el momento pero puede resolver meses después.
        f"Mercados analizados: {poly_total}",
        # BUY_YES/BUY_NO SÍ están acotados a resoluciones de ESTA semana
        # (poly_predictions.resolved_at, no analyzed_at — ver send-weekly-report).
        f"BUY\\_YES (resueltos esta semana): {poly_buy_yes_correct}/{poly_buy_yes_total} ({buy_yes_pct}%)",
        f"BUY\\_NO (resueltos esta semana): {poly_buy_no_correct}/{poly_buy_no_total} ({buy_no_pct}%)",
    ]
    if poly_best_market and poly_best_market != "—":
        lines.append(f"Mejor: {poly_best_market} +{poly_best_edge:.0%} ✅")

    conf_line = (
        f"Confianza media de señales emitidas: {avg_signal_confidence:.0%} (n={n_signal_confidence})"
        if n_signal_confidence > 0
        else "Confianza media de señales emitidas: — (sin señales esta semana)"
    )
    lines += [
        "",
        "🧠 MODELO:",
        f"Pesos actualizados: {up_str} ↑ {down_str} ↓",
        conf_line,
        "Próxima mejora automática: lunes",
    ]

    if closed_trades > 0:
        lines += [
            "",
            "💰 Bankroll virtual:",
            f"Saldo: {bankroll_current:.2f}u | Win rate global: {win_rate:.0%}",
            f"P&L: ⚽ Sports {pnl_sports_bk:+.2f}u | 🔮 Polymarket {pnl_poly_bk:+.2f}u",
            f"📈 ROI total: {roi_total:+.1%}",
            f"  ⚽ Sports: {roi_sports:+.1%} (win rate {wr_sports_bk:.0%}, {n_sports_bk} señales)",
            f"  🔮 Polymarket — ledger crudo: {roi_poly_bk:+.1%} (win rate {wr_poly_bk:.0%}, {n_poly_bk} señales)",
            f"  🔮 Polymarket — emitido real (alertado): {roi_poly_alerted_bk:+.1%} (win rate {wr_poly_alerted_bk:.0%}, {n_poly_alerted_bk} señales)",
        ]

    return "\n".join(lines)
