"""
Motor de alertas Polymarket → telegram-bot.
Envia alerta si abs(edge) > min_edge_for_direction + confianza > min_conf_for_direction.
Los umbrales se cargan desde poly_model_weights/current (ajustados por poly_learning_engine).
edge positivo → BUY_YES; edge negativo → BUY_NO.
volume_spike y smart_money son señales bonus (no requisito).
"""
import logging
from datetime import datetime, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import (
    POLY_LEARNED_PARAMS_FROZEN, POLY_MIN_CONFIDENCE, POLY_MIN_EDGE, TELEGRAM_BOT_URL,
)

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

# Filtro anti-longshot para BUY_YES: el modelo tiene sesgo optimista y compra YES
# en eventos que el mercado considera improbables (mp_yes bajo), que casi nunca ocurren.
# Backtest histórico: BUY_YES con mp_yes < 0.40 → 1 acierto / 31 fallos.
# Solo afecta a BUY_YES; BUY_NO no se toca. Configurable aquí para ajustar sin tocar lógica.
_BUY_YES_MIN_MP_YES = 0.40

# Filtro espejo anti-favorito-extremo para BUY_NO: el modelo apuesta NO contra
# favoritos casi seguros (mp_yes alto), que normalmente ganan → dinero muerto.
# Análisis histórico: BUY_NO con mp_yes >= 0.85 → 0 aciertos / 9 (-100%, 0 ganadoras
# sacrificadas). Las BUY_NO buenas (divergencia alta) están todas en mp_yes < 0.75.
# Conservador en 0.85 (no 0.80, que ya costaría 2 ganadoras). Solo afecta a BUY_NO.
_BUY_NO_MAX_MP_YES = 0.85


# Slot por mercado + dirección (ver check_and_alert).
_SLOT_STATUSES = ("sent", "sent_archived", "resent")
_SLOT_MAX_RESENDS = 1
_SLOT_MIN_GAP_H = 24
_SLOT_MIN_PRICE_GAIN = 0.05   # absoluto, a favor de nuestro lado


# Lecturas del historial del slot en el ciclo en curso. Si falla, check_and_alert NO
# envía (fail-closed); record_slot_cycle() lleva la racha de ciclos con fallos y avisa.
_SLOT_READS = {"ok": 0, "fail": 0}
_SLOT_ALERT_AFTER_CYCLES = 2
# Racha en memoria por si Firestore (que es lo que falla) tampoco deja guardarla.
_SLOT_STREAK_MEM = {"failed_cycles": 0, "alerted": False}


