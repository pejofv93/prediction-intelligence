"""Backtest walk-forward por día del modelo de jugadores pp-v1 (analyzers/player_preview.py) con el
box score de Highlightly ya guardado (hl_match_players, 8 ligas, 2023/24 a la actual). 0 peticiones.

Por defecto SOLO LECTURA. Con CONFIRMAR=ESCRIBIR guarda SOLO match_preview_metrics/pp-v1_backtest
(líneas fijas, métricas agregadas y etiquetas por mercado: de ahí saldrían las ⭐ de jugador).

Regla de ⭐ (la de Europa, decisión del usuario del 28-nov): IC95 por partido de (Brier modelo −
Brier referencia) entero bajo 0 frente a LAS DOS referencias (media del jugador y media de su
posición en la liga) en todo el periodo evaluado (2024/25 a la actual; 2023/24 de calentamiento) y,
además, en las dos últimas temporadas juntas (RECENT_SEASONS) diferencia media ≤ 0 frente a las dos.

Log: solo agregados (recuentos, Brier, IC95, n por mercado, liga y temporada): nada de jugadores,
equipos ni partidos (términos de Highlightly: sus datos no se exponen fuera de la web privada y
Telegram).
"""
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, ROOT)

from analyzers import player_preview as pp  # noqa: E402
from analyzers.basket_preview import labels_from_backtest  # noqa: E402
from analyzers.match_preview import LABEL_EDGE, market_metrics  # noqa: E402

METRIC_FIELDS = ("market", "n_matches", "n_lines", "brier_league", "brier_model", "delta", "ci_lo", "ci_hi",
                 "ci_label")


def load_docs() -> list[dict]:
    from shared.firestore_client import iter_all
    return [d.to_dict() or {} for d in iter_all("hl_match_players")]


def coverage(docs: list[dict]) -> list[str]:
    """Docs por temporada y versión de campos, y por equipo: remates totales frente a a puerta + fuera
    del propio campo de Highlightly (solo fields_version 2)."""
    by = Counter((str(d.get("season")), int(d.get("fields_version") or 1)) for d in docs)
    lines = ["Docs por (temporada, versión de campos): " + str(dict(sorted(by.items())))]
    eq = lo = hi = 0
    for d in docs:
        if int(d.get("fields_version") or 1) < 2:
            continue
        for t in d.get("teams") or []:
            tot = t.get("totals") or {}
            if "shots_off_target" not in tot:
                continue
            a, b = tot.get("shots_total", 0), tot.get("shots_on_target", 0) + tot.get("shots_off_target", 0)
            eq += a == b
            lo += a < b
            hi += a > b
    if eq + lo + hi:
        lines.append(f"Equipos con fields_version 2: remates totales = a puerta + fuera en {eq}, "
                     f"menos en {lo}, más en {hi} (fuera del modelo = totales − a puerta)")
    return lines


def _table(title: str, met: dict) -> list[str]:
    out = [title + "  mercado: partidos · líneas · Brier ref · Brier modelo · diferencia [IC95] · etiqueta"]
    for k, v in sorted(met.items()):
        if not k.endswith("|ALL"):
            continue
        ci = f"[{v['ci_lo']:+.4f}, {v['ci_hi']:+.4f}]" if v["ci_lo"] is not None else "[—]"
        out.append(f"  {v['market']}: {v['n_matches']} · {v['n_lines']} · {v['brier_league']:.4f} · "
                   f"{v['brier_model']:.4f} · {v['delta']:+.4f} {ci} · {v['ci_label']}")
    return out


