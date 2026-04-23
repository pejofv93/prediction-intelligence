"""
Price tracker — snapshots historicos, momentum y volume spike.
Persiste en Firestore poly_price_history para analisis posterior.
"""
import logging
from datetime import datetime, timedelta, timezone

from shared.firestore_client import col

logger = logging.getLogger(__name__)


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
            .where("market_id", "==", market_id)
            .where("timestamp", ">=", cutoff)
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
    """True si vol_24h_actual > 3 x media de los ultimos 7 dias."""
    try:
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        docs = (
            col("poly_price_history")
            .where("market_id", "==", market_id)
            .where("timestamp", ">=", cutoff_7d)
            .order_by("timestamp")
            .stream()
        )
        snapshots = [d.to_dict() for d in docs]
        if len(snapshots) < 2:
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
            .where("market_id", "==", market_id)
            .where("timestamp", ">=", cutoff_2h)
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
            .where("market_id", "==", market_id)
            .where("timestamp", ">=", cutoff_1h)
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
