"""
Backtesting histórico Polymarket.

Fetch mercados cerrados de Gamma API → compara precio a mitad de vida
(desde poly_price_history) vs resultado real (winner YES/NO).
Calcula accuracy por categoría y calibra umbrales de confianza.

Fuente de datos:
  - Gamma API: GET /markets?closed=true&limit=N  → mercados resueltos
  - Firestore poly_price_history                 → snapshots de precio propios

Colecciones Firestore producidas:
  - poly_backtest_results (doc con timestamp)    → métricas globales + por categoría
  - poly_model_weights/backtest_thresholds        → umbrales calibrados por percentil

Entry point: await run_backtest(limit=200)
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
_HTTP_TIMEOUT = 20.0

# Categorías detectables por palabras clave en la pregunta del mercado
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Sports":   ["win", "championship", "playoffs", "super bowl", "world cup", "nba", "nfl",
                 "mlb", "nhl", "ufc", "fight", "match", "title", "game", "series",
                 "liga", "premier", "bundesliga"],
    "Politics": ["election", "president", "senate", "congress", "vote", "poll", "party",
                 "democrat", "republican", "prime minister", "chancellor", "referendum"],
    "Crypto":   ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "defi", "nft",
                 "token", "blockchain", "stablecoin", "coinbase", "binance"],
    "Finance":  ["fed", "rate", "inflation", "gdp", "market", "s&p", "dow", "nasdaq",
                 "recession", "interest", "economy"],
    "Other":    [],
}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def fetch_closed_markets(limit: int = 200) -> list[dict]:
    """
    GET /markets?closed=true&limit={limit} de Gamma API.
    Normaliza cada mercado a {market_id, question, category, volume, winner, end_date}.
    Descarta mercados sin winner claro o sin outcomePrices.
    """
    results: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={"closed": "true", "order": "volume24hr", "limit": str(limit)},
            )
        if resp.status_code != 200:
            logger.error("fetch_closed_markets: Gamma API respondió %d", resp.status_code)
            return []
        raw = resp.json()
        raw_list = raw if isinstance(raw, list) else raw.get("markets", raw.get("data", []))
    except Exception:
        logger.error("fetch_closed_markets: error de red", exc_info=True)
        return []

    for item in raw_list:
        winner = _determine_winner(item)
        if winner is None:
            continue
        market_id = str(item.get("id") or item.get("conditionId") or "")
        if not market_id:
            continue
        question = str(item.get("question", "")).strip()
        volume = _safe_float(item.get("volume") or item.get("volumeClam") or 0)
        end_date_raw = item.get("endDate") or item.get("closedTime") or item.get("end_date")
        end_date = _parse_dt(end_date_raw)
        results.append({
            "market_id": market_id,
            "question":  question,
            "category":  _categorize_market(question),
            "volume":    volume,
            "winner":    winner,
            "end_date":  end_date,
        })

    logger.info("fetch_closed_markets: %d mercados resueltos (de %d raw)", len(results), len(raw_list))
    return results


def _determine_winner(raw: dict) -> Optional[str]:
    """
    Extrae 'YES' o 'NO' del mercado resuelto.
    Prioridad: campo winner → outcomePrices (precio ~1.0 indica ganador).
    Devuelve None si no es determinable.
    """
    # Campo winner explícito
    w = raw.get("winner") or raw.get("winningOutcome") or ""
    if isinstance(w, str) and w.lower() in ("yes", "no"):
        return w.upper()

    # Inferir desde outcomePrices: ["0.001", "0.999"] → YES=idx0 si ~1, NO si ~0
    op = raw.get("outcomePrices") or []
    if isinstance(op, str):
        import json
        try:
            op = json.loads(op)
        except Exception:
            op = []
    if isinstance(op, list) and len(op) >= 2:
        try:
            p0 = float(op[0])
            if p0 > 0.9:
                return "YES"
            if p0 < 0.1:
                return "NO"
        except (ValueError, TypeError):
            pass

    return None


def _categorize_market(question: str) -> str:
    """Clasifica la pregunta en una categoría por palabras clave."""
    q = question.lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if cat == "Other":
            continue
        if any(kw in q for kw in keywords):
            return cat
    return "Other"


def _parse_dt(raw) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Price history lookup
# ---------------------------------------------------------------------------

async def _get_mid_price(market_id: str, end_date: Optional[datetime]) -> Optional[float]:
    """
    Obtiene el precio YES a mitad de vida del mercado desde poly_price_history.
    'Mitad de vida' = snapshot más cercano al punto medio entre primer y último registro.
    Devuelve None si no hay registros para este mercado.
    """
    try:
        from shared.firestore_client import col
        from google.cloud.firestore_v1.base_query import FieldFilter

        snapshots = list(
            col("poly_price_history")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .order_by("timestamp")
            .stream()
        )
        if not snapshots:
            return None

        dicts = [s.to_dict() for s in snapshots]

        # Extraer timestamps
        tss = []
        for d in dicts:
            ts = d.get("timestamp")
            if ts is None:
                continue
            if isinstance(ts, datetime):
                tss.append((ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts, d))
            elif hasattr(ts, "timestamp"):
                tss.append((datetime.fromtimestamp(float(ts.timestamp()), tz=timezone.utc), d))

        if not tss:
            return None

        if len(tss) == 1:
            return _safe_float(tss[0][1].get("price_yes", 0.5)) or None

        # Punto medio temporal
        t_first = tss[0][0]
        t_last  = tss[-1][0]
        t_mid   = t_first + (t_last - t_first) / 2

        # Snapshot más cercano al punto medio
        closest = min(tss, key=lambda x: abs((x[0] - t_mid).total_seconds()))
        price = _safe_float(closest[1].get("price_yes", 0.5))
        return price if price > 0 else None

    except Exception:
        logger.debug("_get_mid_price(%s): error leyendo Firestore", market_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Simulate & metrics
# ---------------------------------------------------------------------------

async def simulate_predictions(markets: list[dict]) -> list[dict]:
    """
    Para cada mercado, busca nuestro precio a mitad de vida en poly_price_history.
    Si lo tenemos: simula si el modelo habría predicho YES (price > 0.55) o NO (price < 0.45).
    Mercados no rastreados (sin precio histórico): excluidos del resultado.

    Devuelve lista de {market_id, question, category, volume, winner,
                        mid_price, predicted, correct, confidence}.
    """
    results: list[dict] = []
    skipped = 0

    for m in markets:
        mid_price = await _get_mid_price(m["market_id"], m.get("end_date"))
        if mid_price is None:
            skipped += 1
            continue

        # Señal del modelo: solo actuar si alejado de 0.5 (umbral base 0.55)
        if mid_price > 0.55:
            predicted = "YES"
            confidence = round(mid_price - 0.5, 4)
        elif mid_price < 0.45:
            predicted = "NO"
            confidence = round(0.5 - mid_price, 4)
        else:
            # Precio demasiado cercano a 0.5 → sin señal clara, excluir
            skipped += 1
            continue

        correct = (predicted == m["winner"])

        results.append({
            "market_id": m["market_id"],
            "question":  m["question"][:100],
            "category":  m["category"],
            "volume":    m["volume"],
            "winner":    m["winner"],
            "mid_price": round(mid_price, 4),
            "predicted": predicted,
            "correct":   correct,
            "confidence": confidence,
        })

    logger.info(
        "simulate_predictions: %d analizados, %d excluidos (sin historial o precio neutro)",
        len(results), skipped,
    )
    return results


def calculate_category_accuracy(results: list[dict]) -> dict[str, dict]:
    """
    Calcula accuracy, tamaño de muestra y volumen promedio por categoría.
    Devuelve {category: {accuracy, n_markets, avg_volume, correct}}.
    """
    by_cat: dict[str, dict] = {}
    for r in results:
        cat = r.get("category", "Other")
        if cat not in by_cat:
            by_cat[cat] = {"correct": 0, "total": 0, "volume_sum": 0.0}
        by_cat[cat]["correct"] += int(r["correct"])
        by_cat[cat]["total"]   += 1
        by_cat[cat]["volume_sum"] += float(r.get("volume", 0))

    out: dict[str, dict] = {}
    for cat, d in by_cat.items():
        n = d["total"]
        out[cat] = {
            "accuracy":   round(d["correct"] / n, 4) if n > 0 else 0.0,
            "n_markets":  n,
            "avg_volume": round(d["volume_sum"] / n, 0) if n > 0 else 0.0,
            "correct":    d["correct"],
        }
    return out


def calibrate_thresholds(results: list[dict]) -> dict[str, float]:
    """
    Para cada umbral de confianza (distancia de mid_price a 0.5):
      filtra los resultados donde confidence >= umbral
      calcula el win rate en ese subconjunto.

    Devuelve {str(threshold): win_rate} para umbrales 0.05..0.30 en pasos de 0.05.
    Un umbral de confianza 0.10 = precio >= 0.60 o <= 0.40.
    """
    thresholds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    out: dict[str, float] = {}
    for thr in thresholds:
        subset = [r for r in results if float(r.get("confidence", 0)) >= thr]
        if not subset:
            out[str(thr)] = 0.0
            continue
        correct = sum(1 for r in subset if r["correct"])
        out[str(thr)] = round(correct / len(subset), 4)
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_backtest(limit: int = 200, days_back: int = 90) -> dict:
    """
    Orquesta el backtest completo:
    1. Fetch mercados cerrados de Gamma API
    2. Simula predicciones vs precio mid-life (poly_price_history)
    3. Calcula accuracy global + por categoría
    4. Calibra umbrales de confianza
    5. Guarda resultados en Firestore

    Devuelve dict con métricas para el endpoint /run-poly-backtest.
    """
    logger.info("run_backtest: iniciando — limit=%d mercados cerrados", limit)

    # 1. Fetch
    markets = await fetch_closed_markets(limit=limit)
    if not markets:
        logger.warning("run_backtest: sin mercados cerrados disponibles")
        return {"status": "no_data", "markets_fetched": 0}

    # 2. Simulate
    results = await simulate_predictions(markets)
    if not results:
        logger.warning("run_backtest: sin mercados rastreados en poly_price_history")
        return {
            "status": "no_history",
            "markets_fetched": len(markets),
            "markets_analyzed": 0,
        }

    # 3. Métricas globales
    n = len(results)
    correct = sum(1 for r in results if r["correct"])
    accuracy = round(correct / n, 4) if n > 0 else 0.0
    avg_vol  = round(sum(r.get("volume", 0) for r in results) / n, 0) if n > 0 else 0.0

    # 4. Por categoría + calibración
    by_category = calculate_category_accuracy(results)
    thresholds  = calibrate_thresholds(results)

    logger.info(
        "run_backtest: n=%d accuracy=%.1f%% categorías=%s",
        n, accuracy * 100, {c: f"{v['accuracy']:.1%}" for c, v in by_category.items()},
    )
    logger.info("run_backtest: calibración umbrales → %s", thresholds)

    # 5. Persistir en Firestore
    now = datetime.now(timezone.utc)
    payload = {
        "run_date":          now,
        "markets_fetched":   len(markets),
        "markets_analyzed":  n,
        "correct":           correct,
        "accuracy":          accuracy,
        "avg_volume":        avg_vol,
        "by_category":       by_category,
        "thresholds":        thresholds,
        "created_at":        now,
    }
    try:
        from shared.firestore_client import col
        col("poly_backtest_results").add(payload)
        logger.info("run_backtest: resultado guardado en poly_backtest_results")
    except Exception:
        logger.error("run_backtest: error guardando en Firestore", exc_info=True)

    # Guardar umbrales calibrados en poly_model_weights/backtest_thresholds
    # para que poly_learning_engine los use como referencia
    try:
        from shared.firestore_client import col as _col
        _col("poly_model_weights").document("backtest_thresholds").set({
            "thresholds":    thresholds,
            "by_category":   by_category,
            "accuracy":      accuracy,
            "sample_size":   n,
            "updated_at":    now,
        })
        logger.info("run_backtest: umbrales calibrados guardados en poly_model_weights/backtest_thresholds")
    except Exception:
        logger.error("run_backtest: error guardando umbrales", exc_info=True)

    return {
        "status":           "ok",
        "markets_fetched":  len(markets),
        "markets_analyzed": n,
        "accuracy":         accuracy,
        "by_category":      by_category,
        "thresholds":       thresholds,
        "avg_volume":       avg_vol,
    }


# Alias para compatibilidad con main.py
run_poly_backtest = run_backtest
