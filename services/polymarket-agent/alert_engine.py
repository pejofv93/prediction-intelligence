"""
Motor de alertas Polymarket → telegram-bot.
Envia alerta si abs(edge) > POLY_MIN_EDGE + confianza > POLY_MIN_CONFIDENCE.
edge positivo → BUY_YES (mercado subvalorado).
edge negativo → BUY_NO (mercado sobrevalorado).
volume_spike y smart_money son señales extra (no requisito).
"""
import logging

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE, TELEGRAM_BOT_URL

logger = logging.getLogger(__name__)


async def check_and_alert(analysis: dict) -> bool:
    """
    Envia alerta Telegram si:
      abs(edge) > POLY_MIN_EDGE (0.08)
      confidence > POLY_MIN_CONFIDENCE (0.65)
    edge positivo → BUY_YES; edge negativo → BUY_NO.
    volume_spike y smart_money son señales bonus incluidas en el mensaje, no requisito.
    Verifica en alerts_sent que no se haya enviado ya.
    NO usa on_snapshot — llama directamente POST {TELEGRAM_BOT_URL}/send-alert.
      Body: {"type": "polymarket", "data": analysis}
      Header: x-cloud-token
      Si falla el POST → loggear y continuar (no bloquear el pipeline)
    Devuelve True si envio alerta.
    """
    import os
    import httpx
    from datetime import datetime, timezone
    from shared.firestore_client import col

    edge = float(analysis.get("edge", 0.0))
    confidence = float(analysis.get("confidence", 0.0))
    volume_spike = bool(analysis.get("volume_spike", False))
    smart_money = bool(analysis.get("smart_money_detected", False))

    if abs(edge) <= POLY_MIN_EDGE:
        logger.debug(
            "check_and_alert(%s): abs(edge)=%.3f <= %.3f — omitida",
            analysis.get("market_id"), abs(edge), POLY_MIN_EDGE,
        )
        return False
    if confidence < POLY_MIN_CONFIDENCE:
        logger.debug(
            "check_and_alert(%s): conf=%.3f < %.3f — omitida",
            analysis.get("market_id"), confidence, POLY_MIN_CONFIDENCE,
        )
        return False

    logger.info(
        "check_and_alert(%s): pasa thresholds edge=%.3f conf=%.2f vol_spike=%s sm=%s",
        analysis.get("market_id"), edge, confidence, volume_spike, smart_money,
    )

    market_id = analysis.get("market_id", "unknown")
    current_price = float(analysis.get("market_price_yes", 0.5))
    alert_key = f"{market_id}_{round(edge, 2)}"

    # Re-alerta permitida si >24h desde la última Y precio cambió >5%
    try:
        from datetime import timedelta
        _cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        _existing = list(
            col("alerts_sent")
            .where("market_id", "==", market_id)
            .order_by("sent_at", direction="DESCENDING")
            .limit(1)
            .stream(timeout=10.0)
        )
        if _existing:
            _last = _existing[0].to_dict()
            _last_sent = _last.get("sent_at")
            if _last_sent and hasattr(_last_sent, "tzinfo") and _last_sent.tzinfo is None:
                _last_sent = _last_sent.replace(tzinfo=timezone.utc)
            _last_price = float(_last.get("last_price", current_price))
            _price_chg = abs(current_price - _last_price) / max(_last_price, 0.001)
            if _last_sent and _last_sent > _cutoff_24h:
                logger.debug("check_and_alert(%s): alerta reciente (<24h) omitida", market_id)
                return False
            if _price_chg <= 0.05:
                logger.debug(
                    "check_and_alert(%s): precio sin cambio significativo (%.1f%%) — omitida",
                    market_id, _price_chg * 100,
                )
                return False
            logger.info(
                "check_and_alert(%s): re-alerta permitida — >24h y precio cambió %.1f%%",
                market_id, _price_chg * 100,
            )
    except Exception:
        logger.error("check_and_alert(%s): error comprobando dedup", market_id, exc_info=True)

    # Enviar alerta al bot de Telegram
    if not TELEGRAM_BOT_URL:
        logger.warning("check_and_alert: TELEGRAM_BOT_URL no configurada — alerta no enviada")
        return False

    cloud_run_token = os.environ.get("CLOUD_RUN_TOKEN", "")

    # Serializar a JSON-safe: convertir datetime a ISO string
    def _to_json_safe(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: _to_json_safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_to_json_safe(i) for i in obj]
        return obj

    analysis_safe = _to_json_safe(analysis)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{TELEGRAM_BOT_URL}/send-alert",
                json={"type": "polymarket", "data": analysis_safe},
                headers={"x-cloud-token": cloud_run_token},
            )
        if resp.status_code not in (200, 201, 202):
            logger.error(
                "check_and_alert(%s): telegram-bot respondio %d",
                market_id, resp.status_code,
            )
            return False
    except Exception:
        logger.error("check_and_alert(%s): error enviando alerta", market_id, exc_info=True)
        return False

    # Guardar dedup record DESPUÉS del POST exitoso
    try:
        col("alerts_sent").add({
            "alert_key": alert_key,
            "market_id": market_id,
            "last_price": current_price,
            "sent_at": datetime.now(timezone.utc),
            "type": "polymarket",
        })
    except Exception:
        logger.error(
            "check_and_alert(%s): error guardando dedup en alerts_sent",
            market_id, exc_info=True,
        )

    # Marcar como alertado en poly_predictions
    try:
        col("poly_predictions").document(market_id).update({"alerted": True})
    except Exception:
        logger.error(
            "check_and_alert(%s): error actualizando alerted en poly_predictions",
            market_id, exc_info=True,
        )

    logger.info(
        "check_and_alert(%s): alerta enviada — edge=%.3f conf=%.2f vol_spike=%s sm=%s",
        market_id, edge, confidence, volume_spike, smart_money,
    )
    return True
