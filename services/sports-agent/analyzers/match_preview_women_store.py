"""
services/sports-agent/analyzers/match_preview_women_store.py

Fútbol femenino (mp-w1) — guardar el backtest. Mismo patrón que match_preview_store (mp-v1):
dry_run=True por defecto; con dry_run=False escribe UN documento:

  match_preview_metrics/mp-w1_backtest
      métricas por mercado y liga (Brier del modelo frente a la media de la liga, IC95 por
      partido, calibración), y los parámetros fijados con 2023/24 + 2024/25: líneas y
      dispersión. Las fichas femeninas (paso 4) leerán de aquí sus líneas y su dispersión, así
      que quedan congeladas hasta que se recalcule a mano.

Datos de Highlightly (hl_team_stats, grupo women): SOLO estadísticas, sin cuotas. El resumen que
devuelve (y que sale en el log público del workflow) lleva solo agregados por liga y mercado:
nunca equipos ni partidos.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from analyzers import match_preview as mp
from analyzers import match_preview_women as mw
from analyzers.match_preview_store import METRICS, _get_doc, _set_docs

logger = logging.getLogger(__name__)


def backtest_doc_id() -> str:
    return f"{mw.MODEL_VERSION}_backtest"


# ── (De)serializar parámetros: Firestore no admite tuplas como claves ──────────

def lines_to_doc(lines: dict) -> dict:
    return {s: {"team": list(t), "total": list(tt)} for s, (t, tt) in lines.items()}


def lines_from_doc(d: dict) -> dict:
    return {s: (tuple(v.get("team") or ()), tuple(v.get("total") or ())) for s, v in (d or {}).items()}


def disp_to_doc(disp: dict) -> dict:
    return {f"{s}_{scope}": r for (s, scope), r in disp.items()}


def disp_from_doc(d: dict) -> dict:
    out = {}
    for k, r in (d or {}).items():
        s, _, scope = k.rpartition("_")
        if s in mp.STATS and scope in ("team", "total") and r:
            out[(s, scope)] = float(r)
    return out


def params_from_backtest(doc: Optional[dict]) -> Optional[tuple[dict, dict]]:
    """(líneas, dispersión) guardadas en el backtest, o None si no hay backtest guardado."""
    if not doc or not doc.get("lines"):
        return None
    return lines_from_doc(doc["lines"]), disp_from_doc(doc.get("dispersion"))


def read_women_docs() -> list[dict]:
    from shared.firestore_client import iter_all
    return [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "women"))]


# ── Backtest ───────────────────────────────────────────────────────────────────

def _compute(matches: list[dict]) -> dict:
    hist = mw.history(matches)
    disp, disp_detail = mw.fit_dispersion(hist)
    lines = mw.fit_lines(matches)
    rows, metrics = mw.backtest(hist, lines, disp)
    return {"disp": disp, "disp_detail": disp_detail, "lines": lines, "rows": rows, "metrics": metrics,
            "coverage": mw.coverage(matches), "line_rates": mw.line_rates(matches, lines)}


async def run_women_backtest(dry_run: bool = True, now: Optional[datetime] = None,
                             docs: Optional[list[dict]] = None) -> dict:
    """docs: para tests (None = leer hl_team_stats del grupo women)."""
    now = now or datetime.now(timezone.utc)
    summary: dict = {"ok": True, "dry_run": dry_run, "doc_id": backtest_doc_id(),
                     "fit_seasons": list(mw.FIT_SEASONS), "backtest_seasons": list(mw.BACKTEST_SEASONS),
                     "errors": []}
    if docs is None:
        docs = await asyncio.to_thread(read_women_docs)
    matches = mw.matches_from_hl(docs)
    r = await asyncio.to_thread(_compute, matches)
    rows, metrics = r["rows"], r["metrics"]
    if not rows:
        summary.update(ok=False, errors=["backtest sin filas: no se guarda nada"])
        return summary
    cells = [v for v in metrics.values() if v["div"] != "ALL"]
    summary.update(
        n_docs=len(docs), n_matches_finished=len(matches), n_rows=len(rows),
        n_matches=len({x[7] for x in rows}),
        edge_cells=sum(1 for v in cells if v["label"] == mp.LABEL_EDGE), n_cells=len(cells),
        dispersion=disp_to_doc(r["disp"]), lines=lines_to_doc(r["lines"]),
        by_market={k: {f: v[f] for f in ("n_matches", "delta", "ci_lo", "ci_hi", "bss_vs_league", "label")}
                   for k, v in metrics.items() if k.endswith("|ALL")})
    doc = {
        "model_version": mw.MODEL_VERSION, "kind": "backtest", "group": "women",
        "fit_seasons": list(mw.FIT_SEASONS), "seasons": list(mw.BACKTEST_SEASONS),
        "divs": {d: mw.DIV_NAMES[d] for d in mw.WOMEN_DIVS.values()},
        "params": {"k": mp.SHRINK_K, "min_cur": mp.MIN_CUR, "weights": [mp.W_CUR, mp.W_PREV_SAME],
                   "rule": "IC95 por partido < 0 en la liga y en el conjunto (informativa: las fichas "
                           "femeninas salen siempre como solo referencia hasta medirlas en directo)"},
        "lines": lines_to_doc(r["lines"]), "dispersion": disp_to_doc(r["disp"]),
        "dispersion_detail": r["disp_detail"],
        "line_rates_fit": {s: {sc: {str(k): v for k, v in d.items()} for sc, d in x.items()}
                           for s, x in r["line_rates"].items()},
        "coverage": {f"{d}|{s}": c for (d, s), c in r["coverage"].items()},
        "n_rows": len(rows), "metrics": metrics, "computed_at": now.isoformat(),
    }
    if not dry_run:
        try:
            _set_docs(METRICS, {backtest_doc_id(): doc})
        except Exception:
            logger.error("women_backtest: error escribiendo", exc_info=True)
            summary.update(ok=False, errors=["escritura fallida (ver logs del servicio)"])
    summary["written"] = 0 if dry_run or not summary["ok"] else 1
    logger.info("women_backtest: filas %d, partidos %d, celdas ✔ %d/%d, escrito %d", len(rows),
                summary["n_matches"], summary["edge_cells"], summary["n_cells"], summary["written"])
    return summary


def read_backtest_doc() -> Optional[dict]:
    return _get_doc(METRICS, backtest_doc_id())
