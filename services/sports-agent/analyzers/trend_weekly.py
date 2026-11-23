"""
services/sports-agent/analyzers/trend_weekly.py

Resumen semanal del feed de tendencias para el tema Telegram "Tendencias":
acierto de la semana y total, por tipo de evidencia y por mercado, con el
estado de la calibración de cada mercado y las señales pendientes.

Se llama al final de trend_grader.run_trend_grader() los lunes, ya con lo del
fin de semana graduado y la calibración recalculada. Solo lee trend_accuracy_log,
trend_market_calibration, trend_signals y trend_runs; escribe únicamente su marca semanal en
trend_weekly_reports. Nunca toca accuracy_log/predictions/shadow_trades ni el
reporte semanal del sistema de valor (/send-weekly-report).

Ventana "semana": graduadas después del corte del resumen anterior (guardado en
la marca), no "últimos 7 días": el grader no corre a hora fija (GitHub lo retrasa
horas) y con 7 días móviles una graduación del lunes podía contarse dos semanas.
"""
import html
import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import (
    CLOUD_RUN_TOKEN,
    TELEGRAM_BOT_URL,
    TREND_CALIBRATION_MIN_SAMPLE,
    TREND_MARKET_TARGET,
    TREND_SHADOW_MARKETS,
)

logger = logging.getLogger(__name__)

