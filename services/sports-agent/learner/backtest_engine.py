"""
Backtest de producción — evalúa predictions resueltas de Firestore.
A diferencia de backtester/backtest_engine.py (que usa API externa de fixtures históricos),
este módulo trabaja con los datos reales de producción: predictions donde correct != None.

Calcula accuracy por:
  - Liga (PL, PD, SA, BL1, FL1, CL)
  - Mercado (h2h, totals, btts, spread, ARBITRAGE)
  - Rango de edge: low [0.08-0.12), mid [0.12-0.15), high [≥0.15]
  - Rango de confianza: low [0.65-0.75), mid [0.75-0.85), high [≥0.85]

Resultado guardado en Firestore model_weights/current como weights_by_accuracy,
usado por load_weights() para calibrar el ensemble en lugar del cold-start.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone

from google.cloud.firestore_v1.base_query import FieldFilter
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_EDGE_BUCKETS = [
    ("low",  0.08, 0.12),
    ("mid",  0.12, 0.15),
    ("high", 0.15, 1.00),
]
_CONF_BUCKETS = [
    ("low",  0.65, 0.75),
    ("mid",  0.75, 0.85),
    ("high", 0.85, 1.00),
]


def _bucket_label(value: float, buckets: list[tuple]) -> str:
    for label, lo, hi in buckets:
        if lo <= value < hi:
            return label
    return "other"


def _compute_accuracy(records: list[dict]) -> dict:
    """Devuelve {n, correct, accuracy} para una lista de predictions."""
    n = len(records)
    correct = sum(1 for r in records if r.get("correct") is True)
    return {"n": n, "correct": correct, "accuracy": round(correct / n, 4) if n > 0 else 0.0}


async def run_production_backtest() -> dict:
    """
    1. Carga todas las predictions resueltas (correct != None) de Firestore.
    2. Calcula accuracy multidimensional.
    3. Guarda resultados en:
       - Firestore model_weights/current → campo weights_by_accuracy
       - Firestore backtest_results/{iso_date} → snapshot completo
    4. Devuelve dict con resumen.
    """
    logger.info("production_backtest: cargando predictions resueltas de Firestore")

    try:
        resolved_docs = list(
            col("predictions")
            .where(filter=FieldFilter("correct", "!=", None))
            .limit(5000)
            .stream()
        )
    except Exception as e:
        logger.error("production_backtest: error leyendo predictions — %s", e)
        return {"error": str(e), "n": 0}

    if not resolved_docs:
        logger.warning("production_backtest: 0 predictions resueltas encontradas")
        return {"n": 0, "message": "sin datos resueltos"}

    records = [d.to_dict() for d in resolved_docs]
    logger.info("production_backtest: %d predictions resueltas", len(records))

    # ── Accuracy por liga ─────────────────────────────────────────────────
    by_league: dict[str, list] = defaultdict(list)
    for r in records:
        lg = str(r.get("league") or "UNKNOWN")
        by_league[lg].append(r)

    accuracy_by_league = {lg: _compute_accuracy(recs) for lg, recs in by_league.items()}

    # ── Accuracy por mercado ──────────────────────────────────────────────
    by_market: dict[str, list] = defaultdict(list)
    for r in records:
        mt = str(r.get("market_type") or "h2h").lower()
        by_market[mt].append(r)

    accuracy_by_market = {mt: _compute_accuracy(recs) for mt, recs in by_market.items()}

    # ── Accuracy por rango de edge ────────────────────────────────────────
    by_edge: dict[str, list] = defaultdict(list)
    for r in records:
        edge = float(r.get("edge") or 0)
        label = _bucket_label(abs(edge), _EDGE_BUCKETS)
        by_edge[label].append(r)

    accuracy_by_edge = {lbl: _compute_accuracy(recs) for lbl, recs in by_edge.items()}

    # ── Accuracy por rango de confianza ───────────────────────────────────
    by_conf: dict[str, list] = defaultdict(list)
    for r in records:
        conf = float(r.get("confidence") or 0)
        label = _bucket_label(conf, _CONF_BUCKETS)
        by_conf[label].append(r)

    accuracy_by_conf = {lbl: _compute_accuracy(recs) for lbl, recs in by_conf.items()}

    # ── Accuracy global ───────────────────────────────────────────────────
    global_metrics = _compute_accuracy(records)

    # ── Construir weights_by_accuracy para calibrar el modelo ─────────────
    # Si una liga tiene accuracy < 40% con ≥10 muestras → reducir peso 20%
    # Si accuracy > 60% con ≥10 muestras → aumentar peso 20%
    league_weight_adj: dict[str, float] = {}
    for lg, stats in accuracy_by_league.items():
        if stats["n"] < 10:
            continue
        acc = stats["accuracy"]
        if acc < 0.40:
            league_weight_adj[lg] = 0.80
        elif acc > 0.60:
            league_weight_adj[lg] = 1.20
        else:
            league_weight_adj[lg] = 1.00

    # Threshold recomendado por bucket de edge (accuracy mínima para confiar)
    edge_thresholds: dict[str, float] = {}
    for lbl, stats in accuracy_by_edge.items():
        if stats["n"] >= 5:
            edge_thresholds[lbl] = stats["accuracy"]

    weights_by_accuracy = {
        "by_league":  accuracy_by_league,
        "by_market":  accuracy_by_market,
        "by_edge":    accuracy_by_edge,
        "by_conf":    accuracy_by_conf,
        "global":     global_metrics,
        "league_weight_adj": league_weight_adj,
        "edge_thresholds": edge_thresholds,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_resolved":  len(records),
    }

    # ── Persistir en Firestore ────────────────────────────────────────────
    try:
        col("model_weights").document("current").set(
            {"weights_by_accuracy": weights_by_accuracy},
            merge=True,
        )
        logger.info(
            "production_backtest: weights_by_accuracy guardado — global acc=%.1f%% n=%d",
            global_metrics["accuracy"] * 100, len(records),
        )
    except Exception as e:
        logger.error("production_backtest: error guardando model_weights — %s", e)

    # Snapshot completo en backtest_results
    try:
        iso_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        col("backtest_results").document(f"prod_{iso_date}").set({
            "type": "production",
            "run_at": datetime.now(timezone.utc),
            "summary": weights_by_accuracy,
        })
    except Exception as e:
        logger.warning("production_backtest: error guardando backtest_results — %s", e)

    summary = {
        "n_resolved": len(records),
        "global_accuracy": global_metrics["accuracy"],
        "global_correct": global_metrics["correct"],
        "leagues_analyzed": len(accuracy_by_league),
        "markets_analyzed": len(accuracy_by_market),
        "league_weight_adj": league_weight_adj,
        "edge_thresholds": edge_thresholds,
    }
    logger.info("production_backtest: completado — %s", summary)
    return summary
