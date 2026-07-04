"""
shared/odds_cache.py
Caché Firestore para eventos de The Odds API (basket, tenis, córners).

Motivo: con min-instances=0 cada ciclo de analyze (4/día, cron 0 1,7,13,19) es un
cold start que borra las cachés en memoria. Fútbol ya persiste sus eventos en Firestore
(league_odds_cache) y por eso re-fetchea ~1/día; basket/tenis/córners NO persistían, así
que re-fetchaban en los 4 ciclos → estampida de créditos de The Odds API (500/mes se
agotaban 5-6×). Este helper les da la misma persistencia 24h que fútbol.

Solo persiste listas NO vacías (una lista vacía con TTL largo envenenaría la caché).
El caller mantiene su caché en memoria para el TTL corto de vacíos/errores.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_COLLECTION = "theodds_events_cache"


def _col():
    from shared.firestore_client import col
    return col(_COLLECTION)


def _doc_id(namespace: str, sport_key: str) -> str:
    return f"{namespace}__{sport_key}"


def get_events(namespace: str, sport_key: str, ttl_seconds: float) -> Optional[list]:
    """
    Devuelve los eventos cacheados en Firestore si son más recientes que ttl_seconds.
    None si no hay caché, expiró, o hubo error (el caller hará el HTTP).
    """
    try:
        snap = _col().document(_doc_id(namespace, sport_key)).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        fetched = data.get("fetched_at")
        if not fetched:
            return None
        if hasattr(fetched, "tzinfo") and fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        if age < ttl_seconds:
            return data.get("events", [])
    except Exception:
        logger.warning("odds_cache.get_events(%s/%s): error leyendo Firestore",
                       namespace, sport_key, exc_info=True)
    return None


def set_events(namespace: str, sport_key: str, events: list) -> None:
    """Persiste eventos en Firestore. Ignora listas vacías (no envenenar la caché)."""
    if not events:
        return
    try:
        _col().document(_doc_id(namespace, sport_key)).set({
            "namespace": namespace,
            "sport_key": sport_key,
            "fetched_at": datetime.now(timezone.utc),
            "events": events,
        })
    except Exception:
        logger.warning("odds_cache.set_events(%s/%s): error escribiendo Firestore",
                       namespace, sport_key, exc_info=True)
