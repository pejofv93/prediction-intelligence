"""
Price tracker — snapshots historicos, momentum, volume spike y monitor de movimientos bruscos.
Persiste en Firestore poly_price_history para analisis posterior.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col

logger = logging.getLogger(__name__)

_PRICE_MOVE_THRESHOLD = 0.05   # 5% en < 1h → alerta (bajado de 8% para capturar movimientos geopolíticos)
_DEDUP_WINDOW_SECONDS = 7_200  # no re-alertar el mismo mercado en 2h
_VOL_THRESHOLD_FOR_ANALYZE = 200_000  # $200k vol mínimo para lanzar mini-analyze post-movimiento


async def save_price_snapshot(
    market_id: str, price_yes: float, price_no: float, volume_24h: float
) -> None:
    """Anade snapshot de precio y volumen a Firestore poly_price_history."""
    try:
        doc = {
            "market_id": market_id,
            "timestamp": datetime.now(timezone.utc),
            "price_yes": float(price_yes),
            "price_no": float(price_no),
            "volume_24h": float(volume_24h),
        }
        col("poly_price_history").add(doc)
    except Exception:
        logger.error("save_price_snapshot(%s): error guardando", market_id, exc_info=True)


async def price_momentum(market_id: str) -> str:
    """
    Lee snapshots ultimas 6h de poly_price_history.
    Sube > 3% → "RISING" | Baja > 3% → "FALLING" | Else → "STABLE".
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        docs = (
            col("poly_price_history")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .where(filter=FieldFilter("timestamp", ">=", cutoff))
            .order_by("timestamp")
            .stream()
        )
        snapshots = [d.to_dict() for d in docs]
        if len(snapshots) < 2:
            return "STABLE"

        oldest_price = float(snapshots[0].get("price_yes", 0.5))
        newest_price = float(snapshots[-1].get("price_yes", 0.5))

        if oldest_price == 0:
            return "STABLE"

        change = (newest_price - oldest_price) / oldest_price
        if change > 0.03:
            return "RISING"
        elif change < -0.03:
            return "FALLING"
        return "STABLE"

    except Exception:
        logger.error("price_momentum(%s): error", market_id, exc_info=True)
        return "STABLE"


