"""
API endpoints: GET /predictions, GET /stats
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from shared.firestore_client import col

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize(doc: dict) -> dict:
    """Convierte datetimes a ISO strings para JSON."""
    out = {}
    for k, v in doc.items():
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = _serialize(v)
        else:
            out[k] = v
    return out


@router.get("/predictions")
async def get_predictions(limit: int = 50) -> list[dict]:
    """
    Ultimas N predicciones ordenadas por created_at DESC (max 200).
    Incluye todos los campos necesarios para mercados múltiples.
    """
    limit = max(1, min(limit, 200))
    try:
        docs = (
            col("predictions")
            .order_by("created_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        fields = {
            "match_id", "home_team", "away_team", "league", "match_date",
            "team_to_back", "odds", "edge", "confidence", "factors", "signals",
            "result", "correct", "sport", "kelly_fraction",
            "market_type", "selection", "bookmaker", "line",
            "elo_sufficient", "h2h_sufficient", "data_source",
            "filtered_reason", "created_at",
        }
        result = []
        for d in docs:
            raw = d.to_dict()
            filtered = {k: v for k, v in raw.items() if k in fields}
            filtered["low_confidence"] = float(raw.get("confidence") or 1.0) < 0.65
            result.append(_serialize(filtered))
        return result
    except Exception:
        logger.error("get_predictions: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error consultando predicciones")


@router.get("/stats")
async def get_stats() -> dict:
    """
    Lee model_weights doc 'current' + accuracy_log.
    Devuelve: accuracy_global, accuracy_by_league, weights,
              weights_history (ultimas 10 semanas), total_predictions, correct_predictions.
    """
    try:
        weights_doc = col("model_weights").document("current").get()
        weights_data = weights_doc.to_dict() if weights_doc.exists else {}

        # Historial de pesos: ultimas 10 entradas de accuracy_log
        log_docs = (
            col("accuracy_log")
            .order_by("week", direction="DESCENDING")
            .limit(10)
            .stream()
        )
        history = []
        for d in log_docs:
            entry = d.to_dict()
            history.append({
                "week": entry.get("week", ""),
                "accuracy": float(entry.get("accuracy", 0)),
                "weights": entry.get("weights_end", {}),
                "predictions_total": int(entry.get("predictions_total", 0)),
                "predictions_correct": int(entry.get("predictions_correct", 0)),
            })

        # Accuracy global desde el doc de pesos (suma de todo el historial)
        total = int(weights_data.get("total_predictions", 0))
        correct = int(weights_data.get("correct_predictions", 0))
        accuracy_global = round(correct / total, 4) if total > 0 else 0.0

        return {
            "accuracy_global": accuracy_global,
            "accuracy_by_league": weights_data.get("accuracy_by_league", {}),
            "weights": weights_data.get("weights", {}),
            "weights_version": int(weights_data.get("version", 0)),
            "weights_history": history,
            "total_predictions": total,
            "correct_predictions": correct,
            "min_edge_threshold": float(weights_data.get("min_edge_threshold", 0.08)),
            "min_confidence": float(weights_data.get("min_confidence", 0.65)),
        }
    except Exception:
        logger.error("get_stats: error", exc_info=True)
        raise HTTPException(status_code=500, detail="Error consultando estadísticas")
