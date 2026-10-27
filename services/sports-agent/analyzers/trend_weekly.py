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


def _fmt_day(d: datetime) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _variant_lines(variants: list[dict]) -> list[str]:
    """Sección "Probando": variantes en sombra frente a v1 en sombra (mismos partidos)."""
    from shared.config import TREND_PROMOTION_MIN_SHADOW

    lines = ["", "Probando (en sombra, no cuenta en el acierto)"]
    alerts = []
    for v in variants:
        name = f"{_MARKET_LABEL.get(v['market'], v['market'])} {v['variant']}"
        mine = _ratio(v["hits"], v["n"] - v["hits"])
        base = _ratio(v["v1_hits"], v["v1_n"] - v["v1_hits"])
        if v["status"] == "promoted":
            lines.append(f"{name}: ✅ en uso · sombra {mine} · v1 {base}")
            continue
        lines.append(f"{name}: {mine} · v1 en sombra {base} · {min(v['n'], TREND_PROMOTION_MIN_SHADOW)}"
                     f"/{TREND_PROMOTION_MIN_SHADOW} para decidir")
        if v["status"] == "ready":
            alerts.append(
                f"🟢 LISTA para sustituir a la regla actual: {name} ({v['desc']}) — {mine} "
                f"frente a v1 en sombra {base}, objetivo {v['target'] * 100:.0f}%. "
                f"No se aplica hasta que la confirmes."
            )
    return lines + ([""] + alerts if alerts else [])


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
    if len(logged) < len(days):
        line += f" · {len(days) - len(logged)} sin ejecución registrada"
    return line


def build_weekly_summary(log_rows: list[dict], week_rows: list[dict],
                         pending: list[dict], calibration: dict[str, dict],
                         period_from: datetime, period_to: datetime,
                         variants: list[dict] | None = None,
                         runs: dict[str, dict] | None = None,
                         shadow_lines: list[str] | None = None) -> str:
    """Texto plano del resumen. Función pura: todo lo que lee entra por parámetro.
    log_rows/week_rows/pending ya vienen sin las variantes ni los mercados en sombra."""
    w_hit, w_miss, w_void = _tally(week_rows)
    t_hit, t_miss, _ = _tally(log_rows)

    lines = [f"📈 Tendencias — resumen semanal ({_fmt_day(period_from)} – {_fmt_day(period_to)})", ""]
    week_line = f"Semana: {_ratio(w_hit, w_miss)} graduadas" if (w_hit + w_miss) else "Semana: sin graduadas"
    if w_void:
        week_line += f" · {w_void} anulada{'s' if w_void != 1 else ''}"
    lines.append(week_line)
    lines.append(f"Total desde el inicio: {_ratio(t_hit, t_miss)} · {len(pending)} pendientes")
    zero_line = _zero_days_line(runs or {}, period_from, period_to)
    if zero_line:
        lines.append(zero_line)

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
        target = TREND_MARKET_TARGET.get(mk, 0.70)
        lines.append(
            f"{_MARKET_LABEL.get(mk, mk)} (obj. {target * 100:.0f}%): {_ratio(wh, wm, with_pct=False)}"
            f" · {_ratio(th, tm)} · {pending_by_market.get(mk, 0)} pend."
            f" · {_calibration_label(calibration.get(mk), th + tm, target)}"
        )

    if variants:
        lines += _variant_lines(variants)
    lines += shadow_lines or []

    lines += [
        "",
        f"Cada mercado con su objetivo · calibración a partir de "
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
        all_rows = [r for r in (d.to_dict() or {} for d in col("trend_accuracy_log").stream())
                    if not_duplicate(r)]
        log_rows = [r for r in all_rows if counts_in_feed(r)]
        all_pending = [s for s in (d.to_dict() or {} for d in
                       col("trend_signals").where(filter=FieldFilter("graded", "==", False)).stream())
                       if not_duplicate(s)]
        pending = [s for s in all_pending if counts_in_feed(s)]
        calibration = {d.id: d.to_dict() or {} for d in col("trend_market_calibration").stream()}
        runs = {d.id: d.to_dict() or {} for d in col("trend_runs").stream()}
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
                                shadow_market_lines(all_rows, all_pending))

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
