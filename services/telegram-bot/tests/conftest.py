"""Setup de tests de telegram-bot: sin GCP ni Telegram reales."""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("FIRESTORE_COLLECTION_PREFIX", "")
os.environ["CLOUD_RUN_TOKEN"] = "test-token"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVICE_DIR = os.path.dirname(_TESTS_DIR)                       # telegram-bot/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SERVICE_DIR))   # raíz del repo
for _p in (_SERVICE_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Firestore/grpc simulados antes de importar shared.* (igual que polymarket-agent).
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
