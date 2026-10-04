"""
services/sports-agent/analyzers/trend_weekly.py

Resumen semanal del feed de tendencias para el tema Telegram "Tendencias":
acierto de la semana y total, por tipo de evidencia y por mercado, con el
estado de la calibración de cada mercado y las señales pendientes.

Se llama al final de trend_grader.run_trend_grader() los lunes, ya con lo del
fin de semana graduado y la calibración recalculada. Solo lee trend_accuracy_log,
trend_market_calibration y trend_signals; escribe únicamente su marca semanal en
trend_weekly_reports. Nunca toca accuracy_log/predictions/shadow_trades ni el
reporte semanal del sistema de valor (/send-weekly-report).

Ventana "semana": graduadas después del corte del resumen anterior (guardado en
la marca), no "últimos 7 días": el grader no corre a hora fija (GitHub lo retrasa
horas) y con 7 días móviles una graduación del lunes podía contarse dos semanas.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import (
    CLOUD_RUN_TOKEN,
    TELEGRAM_BOT_URL,
    TREND_CALIBRATION_MIN_SAMPLE,
    TREND_TARGET_HIT_RATE,
)

logger = logging.getLogger(__name__)

_MARKET_LABEL = {
    "team_goals_over": "Goles del equipo",
    "btts": "Ambos marcan",
    "handicap": "Hándicap",
    "corners": "Córners",
    "cards": "Tarjetas",
    "red_cards": "Expulsiones",
    "shots": "Tiros",
    "shots_on_target": "Tiros a puerta",
    "fouls": "Faltas",
    "ht_goals": "Gol 1ª parte",
    "double_chance": "Doble oportunidad",
    "dnb": "Empate no apuesta",
    "exact_total": "Total exacto",
    "win_margin": "Margen de victoria",
}
_TYPE_LABEL = (("series", "🎯 Serie"), ("model", "📐 Modelo"), ("rolling", "📉 Promedio"))
_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_DISCLAIMER = "📈 Tendencia estadística, no consejo de inversión."


def _tally(rows: list[dict]) -> tuple[int, int, int]:
    hits = sum(1 for r in rows if r.get("result") == "hit")
    misses = sum(1 for r in rows if r.get("result") == "miss")
    voids = sum(1 for r in rows if r.get("result") == "void")
    return hits, misses, voids


def _ratio(hits: int, misses: int, with_pct: bool = True) -> str:
    n = hits + misses
    if n == 0:
        return "—"
    return f"{hits}/{n} ({hits / n * 100:.0f}%)" if with_pct else f"{hits}/{n}"


def _calibration_label(cal: dict | None, graded: int) -> str:
    status = (cal or {}).get("status")
    if status == "at_fixed":
        return "✅ cumple con umbral fijo"
    if status == "calibrated":
        return f"🎚️ calibrado (umbral {cal.get('threshold_effective')})"
    if status == "below_target":
        return f"⚠️ por debajo del {TREND_TARGET_HIT_RATE * 100:.0f}%"
    return f"fijo ({graded}/{TREND_CALIBRATION_MIN_SAMPLE})"


def _fmt_day(d: datetime) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]}"


def build_weekly_summary(log_rows: list[dict], week_rows: list[dict],
                         pending: list[dict], calibration: dict[str, dict],
                         period_from: datetime, period_to: datetime) -> str:
    """Texto plano del resumen. Función pura: todo lo que lee entra por parámetro."""
    w_hit, w_miss, w_void = _tally(week_rows)
    t_hit, t_miss, _ = _tally(log_rows)

    lines = [f"📈 Tendencias — resumen semanal ({_fmt_day(period_from)} – {_fmt_day(period_to)})", ""]
    week_line = f"Semana: {_ratio(w_hit, w_miss)} graduadas" if (w_hit + w_miss) else "Semana: sin graduadas"
    if w_void:
        week_line += f" · {w_void} anulada{'s' if w_void != 1 else ''}"
    lines.append(week_line)
    lines.append(f"Total desde el inicio: {_ratio(t_hit, t_miss)} · {len(pending)} pendientes")

    lines += ["", "Por tipo (total)"]
    for ptype, label in _TYPE_LABEL:
        h, m, _ = _tally([r for r in log_rows if r.get("pattern_type") == ptype])
        if h + m:
            lines.append(f"{label}  {_ratio(h, m)}")

    pending_by_market: dict[str, int] = {}
    for s in pending:
        pending_by_market[s.get("market")] = pending_by_market.get(s.get("market"), 0) + 1
    markets = {r.get("market") for r in log_rows} | set(pending_by_market)
    markets.discard(None)

    def _graded(mk: str) -> int:
        h, m, _ = _tally([r for r in log_rows if r.get("market") == mk])
        return h + m

    lines += ["", "Por mercado — semana · total · pendientes · calibración"]
    for mk in sorted(markets, key=lambda k: (-_graded(k), k)):
        wh, wm, _ = _tally([r for r in week_rows if r.get("market") == mk])
        th, tm, _ = _tally([r for r in log_rows if r.get("market") == mk])
        lines.append(
            f"{_MARKET_LABEL.get(mk, mk)}: {_ratio(wh, wm, with_pct=False)} · {_ratio(th, tm)}"
            f" · {pending_by_market.get(mk, 0)} pend. · {_calibration_label(calibration.get(mk), th + tm)}"
        )

    lines += [
        "",
        f"Objetivo {TREND_TARGET_HIT_RATE * 100:.0f}% · calibración a partir de "
        f"{TREND_CALIBRATION_MIN_SAMPLE} graduadas por mercado",
        _DISCLAIMER,
    ]
    return "\n".join(lines)


def _week_key(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


async def run_trend_weekly_summary(force: bool = False) -> dict:
    """
    Envía el resumen si es lunes (UTC) o force=True. La marca de la semana se crea
    con create() ANTES de enviar: si dos ejecuciones del grader coinciden, solo una
    envía. Si el envío falla, la marca se borra para que el siguiente intento reenvíe.
    """
    from shared.firestore_client import col

    now = datetime.now(timezone.utc)
    if now.weekday() != 0 and not force:
        return {"sent": False, "reason": "no_es_lunes"}

    week_key = _week_key(now)
    reports = col("trend_weekly_reports")
    try:
        last = list(reports.order_by("cutoff", direction="DESCENDING").limit(1).stream())
        prev_cutoff = (last[0].to_dict() or {}).get("cutoff") if last else None
    except Exception:
        logger.warning("trend_weekly: error leyendo el resumen anterior", exc_info=True)
        prev_cutoff = None
    period_from_iso = prev_cutoff or (now - timedelta(days=7)).isoformat()

    try:
        log_rows = [d.to_dict() or {} for d in col("trend_accuracy_log").stream()]
        pending = [
            d.to_dict() or {}
            for d in col("trend_signals").where(filter=FieldFilter("graded", "==", False)).stream()
        ]
        calibration = {d.id: d.to_dict() or {} for d in col("trend_market_calibration").stream()}
    except Exception:
        logger.error("trend_weekly: error leyendo datos de tendencias", exc_info=True)
        return {"sent": False, "reason": "read_failed"}

    week_rows = [r for r in log_rows if str(r.get("graded_at") or "") > period_from_iso]
    cutoff = max((str(r.get("graded_at") or "") for r in log_rows), default=now.isoformat())
    period_from = datetime.fromisoformat(period_from_iso)
    text = build_weekly_summary(log_rows, week_rows, pending, calibration,
                                period_from, now - timedelta(days=1))

    try:
        reports.document(week_key).create({
            "week": week_key, "cutoff": cutoff, "period_from": period_from_iso,
            "created_at": now, "week_graded": len(week_rows), "pending": len(pending),
        })
    except Exception as exc:  # AlreadyExists → otra ejecución ya lo envió esta semana
        logger.info("trend_weekly: resumen %s ya enviado o marca no creada (%s)", week_key, exc)
        return {"sent": False, "reason": "ya_enviado"}

    sent = False
    if TELEGRAM_BOT_URL and CLOUD_RUN_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{TELEGRAM_BOT_URL}/send-alert",
                    headers={"x-cloud-token": CLOUD_RUN_TOKEN},
                    json={"type": "trend", "data": {"text": text}},
                )
            sent = resp.status_code in (200, 202) and bool(resp.json().get("sent"))
        except Exception:
            logger.error("trend_weekly: error enviando resumen %s", week_key, exc_info=True)

    if not sent:
        try:
            reports.document(week_key).delete()
        except Exception:
            logger.warning("trend_weekly: no se pudo borrar la marca %s", week_key, exc_info=True)
        logger.warning("trend_weekly: resumen %s NO enviado — se reintentará", week_key)
        return {"sent": False, "reason": "send_failed"}

    logger.info("trend_weekly: resumen %s enviado (%d graduadas en la semana, %d pendientes)",
                week_key, len(week_rows), len(pending))
    return {"sent": True, "week": week_key, "week_graded": len(week_rows)}
