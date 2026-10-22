"""
Model health monitor — aviso de acierto bajo en las últimas señales ENVIADAS.

Solo avisa: no sube ningún umbral ni suspende ninguna liga/categoría. Antes este
módulo calculaba un "recommended_edge" y una lista de "ligas suspendidas" sobre el
ledger crudo de shadow_trades (Sports y Polymarket mezclados, incluidas señales no
alertadas), y el aviso decía "threshold subido" / "Liga suspendida" aunque nada
leía ese resultado. Ahora hay un grupo por fuente, cada uno sobre lo realmente
enviado:
  - Sports:     predictions con alerted=True y correct graduado, por match_date.
  - Polymarket: alerts_sent (type=polymarket, status sent/sent_archived) graduadas
                por polymarket_resolver (result win/loss), por resolved_at.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col, iter_all
from shared.shadow_engine import (
    _POLY_SENT_STATUSES, _as_utc_dt, _poly_alert_selection, _poly_decimal_odds,
)

logger = logging.getLogger(__name__)

_LAST_N = 20
_MIN_SAMPLE = 20
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

_GROUP_LABELS = {"sports": "⚽ Sports", "polymarket": "🔮 Polymarket"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _sports_alerted_graded() -> list[dict]:
    """[{correct, odds, at}] de las predicciones sports alertadas y graduadas."""
    out = []
    docs = col("predictions").where(filter=FieldFilter("alerted", "==", True)).stream()
    for doc in docs:
        p = doc.to_dict() or {}
        if p.get("correct") not in (True, False):
            continue
        out.append({
            "correct": p["correct"],
            "odds": p.get("odds"),
            "at": _as_utc_dt(p.get("match_date")) or _as_utc_dt(p.get("created_at")),
        })
    return out


def _poly_alerted_graded() -> list[dict]:
    """[{correct, odds, at}] de las alertas Polymarket enviadas y resueltas."""
    out = []
    for doc in iter_all("alerts_sent", where=("type", "==", "polymarket")):
        a = doc.to_dict() or {}
        if a.get("status") not in _POLY_SENT_STATUSES or a.get("result") not in ("win", "loss"):
            continue
        sel = _poly_alert_selection(a)
        mp = a.get("market_price_yes", a.get("last_price"))
        odds = _poly_decimal_odds(sel, float(mp)) if sel and mp is not None else None
        out.append({
            "correct": a["result"] == "win",
            "odds": odds,
            "at": _as_utc_dt(a.get("resolved_at")),
        })
    return out


def _evaluate_group(signals: list[dict]) -> dict:
    """Acierto de las últimas _LAST_N señales graduadas de un grupo."""
    last = sorted(signals, key=lambda s: s.get("at") or _EPOCH, reverse=True)[:_LAST_N]
    n = len(last)
    if n < _MIN_SAMPLE:
        return {
            "status": "SIN MUESTRA", "n": n, "win_rate_last_20": 0.0,
            "expected_win_rate": None, "degraded": False,
        }
    win_rate = round(sum(1 for s in last if s["correct"]) / n, 4)
    # Acierto que implican las cuotas de esas mismas señales (1/cuota), como referencia:
    # un 35% de acierto a cuota media 3.0 no es lo mismo que a 1.5.
    implied = [1.0 / float(s["odds"]) for s in last if s.get("odds") and float(s["odds"]) > 1.0]
    expected = round(sum(implied) / len(implied), 4) if implied else None
    if win_rate < 0.35:
        status = "CRITICO"
    elif win_rate < 0.45:
        status = "DEGRADADO"
    else:
        status = "SALUDABLE"
    return {
        "status": status, "n": n, "win_rate_last_20": win_rate,
        "expected_win_rate": expected, "degraded": status != "SALUDABLE",
    }


def check_model_health(
    sports_signals: Optional[list[dict]] = None,
    poly_signals: Optional[list[dict]] = None,
) -> dict:
    """
    Evalúa Sports y Polymarket por separado sobre las señales realmente enviadas.

    Returns:
    {
      "groups": {"sports": {...}, "polymarket": {...}},
         cada grupo: status ("SALUDABLE"|"DEGRADADO"|"CRITICO"|"SIN MUESTRA"|"ERROR"),
                     n, win_rate_last_20, expected_win_rate, degraded
      "degraded": bool  (algún grupo degradado),
      "checked_at": datetime
    }
    """
    loaders = {"sports": (sports_signals, _sports_alerted_graded),
               "polymarket": (poly_signals, _poly_alerted_graded)}
    groups = {}
    for name, (signals, loader) in loaders.items():
        try:
            groups[name] = _evaluate_group(signals if signals is not None else loader())
        except Exception as e:
            logger.error("check_model_health(%s): error — %s", name, e)
            groups[name] = {"status": "ERROR", "n": 0, "win_rate_last_20": 0.0,
                            "expected_win_rate": None, "degraded": False}
        logger.info(
            "check_model_health(%s): status=%s n=%d win_rate=%.0f%%",
            name, groups[name]["status"], groups[name]["n"],
            groups[name]["win_rate_last_20"] * 100,
        )

    result = {
        "groups": groups,
        "degraded": any(g["degraded"] for g in groups.values()),
        "checked_at": _now_utc(),
    }
    try:
        col("model_weights").document("health_check").set(result)
    except Exception as e:
        logger.error("check_model_health: error guardando health_check: %s", e)
    return result


def format_health_alert(group_name: str, group: dict) -> str:
    """Aviso Telegram de un grupo degradado. Sin acción automática."""
    label = _GROUP_LABELS.get(group_name, group_name)
    lines = [
        f"⚠️ AVISO MODELO — {label} (sin acción automática)",
        f"Acierto últimas {group.get('n', 0)} señales enviadas: "
        f"{float(group.get('win_rate_last_20') or 0):.0%} ({group.get('status')})",
    ]
    expected = group.get("expected_win_rate")
    if expected is not None:
        lines.append(f"Acierto esperado por cuota de esas señales: {expected:.0%}")
    lines.append("No se cambia ningún umbral ni se suspende nada: solo informativo.")
    return "\n".join(lines)


def format_daily_report(
    health: dict,
    shadow_metrics: dict,
    top_signal: Optional[dict] = None,
    pred_stats: Optional[dict] = None,
    tier_stats: Optional[dict] = None,
    conf_calibration: Optional[dict] = None,
) -> str:
    """
    Formato reporte diario matutino.
    pred_stats: {total, pending, resolved, correct, incorrect, synthetic, real_odds}.
    tier_stats: {fuerte: [...], detectada: [...], moderada: [...]}.
    conf_calibration: {bucket: {count, correct, rate}} de model_weights.accuracy_by_confidence.
    Estado del modelo basado en win_rate real (shadow_trades):
      > 50% → ✅ SALUDABLE  |  40-50% → ⚠️ ATENCIÓN  |  < 40% → 🔴 REVISAR
    """
    now = datetime.now(timezone.utc)
    fecha = now.strftime("%d/%m/%Y")

    bankroll = float(shadow_metrics.get("current_bankroll") or 50.0)
    avg_clv = float(shadow_metrics.get("avg_clv") or 0.0)
    roi_total = float(shadow_metrics.get("roi_total") or 0.0)
    win_rate = float(shadow_metrics.get("win_rate") or 0.0)
    closed_trades = int(shadow_metrics.get("closed_trades") or 0)
    roi_sports = float(shadow_metrics.get("roi_sports") or 0.0)
    # Desglose sports vs polymarket (mismo P&L, separado por source en shadow_trades)
    roi_poly = float(shadow_metrics.get("roi_poly") or 0.0)
    wr_sports = float(shadow_metrics.get("win_rate_sports") or 0.0)
    wr_poly = float(shadow_metrics.get("win_rate_poly") or 0.0)
    n_sports = int(shadow_metrics.get("n_sports") or 0)
    n_poly = int(shadow_metrics.get("n_poly") or 0)
    pnl_sports = float(shadow_metrics.get("pnl_sports") or 0.0)
    pnl_poly = float(shadow_metrics.get("pnl_poly") or 0.0)
    # Polymarket "emitido real" (solo señales realmente alertadas) vs ledger crudo
    roi_poly_alerted = float(shadow_metrics.get("roi_poly_alerted") or 0.0)
    wr_poly_alerted = float(shadow_metrics.get("win_rate_poly_alerted") or 0.0)
    n_poly_alerted = int(shadow_metrics.get("n_poly_alerted") or 0)
    pending_poly_alerted = int(shadow_metrics.get("pending_poly_alerted") or 0)

    # Estado del modelo basado en win rate real (no en pesos internos)
    if closed_trades < 10:
        model_status = "SIN DATOS"
        status_emoji = "⏳"
    elif win_rate > 0.50:
        model_status = "SALUDABLE"
        status_emoji = "✅"
    elif win_rate >= 0.40:
        model_status = "ATENCIÓN"
        status_emoji = "⚠️"
    else:
        model_status = "REVISAR"
        status_emoji = "🔴"

    lines = [f"📊 RESUMEN DEL DIA — {fecha}", ""]

    # Bloque de señales deportivas con desglose sintéticas vs cuotas reales
    if pred_stats and pred_stats.get("total", 0) > 0:
        total = pred_stats["total"]
        pending = pred_stats["pending"]
        resolved = pred_stats["resolved"]
        correct = pred_stats["correct"]
        incorrect = pred_stats["incorrect"]
        synthetic = pred_stats.get("synthetic", 0)
        real_odds = pred_stats.get("real_odds", 0)

        lines.append(f"📋 Señales sports: {total} total")
        if synthetic > 0 or real_odds > 0:
            lines.append(f"  · Con cuotas reales: {real_odds} | Sintéticas Poisson: {synthetic}")
        if resolved > 0:
            lines.append(f"✅ Resueltas: {resolved} ({correct} correctas · {incorrect} falladas)")
        if pending > 0:
            lines.append(f"⏳ Pendientes: {pending}")
    else:
        pending_fb = int(shadow_metrics.get("pending_trades") or 0)
        closed_fb = int(shadow_metrics.get("closed_trades") or 0)
        wins_fb = int(shadow_metrics.get("wins") or 0)
        losses_fb = int(shadow_metrics.get("losses") or 0)
        lines.append(f"⏳ Pendientes: {pending_fb}")
        if closed_fb > 0:
            lines.append(f"✅ Resueltas: {closed_fb} ({wins_fb} correctas · {losses_fb} falladas)")

    lines.append("")

    # P&L simulado con Kelly — desglose de aportación por subsistema
    pnl_simulated = round((bankroll - 50.0), 2)
    pnl_sign = "+" if pnl_simulated >= 0 else ""
    lines.append(f"💰 Bankroll virtual: {bankroll:.2f}€ ({pnl_sign}{pnl_simulated:.2f}€ P&L)")
    lines.append(f"  ⚽ Sports: {pnl_sports:+.2f}€ | 🔮 Polymarket: {pnl_poly:+.2f}€")
    lines.append(f"📈 ROI total: {roi_total:+.1%}")
    lines.append(f"  ⚽ Sports: {roi_sports:+.1%} (win rate {wr_sports:.0%}, {n_sports} señales)")
    lines.append(f"  🔮 Polymarket — ledger crudo: {roi_poly:+.1%} (win rate {wr_poly:.0%}, {n_poly} señales)")
    lines.append(
        f"  🔮 Polymarket — emitido real (alertado, 1u/alerta): {roi_poly_alerted:+.1%} "
        f"(win rate {wr_poly_alerted:.0%}, {n_poly_alerted} resueltas, {pending_poly_alerted} pendientes)"
    )

    if avg_clv != 0.0:
        lines.append(f"📐 CLV medio: {avg_clv:+.1%}")

    lines.append("")

    # Estado del modelo (win rate real)
    if closed_trades >= 10:
        lines.append(f"🤖 Modelo: {status_emoji} {model_status} — Win rate: {win_rate:.0%} ({closed_trades} cerrados)")
    else:
        lines.append(f"🤖 Modelo: {status_emoji} {model_status} (< 10 señales cerradas)")

    if top_signal is not None:
        unified_score = top_signal.get("unified_score") or top_signal.get("edge")
        if unified_score is not None:
            market = top_signal.get("market_type") or top_signal.get("market") or top_signal.get("question", "")[:40]
            score_str = f"{unified_score:.0f}/100" if isinstance(unified_score, (int, float)) and unified_score > 1 else f"{float(unified_score):.0%}"
            lines.append(f"\n🏆 Top señal hoy: {market} — Score: {score_str}")

    # Desglose por tiers — formato compacto: contadores + métricas de resueltas
    if tier_stats:
        fuerte    = tier_stats.get("fuerte", [])
        detectada = tier_stats.get("detectada", [])
        moderada  = tier_stats.get("moderada", [])

        def _tier_line(emoji: str, label: str, signals: list) -> str:
            n = len(signals)
            resolved = [p for p in signals if p.get("correct") is not None]
            wins = [p for p in resolved if p.get("correct") is True]
            if resolved:
                wr = len(wins) / len(resolved)
                roi = sum(
                    (float(p.get("odds", 2.0)) - 1.0) if p.get("correct") else -1.0
                    for p in resolved
                ) / len(resolved)
                return f"{emoji} {label}: {n} | Win rate: {wr:.0%} | ROI: {roi:+.0%}"
            return f"{emoji} {label}: {n}"

        total_signals = len(fuerte) + len(detectada) + len(moderada)
        if total_signals > 0:
            lines.append("")
            lines.append(_tier_line("🔥", "Fuertes",    fuerte))
            lines.append(_tier_line("✅", "Detectadas", detectada))
            lines.append(_tier_line("📊", "Moderadas",  moderada))
            wr_str  = f" | Win rate: {win_rate:.0%}" if closed_trades >= 5 else ""
            roi_str = f" | ROI: {roi_sports:+.0%}" if closed_trades >= 5 else ""
            lines.append(f"📈 Total: {total_signals} señales{wr_str}{roi_str}")

    # Tabla de calibración de confianza
    if conf_calibration:
        _bkt_labels = {
            "65_70": "65-70%",
            "70_80": "70-80%",
            "80_90": "80-90%",
            "90_99": "90-99%",
        }
        _calib_rows = []
        for _bkt, _label in _bkt_labels.items():
            _d = conf_calibration.get(_bkt, {})
            _rate = _d.get("rate")
            _cnt = int(_d.get("count", 0))
            if _cnt >= 5 and _rate is not None:
                _flag = " ⚠️" if _rate < 0.40 else ""
                _calib_rows.append(f"  {_label}: {_rate:.0%} win ({_cnt} señales){_flag}")
        if _calib_rows:
            lines.append("")
            lines.append("🎯 Calibración histórica de confianza:")
            lines.extend(_calib_rows)

    return "\n".join(lines)
