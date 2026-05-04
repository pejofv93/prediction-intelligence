"""
Cliente Firestore compartido por todos los servicios.
Importar via: from shared.firestore_client import col, get_client, async_col
PYTHONPATH=/app garantiza que el import funciona en Cloud Run.
"""
from google.cloud import firestore
from shared.config import GOOGLE_CLOUD_PROJECT, COLLECTION_PREFIX

_client = None
_async_client = None


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
