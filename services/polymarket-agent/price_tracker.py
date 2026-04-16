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
