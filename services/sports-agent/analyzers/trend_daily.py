"""
services/sports-agent/analyzers/trend_daily.py

Resumen DIARIO del feed de tendencias para el tema Telegram "Tendencias": misma
estructura que el informe diario de Sports pero sin ROI ni bankroll — envíos del
día anterior, graduadas, pendientes y, por mercado, acierto y estado de la
calibración.

Se llama al final de trend_grader.run_trend_grader(), tras graduar y calibrar.
Los lunes no se envía: ese día sale el resumen semanal (trend_weekly).
Solo lee trend_accuracy_log, trend_market_calibration, trend_signals y trend_runs;
escribe únicamente su marca diaria en trend_daily_reports (create() antes de enviar,
como el semanal: una sola vez por día aunque el grader corra dos veces).

"Graduadas ayer" = graduadas después del corte del diario anterior (guardado en la
marca), no "últimas 24h": el grader no corre a hora fija.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from analyzers.trend_weekly import (
    _MARKET_LABEL, _calibration_label, _ratio, _tally, counts_in_feed, not_duplicate,
    shadow_market_lines,
)
from shared.config import (
    CLOUD_RUN_TOKEN,
    TELEGRAM_BOT_URL,
    TREND_CALIBRATION_MIN_SAMPLE,
    TREND_MARKET_TARGET,
)

logger = logging.getLogger(__name__)


def _sends_line(run: dict | None) -> str:
    """Envíos del feed del día anterior desde trend_runs. Sin fila → "sin datos" (el
    feed no corrió o falló al registrar), nunca un cero que no se ha medido."""
    if not run:
        return "📨 Enviados ayer: sin datos"
    sent = int(run.get("sent") or 0)
    line = f"📨 Enviados ayer: {sent} partido{'s' if sent != 1 else ''}"
    extra = [f"{int(run.get('skipped_already_sent') or 0)} descartados por ya enviados"]
    if int(run.get("new_market_repeats") or 0):
        extra.append(f"{int(run['new_market_repeats'])} por mercado nuevo")
    return line + f" ({' · '.join(extra)})"


def build_daily_summary(log_rows: list[dict], day_rows: list[dict], pending: list[dict],
                        calibration: dict[str, dict], run: dict | None, day: datetime,
                        shadow_lines: list[str] | None = None) -> str:
    """Texto plano del diario. Función pura: todo entra por parámetro, ya sin sombra."""
    d_hit, d_miss, d_void = _tally(day_rows)
    t_hit, t_miss, _ = _tally(log_rows)

    lines = [f"📈 TENDENCIAS — RESUMEN DEL DÍA — {day.strftime('%d/%m/%Y')}", ""]
    lines.append(_sends_line(run))
    lines.append(f"✅ Graduadas ayer: {d_hit + d_miss + d_void} ({d_hit} aciertos · {d_miss} fallos"
                 f" · {d_void} anuladas)")
    lines.append(f"⏳ Pendientes: {len(pending)}")
    lines.append(f"🎯 Acierto total: {_ratio(t_hit, t_miss)}")

    pending_by_market: dict[str, int] = {}
    for s in pending:
        pending_by_market[s.get("market")] = pending_by_market.get(s.get("market"), 0) + 1
    markets = {r.get("market") for r in log_rows} | set(pending_by_market)
    markets.discard(None)

    def _totals(mk: str) -> tuple[int, int]:
        h, m, _ = _tally([r for r in log_rows if r.get("market") == mk])
        return h, m

    order = sorted(markets, key=lambda k: (-sum(_totals(k)), k))
    full = [mk for mk in order if sum(_totals(mk)) >= TREND_CALIBRATION_MIN_SAMPLE]
    thin = [mk for mk in order if sum(_totals(mk)) < TREND_CALIBRATION_MIN_SAMPLE]

    if full:
        lines += ["", "Por mercado — ayer · total · pendientes · calibración"]
        for mk in full:
            dh, dm, _ = _tally([r for r in day_rows if r.get("market") == mk])
            th, tm = _totals(mk)
            target = TREND_MARKET_TARGET.get(mk, 0.70)
            lines.append(
                f"{_MARKET_LABEL.get(mk, mk)} (obj. {target * 100:.0f}%): {_ratio(dh, dm, with_pct=False)}"
                f" · {_ratio(th, tm)} · {pending_by_market.get(mk, 0)} pend."
                f" · {_calibration_label(calibration.get(mk), th + tm, target)}"
            )
    if thin:
        parts = []
        for mk in thin:
            th, tm = _totals(mk)
            part = f"{_MARKET_LABEL.get(mk, mk)} {th}/{th + tm}"
            if pending_by_market.get(mk):
                part += f" ({pending_by_market[mk]} pend.)"
            parts.append(part)
        lines += ["", f"Sin muestra suficiente (<{TREND_CALIBRATION_MIN_SAMPLE} graduadas): "
                      + ", ".join(parts)]

    lines += shadow_lines or []
    lines += ["", "Las variantes en sombra no cuentan en estas cifras.",
              "📈 Tendencia estadística, no consejo de inversión."]
    return "\n".join(lines)


async def run_trend_daily_summary(force: bool = False) -> dict:
    """Envía el diario salvo los lunes (sale el semanal), una vez por día."""
    from shared.firestore_client import col

    now = datetime.now(timezone.utc)
    if now.weekday() == 0 and not force:
        return {"sent": False, "reason": "lunes_semanal"}

    day_key = now.date().isoformat()
    reports = col("trend_daily_reports")
    try:
        last = list(reports.order_by("cutoff", direction="DESCENDING").limit(1).stream())
        prev_cutoff = (last[0].to_dict() or {}).get("cutoff") if last else None
    except Exception:
        logger.warning("trend_daily: error leyendo el diario anterior", exc_info=True)
        prev_cutoff = None
    period_from_iso = prev_cutoff or (now - timedelta(days=1)).isoformat()

    try:
        all_rows = [r for r in (d.to_dict() or {} for d in col("trend_accuracy_log").stream())
                    if not_duplicate(r)]
        log_rows = [r for r in all_rows if counts_in_feed(r)]
        all_pending = [s for s in (d.to_dict() or {} for d in
                       col("trend_signals").where(filter=FieldFilter("graded", "==", False)).stream())
                       if not_duplicate(s)]
        pending = [s for s in all_pending if counts_in_feed(s)]
        calibration = {d.id: d.to_dict() or {} for d in col("trend_market_calibration").stream()}
        run_snap = col("trend_runs").document((now - timedelta(days=1)).date().isoformat()).get()
        run = run_snap.to_dict() if run_snap.exists else None
    except Exception:
        logger.error("trend_daily: error leyendo datos de tendencias", exc_info=True)
        return {"sent": False, "reason": "read_failed"}

    day_rows = [r for r in log_rows if str(r.get("graded_at") or "") > period_from_iso]
    cutoff = max((str(r.get("graded_at") or "") for r in log_rows), default=now.isoformat())
    text = build_daily_summary(log_rows, day_rows, pending, calibration, run, now,
                               shadow_market_lines(all_rows, all_pending))

    try:
        reports.document(day_key).create({
            "day": day_key, "cutoff": cutoff, "period_from": period_from_iso,
            "created_at": now, "day_graded": len(day_rows), "pending": len(pending),
        })
    except Exception as exc:  # AlreadyExists → otra ejecución ya lo envió hoy
        logger.info("trend_daily: diario %s ya enviado o marca no creada (%s)", day_key, exc)
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
            logger.error("trend_daily: error enviando diario %s", day_key, exc_info=True)

    if not sent:
        try:
            reports.document(day_key).delete()
        except Exception:
            logger.warning("trend_daily: no se pudo borrar la marca %s", day_key, exc_info=True)
        logger.warning("trend_daily: diario %s NO enviado — se reintentará", day_key)
        return {"sent": False, "reason": "send_failed"}

    logger.info("trend_daily: diario %s enviado (%d graduadas, %d pendientes)",
                day_key, len(day_rows), len(pending))
    return {"sent": True, "day": day_key, "day_graded": len(day_rows), "pending": len(pending)}
