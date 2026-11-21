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
import html
import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from analyzers.trend_weekly import (
    _MARKET_LABEL, _ratio, _tally, counts_in_feed, fixture_scores,
    not_duplicate, score_band_line, shadow_market_lines,
)
from shared.config import (
    CLOUD_RUN_TOKEN,
    TELEGRAM_BOT_URL,
    TREND_CALIBRATION_MIN_SAMPLE,
    TREND_MARKET_TARGET,
)

logger = logging.getLogger(__name__)


_WEEKDAYS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
_MONTHS_SHORT = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_SEP = "━━━━━━━━━━━━━━"


def _esc(text) -> str:
    return html.escape(str(text), quote=False)


def _sent_yesterday(run: dict | None) -> str:
    """Envíos del feed del día anterior desde trend_runs. Sin fila → "sin datos" (el feed no
    corrió o falló al registrar), nunca un cero que no se ha medido."""
    return "sin datos" if not run else str(int(run.get("sent") or 0))


def _sends_detail(run: dict | None) -> str | None:
    if not run:
        return None
    line = f"Descartados por ya enviados: {int(run.get('skipped_already_sent') or 0)}"
    if int(run.get("new_market_repeats") or 0):
        line += f" · repetidos por mercado nuevo: {int(run['new_market_repeats'])}"
    return line


def market_ok(cal: dict | None, hits: int, misses: int, target: float) -> tuple[bool, str]:
    """(cumple, nota corta) a partir de la calibración; sin estado, el acierto frente al objetivo."""
    status = (cal or {}).get("status")
    if status == "at_fixed":
        return True, ""
    if status == "calibrated" and cal.get("threshold_effective") != cal.get("threshold_fixed"):
        return True, f"umbral {cal.get('threshold_effective')}"
    if status == "calibrated":
        hr = cal.get("hit_rate_fixed")
        return (hr is not None and hr >= target), ""
    if status == "below_target":
        return False, ""
    n = hits + misses
    return (n > 0 and hits / n >= target), ""


def build_daily_summary(log_rows: list[dict], day_rows: list[dict], pending: list[dict],
                        calibration: dict[str, dict], run: dict | None, day: datetime,
                        shadow_lines: list[str] | None = None,
                        scores: dict[str, float] | None = None) -> str:
    """HTML de Telegram del diario de señales. Función pura: todo entra por parámetro, ya sin
    sombra. Visible: título, una línea de resumen y una línea por mercado con muestra suficiente
    (✅ cumple su objetivo / ⚠️ por debajo). Plegado: muestras pequeñas, sombra y detalle."""
    d_hit, d_miss, d_void = _tally(day_rows)
    t_hit, t_miss, _ = _tally(log_rows)

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

    dlabel = f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS_SHORT[day.month - 1]}"
    lines = [f"<b>📈 Señales de tendencia · {dlabel}</b>",
             f"🎯 Acierto <b>{_ratio(t_hit, t_miss)}</b> · 📨 Enviados ayer: {_sent_yesterday(run)}"
             f" · ⏳ Pendientes: {len(pending)}"]

    if full:
        lines.append(_SEP)
        for mk in full:
            dh, dm, _ = _tally([r for r in day_rows if r.get("market") == mk])
            th, tm = _totals(mk)
            target = TREND_MARKET_TARGET.get(mk, 0.70)
            ok, note = market_ok(calibration.get(mk), th, tm, target)
            extra = [f"obj. {target * 100:.0f}%"]
            if dh + dm:
                extra.append(f"ayer {_ratio(dh, dm, with_pct=False)}")
            if pending_by_market.get(mk):
                extra.append(f"{pending_by_market[mk]} pend.")
            if note:
                extra.append(note)
            lines.append(f"{'✅' if ok else '⚠️'} <b>{_esc(_MARKET_LABEL.get(mk, mk))}</b> "
                         f"{_ratio(th, tm)} · " + " · ".join(extra))

    fold = [f"Graduadas ayer: {d_hit + d_miss + d_void} ({d_hit} aciertos · {d_miss} fallos"
            f" · {d_void} anuladas)"]
    detail = _sends_detail(run)
    if detail:
        fold.append(detail)
    if thin:
        fold += ["", f"<b>Muestra pequeña</b> (&lt;{TREND_CALIBRATION_MIN_SAMPLE} graduadas)"]
        for mk in thin:
            th, tm = _totals(mk)
            part = f"{_esc(_MARKET_LABEL.get(mk, mk))} {th}/{th + tm}"
            if pending_by_market.get(mk):
                part += f" ({pending_by_market[mk]} pend.)"
            fold.append(part)
    shadow = [x for x in (shadow_lines or []) if x]
    if shadow:
        fold += ["", f"<b>{_esc(shadow[0])}</b>"] + [_esc(x) for x in shadow[1:]]
    if scores is not None:
        fold += ["", _esc(score_band_line("Por puntuación del partido (total, informativo)",
                                          log_rows, scores))]
    fold += ["", "Las variantes en sombra no cuentan en estas cifras."]
    lines.append("<blockquote expandable>" + "\n".join(fold) + "</blockquote>")
    lines.append("<i>Tendencia estadística, no consejo de inversión.</i>")
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
        scores = fixture_scores(col("trend_signals").select(["fixture_total_score"]).stream())
    except Exception:
        logger.error("trend_daily: error leyendo datos de tendencias", exc_info=True)
        return {"sent": False, "reason": "read_failed"}

    day_rows = [r for r in log_rows if str(r.get("graded_at") or "") > period_from_iso]
    cutoff = max((str(r.get("graded_at") or "") for r in log_rows), default=now.isoformat())
    text = build_daily_summary(log_rows, day_rows, pending, calibration, run, now,
                               shadow_market_lines(all_rows, all_pending), scores)

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
                    json={"type": "trend", "data": {"text": text, "format": "html"}},
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