def compute(games: list[dict]) -> dict:
    rows_p, rows_pos, counts = pp.backtest_rows(games)
    if not rows_p:
        raise ValueError("sin filas evaluables")
    met_p, met_pos = market_metrics(rows_p), market_metrics(rows_pos)
    rec_p = [r for r in rows_p if r[0] in pp.RECENT_SEASONS]
    rec_pos = [r for r in rows_pos if r[0] in pp.RECENT_SEASONS]
    met_rp = market_metrics(rec_p) if rec_p else {}
    met_rpos = market_metrics(rec_pos) if rec_pos else {}
    labels = labels_from_backtest(met_p, met_pos, recent=(met_rp, met_rpos))
    report = [f"Partidos: {len(games)} · por temporada {dict(sorted(Counter(g['season'] for g in games).items()))}",
              f"Recuentos de jugadores: {counts}",
              f"Evaluadas {list(pp.EVAL_SEASONS)} (calentamiento 2324) · filas {len(rows_p)}",
              f"Filas por liga: {dict(sorted(Counter(r[1] for r in rows_p).items()))}"]
    report += _table("[modelo frente a la media del jugador]", met_p)
    report += _table("[modelo frente a la media de su posición en la liga]", met_pos)
    report.append("Por temporada  mercado: partidos · frente al jugador: diferencia [IC95] · frente a la posición")
    for s in pp.EVAL_SEASONS:
        sp = [r for r in rows_p if r[0] == s]
        if not sp:
            continue
        a, b = market_metrics(sp), market_metrics([r for r in rows_pos if r[0] == s])
        report.append(f" {s}:")
        for mk in pp.MARKETS:
            x, y = a.get(f"{mk}|ALL"), b.get(f"{mk}|ALL")
            if x and y:
                report.append(f"  {mk}: {x['n_matches']} · {x['delta']:+.4f} [{x['ci_lo']:+.4f}, {x['ci_hi']:+.4f}] "
                              f"{x['ci_label']} · {y['delta']:+.4f} [{y['ci_lo']:+.4f}, {y['ci_hi']:+.4f}] {y['ci_label']}")
    report += _table(f"[temporadas recientes juntas {list(pp.RECENT_SEASONS)}, frente al jugador]", met_rp)
    report += _table(f"[temporadas recientes juntas {list(pp.RECENT_SEASONS)}, frente a la posición]", met_rpos)
    report.append("Por liga (todo el periodo)  mercado: diferencia frente al jugador / frente a la posición")
    for mk in pp.MARKETS:
        cells = [f"{v['div']} {v['delta']:+.4f}/{met_pos[k]['delta']:+.4f}"
                 for k, v in sorted(met_p.items()) if v["market"] == mk and v["div"] != "ALL" and k in met_pos]
        report.append(f"  {mk}: " + " · ".join(cells))
    report.append("Etiquetas (IC95 entero bajo 0 frente a las dos referencias en todo el periodo; en las "
                  f"recientes juntas {list(pp.RECENT_SEASONS)} diferencia media ≤ 0 frente a las dos): {labels}")
    return {"labels": labels, "met_player": met_p, "met_pos": met_pos, "recent": (met_rp, met_rpos),
            "counts": counts, "report": report, "n_rows": len(rows_p)}


def _slim(met: dict) -> dict:
    return {v["market"]: {f: v[f] for f in METRIC_FIELDS} for k, v in met.items() if k.endswith("|ALL")}


def backtest_doc(out: dict, now_iso: str) -> dict:
    """match_preview_metrics/pp-v1_backtest: solo agregados (nada de jugadores ni equipos)."""
    return {"model_version": pp.MODEL_VERSION, "kind": "backtest", "group": "players",
            "lines": {mk: list(lines) for mk, (_, lines) in pp.MARKETS.items()},
            "labels": out["labels"], "eval_seasons": list(pp.EVAL_SEASONS),
            "recent_seasons": list(pp.RECENT_SEASONS),
            "metrics": {"vs_jugador": _slim(out["met_player"]), "vs_posicion": _slim(out["met_pos"])},
            "recent": {"vs_jugador": _slim(out["recent"][0]), "vs_posicion": _slim(out["recent"][1])},
            "rule": "IC95 bajo 0 frente a la media del jugador y a la de su posición en todo el periodo; "
                    "en recent_seasons juntas diferencia media ≤ 0 frente a las dos",
            "computed_at": now_iso}


def main() -> int:
    docs = load_docs()
    print(f"Docs leídos (hl_match_players): {len(docs)}")
    for line in coverage(docs):
        print(line)
    games = pp.games_from_docs(docs)
    try:
        out = compute(games)
    except ValueError as e:
        print(f"No se puede evaluar: {e}")
        return 1
    for line in out["report"]:
        print(line)
    print(f"\n⭐ ({LABEL_EDGE}): {sorted(mk for mk, lab in out['labels'].items() if lab == LABEL_EDGE)}")
    if os.environ.get("CONFIRMAR") == "ESCRIBIR":
        from datetime import datetime, timezone
        from shared.firestore_client import col
        col("match_preview_metrics").document(pp.METRICS_DOC).set(
            backtest_doc(out, datetime.now(timezone.utc).isoformat()))
        print(f"\nEscrito match_preview_metrics/{pp.METRICS_DOC}.")
    else:
        print("\nSin escribir nada (solo lectura).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
