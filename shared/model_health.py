"""
Model health monitor — detecta degradacion y auto-ajusta thresholds.
"""
import logging
from datetime import datetime, timezone
from statistics import mean
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col

logger = logging.getLogger(__name__)

_DEFAULT_SPORTS_MIN_EDGE = 0.08


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def check_model_health(trades: Optional[list[dict]] = None) -> dict:
    """
    Analiza ultimas 20 senales cerradas y evalua salud del modelo.

    Si trades=None: leer col("shadow_trades") filtrado por result in [win,loss],
    ordenado por closed_at DESC, limit 20.

    Returns:
    {
      status: "SALUDABLE"|"DEGRADADO"|"CRITICO",
      win_rate_last_20: float,
      recommended_edge: float,
      blacklisted_leagues: list,
      degraded: bool,
      message: str
    }
    """
    try:
        # Guard: sin muestra suficiente no ajustar thresholds
        try:
            weights_doc = col("model_weights").document("current").get()
            data = weights_doc.to_dict() if weights_doc.exists else {}
            total_closed = int(data.get("total_predictions", 0))
        except Exception as e:
            logger.warning("check_model_health: no se pudo leer total_predictions — %s", e)
            total_closed = 0

        if total_closed < 20:
            logger.info("check_model_health: omitido — %d/20 trades mínimos", total_closed)
            return {
                "status": "skipped",
                "win_rate_last_20": 0.0,
                "recommended_edge": _DEFAULT_SPORTS_MIN_EDGE,
                "blacklisted_leagues": [],
                "degraded": False,
                "message": f"Health check omitido: {total_closed}/20 trades cerrados mínimos.",
                "checked_at": _now_utc(),
            }

        if trades is None:
            try:
                docs = (
                    col("shadow_trades")
                    .where(filter=FieldFilter("result", "in", ["win", "loss"]))
                    .order_by("closed_at", direction="DESCENDING")
                    .limit(20)
                    .stream()
                )
                trades = [d.to_dict() for d in docs]
            except Exception as e:
                logger.error("check_model_health: error leyendo shadow_trades: %s", e)
                trades = []

        # Filtrar solo trades cerrados con resultado valido
        closed = [t for t in trades if t.get("result") in ("win", "loss")]
        wins = [t for t in closed if t.get("result") == "win"]

        win_rate = round(len(wins) / len(closed), 4) if closed else 0.0

        # Sin suficiente muestra estadistica: no ajustar threshold
        if len(closed) < 20:
            result = {
                "status": "SALUDABLE",
                "win_rate_last_20": win_rate,
                "recommended_edge": _DEFAULT_SPORTS_MIN_EDGE,
                "blacklisted_leagues": [],
                "degraded": False,
                "message": f"Muestra insuficiente ({len(closed)} trades cerrados < 20). Threshold fijo en {_DEFAULT_SPORTS_MIN_EDGE:.0%}.",
                "checked_at": _now_utc(),
            }
            try:
                col("model_weights").document("health_check").set(result)
            except Exception as e:
                logger.error("check_model_health: error guardando health_check: %s", e)
            logger.info("check_model_health: muestra insuficiente (%d < 20), threshold=%.1f%%", len(closed), _DEFAULT_SPORTS_MIN_EDGE * 100)
            return result

        recommended_edge = _DEFAULT_SPORTS_MIN_EDGE

        # Determinar status y ajustar threshold
        if win_rate < 0.35:
            status = "CRITICO"
            recommended_edge = round(_DEFAULT_SPORTS_MIN_EDGE * 1.4, 4)
            degraded = True
        elif win_rate < 0.45:
            status = "DEGRADADO"
            recommended_edge = round(_DEFAULT_SPORTS_MIN_EDGE * 1.2, 4)
            degraded = True
        elif win_rate > 0.65:
            status = "SALUDABLE"
            recommended_edge = round(_DEFAULT_SPORTS_MIN_EDGE * 0.95, 4)
            degraded = False
        else:
            status = "SALUDABLE"
            recommended_edge = _DEFAULT_SPORTS_MIN_EDGE
            degraded = False

        # Blacklist por liga: win_rate < 40% en >= 10 senales
        league_stats: dict[str, dict] = {}
        for t in closed:
            league = t.get("category") or t.get("league") or "unknown"
            if league not in league_stats:
                league_stats[league] = {"wins": 0, "n": 0}
            league_stats[league]["n"] += 1
            if t.get("result") == "win":
                league_stats[league]["wins"] += 1

        blacklisted_leagues = []
        for league, stats in league_stats.items():
            if stats["n"] >= 10:
                league_wr = stats["wins"] / stats["n"]
                if league_wr < 0.40:
                    blacklisted_leagues.append(league)

        message = (
            f"Modelo {status} — Win rate ultimas {len(closed)} senales: {win_rate:.0%}. "
            f"Edge recomendado: {recommended_edge:.1%}."
        )
        if blacklisted_leagues:
            message += f" Ligas en blacklist: {', '.join(blacklisted_leagues)}."

        result = {
            "status": status,
            "win_rate_last_20": win_rate,
            "recommended_edge": recommended_edge,
            "blacklisted_leagues": blacklisted_leagues,
            "degraded": degraded,
            "message": message,
            "checked_at": _now_utc(),
        }

        # Guardar en Firestore
        try:
            col("model_weights").document("health_check").set(result)
        except Exception as e:
            logger.error("check_model_health: error guardando health_check: %s", e)

        logger.info(
            "check_model_health: status=%s win_rate=%.0f%% edge=%.1f%%",
            status, win_rate * 100, recommended_edge * 100,
        )
        return result

    except Exception as e:
        logger.error("check_model_health: error general: %s", e)
        return {
            "status": "SALUDABLE",
            "win_rate_last_20": 0.0,
            "recommended_edge": _DEFAULT_SPORTS_MIN_EDGE,
            "blacklisted_leagues": [],
            "degraded": False,
            "message": "Error al evaluar salud del modelo.",
            "checked_at": _now_utc(),
        }


def format_health_alert(health: dict) -> str:
    """
    Formato Telegram para alerta de degradacion.
    """
    win_rate = float(health.get("win_rate_last_20") or 0)
    recommended_edge = float(health.get("recommended_edge") or _DEFAULT_SPORTS_MIN_EDGE)
    degraded = health.get("degraded", False)
    blacklisted = health.get("blacklisted_leagues") or []

    action_word = "subido" if degraded else "ajustado"
    lines = [
        "🚨 ALERTA MODELO",
        f"Win rate ultimas 20 senales: {win_rate:.0%}",
        f"Accion: threshold {action_word} a {recommended_edge:.1%}",
    ]

    # Obtener wins/n para cada liga en blacklist si estan disponibles en el health dict
    for league in blacklisted:
        lines.append(f"Liga suspendida: {league}")

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

    # P&L simulado con Kelly
    pnl_simulated = round((bankroll - 50.0), 2)
    pnl_sign = "+" if pnl_simulated >= 0 else ""
    lines.append(f"💰 Bankroll virtual: {bankroll:.2f}€ ({pnl_sign}{pnl_simulated:.2f}€ P&L)")
    lines.append(f"📈 ROI total: {roi_total:+.1%} | ROI sports: {roi_sports:+.1%}")

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
