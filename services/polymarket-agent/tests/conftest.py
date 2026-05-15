"""
conftest.py — Setup global para tests de polymarket-agent.

Debe correr ANTES de cualquier import de groq_analyzer o shared.*.
Orden garantizado por pytest: conftest.py se carga antes que los test modules.
"""
import os
import sys
from unittest.mock import MagicMock

# ── 1. Variables de entorno requeridas ────────────────────────────────────────
# shared/config.py hace os.environ["GOOGLE_CLOUD_PROJECT"] (KeyError si no está).
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ.setdefault("FIRESTORE_COLLECTION_PREFIX", "")

# ── 2. Rutas sys.path ─────────────────────────────────────────────────────────
_TESTS_DIR   = os.path.dirname(os.path.abspath(__file__))
_SERVICE_DIR = os.path.dirname(_TESTS_DIR)                         # polymarket-agent/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SERVICE_DIR))     # prediction-intelligence-ok/

for _p in (_SERVICE_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 3. Mock de google.cloud ───────────────────────────────────────────────────
# shared/firestore_client.py importa `from google.cloud import firestore` al nivel
# de módulo. Mockearlo en sys.modules previene el error de conexión a GCP.
# Este mock se aplica ANTES de que cualquier test importe shared.firestore_client.
_gcloud_mock = MagicMock()
for _mod in (
    "google",
    "google.cloud",
    "google.cloud.firestore",
    "google.cloud.firestore_v1",
    "google.cloud.firestore_v1.base_query",
    "google.api_core",
    "google.api_core.exceptions",
    "grpc",
):
    sys.modules.setdefault(_mod, _gcloud_mock)