async def record_slot_cycle(source: str) -> dict:
    """
    Cierra el ciclo de lecturas del historial de alerts_sent (mismo esquema que
    scanner.record_gamma_cycle). Ciclo fallido = al menos una lectura fallida, porque
    cada una es una alerta que no salió. Racha en agent_state/poly_slot_health (o en
    memoria si Firestore no responde); aviso por Telegram al llegar a
    _SLOT_ALERT_AFTER_CYCLES ciclos seguidos (una vez por racha) y al recuperarse.
    Nunca lanza.
    """
    import httpx
    from shared.firestore_client import col

    ok, fail = _SLOT_READS["ok"], _SLOT_READS["fail"]
    _SLOT_READS["ok"] = _SLOT_READS["fail"] = 0
    if ok + fail == 0:
        return {"ok": 0, "fail": 0, "streak": None}
    failed = fail > 0
    now = datetime.now(timezone.utc)
    ref = None
    prev = dict(_SLOT_STREAK_MEM)
    try:
        ref = col("agent_state").document("poly_slot_health")
        snap = ref.get()
        if snap.exists:
            prev = snap.to_dict() or prev
    except Exception:
        logger.error("record_slot_cycle: no se pudo leer agent_state/poly_slot_health — racha en memoria",
                     exc_info=True)
    alerted = bool(prev.get("alerted"))
    streak = int(prev.get("failed_cycles") or 0) + 1 if failed else 0
    text = None
    if failed:
        logger.error(
            "SLOT_HISTORY_CYCLE_FAILED (%s): %d/%d lecturas del historial fallidas (alertas no enviadas) "
            "— racha %d ciclo(s)", source, fail, ok + fail, streak,
        )
        if streak >= _SLOT_ALERT_AFTER_CYCLES and not alerted:
            text = (
                f"🚨 polymarket-agent: no se puede leer el historial de alertas (alerts_sent)\n"
                f"{streak} ciclos seguidos con lecturas fallidas (último: {source}, {fail}/{ok + fail}). "
                f"Sin historial el dedup no envía: esas alertas de Polymarket no han salido."
            )
            alerted = True
    else:
        if alerted:
            text = f"✅ polymarket-agent: el historial de alertas vuelve a leerse ({source})"
        alerted = False
    _SLOT_STREAK_MEM.update(failed_cycles=streak, alerted=alerted)
    try:
        if ref is not None:
            ref.set({"failed_cycles": streak, "alerted": alerted, "last_cycle_at": now,
                     "last_source": source, "last_ok": ok, "last_fail": fail,
                     **({"last_ok_at": now} if not failed else {})}, merge=True)
    except Exception:
        logger.error("record_slot_cycle: no se pudo guardar agent_state/poly_slot_health", exc_info=True)
    if text:
        try:
            if TELEGRAM_BOT_URL:
                import os
                async with httpx.AsyncClient(timeout=10.0) as client:
                    # type "matched": texto plano (parse_mode=None) al canal General.
                    await client.post(
                        f"{TELEGRAM_BOT_URL}/send-alert",
                        json={"type": "matched", "data": {"text": text}},
                        headers={"x-cloud-token": os.environ.get("CLOUD_RUN_TOKEN", "")},
                    )
            else:
                logger.error("record_slot_cycle: TELEGRAM_BOT_URL no configurada — aviso no enviado: %s", text)
        except Exception:
            logger.error("record_slot_cycle: error enviando aviso a Telegram", exc_info=True)
    return {"ok": ok, "fail": fail, "streak": streak}


def _as_utc(ts):
    if ts is not None and getattr(ts, "tzinfo", 1) is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _sent_price(doc: dict):
    """Precio YES enviado. Docs anteriores a la "foto de lo enviado" solo tienen last_price."""
    v = doc.get("market_price_yes", doc.get("last_price"))
    return float(v) if v is not None else None


