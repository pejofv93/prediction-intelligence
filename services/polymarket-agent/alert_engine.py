"""
Motor de alertas Polymarket → telegram-bot.
Envia alerta si abs(edge) > min_edge_for_direction + confianza > min_conf_for_direction.
Los umbrales se cargan desde poly_model_weights/current (ajustados por poly_learning_engine).
edge positivo → BUY_YES; edge negativo → BUY_NO.
volume_spike y smart_money son señales bonus (no requisito).
"""
import logging

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import POLY_MIN_CONFIDENCE, POLY_MIN_EDGE, TELEGRAM_BOT_URL

logger = logging.getLogger(__name__)

# Cache de umbrales por dirección — cargado una vez al arrancar el servicio
_LEARNED_THRESHOLDS: dict | None = None

# Umbrales base usados cuando hay < 20 outcomes resueltos
_BASE_THRESHOLDS = {
    "buy_yes_min_edge": 0.10,
    "buy_yes_min_confidence": 0.60,
    "buy_no_min_edge": 0.08,
    "buy_no_min_confidence": 0.55,
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
            accuracy = float(data.get("accuracy_overall", 0))
            if accuracy < 0.50:
                # Accuracy below chance — learned thresholds are unreliable, use base
                logger.warning(
                    "_load_learned_thresholds: accuracy=%.1f%% < 50%% (%d outcomes) — "
                    "umbrales aprendidos ignorados, usando BASE para no bloquear todas las señales",
                    accuracy * 100, sample_size,
                )
                _LEARNED_THRESHOLDS = dict(_BASE_THRESHOLDS)
                return _LEARNED_THRESHOLDS

            # Cap: learned thresholds cannot exceed these maximums (Groq never reaches >0.85)
            _MAX_THRESHOLDS = {
                "buy_yes_min_edge": 0.20,
                "buy_yes_min_confidence": 0.75,
                "buy_no_min_edge": 0.18,
                "buy_no_min_confidence": 0.72,
            }
            loaded = {}
            for key in ("buy_yes_min_edge", "buy_yes_min_confidence",
                        "buy_no_min_edge", "buy_no_min_confidence"):
                raw = float(data.get(key) or _BASE_THRESHOLDS[key])
                capped = min(raw, _MAX_THRESHOLDS[key])
                if capped < raw:
                    logger.warning(
                        "_load_learned_thresholds: %s=%.3f capeado a %.3f",
                        key, raw, capped,
                    )
                loaded[key] = capped
            _LEARNED_THRESHOLDS = loaded
            logger.info(
                "_load_learned_thresholds: umbral_mode=learned (%d outcomes, acc=%.1f%%) v%s "
                "BUY_YES(edge=%.3f conf=%.2f) BUY_NO(edge=%.3f conf=%.2f)",
                sample_size, accuracy * 100, data.get("version", "?"),
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
        logger.info(
            "check_and_alert(%s): SKIP_REC rec=%s edge=%.3f conf=%.2f — solo alertamos BUY_YES/BUY_NO",
            analysis.get("market_id"), rec,
            float(analysis.get("edge", 0)), float(analysis.get("confidence", 0)),
        )
        return False

    # MEJORA 5: descartar mercados que cierran en más de 30 días
    end_date_iso = analysis.get("end_date_iso")
    if end_date_iso:
        try:
            _end_dt = datetime.fromisoformat(str(end_date_iso))
            if _end_dt.tzinfo is None:
                _end_dt = _end_dt.replace(tzinfo=timezone.utc)
            _days_left = (_end_dt - datetime.now(timezone.utc)).days
            if _days_left > 60:
                logger.info(
                    "check_and_alert(%s): cierra en %dd > 60d — omitida",
                    analysis.get("market_id"), _days_left,
                )
                return False
        except Exception:
            pass

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

    # Ajuste dinámico por accuracy de categoría
    _category = str(analysis.get("category") or "")
    if _category:
        try:
            _wdoc = col("poly_model_weights").document("current").get()
            if _wdoc.exists:
                _by_cat = _wdoc.to_dict().get("by_category", {})
                _cs = _by_cat.get(_category, {})
                _cn = int(_cs.get("total", 0))
                _ca = float(_cs.get("accuracy", 0))
                if _cn >= 5:
                    if _ca < 0.50:
                        effective_min_edge = round(effective_min_edge + 0.03, 4)
                        logger.debug(
                            "check_and_alert(%s): cat=%s acc=%.0f%% n=%d → +3%% edge (bajo rendimiento)",
                            analysis.get("market_id"), _category, _ca * 100, _cn,
                        )
                    elif _ca > 0.70:
                        effective_min_edge = round(max(0.05, effective_min_edge - 0.02), 4)
                        logger.debug(
                            "check_and_alert(%s): cat=%s acc=%.0f%% n=%d → -2%% edge (alto rendimiento)",
                            analysis.get("market_id"), _category, _ca * 100, _cn,
                        )
        except Exception:
            pass

    # Volume spike: movimiento brusco indica información nueva → umbrales más permisivos
    _SPIKE_MAX_EDGE = 0.05
    _SPIKE_MAX_CONF = 0.55
    if volume_spike and effective_min_edge > _SPIKE_MAX_EDGE:
        logger.info(
            "check_and_alert(%s): volume_spike → edge reducido %.3f→%.3f",
            analysis.get("market_id"), effective_min_edge, _SPIKE_MAX_EDGE,
        )
        effective_min_edge = _SPIKE_MAX_EDGE
    if volume_spike and effective_min_conf > _SPIKE_MAX_CONF:
        logger.info(
            "check_and_alert(%s): volume_spike → conf reducida %.3f→%.3f",
            analysis.get("market_id"), effective_min_conf, _SPIKE_MAX_CONF,
        )
        effective_min_conf = _SPIKE_MAX_CONF

    if abs(edge) < effective_min_edge:
        logger.info(
            "check_and_alert(%s): SKIP_EDGE abs(edge)=%.3f < %.3f (%s) — omitida",
            analysis.get("market_id"), abs(edge), effective_min_edge, direction,
        )
        return False
    if confidence < effective_min_conf:
        logger.info(
            "check_and_alert(%s): SKIP_CONF conf=%.3f < %.3f (%s) — omitida",
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
    cutoff_7d = now - timedelta(days=7)

    # --- Guard: contradicción con alerta opuesta en las últimas 6h ---
    _opposite = "BUY_NO" if direction == "BUY_YES" else "BUY_YES"
    try:
        _cutoff_6h = now - timedelta(hours=6)
        _opp_docs = list(
            col("alerts_sent")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .where(filter=FieldFilter("direction", "==", _opposite))
            .where(filter=FieldFilter("status", "==", "sent"))
            .limit(1)
            .stream()
        )
        if _opp_docs:
            _opp_sent_at = _opp_docs[0].to_dict().get("sent_at")
            if _opp_sent_at:
                if hasattr(_opp_sent_at, "tzinfo") and _opp_sent_at.tzinfo is None:
                    _opp_sent_at = _opp_sent_at.replace(tzinfo=timezone.utc)
                if _opp_sent_at >= _cutoff_6h:
                    logger.warning(
                        "check_and_alert(%s): CONTRADICCIÓN detectada — alertamos %s hace %.0fmin, "
                        "ahora %s — omitida",
                        market_id, _opposite,
                        (now - _opp_sent_at).total_seconds() / 60,
                        direction,
                    )
                    return False
    except Exception:
        logger.warning("check_and_alert(%s): error en guard contradicción — continuando", market_id, exc_info=True)

    # MEJORA 3: dedup reforzado por market_id — si ya alertamos este mercado en 24h
    # con precio sin cambio >5%, no volver a alertar aunque el alert_key sea distinto.
    # Filtra por market_id solo (un campo) para evitar índice compuesto Firestore.
    try:
        _mid_docs = list(
            col("alerts_sent")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .limit(10)
            .stream()
        )
        # Filtrar sent + recientes en Python
        _sent_docs = [d.to_dict() for d in _mid_docs if d.to_dict().get("status") == "sent"]
        if _sent_docs:
            _last = max(
                _sent_docs,
                key=lambda d: d.get("sent_at") or datetime.min.replace(tzinfo=timezone.utc),
            )
            _last_sent = _last.get("sent_at")
            _last_price_mid = float(_last.get("last_price", current_price))
            if _last_sent:
                if hasattr(_last_sent, "tzinfo") and _last_sent.tzinfo is None:
                    _last_sent = _last_sent.replace(tzinfo=timezone.utc)
                _pchg = abs(current_price - _last_price_mid) / max(_last_price_mid, 0.001)
                if _last_sent > cutoff_24h and _pchg <= 0.05:
                    logger.info(
                        "check_and_alert(%s): SKIP_DEDUP_24H price_chg=%.1f%% <24h — omitida",
                        market_id, _pchg * 100,
                    )
                    return False
                if _last_sent > cutoff_7d and _pchg <= 0.05:
                    logger.info(
                        "check_and_alert(%s): SKIP_DEDUP_7D price_chg=%.1f%% <7d — omitida",
                        market_id, _pchg * 100,
                    )
                    return False
    except Exception:
        logger.warning("check_and_alert(%s): error en dedup reforzado — continuando", market_id, exc_info=True)

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
                logger.info("check_and_alert(%s): SKIP_DEDUP_TX_24H (<24h) — omitida [tx]", market_id)
                return False
            if price_chg <= 0.05 and sent_at and sent_at > cutoff_7d:
                logger.info(
                    "check_and_alert(%s): SKIP_DEDUP_TX_7D price_chg=%.1f%% <7d — omitida [tx]",
                    market_id, price_chg * 100,
                )
                return False
            logger.info(
                "check_and_alert(%s): re-alerta permitida — precio cambió %.1f%% o >7d [tx]",
                market_id, price_chg * 100,
            )

        # Reclamar el slot atómicamente — status="pending" hasta confirmar el POST
        transaction.set(dedup_ref, {
            "alert_key": alert_key,
            "market_id": market_id,
            "direction": direction,
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