async def volume_spike(market_id: str) -> bool:
    """True si vol_24h_actual > 3 x media de los ultimos 3 dias (min 2 snapshots)."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        docs = (
            col("poly_price_history")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .where(filter=FieldFilter("timestamp", ">=", cutoff))
            .order_by("timestamp")
            .stream()
        )
        snapshots = [d.to_dict() for d in docs]
        if len(snapshots) < 2:
            if snapshots:
                return float(snapshots[0].get("volume_24h", 0)) > 50_000
            return False

        current_vol = float(snapshots[-1].get("volume_24h", 0))
        avg_vol = sum(float(s.get("volume_24h", 0)) for s in snapshots[:-1]) / max(len(snapshots) - 1, 1)

        if avg_vol == 0:
            return False
        return current_vol > (avg_vol * 3)

    except Exception:
        logger.error("volume_spike(%s): error", market_id, exc_info=True)
        return False


async def smart_money_detection(market_id: str) -> dict:
    """
    Detecta smart money usando heuristica de velocidad del spike de volumen.
    Heuristica: si el volumen sube > 5x la media en < 1 hora → probable smart money.
    hours_before_news siempre None (no tenemos timestamps de noticias).
    Devuelve {"is_smart_money": bool, "hours_before_news": None}.
    """
    try:
        cutoff_2h = datetime.now(timezone.utc) - timedelta(hours=2)
        docs = (
            col("poly_price_history")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .where(filter=FieldFilter("timestamp", ">=", cutoff_2h))
            .order_by("timestamp")
            .stream()
        )
        snapshots = [d.to_dict() for d in docs]
        if len(snapshots) < 2:
            return {"is_smart_money": False, "hours_before_news": None}

        # Buscar ventana de < 60 min con crecimiento > 5x
        for i in range(len(snapshots) - 1):
            t1 = snapshots[i].get("timestamp")
            t2 = snapshots[-1].get("timestamp")
            if t1 and t2:
                if hasattr(t1, "tzinfo") and t1.tzinfo is None:
                    t1 = t1.replace(tzinfo=timezone.utc)
                if hasattr(t2, "tzinfo") and t2.tzinfo is None:
                    t2 = t2.replace(tzinfo=timezone.utc)
                minutes_elapsed = (t2 - t1).total_seconds() / 60
                if minutes_elapsed > 60:
                    continue
            vol_start = float(snapshots[i].get("volume_24h", 0))
            vol_end = float(snapshots[-1].get("volume_24h", 0))
            if vol_start > 0 and vol_end > (vol_start * 5):
                return {"is_smart_money": True, "hours_before_news": None}

        return {"is_smart_money": False, "hours_before_news": None}

    except Exception:
        logger.error("smart_money_detection(%s): error", market_id, exc_info=True)
        return {"is_smart_money": False, "hours_before_news": None}


async def classify_volume_spike(market_id: str) -> str:
    """
    Clasifica el origen de un spike de volumen en:
      SMART_MONEY   — spike gradual (>2h) con precio moviéndose consistentemente
      MANIPULATION  — spike súbito (<30min) y precio regresa al nivel anterior en <2h
      WASH_TRADING  — volumen alto pero precio casi sin moverse (<1% variación total)
      ORGANIC       — spike sin patrón específico (o sin datos suficientes)

    Usa snapshots de poly_price_history de las últimas 4h.
    Solo se llama cuando volume_spike() retorna True.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
        docs = (
            col("poly_price_history")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .where(filter=FieldFilter("timestamp", ">=", cutoff))
            .order_by("timestamp")
            .stream()
        )
        snaps = [d.to_dict() for d in docs]
        if len(snaps) < 3:
            return "ORGANIC"

        # Extraer series de tiempo
        times = []
        for s in snaps:
            t = s.get("timestamp")
            if t is not None and hasattr(t, "tzinfo") and t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            times.append(t)

        volumes = [float(s.get("volume_24h", 0)) for s in snaps]
        prices = [float(s.get("price_yes", 0.5)) for s in snaps]

        vol_start = volumes[0]
        vol_peak_idx = volumes.index(max(volumes))
        vol_peak = volumes[vol_peak_idx]
        vol_end = volumes[-1]

        # --- WASH_TRADING: volumen alto, precio casi inmóvil ---
        price_range = max(prices) - min(prices)
        if vol_peak > vol_start * 3 and price_range < 0.01:
            logger.info(
                "classify_volume_spike(%s): WASH_TRADING vol_peak=%.0f price_range=%.3f",
                market_id, vol_peak, price_range,
            )
            return "WASH_TRADING"

        # Tiempo desde inicio hasta pico de volumen
        if times[0] and times[vol_peak_idx]:
            minutes_to_peak = abs((times[vol_peak_idx] - times[0]).total_seconds()) / 60
        else:
            minutes_to_peak = 120  # desconocido → asumir gradual

        # --- MANIPULATION: spike súbito (<30min) y precio vuelve al inicio ---
        if minutes_to_peak < 30:
            price_at_peak = prices[vol_peak_idx]
            price_at_end = prices[-1]
            price_returned = abs(price_at_end - prices[0]) < abs(price_at_peak - prices[0]) * 0.40
            if price_returned:
                logger.info(
                    "classify_volume_spike(%s): MANIPULATION spike en %.0fmin precio regresó",
                    market_id, minutes_to_peak,
                )
                return "MANIPULATION"

        # --- SMART_MONEY: spike gradual (>2h) y precio se mueve consistentemente ---
        if minutes_to_peak >= 120:
            # Precio se mueve consistentemente: cada snapshot va en la misma dirección
            price_deltas = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
            positive_moves = sum(1 for d in price_deltas if d > 0.001)
            negative_moves = sum(1 for d in price_deltas if d < -0.001)
            dominant_direction = max(positive_moves, negative_moves)
            total_moves = positive_moves + negative_moves
            if total_moves > 0 and dominant_direction / total_moves >= 0.70:
                logger.info(
                    "classify_volume_spike(%s): SMART_MONEY spike en %.0fmin dirección consistente %.0f%%",
                    market_id, minutes_to_peak, (dominant_direction / total_moves) * 100,
                )
                return "SMART_MONEY"

        return "ORGANIC"

    except Exception:
        logger.error("classify_volume_spike(%s): error", market_id, exc_info=True)
        return "ORGANIC"


