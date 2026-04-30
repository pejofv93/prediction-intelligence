"""
Motor de alertas Polymarket → telegram-bot.
Envia alerta si abs(edge) > min_edge_for_direction + confianza > min_conf_for_direction.
Los umbrales se cargan desde poly_model_weights/current (ajustados por poly_learning_engine).
edge positivo → BUY_YES; edge negativo → BUY_NO.
volume_spike y smart_money son señales bonus (no requisito).
"""
import logging

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE, TELEGRAM_BOT_URL

logger = logging.getLogger(__name__)

# Cache de umbrales por dirección — cargado una vez al arrancar el servicio
_LEARNED_THRESHOLDS: dict | None = None

# Umbrales base usados cuando hay < 20 outcomes resueltos
_BASE_THRESHOLDS = {
    "buy_yes_min_edge": 0.08,
    "buy_yes_min_confidence": POLY_MIN_CONFIDENCE,
    "buy_no_min_edge": 0.07,
    "buy_no_min_confidence": POLY_MIN_CONFIDENCE,
}
_MIN_OUTCOMES_FOR_LEARNED = 20


def _load_learned_thresholds() -> dict:
    """
    Lee poly_model_weights/current desde Firestore.
    Aplica umbrales aprendidos solo si hay >= 20 outcomes resueltos;
    si no, usa umbrales base más permisivos para no bloquear señales tempranas.
    """
    global _LEARNED_THRESHOLDS
    if _LEARNED_THRESHOLDS is not None:
        return _LEARNED_THRESHOLDS

    try:
        from shared.firestore_client import col
        doc = col("poly_model_weights").document("current").get()
        if doc.exists:
            data = doc.to_dict()
            sample_size = int(data.get("sample_size", 0))
            if sample_size < _MIN_OUTCOMES_FOR_LEARNED:
                logger.info(
                    "_load_learned_thresholds: umbral_mode=base (%d outcomes < %d) "
                    "BUY_YES(edge=%.2f conf=%.2f) BUY_NO(edge=%.2f conf=%.2f)",
                    sample_size, _MIN_OUTCOMES_FOR_LEARNED,
                    _BASE_THRESHOLDS["buy_yes_min_edge"], _BASE_THRESHOLDS["buy_yes_min_confidence"],
                    _BASE_THRESHOLDS["buy_no_min_edge"], _BASE_THRESHOLDS["buy_no_min_confidence"],
                )
                _LEARNED_THRESHOLDS = dict(_BASE_THRESHOLDS)
                return _LEARNED_THRESHOLDS
            loaded = {
                "buy_yes_min_edge":        float(data.get("buy_yes_min_edge", POLY_MIN_EDGE)),
                "buy_yes_min_confidence":  float(data.get("buy_yes_min_confidence", POLY_MIN_CONFIDENCE)),
                "buy_no_min_edge":         float(data.get("buy_no_min_edge", POLY_MIN_EDGE)),
                "buy_no_min_confidence":   float(data.get("buy_no_min_confidence", POLY_MIN_CONFIDENCE)),
            }
            _LEARNED_THRESHOLDS = loaded
            logger.info(
                "_load_learned_thresholds: umbral_mode=learned (%d outcomes) v%s "
                "BUY_YES(edge=%.3f conf=%.2f) BUY_NO(edge=%.3f conf=%.2f)",
                sample_size, data.get("version", "?"),
                loaded["buy_yes_min_edge"], loaded["buy_yes_min_confidence"],
                loaded["buy_no_min_edge"],  loaded["buy_no_min_confidence"],
            )
            return loaded
    except Exception:
        logger.warning("_load_learned_thresholds: error leyendo Firestore — usando base", exc_info=True)

    _LEARNED_THRESHOLDS = dict(_BASE_THRESHOLDS)
    return _LEARNED_THRESHOLDS


