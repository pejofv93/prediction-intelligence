"""
Reset manual del threshold en Firestore model_weights/current.

Usar cuando el health check haya ajustado edge automaticamente
con muestra insuficiente (< 20 trades).

Uso:
  GOOGLE_CLOUD_PROJECT=prediction-intelligence \
  FIRESTORE_COLLECTION_PREFIX=prod \
  python scripts/reset_threshold.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.config import COLLECTION_PREFIX
from shared.firestore_client import col


def reset_threshold() -> None:
    doc_ref = col("model_weights").document("current")
    doc_ref.set(
        {
            "min_edge_threshold": 0.08,
            "min_confidence": 0.65,
            "health_override": "manual_reset_20260424",
        },
        merge=True,
    )
    print(
        f"OK — threshold reseteado en {COLLECTION_PREFIX}model_weights/current: "
        "min_edge=0.08, min_conf=0.65"
    )


if __name__ == "__main__":
    reset_threshold()
