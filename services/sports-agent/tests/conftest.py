"""
conftest.py — Setup global para tests de sports-agent.

shared/config.py hace os.environ["GOOGLE_CLOUD_PROJECT"] (KeyError si no está): valores
de prueba antes de cualquier import. Ningún test habla con Firestore ni con la red.
"""
import os
import sys

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("FIRESTORE_COLLECTION_PREFIX", "test_")

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVICE_DIR = os.path.dirname(_TESTS_DIR)                         # sports-agent/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SERVICE_DIR))     # prediction-intelligence-ok/

for _p in (_SERVICE_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
