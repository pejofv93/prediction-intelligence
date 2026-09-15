"""
API endpoints: GET /trend-signals, GET /trend-accuracy
Lee el feed de tendencias estadísticas (colección Firestore trend_signals,
poblada por services/sports-agent/analyzers/trend_finder.py) y su graduación
(trend_accuracy_log, poblada por analyzers/trend_grader.py).

Aislado del resto del dashboard a propósito: no toca predictions/shadow_trades/
accuracy_log — es una lectura de solo-estadística, sin cuotas ni EV.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/trend-signals")
async def trend_signals(pattern_type: str = "", limit: int = 100) -> dict:
    """
    Últimas señales del feed de tendencias, más recientes primero.
    pattern_type: "series" | "rolling" | "" (todas).
    """
    from shared.firestore_client import col

    signals: list[dict] = []
    try:
        for d in col("trend_signals").stream():
            doc = (d.to_dict() or {}) | {"id": d.id}
            if pattern_type and doc.get("pattern_type") != pattern_type:
                continue
            signals.append(doc)
    except Exception as e:
        logger.error("trend_signals: error leyendo Firestore — %s", e)
        return {"signals": [], "count": 0, "error": "No se pudieron leer las señales"}

    signals.sort(key=lambda s: s.get("sent_at", ""), reverse=True)
    return {
        "signals": signals[:limit],
        "count": len(signals),
        "pending_grade": sum(1 for s in signals if not s.get("graded")),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/trend-accuracy")
async def trend_accuracy() -> dict:
    """
    Hit-rate real de las tendencias graduadas, separado por tipo de evidencia
    (series = hit-rate real, rolling = promedio — evidencia más débil) y
    desglosado por mercado y por equipo, para poder comparar los dos tipos
    de patrón sin que se mezclen.
    """
    from shared.firestore_client import col

    rows: list[dict] = []
    try:
        for d in col("trend_accuracy_log").stream():
            rows.append(d.to_dict() or {})
    except Exception as e:
        logger.error("trend_accuracy: error leyendo Firestore — %s", e)
        return {"error": "No se pudo leer la graduación", "by_pattern_type": {}, "by_market": {}}

    def _bucket(rows_subset: list[dict]) -> dict:
        n = len(rows_subset)
        hits = sum(1 for r in rows_subset if r.get("result") == "hit")
        return {"n": n, "hits": hits, "hit_rate": round(hits / n, 4) if n else None}

    by_pattern_type: dict[str, dict] = {}
    for pt in ("series", "rolling"):
        by_pattern_type[pt] = _bucket([r for r in rows if r.get("pattern_type") == pt])

    by_market: dict[str, dict] = {}
    markets = {r.get("market") for r in rows if r.get("market")}
    for m in markets:
        by_market[m] = _bucket([r for r in rows if r.get("market") == m])

    by_team: dict[str, dict] = defaultdict(list)
    for r in rows:
        team = r.get("team")
        if team:
            by_team[team].append(r)
    team_ranking = sorted(
        (
            {"team": team, **_bucket(team_rows)}
            for team, team_rows in by_team.items()
            if len(team_rows) >= 3
        ),
        key=lambda t: (t["hit_rate"] or 0),
        reverse=True,
    )

    return {
        "total_graded": len(rows),
        "by_pattern_type": by_pattern_type,
        "by_market": by_market,
        "team_ranking": team_ranking,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
