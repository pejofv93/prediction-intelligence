"""
Motor de aprendizaje Polymarket.
Lee outcomes resueltos en prodshadow_trades (source=polymarket),
calcula accuracy por dirección (BUY_YES / BUY_NO) y ajusta
POLY_MIN_EDGE / POLY_MIN_CONFIDENCE en prodpoly_model_weights.

Colecciones usadas:
  {PREFIX}shadow_trades        — trades shadow con result=win/loss
  {PREFIX}poly_model_weights   — pesos/umbrales ajustados (doc "current")
"""
import logging
from datetime import datetime, timezone

from shared.config import POLY_MIN_EDGE, POLY_MIN_CONFIDENCE
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Límites de ajuste
_EDGE_MIN = 0.07
_EDGE_MAX = 0.40
_CONF_MIN = 0.60
_CONF_MAX = 0.90
# Mínimo de trades resueltos por dirección para ajustar su umbral
_MIN_SAMPLE = 5


# ---------------------------------------------------------------------------
# Lectura de datos
# ---------------------------------------------------------------------------

def _load_resolved_trades() -> list[dict]:
    """Lee todos los shadow_trades de polymarket con result win/loss."""
    try:
        docs = (
            col("shadow_trades")
            .where("source", "==", "polymarket")
            .stream()
        )
        trades = []
        for doc in docs:
            d = doc.to_dict()
            if d.get("result") in ("win", "loss"):
                trades.append(d)
        logger.info("_load_resolved_trades: %d trades resueltos", len(trades))
        return trades
    except Exception:
        logger.error("_load_resolved_trades: error leyendo Firestore", exc_info=True)
        return []


def _load_current_weights() -> dict:
    """Carga el documento 'current' de poly_model_weights o devuelve defaults."""
    defaults = {
        "version": 0,
        "min_edge": POLY_MIN_EDGE,
        "min_confidence": POLY_MIN_CONFIDENCE,
        "buy_yes_min_edge": POLY_MIN_EDGE,
        "buy_yes_min_confidence": POLY_MIN_CONFIDENCE,
        "buy_no_min_edge": POLY_MIN_EDGE,
        "buy_no_min_confidence": POLY_MIN_CONFIDENCE,
    }
    try:
        doc = col("poly_model_weights").document("current").get()
        if doc.exists:
            saved = doc.to_dict()
            for k, v in defaults.items():
                saved.setdefault(k, v)
            return saved
    except Exception:
        logger.error("_load_current_weights: error leyendo Firestore", exc_info=True)
    return defaults


# ---------------------------------------------------------------------------
# Análisis estadístico
# ---------------------------------------------------------------------------

def _analyze(trades: list[dict]) -> dict:
    """
    Devuelve dict con:
      total, wins, losses, accuracy,
      buy_yes_{total,wins,accuracy,avg_edge},
      buy_no_{total,wins,accuracy,avg_edge},
      vol_spike_{total,wins,accuracy},
      by_category: {cat: {total, wins, accuracy}}
    """
    wins = [t for t in trades if t.get("result") == "win"]
    losses = [t for t in trades if t.get("result") == "loss"]
    total = len(trades)
    accuracy = round(len(wins) / total, 4) if total else 0.0

    def _dir_stats(direction: str) -> dict:
        subset = [t for t in trades if _get_selection(t) == direction]
        w = [t for t in subset if t.get("result") == "win"]
        edges = [abs(float(t.get("edge", 0))) for t in subset]
        confs = [float(t.get("confidence", 0)) for t in subset]
        acc = round(len(w) / len(subset), 4) if subset else 0.0
        return {
            "total": len(subset),
            "wins": len(w),
            "accuracy": acc,
            "avg_abs_edge": round(sum(edges) / len(edges), 4) if edges else 0.0,
            "avg_confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
            "win_edges": sorted([abs(float(t.get("edge", 0))) for t in w]),
            "loss_edges": sorted([abs(float(t.get("edge", 0))) for t in
                                   [t for t in subset if t.get("result") == "loss"]]),
        }

    def _vol_stats() -> dict:
        spike = [t for t in trades if t.get("signal_data", {}).get("volume_spike") is True
                 or t.get("volume_spike") is True]
        w = [t for t in spike if t.get("result") == "win"]
        acc = round(len(w) / len(spike), 4) if spike else 0.0
        return {"total": len(spike), "wins": len(w), "accuracy": acc}

    def _cat_stats() -> dict:
        cats: dict[str, list] = {}
        for t in trades:
            cat = t.get("category") or "unknown"
            cats.setdefault(cat, []).append(t)
        result = {}
        for cat, ts in cats.items():
            w = [t for t in ts if t.get("result") == "win"]
            result[cat] = {
                "total": len(ts),
                "wins": len(w),
                "accuracy": round(len(w) / len(ts), 4) if ts else 0.0,
            }
        return result

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "accuracy": accuracy,
        "buy_yes": _dir_stats("BUY_YES"),
        "buy_no": _dir_stats("BUY_NO"),
        "vol_spike": _vol_stats(),
        "by_category": _cat_stats(),
    }


