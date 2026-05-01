"""
fix_accuracy_log.py — lee y (opcionalmente) corrige prodaccuracy_log para W17/W18.

Modo lectura:  python fix_accuracy_log.py
Modo escritura: python fix_accuracy_log.py --apply

Requiere env vars:
  GCP_SA_KEY                   — JSON del service account (string completo)
  GOOGLE_CLOUD_PROJECT         — proyecto GCP (ej. prediction-intelligence)
  FIRESTORE_COLLECTION_PREFIX  — prefijo de colecciones (ej. prod)
"""
import json
import os
import sys
from datetime import datetime, timezone

from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account
from google.cloud import firestore

APPLY = "--apply" in sys.argv

sa_key_raw = os.environ.get("GCP_SA_KEY", "")
project = os.environ.get("GOOGLE_CLOUD_PROJECT", "prediction-intelligence")
prefix = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "prod")

if not sa_key_raw:
    print("ERROR: GCP_SA_KEY env var is empty")
    sys.exit(1)

creds = service_account.Credentials.from_service_account_info(
    json.loads(sa_key_raw),
    scopes=["https://www.googleapis.com/auth/datastore"],
)
db = firestore.Client(project=project, credentials=creds)


def col(name):
    return db.collection(f"{prefix}{name}")


# ISO week dates for 2026
# W17: Mon 2026-04-20 → Sun 2026-04-26
# W18: Mon 2026-04-27 → Sun 2026-05-03
WEEKS = {
    "2026-W17": (
        datetime(2026, 4, 20, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc),
    ),
    "2026-W18": (
        datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5,  4, 0, 0, 0, tzinfo=timezone.utc),
    ),
}

print("=" * 60)
print(f"PROJECT : {project}")
print(f"PREFIX  : {prefix}")
print(f"MODE    : {'APPLY (will write)' if APPLY else 'DRY-RUN (read-only)'}")
print("=" * 60)

# ── PASO 1: Estado actual de accuracy_log ────────────────────
print("\n── CURRENT STATE: prodaccuracy_log ──")
for week in WEEKS:
    doc = col("accuracy_log").document(week).get()
    if doc.exists:
        d = doc.to_dict()
        print(f"\n  {week}:")
        print(f"    predictions_total   = {d.get('predictions_total')}")
        print(f"    predictions_correct = {d.get('predictions_correct')}")
        print(f"    accuracy            = {d.get('accuracy')}")
        print(f"    weights_start       = {d.get('weights_start')}")
        print(f"    weights_end         = {d.get('weights_end')}")
    else:
        print(f"\n  {week}: DOC NOT FOUND")

# ── PASO 2: Contar desde prodpredictions por created_at ──────
print("\n── RECALCULATING FROM prodpredictions (by created_at) ──")
results = {}

for week, (start, end) in WEEKS.items():
    try:
        docs = list(
            col("predictions")
            .where(filter=FieldFilter("created_at", ">=", start))
            .where(filter=FieldFilter("created_at", "<", end))
            .stream()
        )
    except Exception as e:
        print(f"\n  {week}: ERROR querying predictions — {e}")
        results[week] = None
        continue

    total = len(docs)
    resolved = [d.to_dict() for d in docs if d.to_dict().get("correct") is not None]
    correct = sum(1 for p in resolved if p.get("correct") is True)
    accuracy = round(correct / len(resolved), 4) if resolved else 0.0

    error_counts: dict[str, int] = {}
    for p in resolved:
        et = p.get("error_type") or "none"
        error_counts[et] = error_counts.get(et, 0) + 1

    print(f"\n  {week}:")
    print(f"    predictions queried = {total}")
    print(f"    resolved (correct != None) = {len(resolved)}")
    print(f"    correct = {correct}")
    print(f"    accuracy = {accuracy:.1%}")
    print(f"    error_types = {error_counts}")

    results[week] = {
        "total": total,
        "resolved": len(resolved),
        "correct": correct,
        "accuracy": accuracy,
        "error_counts": error_counts,
    }

# ── PASO 3: Aplicar fix si --apply ───────────────────────────
if not APPLY:
    print("\n── DRY-RUN: no changes written. Re-run with --apply to fix. ──")
    sys.exit(0)

print("\n── APPLYING FIX ──")

# Leer model_weights/current para weights_start/end actuales
try:
    mw_doc = col("model_weights").document("current").get()
    current_weights = mw_doc.to_dict().get("weights", {}) if mw_doc.exists else {}
except Exception as e:
    current_weights = {}
    print(f"  WARNING: could not read model_weights — {e}")

now = datetime.now(timezone.utc)

for week, data in results.items():
    if data is None:
        print(f"  {week}: SKIPPED (query error)")
        continue

    doc_ref = col("accuracy_log").document(week)
    existing = doc_ref.get()

    payload = {
        "week": week,
        "predictions_total": data["resolved"],
        "predictions_correct": data["correct"],
        "accuracy": data["accuracy"],
        "weights_end": current_weights,
        "fixed_at": now,
    }

    if existing.exists:
        # Preserve weights_start and created_at from original doc
        orig = existing.to_dict()
        if orig.get("weights_start"):
            payload["weights_start"] = orig["weights_start"]
        if orig.get("created_at"):
            payload["created_at"] = orig["created_at"]
        doc_ref.update(payload)
        print(f"  {week}: UPDATED → total={data['resolved']} correct={data['correct']} accuracy={data['accuracy']:.1%}")
    else:
        payload["created_at"] = now
        payload["weights_start"] = current_weights
        doc_ref.set(payload)
        print(f"  {week}: CREATED → total={data['resolved']} correct={data['correct']} accuracy={data['accuracy']:.1%}")

print("\n── FIX COMPLETE ──")
