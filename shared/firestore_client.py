"""
Cliente Firestore compartido por todos los servicios.
Importar via: from shared.firestore_client import col, get_client, async_col
PYTHONPATH=/app garantiza que el import funciona en Cloud Run.

Al importar este módulo se aplica un parche de compatibilidad grpc/firestore
(ver _patch_firestore_retry_bug). Todas las consultas del proyecto pasan por
col()/get_client()/async_col(), así que con parchear aquí basta — no hace falta
repetirlo en el main.py de cada servicio.
"""
import logging

from google.cloud import firestore
from shared.config import GOOGLE_CLOUD_PROJECT, COLLECTION_PREFIX

logger = logging.getLogger(__name__)

_client = None
_async_client = None


def _patch_firestore_retry_bug() -> None:
    """
    google-cloud-firestore 2.x, con según qué grpcio, accede a
    `_UnaryStreamMultiCallable._retry` — un atributo que ese grpcio no expone —
    dentro de Query._retry_query_after_exception. El AttributeError resultante
    aborta el .stream() y ENMASCARA el error real (o inexistente).

    Se vio en telegram-bot el 2026-08-28: tras un deploy que trajo un grpcio nuevo
    (requirements fijaba firestore pero no grpcio), el reporte diario salió en
    ceros porque calculate_metrics cayó en su `except`. polymarket-agent ya lo
    parcheaba en su propio main.py desde 2026-04-24 (commit af9b132); esto lo
    centraliza para TODOS los servicios.

    Fix: si falta el atributo, devolver False (no reintentar) y dejar que el error
    original —si lo hay— se propague limpio en vez de un AttributeError espurio.
    Idempotente y defensivo: nunca debe romper el import del cliente.
    """
    patched: list[str] = []
    for module_path, class_name in (
        ("google.cloud.firestore_v1.query", "Query"),
        ("google.cloud.firestore_v1.async_query", "AsyncQuery"),
    ):
        try:
            mod = __import__(module_path, fromlist=[class_name])
            klass = getattr(mod, class_name)
        except Exception as exc:  # noqa: BLE001 — jamás romper el import por esto
            logger.warning("firestore_client: no se pudo cargar %s — %s", class_name, exc)
            continue
        original = getattr(klass, "_retry_query_after_exception", None)
        if original is None:
            # Esta versión de firestore no expone ese método en esa clase (p. ej. AsyncQuery
            # en algunas versiones). Nada que parchear, no es un problema.
            continue
        if getattr(original, "_grpc_retry_patched", False):
            continue

        def _safe_retry(self, *args, _original=original, **kwargs):
            try:
                return _original(self, *args, **kwargs)
            except AttributeError:
                return False

        _safe_retry._grpc_retry_patched = True
        klass._retry_query_after_exception = _safe_retry
        patched.append(class_name)
    if patched:
        logger.info(
            "firestore_client: parche _retry_query_after_exception aplicado (%s)",
            ", ".join(patched),
        )


_patch_firestore_retry_bug()


def get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=GOOGLE_CLOUD_PROJECT)
    return _client


def col(name: str) -> firestore.CollectionReference:
    """Devuelve referencia a coleccion con prefijo (cliente síncrono — usar para escrituras)."""
    return get_client().collection(f"{COLLECTION_PREFIX}{name}")


def get_async_client() -> firestore.AsyncClient:
    global _async_client
    if _async_client is None:
        _async_client = firestore.AsyncClient(project=GOOGLE_CLOUD_PROJECT)
    return _async_client


def async_col(name: str) -> firestore.AsyncCollectionReference:
    """Devuelve referencia a coleccion con prefijo (cliente async — usar para lecturas en contexto async)."""
    return get_async_client().collection(f"{COLLECTION_PREFIX}{name}")