def _get_selection(trade: dict) -> str:
    """Extrae la dirección BUY_YES / BUY_NO del trade."""
    sel = trade.get("selection") or ""
    if sel:
        return sel
    # Fallback: derivar del signo del edge
    edge = float(trade.get("edge", 0))
    return "BUY_YES" if edge >= 0 else "BUY_NO"


# ---------------------------------------------------------------------------
# Ajuste de umbrales
# ---------------------------------------------------------------------------

def _new_threshold(
    stats: dict,
    current_edge: float,
    current_conf: float,
    direction: str,
) -> tuple[float, float]:
    """
    Calcula nuevos (min_edge, min_confidence) para una dirección.

    Reglas:
    - Con >= MIN_SAMPLE trades resueltos:
        new_edge = min(win_edges) si accuracy >= 0.70
        new_edge = min(win_edges) + 0.02 si accuracy < 0.70  (más estricto)
    - Con < MIN_SAMPLE: ajuste pequeño basado en accuracy actual
        accuracy < 0.55 → +0.01 edge
        accuracy > 0.80 → -0.01 edge (más permisivo)
    - Confidence: no se ajusta si la diferencia win/loss es < 0.05
    """
    n = stats["total"]
    acc = stats["accuracy"]
    win_edges = stats["win_edges"]
    loss_edges = stats["loss_edges"]

    if n >= _MIN_SAMPLE and win_edges:
        min_win_edge = min(win_edges)
        new_edge = min_win_edge if acc >= 0.70 else min_win_edge + 0.02
        # Si la accuracy es muy buena (>= 0.85), permíte bajar un poco más
        if acc >= 0.85 and win_edges:
            new_edge = max(_EDGE_MIN, min_win_edge - 0.01)
    else:
        # Ajuste incremental con poca muestra
        if acc < 0.55:
            new_edge = current_edge + 0.01
        elif acc > 0.80:
            new_edge = max(_EDGE_MIN, current_edge - 0.01)
        else:
            new_edge = current_edge

    # Confidence: subir si accuracy es baja, bajar si es muy alta
    new_conf = current_conf
    if acc < 0.60 and n >= _MIN_SAMPLE:
        new_conf = min(_CONF_MAX, current_conf + 0.05)
    elif acc > 0.85 and n >= _MIN_SAMPLE:
        new_conf = max(_CONF_MIN, current_conf - 0.02)

    # Clamp
    new_edge = round(max(_EDGE_MIN, min(_EDGE_MAX, new_edge)), 4)
    new_conf = round(max(_CONF_MIN, min(_CONF_MAX, new_conf)), 4)

    logger.info(
        "_new_threshold(%s): n=%d acc=%.0f%% edge: %.3f→%.3f conf: %.2f→%.2f",
        direction, n, acc * 100, current_edge, new_edge, current_conf, new_conf,
    )
    return new_edge, new_conf


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def run_poly_learning() -> dict:
    """
    Pipeline completo:
    1. Lee trades resueltos
    2. Calcula estadísticas
    3. Ajusta umbrales por dirección
    4. Guarda en poly_model_weights/current
    Devuelve el nuevo doc guardado.
    """
    now = datetime.now(timezone.utc)

    # 1. Datos
    trades = _load_resolved_trades()
    if not trades:
        logger.warning("run_poly_learning: sin trades resueltos — abortando")
        return {}

    # 2. Análisis
    stats = _analyze(trades)
    logger.info(
        "run_poly_learning: total=%d acc=%.0f%% | BUY_YES %d/%.0f%% | BUY_NO %d/%.0f%%",
        stats["total"], stats["accuracy"] * 100,
        stats["buy_yes"]["total"], stats["buy_yes"]["accuracy"] * 100,
        stats["buy_no"]["total"], stats["buy_no"]["accuracy"] * 100,
    )

    # 3. Cargar pesos actuales
    current = _load_current_weights()

    # 4. Ajustar por dirección
    new_yes_edge, new_yes_conf = _new_threshold(
        stats["buy_yes"],
        current["buy_yes_min_edge"],
        current["buy_yes_min_confidence"],
        "BUY_YES",
    )
    new_no_edge, new_no_conf = _new_threshold(
        stats["buy_no"],
        current["buy_no_min_edge"],
        current["buy_no_min_confidence"],
        "BUY_NO",
    )

    # Umbral global = el más restrictivo de los dos (conservador)
    new_global_edge = round(max(new_yes_edge, new_no_edge), 4)
    new_global_conf = round(max(new_yes_conf, new_no_conf), 4)

    # 5. Construir documento
    new_version = int(current.get("version", 0)) + 1
    doc = {
        "version": new_version,
        "updated_at": now,
        # Umbrales globales (usados por alert_engine si no hay dirección específica)
        "min_edge": new_global_edge,
        "min_confidence": new_global_conf,
        # Umbrales por dirección
        "buy_yes_min_edge": new_yes_edge,
        "buy_yes_min_confidence": new_yes_conf,
        "buy_no_min_edge": new_no_edge,
        "buy_no_min_confidence": new_no_conf,
        # Snapshot de accuracy
        "accuracy_overall": stats["accuracy"],
        "accuracy_buy_yes": stats["buy_yes"]["accuracy"],
        "accuracy_buy_no": stats["buy_no"]["accuracy"],
        "accuracy_vol_spike": stats["vol_spike"]["accuracy"],
        "sample_size": stats["total"],
        "sample_buy_yes": stats["buy_yes"]["total"],
        "sample_buy_no": stats["buy_no"]["total"],
        "by_category": stats["by_category"],
        # Herencia de versión anterior para trazabilidad
        "prev_min_edge": current.get("min_edge", POLY_MIN_EDGE),
        "prev_min_confidence": current.get("min_confidence", POLY_MIN_CONFIDENCE),
    }

    # 6. Guardar
    try:
        col("poly_model_weights").document("current").set(doc)
        logger.info(
            "run_poly_learning: poly_model_weights guardado v%d "
            "global(edge=%.3f conf=%.2f) BUY_YES(%.3f/%.2f) BUY_NO(%.3f/%.2f)",
            new_version,
            new_global_edge, new_global_conf,
            new_yes_edge, new_yes_conf,
            new_no_edge, new_no_conf,
        )
    except Exception:
        logger.error("run_poly_learning: error guardando poly_model_weights", exc_info=True)

    return doc