_MARKET_LABEL = {
    "team_goals_over": "Goles 2+ del equipo",
    "team_goals_over_3": "Goles 3+ del equipo",
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


def _calibration_label(cal: dict | None, graded: int, target: float) -> str:
    status = (cal or {}).get("status")
    if status == "at_fixed":
        return "✅ cumple con umbral fijo"
    if status == "calibrated" and cal.get("threshold_effective") != cal.get("threshold_fixed"):
        return f"🎚️ calibrado (umbral {cal.get('threshold_effective')})"
    if status == "calibrated":  # "calibrado" en el propio umbral fijo: no ha endurecido nada
        hr = cal.get("hit_rate_fixed")
        return ("✅ cumple con umbral fijo" if hr is not None and hr >= target
                else f"⚠️ por debajo de su objetivo ({target * 100:.0f}%)")
    if status == "below_target":
        return f"⚠️ por debajo de su objetivo ({target * 100:.0f}%)"
    return f"fijo ({graded}/{TREND_CALIBRATION_MIN_SAMPLE})"


def not_duplicate(r: dict) -> bool:
    """Fuera las copias de una misma señal en otro id del mismo partido (`duplicate_of` =
    id de la señal que se conserva; CL_SF_* vs id de football-data, marcadas 27-oct-2026).
    No cuentan en nada: ni acierto, ni sombra, ni calibración, ni variantes."""
    return not r.get("duplicate_of")


def counts_in_feed(r: dict) -> bool:
    """Fila/señal que cuenta en el acierto: ni variante en sombra ni de un mercado en sombra
    (TREND_SHADOW_MARKETS — tampoco sus graduadas de cuando se enviaba)."""
    return not r.get("shadow") and r.get("market") not in TREND_SHADOW_MARKETS


def shadow_market_lines(all_rows: list[dict], all_pending: list[dict]) -> list[str]:
    """Mercados en sombra aparte, con su objetivo propio: lo graduado en sombra y lo que se
    llegó a enviar antes de pasar a sombra."""
    if not TREND_SHADOW_MARKETS:
        return []
    lines = ["", "En sombra (se gradúa, no se envía ni cuenta en el acierto)"]
    for mk in sorted(TREND_SHADOW_MARKETS):
        rows = [r for r in all_rows if r.get("market") == mk]
        sh_h, sh_m, _ = _tally([r for r in rows if r.get("shadow")])
        old_h, old_m, _ = _tally([r for r in rows if not r.get("shadow")])
        pend = sum(1 for p in all_pending if p.get("market") == mk)
        target = TREND_MARKET_TARGET.get(mk, 0.70)
        lines.append(f"{_MARKET_LABEL.get(mk, mk)} (obj. {target * 100:.0f}%): en sombra {_ratio(sh_h, sh_m)}"
                     f" · enviadas antes {_ratio(old_h, old_m)} · {pend} pend.")
    return lines


# Tramos de puntuación del partido (informativo, desde 2026-11-01): el mínimo para enviar
# es TREND_MIN_FIXTURE_SCORE (5,5); se compara 5,5-7 frente a 7 o más.
_SCORE_BANDS = (("5,5-7", 5.5, 7.0), ("7+", 7.0, float("inf")))


def fixture_scores(signals) -> dict[str, float]:
    """{signal_id: fixture_total_score} de trend_signals, para las filas de
    trend_accuracy_log anteriores a que el grader copiara la puntuación."""
    out = {}
    for d in signals:
        v = (d.to_dict() or {}).get("fixture_total_score")
        if isinstance(v, (int, float)):
            out[d.id] = float(v)
    return out


def score_band_line(label: str, rows: list[dict], scores: dict[str, float]) -> str:
    """'<label>: 5,5-7 h/n (%) · 7+ h/n (%)' por la puntuación del partido de cada fila."""
    parts = []
    for name, lo, hi in _SCORE_BANDS:
        band = []
        for r in rows:
            s = r.get("fixture_total_score")
            if not isinstance(s, (int, float)):
                s = scores.get(r.get("signal_id"))
            if isinstance(s, (int, float)) and lo <= s < hi:
                band.append(r)
        h, m, _ = _tally(band)
        parts.append(f"{name} {_ratio(h, m)}")
    return f"{label}: " + " · ".join(parts)


def _fmt_day(d: datetime) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _fmt_period(a: datetime, b: datetime) -> str:
    """«16–22 nov»; si cambia el mes, «30 nov – 6 dic»."""
    if a.month == b.month and a.year == b.year:
        return f"{a.day}–{b.day} {_MONTHS[b.month - 1]}"
    return f"{_fmt_day(a)} – {_fmt_day(b)}"


def _esc(text) -> str:
    return html.escape(str(text), quote=False)


def _variant_parts(variants: list[dict]) -> tuple[list[str], list[str]]:
    """("Probando" para el plegable, avisos de variante LISTA para la parte visible).
    Variantes en sombra frente a v1 en sombra (mismos partidos)."""
    from shared.config import TREND_PROMOTION_MIN_SHADOW, TREND_VARIANT_COUNT_SINCE

    y, m, d = (int(x) for x in TREND_VARIANT_COUNT_SINCE.split("-"))
    lines = [f"<b>Probando</b> (en sombra, no cuenta en el acierto; recuento desde el "
             f"{d} {_MONTHS[m - 1]} {y})"]
    alerts = []
    for v in variants:
        name = _esc(f"{_MARKET_LABEL.get(v['market'], v['market'])} {v['variant']}")
        mine = _ratio(v["hits"], v["n"] - v["hits"])
        base = _ratio(v["v1_hits"], v["v1_n"] - v["v1_hits"])
        if v["status"] == "promoted":
            lines.append(f"{name}: ✅ en uso · sombra {mine} · v1 {base}")
            continue
        lines.append(f"{name}: {mine} · v1 en sombra {base} · {min(v['n'], TREND_PROMOTION_MIN_SHADOW)}"
                     f"/{TREND_PROMOTION_MIN_SHADOW} para decidir")
        if v["status"] == "ready":
            alerts.append(
                f"🟢 <b>LISTA</b> para sustituir a la regla actual: {name} ({_esc(v['desc'])}) — "
                f"{mine} frente a v1 en sombra {base}, objetivo {v['target'] * 100:.0f}%. "
                f"No se aplica hasta que la confirmes."
            )
    return lines, alerts


def _zero_days_line(runs: dict[str, dict], period_from: datetime, period_to: datetime) -> str | None:
    """Días del periodo con 0 mensajes (un envío por partido desde 2026-10-23: un día en que
    los 10 mejores ya habían salido se queda vacío). runs = trend_runs por fecha UTC. Los días
    sin fila son días en que el feed no corrió (o anteriores al registro): se cuentan aparte."""
    days = [(period_from + timedelta(days=i)).date().isoformat()
            for i in range((period_to.date() - period_from.date()).days + 1)]
    logged = [d for d in days if d in runs]
    if not logged:
        return None
    zero = sum(1 for d in logged if int(runs[d].get("sent") or 0) == 0)
    line = f"Días sin mensajes: {zero} de {len(logged)}"
    missing = [d for d in days if d not in runs]
    if missing:
        when = ", ".join(_fmt_day(datetime.fromisoformat(d)) for d in missing)
        line += f" · {len(missing)} sin ejecución registrada ({when})"
    return line


def build_weekly_summary(log_rows: list[dict], week_rows: list[dict],
                         pending: list[dict], calibration: dict[str, dict],
                         period_from: datetime, period_to: datetime,
                         variants: list[dict] | None = None,
                         runs: dict[str, dict] | None = None,
                         shadow_lines: list[str] | None = None,
                         scores: dict[str, float] | None = None) -> str:
    """HTML de Telegram del semanal, con el mismo formato que el diario (trend_daily).
    Función pura: todo lo que lee entra por parámetro; log_rows/week_rows/pending ya vienen
    sin las variantes ni los mercados en sombra. Visible: título, resumen, una línea por
    mercado con muestra suficiente (✅ cumple su objetivo / ⚠️ por debajo) y los avisos de
    variante LISTA. Plegado: por tipo, muestras pequeñas, pruebas, sombra y puntuación."""
    from analyzers.trend_daily import _SEP, market_ok   # import aquí: trend_daily importa este módulo

    w_hit, w_miss, w_void = _tally(week_rows)
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

    week = f"<b>{_ratio(w_hit, w_miss)}</b>" if (w_hit + w_miss) else "sin graduadas"
    lines = [f"<b>📈 Señales de tendencia · semana {_fmt_period(period_from, period_to)}</b>",
             f"🎯 Semana {week} · Total {_ratio(t_hit, t_miss)} · ⏳ Pendientes: {len(pending)}"]
    zero_line = _zero_days_line(runs or {}, period_from, period_to)
    if zero_line:
        lines.append(f"📭 {zero_line}")

    if full:
        lines.append(_SEP)
        for mk in full:
            wh, wm, _ = _tally([r for r in week_rows if r.get("market") == mk])
            th, tm = _totals(mk)
            target = TREND_MARKET_TARGET.get(mk, 0.70)
            ok, note = market_ok(calibration.get(mk), th, tm, target)
            extra = [f"obj. {target * 100:.0f}%"]
            if wh + wm:
                extra.append(f"semana {_ratio(wh, wm, with_pct=False)}")
            if pending_by_market.get(mk):
                extra.append(f"{pending_by_market[mk]} pend.")
            if note:
                extra.append(note)
            lines.append(f"{'✅' if ok else '⚠️'} <b>{_esc(_MARKET_LABEL.get(mk, mk))}</b> "
                         f"{_ratio(th, tm)} · " + " · ".join(extra))

    probando, alerts = _variant_parts(variants) if variants else ([], [])
    lines += alerts

    fold = [f"Graduadas en la semana: {w_hit + w_miss + w_void} ({w_hit} aciertos · {w_miss} fallos"
            f" · {w_void} anuladas)"]
    by_type = []
    for ptype, label in _TYPE_LABEL:
        h, m, _ = _tally([r for r in log_rows if r.get("pattern_type") == ptype])
        if h + m:
            by_type.append(f"{label} {_ratio(h, m)}")
    if by_type:
        fold += ["", "<b>Por tipo</b> (total)"] + by_type
    if thin:
        fold += ["", f"<b>Muestra pequeña</b> (&lt;{TREND_CALIBRATION_MIN_SAMPLE} graduadas)"]
        for mk in thin:
            wh, wm, _ = _tally([r for r in week_rows if r.get("market") == mk])
            th, tm = _totals(mk)
            part = f"{_esc(_MARKET_LABEL.get(mk, mk))} {th}/{th + tm}"
            if wh + wm:
                part += f" · semana {wh}/{wh + wm}"
            if pending_by_market.get(mk):
                part += f" ({pending_by_market[mk]} pend.)"
            fold.append(part)
    if probando:
        fold += [""] + probando
    shadow = [x for x in (shadow_lines or []) if x]
    if shadow:
        fold += ["", f"<b>{_esc(shadow[0])}</b>"] + [_esc(x) for x in shadow[1:]]
    if scores is not None:
        fold += ["", "<b>Por puntuación del partido</b> (informativo)",
                 _esc(score_band_line("Semana", week_rows, scores)),
                 _esc(score_band_line("Total", log_rows, scores))]
    fold += ["", f"Cada mercado con su objetivo · calibración a partir de "
                 f"{TREND_CALIBRATION_MIN_SAMPLE} graduadas por mercado. "
                 f"Las variantes en sombra no cuentan en estas cifras."]
    lines.append("<blockquote expandable>" + "\n".join(fold) + "</blockquote>")
    lines.append(f"<i>{_esc(_DISCLAIMER[2:])}</i>")
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
        all_rows = [r for r in (d.to_dict() or {} for d in col("trend_accuracy_log").stream())
                    if not_duplicate(r)]
        log_rows = [r for r in all_rows if counts_in_feed(r)]
        all_pending = [s for s in (d.to_dict() or {} for d in
                       col("trend_signals").where(filter=FieldFilter("graded", "==", False)).stream())
                       if not_duplicate(s)]
        pending = [s for s in all_pending if counts_in_feed(s)]
        calibration = {d.id: d.to_dict() or {} for d in col("trend_market_calibration").stream()}
        runs = {d.id: d.to_dict() or {} for d in col("trend_runs").stream()}
        scores = fixture_scores(col("trend_signals").select(["fixture_total_score"]).stream())
    except Exception:
        logger.error("trend_weekly: error leyendo datos de tendencias", exc_info=True)
        return {"sent": False, "reason": "read_failed"}

    week_rows = [r for r in log_rows if str(r.get("graded_at") or "") > period_from_iso]
    cutoff = max((str(r.get("graded_at") or "") for r in log_rows), default=now.isoformat())
    period_from = datetime.fromisoformat(period_from_iso)
    from analyzers.trend_variants import active_rules, promotion_status
    variants = promotion_status([r for r in all_rows if r.get("shadow")], active_rules())
    text = build_weekly_summary(log_rows, week_rows, pending, calibration,
                                period_from, now - timedelta(days=1), variants, runs,
                                shadow_market_lines(all_rows, all_pending), scores)

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
                    json={"type": "trend", "data": {"text": text, "format": "html"}},
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
