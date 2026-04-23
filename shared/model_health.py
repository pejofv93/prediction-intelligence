"""
Model health monitor — detecta degradacion y auto-ajusta thresholds.
"""
import logging
from datetime import datetime, timezone
from statistics import mean
from typing import Optional

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
        if trades is None:
            try:
                docs = (
                    col("shadow_trades")
                    .where("result", "in", ["win", "loss"])
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
) -> str:
    """
    Formato reporte diario matutino.
    """
    now = datetime.now(timezone.utc)
    fecha = now.strftime("%d/%m/%Y")

    status = health.get("status", "SALUDABLE")
    if status == "SALUDABLE":
        status_emoji = "✅"
    elif status == "DEGRADADO":
        status_emoji = "⚠️"
    else:
        status_emoji = "🚨"

    pending = int(shadow_metrics.get("pending_trades") or shadow_metrics.get("pending") or 0)
    bankroll = float(shadow_metrics.get("current_bankroll") or 50.0)
    avg_clv = float(shadow_metrics.get("avg_clv") or 0.0)
    roi_total = float(shadow_metrics.get("roi_total") or 0.0)
    win_rate = float(shadow_metrics.get("win_rate") or 0.0)

    lines = [
        f"📊 RESUMEN DEL DIA — {fecha}",
        "",
        f"Senales activas: {pending} pendientes",
        f"Bankroll virtual: {bankroll:.2f}€",
    ]

    if avg_clv != 0.0:
        lines.append(f"CLV medio: {avg_clv:+.1%}")

    lines.append(f"Modelo: {status_emoji} {status}")

    if top_signal is not None:
        unified_score = top_signal.get("unified_score") or top_signal.get("edge")
        if unified_score is not None:
            market = top_signal.get("market_type") or top_signal.get("market") or top_signal.get("question", "")[:40]
            score_str = f"{unified_score:.0f}/100" if isinstance(unified_score, (int, float)) and unified_score > 1 else f"{float(unified_score):.0%}"
            lines.append(f"\n🏆 Top senal hoy: {market} — Score: {score_str}")

    lines += [
        "",
        f"📈 ROI total: {roi_total:+.1%}",
        f"🎯 Win rate: {win_rate:.0%}",
    ]

    return "\n".join(lines)