async def detect_whale_activity(market_id: str) -> dict:
    """
    Detecta actividad inusual de ballenas en un mercado.
    Lee poly_price_history de la ultima 1h para el mercado.

    Heuristica:
    1. WHALE_DETECTED: Si volumen mas reciente > 30% del volumen acumulado en 1h
       (interpretamos que un solo trade grande movio el mercado)
       Senal: volume_snapshots[-1] > sum(all_volumes_1h) * 0.30

    2. POSSIBLE_MANIPULATION: Si precio movio >5% en <10 min
       (dos snapshots consecutivos con |price_diff| > 0.05)

    Returns:
    {
      whale_detected: bool,
      possible_manipulation: bool,
      volume_ratio: float,   # ultimo vol / vol acumulado 1h
      price_spike_pct: float | None,  # max movimiento entre snapshots consecutivos
      message: str | None    # "BALLENA DETECTADA — movimiento inusual" si whale
    }

    Si hay menos de 2 snapshots en 1h: devolver {whale_detected: False, possible_manipulation: False}
    Nunca falla.
    """
    _default = {
        "whale_detected": False,
        "possible_manipulation": False,
        "volume_ratio": 0.0,
        "price_spike_pct": None,
        "message": None,
    }
    try:
        cutoff_1h = datetime.now(timezone.utc) - timedelta(hours=1)
        docs = (
            col("poly_price_history")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .where(filter=FieldFilter("timestamp", ">=", cutoff_1h))
            .order_by("timestamp")
            .stream()
        )
        snapshots = [d.to_dict() for d in docs]

        if len(snapshots) < 2:
            return _default

        volumes = [float(s.get("volume_24h", 0)) for s in snapshots]
        total_vol = sum(volumes)
        last_vol = volumes[-1]
        volume_ratio = last_vol / total_vol if total_vol > 0 else 0.0
        whale_detected = volume_ratio > 0.30

        # Detectar spike de precio entre snapshots consecutivos
        prices = [float(s.get("price_yes", 0.5)) for s in snapshots]
        timestamps = []
        for s in snapshots:
            t = s.get("timestamp")
            if t is not None and hasattr(t, "tzinfo") and t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            timestamps.append(t)

        max_spike_pct: float | None = None
        possible_manipulation = False
        for i in range(len(prices) - 1):
            price_diff = abs(prices[i + 1] - prices[i])
            spike_pct = price_diff / max(prices[i], 0.01)
            if max_spike_pct is None or spike_pct > max_spike_pct:
                max_spike_pct = spike_pct
            # Verificar ventana temporal < 10 min
            if spike_pct > 0.05 and timestamps[i] is not None and timestamps[i + 1] is not None:
                minutes_elapsed = abs(
                    (timestamps[i + 1] - timestamps[i]).total_seconds()
                ) / 60
                if minutes_elapsed < 10:
                    possible_manipulation = True

        message = None
        if whale_detected:
            message = "BALLENA DETECTADA — movimiento inusual"

        return {
            "whale_detected": whale_detected,
            "possible_manipulation": possible_manipulation,
            "volume_ratio": round(volume_ratio, 4),
            "price_spike_pct": round(max_spike_pct * 100, 2) if max_spike_pct is not None else None,
            "message": message,
        }

    except Exception:
        logger.error("detect_whale_activity(%s): error", market_id, exc_info=True)
        return _default


