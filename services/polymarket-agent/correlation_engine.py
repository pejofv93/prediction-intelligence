"""
Correlacion entre mercados Polymarket del mismo grupo tematico.
Detecta divergencias entre mercados correlacionados.
"""
import logging
import math
from datetime import datetime, timedelta, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Grupos tematicos: mercados que deberian moverse juntos
_TOPIC_GROUPS = {
    "crypto_btc": ["bitcoin", "btc", "$150k", "$200k", "$100k", "crypto", "halving"],
    "us_politics": ["trump", "democrat", "republican", "election", "president", "white house"],
    "macro_us": ["fed", "interest rate", "inflation", "cpi", "recession", "jerome powell"],
    "iran_conflict": ["iran", "ceasefire", "nuclear", "hormuz", "tehran"],
    "ukraine_war": ["ukraine", "russia", "zelensky", "nato", "kyiv"],
}


def assign_topic_group(question: str) -> str | None:
    """Asigna un mercado a su grupo tematico. Devuelve None si no encaja."""
    q_lower = question.lower()
    for group, keywords in _TOPIC_GROUPS.items():
        if any(kw in q_lower for kw in keywords):
            return group
    return None


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Correlacion de Pearson entre dos listas de igual longitud. Devuelve 0.0 si imposible."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def _align_series(
    snaps_a: list[dict], snaps_b: list[dict]
) -> tuple[list[float], list[float]]:
    """
    Alineacion temporal simple: para cada snapshot de A busca el snapshot de B
    con timestamp mas cercano. Devuelve dos listas de precios alineadas.
    """
    aligned_a: list[float] = []
    aligned_b: list[float] = []

    def _ts(snap: dict) -> datetime:
        t = snap.get("timestamp")
        if t is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if hasattr(t, "tzinfo") and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t

    ts_b = [_ts(s) for s in snaps_b]
    prices_b = [float(s.get("price_yes", 0.5)) for s in snaps_b]

    for snap_a in snaps_a:
        ta = _ts(snap_a)
        pa = float(snap_a.get("price_yes", 0.5))
        # Buscar indice con timestamp mas cercano en B
        closest_idx = min(range(len(ts_b)), key=lambda i: abs((ts_b[i] - ta).total_seconds()))
        aligned_a.append(pa)
        aligned_b.append(prices_b[closest_idx])

    return aligned_a, aligned_b


async def build_correlation_graph() -> dict[str, list[dict]]:
    """
    Lee todos los poly_markets activos, los agrupa por tema.
    Calcula correlacion de precio entre pares del mismo grupo
    usando poly_price_history (ultimas 72h, minimo 5 snapshots por mercado).

    Returns: {group_name: [market_docs con correlation_data anadido]}

    Para cada par de mercados en el mismo grupo:
    - Correlacion de Pearson entre series de price_yes
    - Si correlacion > 0.7: mercados estan correlacionados
    - Guardar en col("market_correlations") doc por par:
      {market_a, market_b, group, correlation, computed_at}

    Usa alineacion temporal simple: emparejar por timestamp mas cercano.
    Maximo 10 pares por grupo para no agotar quota.
    """
    result: dict[str, list[dict]] = {}
    try:
        cutoff_72h = datetime.now(timezone.utc) - timedelta(hours=72)

        # Leer todos los mercados activos
        market_docs = list(col("poly_markets").stream())
        markets = [d.to_dict() for d in market_docs]

        # Agrupar por tema
        groups: dict[str, list[dict]] = {}
        for market in markets:
            question = market.get("question", market.get("market_question", ""))
            group = assign_topic_group(question)
            if group:
                groups.setdefault(group, []).append(market)

        for group_name, group_markets in groups.items():
            result[group_name] = group_markets
            if len(group_markets) < 2:
                continue

            # Obtener historiales de precio para cada mercado del grupo
            histories: dict[str, list[dict]] = {}
            for market in group_markets:
                mid = market.get("market_id", "")
                if not mid:
                    continue
                try:
                    docs = (
                        col("poly_price_history")
                        .where(filter=FieldFilter("market_id", "==", mid))
                        .where(filter=FieldFilter("timestamp", ">=", cutoff_72h))
                        .order_by("timestamp")
                        .stream()
                    )
                    snaps = [d.to_dict() for d in docs]
                    if len(snaps) >= 5:
                        histories[mid] = snaps
                except Exception:
                    logger.debug("build_correlation_graph: error leyendo history de %s", mid)

            # Calcular correlacion por pares (max 10 pares por grupo)
            market_ids = list(histories.keys())
            pairs_computed = 0
            for i in range(len(market_ids)):
                for j in range(i + 1, len(market_ids)):
                    if pairs_computed >= 10:
                        break
                    mid_a = market_ids[i]
                    mid_b = market_ids[j]
                    xs, ys = _align_series(histories[mid_a], histories[mid_b])
                    if len(xs) < 5:
                        continue
                    corr = _pearson(xs, ys)
                    pairs_computed += 1

                    # Guardar correlacion en Firestore
                    try:
                        pair_key = f"{min(mid_a, mid_b)}_{max(mid_a, mid_b)}"
                        col("market_correlations").document(pair_key).set({
                            "market_a": mid_a,
                            "market_b": mid_b,
                            "group": group_name,
                            "correlation": round(corr, 4),
                            "computed_at": datetime.now(timezone.utc),
                        })
                        logger.debug(
                            "correlation_graph: %s <-> %s corr=%.3f grupo=%s",
                            mid_a, mid_b, corr, group_name,
                        )
                    except Exception:
                        logger.debug(
                            "build_correlation_graph: error guardando par %s_%s", mid_a, mid_b
                        )
                if pairs_computed >= 10:
                    break

    except Exception:
        logger.error("build_correlation_graph: error no controlado", exc_info=True)

    return result