async def check_and_alert(analysis: dict) -> bool:
    """
    Envía alerta Telegram si los umbrales aprendidos se cumplen.
    Deduplicación atómica via transacción Firestore sobre doc de ID determinista
    (alert_key), eliminando la race condition read-then-write.

    Flujo:
      1. Validar thresholds (edge / confidence por dirección).
      2. Transacción Firestore: check-and-claim en un solo op atómico.
         Si la transacción falla (contention: otro request ya reclamó), no enviar.
      3. POST Telegram.
      4. Marcar doc dedup como "sent" y actualizar poly_predictions.alerted.
    """
    import os
    import httpx
    from datetime import datetime, timedelta, timezone
    from google.cloud import firestore as _firestore
    from shared.firestore_client import col, get_client

    rec = str(analysis.get("recommendation", "")).upper()
    if rec not in ("BUY_YES", "BUY_NO"):
        logger.debug(
            "check_and_alert(%s): rec=%s — solo alertamos BUY_YES/BUY_NO",
            analysis.get("market_id"), rec,
        )
        return False

    edge = float(analysis.get("edge", 0.0))
    confidence = float(analysis.get("confidence", 0.0))
    volume_spike = bool(analysis.get("volume_spike", False))
    smart_money = bool(analysis.get("smart_money_detected", False))

    # --- Umbrales aprendidos por dirección ---
    thresholds = _load_learned_thresholds()
    direction = "BUY_YES" if edge >= 0 else "BUY_NO"
    dir_key = "buy_yes" if direction == "BUY_YES" else "buy_no"
    effective_min_edge = thresholds[f"{dir_key}_min_edge"]
    effective_min_conf = thresholds[f"{dir_key}_min_confidence"]

    if abs(edge) < effective_min_edge:
        logger.debug(
            "check_and_alert(%s): abs(edge)=%.3f < %.3f (%s learned) — omitida",
            analysis.get("market_id"), abs(edge), effective_min_edge, direction,
        )
        return False
    if confidence < effective_min_conf:
        logger.debug(
            "check_and_alert(%s): conf=%.3f < %.3f (%s learned) — omitida",
            analysis.get("market_id"), confidence, effective_min_conf, direction,
        )
        return False

    market_id = str(analysis.get("market_id") or "")
    if not market_id:
        logger.warning("check_and_alert: market_id vacío — alerta omitida sin guardar dedup")
        return False

    logger.info(
        "check_and_alert(%s): pasa thresholds %s edge=%.3f>=%.3f conf=%.2f>=%.2f vol_spike=%s sm=%s",
        market_id, direction,
        abs(edge), effective_min_edge, confidence, effective_min_conf,
        volume_spike, smart_money,
    )

    current_price = float(analysis.get("market_price_yes", 0.5))
    # ID determinista: misma señal (mismo mercado + misma magnitud de edge) = mismo doc.
    alert_key = f"{market_id}_{round(edge, 2)}"
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    db = get_client()
    dedup_ref = col("alerts_sent").document(alert_key)

    # --- Transacción atómica: check-and-claim ---
    # Si dos requests concurrentes llegan aquí simultáneamente, solo uno ganará.
    # El perdedor recibe Aborted/contention y no envía.
    @_firestore.transactional
    def _claim(transaction: _firestore.Transaction) -> bool:
        snap = dedup_ref.get(transaction=transaction)
        if snap.exists:
            data = snap.to_dict()
            sent_at = data.get("sent_at")
            if sent_at and hasattr(sent_at, "tzinfo") and sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            last_price = float(data.get("last_price", current_price))
            price_chg = abs(current_price - last_price) / max(last_price, 0.001)

            if sent_at and sent_at > cutoff_24h:
                logger.debug("check_and_alert(%s): alerta reciente (<24h) omitida [tx]", market_id)
                return False
            if price_chg <= 0.05:
                logger.debug(
                    "check_and_alert(%s): precio sin cambio (%.1f%%) omitida [tx]",
                    market_id, price_chg * 100,
                )
                return False
            logger.info(
                "check_and_alert(%s): re-alerta permitida — >24h y precio cambió %.1f%% [tx]",
                market_id, price_chg * 100,
            )

        # Reclamar el slot atómicamente — status="pending" hasta confirmar el POST
        transaction.set(dedup_ref, {
            "alert_key": alert_key,
            "market_id": market_id,
            "last_price": current_price,
            "sent_at": now,
            "type": "polymarket",
            "status": "pending",
        })
        return True

    try:
        should_send = _claim(db.transaction())
    except Exception:
        logger.error(
            "check_and_alert(%s): transacción dedup falló (contention o error) — omitida",
            market_id, exc_info=True,
        )
        return False

    if not should_send:
        return False

    # --- POST Telegram ---
    if not TELEGRAM_BOT_URL:
        logger.warning("check_and_alert: TELEGRAM_BOT_URL no configurada — alerta no enviada")
        # Revertir claim para no bloquear futuros intentos
        try:
            dedup_ref.delete()
        except Exception:
            pass
        return False

    cloud_run_token = os.environ.get("CLOUD_RUN_TOKEN", "")

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
            # Revertir claim para que el error no bloquee futuros reintentos
            try:
                dedup_ref.delete()
            except Exception:
                pass
            return False
    except Exception:
        logger.error("check_and_alert(%s): error enviando alerta", market_id, exc_info=True)
        try:
            dedup_ref.delete()
        except Exception:
            pass
        return False

    # --- Confirmar: status pending → sent ---
    try:
        dedup_ref.update({"status": "sent"})
    except Exception:
        logger.warning(
            "check_and_alert(%s): no se pudo marcar status=sent (no crítico)", market_id,
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
