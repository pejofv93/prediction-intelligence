"""
scripts/backfill_predictions_result.py

Backfill para corregir las predicciones de prodpredictions donde
el campo `result` fue almacenado como map (dict) en lugar de string.

Causa raíz: check_result() retornaba el dict completo de get_match_result()
en lugar de extraer result["result"]. Corregido en learning_engine.py.

Acciones:
1. Lee todos los docs de prodpredictions donde result es mapValue
2. Extrae el string real (HOME_WIN / AWAY_WIN / DRAW) del map
3. Re-evalúa correct comparando team_to_back con el string
4. Sobrescribe result (string), correct (bool), error_type en Firestore
5. Crea o actualiza el shadow_trade correspondiente con win/loss
6. Recalcula model_weights.correct_predictions y accuracy

Uso:
    python3 scripts/backfill_predictions_result.py

Requiere:
    - gcloud auth print-access-token --account pejocanal@gmail.com (activo)
    - El token debe pertenecer a una cuenta con roles/datastore.owner o roles/owner
"""

import json
import logging
import os
import subprocess
import sys
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")

PROJECT = "prediction-intelligence"
PREFIX = "prod"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
ACCOUNT = "pejocanal@gmail.com"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", f"--account={ACCOUNT}"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(f"No se pudo obtener token para {ACCOUNT}. Comprueba: gcloud auth list")
    return token


# ── REST helpers ──────────────────────────────────────────────────────────────

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
    """Lista todos los docs de una colección con paginación."""
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
    """Extrae el valor Python de un campo Firestore."""
    v = fields.get(key, {})
    if "stringValue" in v:
        return v["stringValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "mapValue" in v:
        return v["mapValue"].get("fields", {})  # devuelve el dict interno
    if "nullValue" in v:
        return None
    return None


def _to_fs_value(val) -> dict:
    """Convierte valor Python a Firestore value."""
    if val is None:
        return {"nullValue": None}
    if isinstance(val, bool):
        return {"booleanValue": val}
    if isinstance(val, int):
        return {"integerValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": val}
    return {"stringValue": str(val)}


def _patch_doc(collection: str, doc_id: str, fields: dict) -> None:
    """PATCH (update) de campos específicos en un doc."""
    update_mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    url = f"{BASE}/{PREFIX}{collection}/{doc_id}?{update_mask}"
    fs_fields = {k: _to_fs_value(v) for k, v in fields.items()}
    _request(url, method="PATCH", body={"fields": fs_fields})


def _set_doc(collection: str, doc_id: str, fields: dict) -> None:
    """SET (create or overwrite) de un doc."""
    url = f"{BASE}/{PREFIX}{collection}/{doc_id}"
    fs_fields = {k: _to_fs_value(v) for k, v in fields.items()}
    _request(url, method="PATCH", body={"fields": fs_fields})


def _query_shadow_trades(signal_id: str) -> list[dict]:
    """Busca shadow_trades por signal_id=signal_id y source=sports."""
    url = f"{BASE}:runQuery"
    body = {
        "structuredQuery": {
            "from": [{"collectionId": f"{PREFIX}shadow_trades"}],
            "where": {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        {"fieldFilter": {"field": {"fieldPath": "signal_id"}, "op": "EQUAL",
                                         "value": {"stringValue": signal_id}}},
                        {"fieldFilter": {"field": {"fieldPath": "source"}, "op": "EQUAL",
                                         "value": {"stringValue": "sports"}}},
                    ],
                }
            },
            "limit": 1,
        }
    }
    results = _request(url, method="POST", body=body)
    return [r for r in results if "document" in r]


# ── Lógica de evaluate_prediction ─────────────────────────────────────────────

_SIGNAL_TO_ERROR = {
    "poisson": "poisson_overweighted",
    "elo":     "elo_misleading",
    "form":    "form_misleading",
    "h2h":     "h2h_irrelevant",
}

_DEFAULT_WEIGHTS = {
    "poisson": 0.40, "elo": 0.25, "form": 0.20, "h2h": 0.15,
}