def _sent_edge(doc: dict):
    """Edge enviado. Docs antiguos no lo guardan, pero su clave era market_id_edge."""
    if doc.get("edge") is not None:
        return float(doc["edge"])
    try:
        return float(str(doc.get("alert_key") or "").rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None


def _doc_direction(doc: dict):
    d = doc.get("direction") or doc.get("recommendation")
    if d in ("BUY_YES", "BUY_NO"):
        return d
    e = _sent_edge(doc)
    return None if e is None else ("BUY_YES" if e >= 0 else "BUY_NO")


def _sent_docs(history: list[dict]) -> list[dict]:
    return [h for h in history if h.get("status") in _SLOT_STATUSES and h.get("sent_at")]


def _same_direction_count(history: list[dict], direction: str) -> int:
    return sum(1 for h in _sent_docs(history) if _doc_direction(h) == direction)


def _slot_decision(history: list[dict], direction: str, price: float, edge: float,
                   now: datetime) -> tuple[str, str, dict | None]:
    """("first" | "resend" | "skip", motivo, envío previo) para mercado+dirección.

    history: docs de alerts_sent del mercado (incluye los de la clave antigua
    market_id+edge, así que los mercados ya repetidos no ganan reenvíos nuevos).
    """
    sent = _sent_docs(history)
    opposite = [h for h in sent if _doc_direction(h) not in (None, direction)]
    if opposite:
        last_opp = max(opposite, key=lambda h: _as_utc(h["sent_at"]))
        return ("skip", f"cambio de dirección {_doc_direction(last_opp)} → {direction} "
                        f"(enviado {_as_utc(last_opp['sent_at']):%d/%m}; solo se registra)", None)
    same = [h for h in sent if _doc_direction(h) == direction]
    if not same:
        return "first", "primer envío", None
    prev = max(same, key=lambda h: _as_utc(h["sent_at"]))
    if len(same) > _SLOT_MAX_RESENDS:
        return "skip", f"reenvío ya usado ({len(same)} envíos)", prev
    gap_h = (now - _as_utc(prev["sent_at"])).total_seconds() / 3600
    if gap_h < _SLOT_MIN_GAP_H:
        return "skip", f"último envío hace {gap_h:.1f}h < {_SLOT_MIN_GAP_H}h", prev
    prev_price, prev_edge = _sent_price(prev), _sent_edge(prev)
    if prev_price is None or prev_edge is None:
        return "skip", "sin precio/edge de referencia", prev
    gain = (prev_price - price) if direction == "BUY_YES" else (price - prev_price)
    if gain < _SLOT_MIN_PRICE_GAIN:
        return "skip", f"precio YES {prev_price:.3f} → {price:.3f} sin mejora ≥{_SLOT_MIN_PRICE_GAIN}", prev
    if abs(edge) < abs(float(prev_edge)):
        return "skip", f"|edge| {abs(edge):.3f} < enviado {abs(float(prev_edge)):.3f}", prev
    return "resend", f"precio YES {prev_price:.3f} → {price:.3f}, edge {float(prev_edge):+.3f} → {edge:+.3f}", prev


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
            if POLY_LEARNED_PARAMS_FROZEN:
                # Umbrales por dirección dormidos: aprendidos de la lista sesgada y con
                # accuracy_overall de esa misma lista. Base siempre, pase lo que pase con el acierto.
                logger.info(
                    "_load_learned_thresholds: umbral_mode=base (congelado; acc=%.1f%% n=%d v%s)",
                    accuracy * 100, sample_size, data.get("version", "?"),
                )
                _LEARNED_THRESHOLDS = dict(_BASE_THRESHOLDS)
                return _LEARNED_THRESHOLDS
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

    # Filtro anti-longshot BUY_YES: no comprar YES en eventos que el mercado considera
    # improbables (mp_yes bajo). Sesgo optimista del modelo — backtest: 1/31 en longshots.
    # No afecta a BUY_NO.
    if direction == "BUY_YES":
        mp_yes = float(analysis.get("market_price_yes", 0.5))
        if mp_yes < _BUY_YES_MIN_MP_YES:
            logger.info(
                "check_and_alert(%s): SKIP_LONGSHOT BUY_YES mp_yes=%.3f < %.3f — omitida "
                "(anti-longshot: modelo optimista en evento improbable)",
                analysis.get("market_id"), mp_yes, _BUY_YES_MIN_MP_YES,
            )
            return False

    # Filtro espejo anti-favorito-extremo BUY_NO: no apostar NO contra favoritos casi
    # seguros (mp_yes alto). Histórico: mp_yes >= 0.85 → 0/9 (dinero muerto). No afecta a BUY_YES.
    if direction == "BUY_NO":
        mp_yes = float(analysis.get("market_price_yes", 0.5))
        if mp_yes >= _BUY_NO_MAX_MP_YES:
            logger.info(
                "check_and_alert(%s): SKIP_FADE_STRONG_FAVORITE BUY_NO mp_yes=%.3f >= %.3f — omitida "
                "(anti-favorito-extremo: NO contra favorito casi seguro)",
                analysis.get("market_id"), mp_yes, _BUY_NO_MAX_MP_YES,
            )
            return False

    market_id = str(analysis.get("market_id") or "")
    if not market_id:
        logger.warning("check_and_alert: market_id vacío — alerta omitida sin guardar dedup")
        return False

    # Última comprobación justo antes de enviar: el mercado sigue abierto y el partido
    # no ha empezado. analyze_market ya lo mira, pero entre el análisis y el envío pasan
    # la llamada al LLM, ballenas y wallets. Sin respuesta de Gamma → no se envía.
    from scanner import fetch_market_state
    _state = await fetch_market_state(market_id)
    if _state is None or not _state["open"]:
        logger.warning(
            "check_and_alert(%s): SKIP_MARKET_NOT_OPEN reason=%s — omitida",
            market_id, (_state or {}).get("reason", "gamma_unavailable"),
        )
        return False

    logger.info(
        "check_and_alert(%s): pasa thresholds %s edge=%.3f>=%.3f conf=%.2f>=%.2f vol_spike=%s sm=%s",
        market_id, direction,
        abs(edge), effective_min_edge, confidence, effective_min_conf,
        volume_spike, smart_money,
    )

    current_price = float(analysis.get("market_price_yes", 0.5))
    now = datetime.now(timezone.utc)

    # --- Slot por mercado + dirección (criterio de baloncesto, _bball_resend_decision) ---
    # Antes: clave market_id + edge redondeado y bloqueo solo con precio RELATIVO ≤5% en
    # 7 días → el mismo mercado salía hasta 9 veces persiguiendo el precio, y cada envío
    # abría su shadow_trade. Ahora: un envío por mercado y dirección, 1 reenvío como
    # máximo y solo con mejora material; el cambio de dirección no se alerta.
    try:
        _history = [
            d.to_dict() for d in col("alerts_sent")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .limit(50)
            .stream()
        ]
        _SLOT_READS["ok"] += 1
    except Exception:
        _SLOT_READS["fail"] += 1
        logger.error("check_and_alert(%s): SLOT_HISTORY_READ_FAILED — omitida (sin historial no se "
                     "envía)", market_id, exc_info=True)
        return False
    action, why, prev = _slot_decision(_history, direction, current_price, edge, now)
    if action == "skip":
        logger.info("check_and_alert(%s): SKIP_SLOT %s %s — omitida", market_id, direction, why)
        return False
    is_resend = action == "resend"
    _n_same = _same_direction_count(_history, direction)
    alert_key = f"{market_id}_{direction}" + (f"_r{_n_same}" if _n_same else "")

    db = get_client()
    dedup_ref = col("alerts_sent").document(alert_key)

    # --- Transacción atómica: check-and-claim ---
    # El doc id es determinista (mercado+dirección+nº de reenvío): si dos requests
    # concurrentes llegan aquí, solo uno lo crea.
    @_firestore.transactional
    def _claim(transaction: _firestore.Transaction) -> bool:
        _snap = dedup_ref.get(transaction=transaction)
        _cur = (_snap.to_dict() or {}) if _snap.exists else {}
        # Un "pending" de más de 10 min es un envío que murió a medias: se reclama.
        _stale = (_cur.get("status") == "pending" and _cur.get("sent_at") is not None
                  and now - _as_utc(_cur["sent_at"]) > timedelta(minutes=10))
        if _snap.exists and not _stale:
            logger.info("check_and_alert(%s): SKIP_SLOT_TX %s ya reclamado — omitida [tx]",
                        market_id, alert_key)
            return False
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

    if is_resend:
        # El telegram-bot lo marca como reenvío y main.py no abre otro shadow_trade.
        analysis["alert_resend"] = {
            "prev_price": _sent_price(prev),
            "prev_edge": _sent_edge(prev),
            "prev_sent_at": _as_utc(prev.get("sent_at")).isoformat(),
            "reason": why,
        }
        logger.info("check_and_alert(%s): REENVÍO %s — %s", market_id, direction, why)

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
        # Foto de lo ENVIADO: alerts_sent es la fuente de verdad para graduar.
        # Un reenvío queda como "resent": el resolver solo gradúa sent/sent_archived,
        # así que el mercado+dirección cuenta como una sola apuesta.
        dedup_ref.update({
            "status": "resent" if is_resend else "sent",
            **({"resend_of": prev.get("alert_key"), "resend_reason": why} if is_resend else {}),
            "recommendation": rec,
            "market_price_yes": current_price,
            "real_prob": analysis.get("real_prob"),
            "edge": edge,
            "confidence": confidence,
            "category": analysis.get("category"),
            "categorizer_version": analysis.get("categorizer_version"),
            "question": analysis.get("question"),
            "end_date_iso": analysis.get("end_date_iso"),
        })
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