async def monitor_price_changes() -> int:
    """
    Detecta movimientos bruscos de precio (>8% en <1h) en mercados activos.
    Compara el snapshot más reciente contra el más antiguo de la última 1h en poly_price_history.
    Envía alerta Telegram (topic Polymarket) si detecta movimiento significativo.
    Dedup: no re-alerta el mismo mercado en 2h.
    Devuelve número de alertas enviadas.
    """
    import httpx

    now = datetime.now(timezone.utc)
    cutoff_2h = now - timedelta(hours=2)
    alerts_sent = 0

    bot_url = os.environ.get("TELEGRAM_BOT_URL", "")
    cloud_run_token = os.environ.get("CLOUD_RUN_TOKEN", "")

    # Leer mercados activos de enriched_markets
    try:
        raw_docs = list(col("enriched_markets").limit(150).stream(timeout=30.0))
        markets = [d.to_dict() for d in raw_docs if d.to_dict().get("market_id")]
    except Exception:
        logger.error("monitor_price_changes: error leyendo enriched_markets", exc_info=True)
        return 0

    logger.info("monitor_price_changes: evaluando %d mercados", len(markets))

    for market in markets:
        market_id = market.get("market_id", "")
        question = market.get("question", "")[:120]
        if not market_id:
            continue

        try:
            # Snapshots de las últimas 2h para este mercado
            snaps = [
                d.to_dict()
                for d in col("poly_price_history")
                .where(filter=FieldFilter("market_id", "==", market_id))
                .where(filter=FieldFilter("timestamp", ">=", cutoff_2h))
                .order_by("timestamp")
                .stream()
            ]

            if len(snaps) < 2:
                continue

            oldest = snaps[0]
            latest = snaps[-1]

            t_old = oldest.get("timestamp")
            t_new = latest.get("timestamp")
            for t in (t_old, t_new):
                if t is not None and hasattr(t, "tzinfo") and t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)

            if t_old is None or t_new is None:
                continue

            if hasattr(t_old, "tzinfo") and t_old.tzinfo is None:
                t_old = t_old.replace(tzinfo=timezone.utc)
            if hasattr(t_new, "tzinfo") and t_new.tzinfo is None:
                t_new = t_new.replace(tzinfo=timezone.utc)

            minutes_elapsed = (t_new - t_old).total_seconds() / 60
            if minutes_elapsed < 5:
                continue

            price_old = float(oldest.get("price_yes", 0))
            price_new = float(latest.get("price_yes", 0))

            if price_old <= 0:
                continue

            pct_change = (price_new - price_old) / price_old

            if abs(pct_change) < _PRICE_MOVE_THRESHOLD:
                continue

            # Dedup 2h — no re-alertar el mismo mercado si ya se envió en la ventana
            dedup_key = f"{market_id}_price_movement"
            dedup_ref = col("alerts_sent").document(dedup_key)
            try:
                dedup_doc = dedup_ref.get()
                if dedup_doc.exists:
                    last_sent = dedup_doc.to_dict().get("sent_at")
                    if last_sent:
                        if hasattr(last_sent, "tzinfo") and last_sent.tzinfo is None:
                            last_sent = last_sent.replace(tzinfo=timezone.utc)
                        if (now - last_sent).total_seconds() < _DEDUP_WINDOW_SECONDS:
                            logger.info(
                                "monitor_price_changes(%s): SKIP_DEDUP_2H — alerta enviada hace %.0f min",
                                market_id, (now - last_sent).total_seconds() / 60,
                            )
                            continue
            except Exception:
                logger.warning("monitor_price_changes(%s): error leyendo dedup — omitiendo alerta por seguridad", market_id)
                continue

            vol_24h = float(latest.get("volume_24h", 0))
            pp_change = (price_new - price_old) * 100
            pp_sign = "+" if pp_change > 0 else ""
            text = (
                f"🚨 MOVIMIENTO BRUSCO\n"
                f"{question}\n"
                f"Cambio: {pp_sign}{pp_change:.1f}pp ({price_old*100:.1f}% → {price_new*100:.1f}%) en {minutes_elapsed:.0f} min\n"
                f"Vol 1h: ${vol_24h:,.0f}\n"
                f"⚡ Posible información privilegiada"
            )

            if not bot_url:
                logger.warning("monitor_price_changes: TELEGRAM_BOT_URL no configurada — alerta omitida")
                break

            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"{bot_url}/send-alert",
                        json={"type": "polymarket_resolution", "data": {"text": text}},
                        headers={"x-cloud-token": cloud_run_token},
                    )
                if resp.status_code in (200, 201, 202):
                    try:
                        dedup_ref.set({"market_id": market_id, "sent_at": now, "pct_change": round(pct_change, 4)})
                    except Exception:
                        logger.warning("monitor_price_changes(%s): error guardando dedup — puede re-alertar", market_id)
                    alerts_sent += 1
                    logger.info(
                        "monitor_price_changes(%s): alerta enviada — cambio=%s%.1fpp en %.0fmin",
                        market_id, pp_sign, abs(pp_change), minutes_elapsed,
                    )
                    # Mini-analyze: si volumen alto, evaluar señal accionable post-movimiento
                    if vol_24h >= _VOL_THRESHOLD_FOR_ANALYZE:
                        asyncio.create_task(
                            _analyze_price_move(
                                market_id, question, pct_change, price_new, vol_24h
                            )
                        )
                        logger.info(
                            "monitor_price_changes(%s): lanzando mini-analyze (vol=$%.0f move=%.1f%%)",
                            market_id, vol_24h, pct_change * 100,
                        )
                else:
                    logger.warning(
                        "monitor_price_changes(%s): telegram-bot respondio %d", market_id, resp.status_code
                    )
            except Exception:
                logger.error("monitor_price_changes(%s): error enviando alerta", market_id, exc_info=True)

        except Exception:
            logger.error("monitor_price_changes(%s): error procesando", market_id, exc_info=True)

    logger.info("monitor_price_changes: %d alertas enviadas de %d mercados", alerts_sent, len(markets))
    return alerts_sent


