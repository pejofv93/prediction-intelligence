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
    pattern_type: "series" | "rolling" | "model" | "" (todas).
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
    (series = hit-rate histórico real, rolling = promedio — evidencia más
    débil, model = salida de Poisson/ELO — naturaleza distinta a las otras
    dos) y desglosado por mercado y por equipo, para poder comparar los tres
    sin que se mezclen.
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
        # "void" (DNB anulado por empate) no cuenta ni como acierto ni como
        # fallo — se excluye del hit-rate, pero se informa aparte.
        graded_rows = [r for r in rows_subset if r.get("result") in ("hit", "miss")]
        n = len(graded_rows)
        hits = sum(1 for r in graded_rows if r.get("result") == "hit")
        voids = sum(1 for r in rows_subset if r.get("result") == "void")
        return {"n": n, "hits": hits, "voids": voids, "hit_rate": round(hits / n, 4) if n else None}

    by_pattern_type: dict[str, dict] = {}
    for pt in ("series", "rolling", "model"):
        by_pattern_type[pt] = _bucket([r for r in rows if r.get("pattern_type") == pt])

    by_market: dict[str, dict] = {}
    markets = {r.get("market") for r in rows if r.get("market")}
    for m in markets:
        by_market[m] = _bucket([r for r in rows if r.get("market") == m])

    # Aviso de "listo para normalización por percentil" — alternativa al score
    # crudo (rate*log(n)) que suma rank_and_cap por partido (ver
    # TREND_PERCENTILE_MIN_SAMPLE en shared/config.py) que necesita muestra
    # por mercado para calibrar percentiles con sentido. Se avisa aquí para no
    # depender de acordarse.
    from shared.config import TREND_PERCENTILE_MIN_SAMPLE

    markets_ready = sorted(
        m for m, bucket in by_market.items() if bucket["n"] >= TREND_PERCENTILE_MIN_SAMPLE
    )
    percentile_readiness = {
        "min_sample_per_market": TREND_PERCENTILE_MIN_SAMPLE,
        "markets_ready": markets_ready,
        "note": (
            f"Ya hay {len(markets_ready)} mercado(s) con ≥{TREND_PERCENTILE_MIN_SAMPLE} señales "
            "graduadas — hay muestra suficiente para normalizar su score por percentil "
            "en vez del hit-rate/ratio crudo." if markets_ready else
            f"Ningún mercado llega aún a {TREND_PERCENTILE_MIN_SAMPLE} señales graduadas — "
            "el score crudo sigue siendo la opción razonable."
        ),
    }

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
        "percentile_readiness": percentile_readiness,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
