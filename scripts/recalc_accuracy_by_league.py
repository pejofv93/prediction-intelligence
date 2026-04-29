"""
scripts/recalc_accuracy_by_league.py

Recalcula accuracy_by_league en prodmodel_weights/current agrupando
los docs resueltos (correct != None) de prodpredictions por league.

Uso:
    python3 scripts/recalc_accuracy_by_league.py

Requiere:
    - gcloud auth print-access-token --account pejocanal@gmail.com (activo)
"""

import json
import logging
import subprocess
import sys
import urllib.request
import urllib.error
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("recalc")

PROJECT = "prediction-intelligence"
PREFIX = "prod"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
ACCOUNT = "pejocanal@gmail.com"


def _get_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", f"--account={ACCOUNT}"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(f"No se pudo obtener token para {ACCOUNT}")
    return token


_TOKEN_CACHE = [None]


def _tok() -> str:
    if not _TOKEN_CACHE[0]:
        _TOKEN_CACHE[0] = _get_token()
    return _TOKEN_CACHE[0]


def _request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {_tok()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _list_collection(collection: str) -> list[dict]:
    docs = []
    page_token = None
    while True:
        url = f"{BASE}/{PREFIX}{collection}?pageSize=300"
        if page_token:
            url += f"&pageToken={page_token}"
        data = _request(url)
        docs.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return docs


def _fv(fields: dict, key: str):
    v = fields.get(key, {})
    if "stringValue" in v:
        return v["stringValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "nullValue" in v:
        return None
    return None


def main():
    logger.info("=== Recalcular accuracy_by_league ===")

    token = _get_token()
    logger.info("Token OK (%d chars)", len(token))

    logger.info("Leyendo prodpredictions...")
    all_docs = _list_collection("predictions")
    logger.info("Total docs: %d", len(all_docs))

    # Agrupar por liga, solo docs resueltos (correct is bool)
    by_league: dict[str, dict] = defaultdict(lambda: {"correct": 0, "total": 0})
    unresolved = 0

    for doc in all_docs:
        fields = doc.get("fields", {})
        correct_raw = fields.get("correct", {})

        # Solo docs con correct=True o correct=False (resueltos)
        if "booleanValue" not in correct_raw:
            unresolved += 1
            continue

        correct = correct_raw["booleanValue"]
        league = _fv(fields, "league") or "UNKNOWN"

        by_league[league]["total"] += 1
        if correct:
            by_league[league]["correct"] += 1

    logger.info("Docs resueltos: %d | Sin resolver: %d", sum(v["total"] for v in by_league.values()), unresolved)

    # Calcular accuracy por liga
    logger.info("Accuracy por liga:")
    accuracy_map = {}
    for league, counts in sorted(by_league.items()):
        total = counts["total"]
        correct = counts["correct"]
        acc = round(correct / total, 4) if total > 0 else 0.0
        accuracy_map[league] = acc
        logger.info("  %-8s %d/%d = %.1f%%", league, correct, total, acc * 100)

    if not accuracy_map:
        logger.warning("No hay docs resueltos — nada que actualizar.")
        return

    # Construir mapValue para Firestore
    map_fields = {k: {"doubleValue": v} for k, v in accuracy_map.items()}

    # PATCH usando updateMask con el campo de mapa completo
    update_mask = "updateMask.fieldPaths=accuracy_by_league"
    url = f"{BASE}/{PREFIX}model_weights/current?{update_mask}"
    body = {
        "fields": {
            "accuracy_by_league": {
                "mapValue": {"fields": map_fields}
            }
        }
    }

    try:
        _request(url, method="PATCH", body=body)
        logger.info("prodmodel_weights/current.accuracy_by_league actualizado con %d ligas.", len(accuracy_map))
    except Exception as e:
        logger.error("ERROR actualizando model_weights: %s", e)
        raise

    logger.info("=== Completado ===")


if __name__ == "__main__":
    main()
