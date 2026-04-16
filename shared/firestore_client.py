"""
Cliente Firestore compartido por todos los servicios.
Importar via: from shared.firestore_client import col, get_client
PYTHONPATH=/app garantiza que el import funciona en Cloud Run.
"""
from google.cloud import firestore
from shared.config import GOOGLE_CLOUD_PROJECT, COLLECTION_PREFIX

_client = None


def get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=GOOGLE_CLOUD_PROJECT)
    return _client


def col(name: str) -> firestore.CollectionReference:
    """Devuelve referencia a coleccion con prefijo."""
    return get_client().collection(f"{COLLECTION_PREFIX}{name}")
