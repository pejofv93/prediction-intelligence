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

    Devuelve el mensaje listo para enviar por Telegram.
    """
    # TODO: implementar en Sesion 6
    raise NotImplementedError("generate_weekly_report pendiente — Sesion 6")