def _evaluate(data: dict, fields: dict, actual_result_str: str) -> dict:
    team_to_back = _fv(fields, "team_to_back") or ""
    home_team = _fv(fields, "home_team") or ""
    away_team = _fv(fields, "away_team") or ""
    home_team_id = str(_fv(fields, "home_team_id") or "")
    away_team_id = str(_fv(fields, "away_team_id") or "")

    if team_to_back == home_team or team_to_back == home_team_id:
        correct = (actual_result_str == "HOME_WIN")
    elif team_to_back == away_team or team_to_back == away_team_id:
        correct = (actual_result_str == "AWAY_WIN")
    else:
        logger.warning("  team_to_back=%r no coincide home=%r away=%r", team_to_back, home_team, away_team)
        correct = False

    if correct:
        return {"correct": True, "error_type": None}

    data_source = _fv(fields, "data_source") or "statistical_model"
    if data_source != "statistical_model":
        return {"correct": False, "error_type": None}

    factors_raw = fields.get("factors", {}).get("mapValue", {}).get("fields", {})
    factors = {k: float(v.get("doubleValue", v.get("integerValue", 0))) for k, v in factors_raw.items()}

    if not factors:
        return {"correct": False, "error_type": "poisson_overweighted"}

    relevant = {k: v for k, v in factors.items() if k in _SIGNAL_TO_ERROR}
    if not relevant:
        return {"correct": False, "error_type": "odds_inefficiency"}

    odds = float(_fv(fields, "odds") or 2.0)
    edge = float(_fv(fields, "edge") or 0.0)
    if edge > 0.15 and odds < 1.5:
        return {"correct": False, "error_type": "odds_inefficiency"}

    dominant = max(relevant, key=lambda k: relevant[k])
    return {"correct": False, "error_type": _SIGNAL_TO_ERROR.get(dominant, "poisson_overweighted")}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=== Backfill prodpredictions — inicio ===")
    logger.info("Proyecto: %s | Prefix: %s | Cuenta: %s", PROJECT, PREFIX, ACCOUNT)

    # 1. Obtener token
    token = _get_token()
    logger.info("Token obtenido (%d chars)", len(token))

    # 2. Leer todos los docs de prodpredictions
    logger.info("Leyendo prodpredictions...")
    all_docs = _list_collection("predictions")
    logger.info("Total docs: %d", len(all_docs))

    # 3. Filtrar los que tienen result como mapValue (el bug)
    to_fix = []
    for doc in all_docs:
        doc_id = doc["name"].split("/")[-1]
        fields = doc.get("fields", {})
        result_raw = fields.get("result", {})
        # El bug: result es mapValue con campo "result" dentro
        if "mapValue" in result_raw:
            inner = result_raw["mapValue"].get("fields", {})
            if "result" in inner:
                to_fix.append((doc_id, fields))

    logger.info("Docs con result=map (bug): %d de %d", len(to_fix), len(all_docs))

    if not to_fix:
        logger.info("Nada que corregir.")
        return

    # 4. Re-evaluar cada predicción
    corrections = []
    for doc_id, fields in to_fix:
        result_raw = fields["result"]["mapValue"]["fields"]
        actual_result_str = result_raw.get("result", {}).get("stringValue", "")
        goals_h = result_raw.get("goals_home", {}).get("integerValue", "?")
        goals_a = result_raw.get("goals_away", {}).get("integerValue", "?")

        if not actual_result_str:
            logger.warning("  doc %s: result map sin campo 'result' — omitiendo", doc_id)
            continue

        evaluation = _evaluate({}, fields, actual_result_str)
        corrections.append({
            "doc_id": doc_id,
            "match_id": str(_fv(fields, "match_id") or doc_id),
            "home_team": _fv(fields, "home_team") or "",
            "away_team": _fv(fields, "away_team") or "",
            "team_to_back": _fv(fields, "team_to_back") or "",
            "league": _fv(fields, "league") or "",
            "sport": _fv(fields, "sport") or "football",
            "odds": float(_fv(fields, "odds") or 2.0),
            "edge": float(_fv(fields, "edge") or 0.0),
            "result_str": actual_result_str,
            "goals": f"{goals_h}-{goals_a}",
            "correct": evaluation["correct"],
            "error_type": evaluation["error_type"],
            "created_at": _fv(fields, "created_at"),
        })

    # 5. Mostrar resumen antes de escribir
    n_true = sum(1 for c in corrections if c["correct"])
    n_false = sum(1 for c in corrections if not c["correct"])
    logger.info("Correcciones: %d total — %d TRUE, %d FALSE", len(corrections), n_true, n_false)
    for c in corrections:
        mark = "TRUE " if c["correct"] else "false"
        logger.info(
            "  %s | %-20s | %-8s | %s | => %s",
            c["doc_id"], c["team_to_back"][:20], c["result_str"], c["goals"], mark,
        )

    # 6. Escribir correcciones en Firestore
    logger.info("Actualizando prodpredictions en Firestore...")
    ok = err = 0
    for c in corrections:
        try:
            _patch_doc("predictions", c["doc_id"], {
                "result": c["result_str"],
                "correct": c["correct"],
                "error_type": c["error_type"],
            })
            ok += 1
        except Exception as e:
            logger.error("  ERROR actualizando %s: %s", c["doc_id"], e)
            err += 1

    logger.info("prodpredictions: %d OK, %d errores", ok, err)

    # 7. Crear/actualizar shadow_trades
    logger.info("Sincronizando prodshadow_trades...")
    now_iso = datetime.now(timezone.utc).isoformat()
    st_created = st_updated = 0

    for c in corrections:
        shadow_result = "win" if c["correct"] else "loss"
        pnl = round((c["odds"] - 1.0) if c["correct"] else -1.0, 4)

        try:
            existing = _query_shadow_trades(c["match_id"])
            if existing:
                trade_doc = existing[0]["document"]
                trade_id = trade_doc["name"].split("/")[-1]
                _patch_doc("shadow_trades", trade_id, {
                    "result": shadow_result,
                    "pnl": pnl,
                    "resolved_at": now_iso,
                })
                st_updated += 1
                logger.info("  shadow_trade actualizado: %s -> %s (pnl=%.2f)", c["match_id"], shadow_result, pnl)
            else:
                trade_id = str(uuid.uuid4())
                _set_doc("shadow_trades", trade_id, {
                    "id": trade_id,
                    "signal_id": c["match_id"],
                    "source": "sports",
                    "sport": c["sport"],
                    "league": c["league"],
                    "home_team": c["home_team"],
                    "away_team": c["away_team"],
                    "team_to_back": c["team_to_back"],
                    "odds": c["odds"],
                    "edge": c["edge"],
                    "stake": 10.0,
                    "result": shadow_result,
                    "pnl": pnl,
                    "created_at": c["created_at"] or now_iso,
                    "resolved_at": now_iso,
                })
                st_created += 1
                logger.info("  shadow_trade creado: %s -> %s (pnl=%.2f)", c["match_id"], shadow_result, pnl)
        except Exception as e:
            logger.error("  ERROR shadow_trade %s: %s", c["match_id"], e)

    logger.info("shadow_trades: %d creados, %d actualizados", st_created, st_updated)

    # 8. Recalcular accuracy en model_weights
    logger.info("Recalculando accuracy en prodmodel_weights...")
    all_pred_docs = _list_collection("predictions")
    total_eval = correct_count = 0
    for doc in all_pred_docs:
        fields = doc.get("fields", {})
        c = _fv(fields, "correct")
        if c is True:
            total_eval += 1
            correct_count += 1
        elif c is False:
            total_eval += 1

    new_accuracy = round(correct_count / total_eval, 4) if total_eval > 0 else 0.0
    logger.info("Accuracy: %d/%d = %.1f%%", correct_count, total_eval, new_accuracy * 100)

    try:
        _patch_doc("model_weights", "current", {
            "total_predictions": total_eval,
            "correct_predictions": correct_count,
            "accuracy": new_accuracy,
            "backfill_at": now_iso,
            "backfill_note": "backfill_predictions_result.py — fix check_result() dict->str",
        })
        logger.info("prodmodel_weights/current actualizado")
    except Exception as e:
        logger.error("ERROR actualizando model_weights: %s", e)

    # 9. Resumen final
    logger.info("=== Backfill completado ===")
    logger.info("  Predicciones corregidas: %d", len(corrections))
    logger.info("  correct=True:  %d (%.1f%%)", n_true, 100 * n_true / len(corrections) if corrections else 0)
    logger.info("  correct=False: %d (%.1f%%)", n_false, 100 * n_false / len(corrections) if corrections else 0)
    logger.info("  Accuracy global: %.1f%%", new_accuracy * 100)
    logger.info("  shadow_trades creados:    %d", st_created)
    logger.info("  shadow_trades actualizados: %d", st_updated)


if __name__ == "__main__":
    main()