# ---------------------------------------------------------------------------
# CLI de diagnóstico
# ---------------------------------------------------------------------------

def print_report(doc: dict, stats: dict) -> None:
    """Imprime resumen legible por consola (rich si disponible)."""
    try:
        from rich.console import Console
        from rich.table import Table

        c = Console()
        c.print("\n[bold #F7931A]POLY LEARNING ENGINE — Reporte[/]")
        c.print(f"Trades resueltos: [bold]{stats['total']}[/] "
                f"| Accuracy: [bold]{stats['accuracy']*100:.1f}%[/]")

        t = Table(show_header=True, header_style="bold")
        t.add_column("Dirección")
        t.add_column("Trades", justify="right")
        t.add_column("Wins", justify="right")
        t.add_column("Accuracy", justify="right")
        t.add_column("Avg |Edge|", justify="right")
        t.add_column("New min_edge", justify="right")
        t.add_column("New min_conf", justify="right")

        for direction, key_e, key_c in [
            ("BUY_YES", "buy_yes_min_edge", "buy_yes_min_confidence"),
            ("BUY_NO", "buy_no_min_edge", "buy_no_min_confidence"),
        ]:
            s = stats[direction.lower().replace("_", "_")]
            color = "green" if s["accuracy"] >= 0.70 else "red"
            t.add_row(
                direction,
                str(s["total"]),
                str(s["wins"]),
                f"[{color}]{s['accuracy']*100:.0f}%[/]",
                f"{s['avg_abs_edge']:.3f}",
                f"{doc.get(key_e, '?'):.3f}",
                f"{doc.get(key_c, '?'):.2f}",
            )

        c.print(t)

        if stats["by_category"]:
            c.print("\n[bold]Accuracy por categoría:[/]")
            for cat, cs in stats["by_category"].items():
                c.print(f"  {cat}: {cs['wins']}/{cs['total']} = {cs['accuracy']*100:.0f}%")

        c.print(f"\n[bold]Umbrales globales nuevos:[/] edge={doc.get('min_edge')} "
                f"conf={doc.get('min_confidence')}")
        c.print(f"[dim]Versión modelo: {doc.get('version')} — {doc.get('updated_at')}[/]\n")

    except ImportError:
        print(f"POLY LEARNING: {stats['total']} trades | acc={stats['accuracy']*100:.1f}% "
              f"| BUY_YES={stats['buy_yes']['accuracy']*100:.0f}% "
              f"| BUY_NO={stats['buy_no']['accuracy']*100:.0f}%")
        print(f"New thresholds: global(edge={doc.get('min_edge')} conf={doc.get('min_confidence')}) "
              f"BUY_YES(edge={doc.get('buy_yes_min_edge')} conf={doc.get('buy_yes_min_confidence')}) "
              f"BUY_NO(edge={doc.get('buy_no_min_edge')} conf={doc.get('buy_no_min_confidence')})")


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        print("ERROR: GOOGLE_CLOUD_PROJECT no definido")
        sys.exit(1)

    trades = _load_resolved_trades()
    if not trades:
        print("Sin trades resueltos.")
        sys.exit(0)

    stats = _analyze(trades)
    doc = run_poly_learning()
    if doc:
        print_report(doc, stats)