async def _analyze_price_move(
    market_id: str,
    question: str,
    pct_change: float,
    price_new: float,
    vol_24h: float,
) -> None:
    """
    Mini-analyze post-movimiento brusco.
    Si precio bajó → evalúa BUY_YES (mercado puede estar sobrevendido).
    Si precio subió → evalúa BUY_NO (mercado puede estar sobrecomprado).
    Genera señal Telegram si edge > 5% tras el movimiento.
    Solo se lanza cuando vol_24h > $200k.
    """
    try:
        from groq_analyzer import analyze_market
        from alert_engine import check_and_alert

        enriched_doc = col("enriched_markets").document(market_id).get()
        if not enriched_doc.exists:
            logger.info("_analyze_price_move(%s): no en enriched_markets — omitido", market_id)
            return

        enriched = enriched_doc.to_dict()
        # Actualizar con precio actual post-movimiento para que Groq evalúe la nueva valoración
        enriched["price_yes"] = price_new
        enriched["volume_spike"] = True
        enriched["movement_trigger"] = True
        enriched["movement_pct"] = round(pct_change, 4)

        prediction = await analyze_market(enriched)
        if prediction is None:
            logger.info("_analyze_price_move(%s): analyze_market devolvió None", market_id)
            return

        edge = float(prediction.get("edge", 0))
        rec = str(prediction.get("recommendation", "PASS")).upper()

        # Verificar coherencia: precio bajó → esperamos BUY_YES; precio subió → BUY_NO
        direction_ok = (pct_change < 0 and rec == "BUY_YES") or (pct_change > 0 and rec == "BUY_NO")

        if abs(edge) < 0.05:
            logger.info(
                "_analyze_price_move(%s): edge=%.3f < 0.05 — sin señal (move=%.1f%% rec=%s)",
                market_id, edge, pct_change * 100, rec,
            )
            return

        prediction["volume_spike"] = True
        alerted = await check_and_alert(prediction)
        logger.info(
            "_analyze_price_move(%s): move=%.1f%% edge=%.3f conf=%.2f rec=%s dir_ok=%s alerted=%s",
            market_id, pct_change * 100, edge,
            float(prediction.get("confidence", 0)), rec, direction_ok, alerted,
        )

        if alerted:
            try:
                from shared.shadow_engine import track_new_signal
                await track_new_signal(prediction, "polymarket_movement")
            except Exception:
                pass

    except Exception:
        logger.error("_analyze_price_move(%s): error", market_id, exc_info=True)


def apply_whale_to_signal(signal: dict, whale_data: dict, signal_direction: str = "YES") -> dict:
    """
    Ajusta signal segun actividad de ballena.
    signal_direction: "YES" o "NO" — hacia donde va la senal.

    Logica (usando price_momentum como proxy de direccion ballena):
    - Si whale_detected y possible_manipulation:
        signal["suspicious"] = True
        NO cambiar confidence (no bloquear senal, solo marcar)
    - Si whale_detected y no manipulation:
        Anadir signal["whale_badge"] = "BALLENA DETECTADA"
        (solo informativo)

    Devolver signal con campos anadidos. Nunca falla.
    """
    try:
        whale_detected = bool(whale_data.get("whale_detected", False))
        possible_manipulation = bool(whale_data.get("possible_manipulation", False))

        if whale_detected and possible_manipulation:
            signal["suspicious"] = True
        elif whale_detected:
            signal["whale_badge"] = "BALLENA DETECTADA"
    except Exception:
        logger.error("apply_whale_to_signal: error aplicando datos de ballena", exc_info=True)
    return signal
