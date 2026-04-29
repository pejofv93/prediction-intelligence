"""
scripts/backfill_filtered_reason.py

Para cada doc pendiente en prodpredictions (result=null / correct=null),
evalúa si pasaría los filtros actuales del value_bet_engine.
Si no pasa, añade el campo filtered_reason con el motivo.

Filtros evaluados (los que pueden aplicarse con los datos guardados):
  - underdog_extremo : odds > 4.5 (PD/SA/PL/BL1) ó > 5.0 (resto)
  - away_zona_muerta : AWAY + odds entre 2.5 y 3.5
  - away_pd_ded      : AWAY + league PD/DED + odds > 2.5
  - away_gate        : AWAY + no pasa el gate final (odds ≥2.5 y no (>3.5+conf>0.85))

NO evaluable en retroactivo:
  - draw_filter (p_draw > 0.30) — p_draw no se guarda en la predicción

Uso:
    python3 scripts/backfill_filtered_reason.py [--dry-run]

Requiere:
    - gcloud auth print-access-token --account pejocanal@gmail.com (activo)
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DRY_RUN = "--dry-run" in sys.argv

PROJECT_ID = "prediction-intelligence"
DATABASE_ID = "(default)"
COLLECTION = "prodpredictions"
BASE_URL = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/{DATABASE_ID}/documents"

_TOP6_LEAGUES = {"PD", "SA", "PL", "BL1", "CL", "FL1", "DED", "BSA"}
_UNDERDOG_THRESH = {"PD": 4.5, "SA": 4.5, "PL": 4.5, "BL1": 4.5}
_UNDERDOG_DEFAULT = 5.0


def _token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--account=pejocanal@gmail.com"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("No se pudo obtener token. Comprueba: gcloud auth list")
    return token


def _get(url: str, token: str) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _patch(doc_path: str, fields: dict, token: str) -> None:
    import urllib.request
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    url = f"https://firestore.googleapis.com/v1/{doc_path}?{mask}"
    body = {"fields": {k: {"stringValue": v} for k, v in fields.items()}}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="PATCH",
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def _fv(fields: dict, key: str):
    """Extrae valor de un campo Firestore (maneja stringValue, doubleValue, etc.)."""
    field = fields.get(key, {})
    for t in ("stringValue", "integerValue", "doubleValue", "booleanValue", "nullValue"):
        if t in field:
            v = field[t]
            if t in ("integerValue",):
                return int(v)
            if t in ("doubleValue",):
                return float(v)
            return v
    return None


def _evaluate_filters(fields: dict) -> str | None:
    """
    Evalúa los filtros retroactivos. Devuelve el motivo si no pasaría, None si pasaría.
    """
    odds = _fv(fields, "odds")
    league = _fv(fields, "league") or ""
    team_to_back = _fv(fields, "team_to_back") or ""
    away_team = _fv(fields, "away_team") or ""
    confidence = _fv(fields, "confidence") or 0.0

    if odds is None:
        return None  # sin datos suficientes, no tocar

    try:
        odds = float(odds)
        confidence = float(confidence)
    except (TypeError, ValueError):
        return None

    is_away = (team_to_back == away_team and bool(away_team))

    # Filtro 1: underdog extremo
    thresh = _UNDERDOG_THRESH.get(league, _UNDERDOG_DEFAULT)
    if odds > thresh:
        return "underdog_extremo"

    # Solo aplica a AWAY desde aquí
    if not is_away:
        return None

    # Filtro 2: AWAY zona muerta 2.5–3.5
    if 2.5 <= odds < 3.5:
        return "away_zona_muerta"

    # Filtro 3: AWAY PD/DED con odds > 2.5
    if league in ("PD", "DED") and odds > 2.5:
        return "away_pd_ded"

    # Filtro 4: AWAY gate final — pasa solo si favorito (<2.5) o underdog extremo (>3.5+conf>0.85)
    if not (odds < 2.5 or (odds > 3.5 and confidence > 0.85)):
        return "away_gate"

    return None


def main():
    token = _token()
    logger.info("Token obtenido. DRY_RUN=%s", DRY_RUN)

    # Paginar todos los docs de la colección
    all_docs = []
    page_token = None
    while True:
        url = f"{BASE_URL}/{COLLECTION}?pageSize=300"
        if page_token:
            url += f"&pageToken={page_token}"
        data = _get(url, token)
        all_docs.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    logger.info("Total docs leídos: %d", len(all_docs))

    pending = 0
    already_tagged = 0
    tagged_now = 0
    passes_filter = 0
    errors = 0

    for doc in all_docs:
        fields = doc.get("fields", {})

        # Solo docs pendientes (result=null o correct=null)
        result_val = _fv(fields, "result")
        correct_val = _fv(fields, "correct")
        if result_val is not None or correct_val is not None:
            continue

        pending += 1

        # Si ya tiene filtered_reason, skip
        if _fv(fields, "filtered_reason") is not None:
            already_tagged += 1
            continue

        reason = _evaluate_filters(fields)
        if reason is None:
            passes_filter += 1
            continue

        doc_name = doc["name"]  # proyectos/.../documents/COLLECTION/doc_id
        doc_id = doc_name.split("/")[-1]
        logger.info("  → %s: filtered_reason=%s (league=%s odds=%s)",
                    doc_id, reason,
                    _fv(fields, "league"), _fv(fields, "odds"))

        if not DRY_RUN:
            try:
                _patch(doc_name, {"filtered_reason": reason}, token)
                tagged_now += 1
            except Exception as e:
                logger.error("  ERROR actualizando %s: %s", doc_id, e)
                errors += 1
        else:
            tagged_now += 1

    logger.info("=== Resultado ===")
    logger.info("  Docs totales        : %d", len(all_docs))
    logger.info("  Pendientes (sin res): %d", pending)
    logger.info("  Ya tenían reason    : %d", already_tagged)
    logger.info("  Pasan filtros (OK)  : %d", passes_filter)
    logger.info("  Marcados OBSOLETA   : %d%s", tagged_now, " (dry-run)" if DRY_RUN else "")
    logger.info("  Errores             : %d", errors)


if __name__ == "__main__":
    main()