async def propagate_signal(
    market_id: str, price_change_pct: float, current_price: float
) -> list[dict]:
    """
    Si mercado X sube >5%: buscar mercados correlacionados en market_correlations.
    Para mercados con correlacion > 0.7:
    - Calcular precio esperado del mercado correlacionado
    - Si precio actual del correlacionado diverge >8% del esperado: generar senal secundaria

    Returns lista de senales secundarias:
    [{
      primary_market_id, secondary_market_id, secondary_question,
      correlation, divergence_pct, suggested_direction: "BUY_YES"|"BUY_NO",
      suggested_edge: float,  # divergencia como proxy del edge
      type: "CORRELATION_SIGNAL"
    }]

    Proteger con try/except — devolver [] si falla.
    """
    signals: list[dict] = []
    try:
        if abs(price_change_pct) <= 5.0:
            return signals

        # Buscar todos los pares donde este mercado aparece
        pairs_as_a = list(
            col("market_correlations").where(filter=FieldFilter("market_a", "==", market_id)).stream()
        )
        pairs_as_b = list(
            col("market_correlations").where(filter=FieldFilter("market_b", "==", market_id)).stream()
        )
        all_pairs = [d.to_dict() for d in pairs_as_a + pairs_as_b]

        for pair in all_pairs:
            corr = float(pair.get("correlation", 0))
            if corr <= 0.7:
                continue

            # Identificar el mercado secundario
            if pair.get("market_a") == market_id:
                secondary_id = pair.get("market_b", "")
            else:
                secondary_id = pair.get("market_a", "")

            if not secondary_id:
                continue

            # Obtener precio actual del mercado secundario
            try:
                sec_doc = col("poly_markets").document(secondary_id).get()
                if not sec_doc.exists:
                    continue
                sec_data = sec_doc.to_dict()
                sec_price = float(sec_data.get("price_yes", 0.5))
                sec_question = sec_data.get("question", sec_data.get("market_question", ""))

                # Precio esperado: si mercado primario subio X%, el secundario deberia subir X%*corr
                expected_change = (price_change_pct / 100.0) * corr
                # Precio esperado del secundario basado en la correlacion
                # Aproximacion simple: si el primario esta en current_price y cambio price_change_pct
                # el secundario deberia reflejar una variacion proporcional
                original_price = current_price / (1 + price_change_pct / 100.0)
                expected_sec_price = sec_price * (1 + expected_change)

                divergence = (expected_sec_price - sec_price) / max(sec_price, 0.01)
                divergence_pct = abs(divergence) * 100.0

                if divergence_pct <= 8.0:
                    continue

                # Determinar direccion sugerida
                if divergence > 0:
                    suggested_direction = "BUY_YES"
                else:
                    suggested_direction = "BUY_NO"

                signals.append({
                    "primary_market_id": market_id,
                    "secondary_market_id": secondary_id,
                    "secondary_question": sec_question,
                    "correlation": round(corr, 4),
                    "divergence_pct": round(divergence_pct, 2),
                    "suggested_direction": suggested_direction,
                    "suggested_edge": round(divergence_pct / 100.0, 4),
                    "type": "CORRELATION_SIGNAL",
                })
                logger.info(
                    "propagate_signal: senal secundaria %s → %s dir=%s div=%.1f%%",
                    market_id, secondary_id, suggested_direction, divergence_pct,
                )
            except Exception:
                logger.debug("propagate_signal: error procesando par secundario %s", secondary_id)

    except Exception:
        logger.error("propagate_signal(%s): error — devolviendo []", market_id, exc_info=True)
        return []

    return signals


async def save_market_group_labels() -> None:
    """
    Lee todos los poly_predictions, asigna topic_group via assign_topic_group,
    actualiza el campo group en Firestore poly_predictions si no existe.
    Ejecutar una vez al deploy.
    """
    try:
        docs = list(col("poly_predictions").stream())
        updated = 0
        for doc in docs:
            data = doc.to_dict()
            if data.get("group"):
                continue  # ya tiene grupo
            question = data.get("question", data.get("market_question", ""))
            group = assign_topic_group(question)
            if group:
                try:
                    col("poly_predictions").document(doc.id).update({"group": group})
                    updated += 1
                except Exception:
                    logger.debug("save_market_group_labels: error actualizando %s", doc.id)
        logger.info("save_market_group_labels: %d mercados etiquetados con grupo", updated)
    except Exception:
        logger.error("save_market_group_labels: error no controlado", exc_info=True)
