"""Backtest de Europa (Champions, Europa League, Conference League) con mp-e1:
analyzers/match_preview_uefa.py.

SOLO LECTURA por defecto (el workflow uefa-backtest no ofrece otra cosa). Con CONFIRMAR=ESCRIBIR (solo
a mano, más adelante y con OK) guardaría UN documento: match_preview_metrics/mp-e1_backtest (métricas
agregadas por mercado y etiquetas), nada más.

Lee hl_team_stats (grupo uefa) y hl_matches (partidos de liga de Highlightly, para emparejar ids con
nombres de football-data) de Firestore, y baja los CSV públicos de football-data.co.uk de las 8 ligas,
las segundas y B1/T1/G1/SC0 desde 2019/20. No pide nada a Highlightly.
Log: solo agregados (recuentos, métricas por mercado y competición, factores por liga): nada de
equipos ni de partidos.
"""
import asyncio
import math
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, ROOT)

from analyzers import match_preview as mp  # noqa: E402
from analyzers import match_preview_cups as mc  # noqa: E402
from analyzers import match_preview_uefa as mu  # noqa: E402

DOC_ID = "mp-e1_backtest"
FIRST_SEASON_END = int(os.environ.get("FIRST_SEASON_END") or 2020)   # 2019/20: la anterior a 2020/21


async def _history(last_end: int) -> tuple[list[dict], list[str]]:
    import httpx
    from collectors import fdco_team_matches as fd
    rows, failed = [], []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for end in range(FIRST_SEASON_END, last_end + 1):
            season = fd.season_code(end)
            for div in mu.ALL_DIVS:
                raw = await fd.fetch_csv_rows(client, div, season)
                if raw is None:
                    failed.append(f"{div}_{season}")
                    continue
                rows.extend(fd._to_team_rows(raw, div, season))
                await asyncio.sleep(0.3)
    return mp.matches_from_team_rows(rows), failed


def _fmt(x) -> str:
    return "—" if x is None else f"{x:+.4f}"


def print_metrics(name: str, rows: list[tuple], by_comp: bool = False, n_boot: int = mu.N_BOOT) -> dict:
    """Imprime las métricas por mercado (todas las competiciones y, si by_comp, por competición).
    Solo agregados. Devuelve las métricas."""
    if not rows:
        print(f"\n[{name}] sin filas")
        return {}
    met = mu.market_metrics(rows, n_boot=n_boot)
    print(f"\n[{name}]  mercado: partidos · Brier ref · Brier modelo · diferencia [IC95 bootstrap] · etiqueta")
    for v in sorted((v for v in met.values() if v["div"] == "ALL"), key=lambda v: v["market"]):
        print(f"  {v['market']:14} {v['n_matches']:5d} · {v['brier_ref']:.4f} · {v['brier_model']:.4f} · "
              f"{v['delta']:+.4f} [{_fmt(v['ci_lo'])}, {_fmt(v['ci_hi'])}] · {v['ci_label']}")
    if by_comp:
        print(f"  [{name}] por competición: comp · mercado · partidos · diferencia [IC95] · etiqueta del IC")
        for v in sorted((v for v in met.values() if v["div"] != "ALL"), key=lambda v: (v["div"], v["market"])):
            print(f"    {v['div']:3} {v['market']:14} {v['n_matches']:4d} · {v['delta']:+.4f} "
                  f"[{_fmt(v['ci_lo'])}, {_fmt(v['ci_hi'])}] · {v['ci_label']}")
    return met


def report(docs: list[dict], league: list[dict], matches: list[dict], n_boot: int = mu.N_BOOT) -> dict:
    """Backtest completo e impresión de agregados. Devuelve {"labels", "metrics", "counts", "comp_labels"}."""
    from collectors.highlightly_players import team_similarity
    hist = mp.History(matches, prev_divs=mu.ALL_DIVS)
    csv_by_div_day: dict = {}
    for m in matches:
        csv_by_div_day.setdefault((m["div"], m["date"]), []).append(m)
    ids = mc.hl_id_names(league, csv_by_div_day, team_similarity)
    tdiv = mu.team_divs_all(hist)
    print(f"Ids de Highlightly emparejados por votos: {len(ids)} · equipos-temporada con liga en CSV: {len(tdiv)}")
    counts = Counter()
    ms = mu.uefa_matches(docs, mu.make_resolver(ids, tdiv, team_similarity), counts)
    print("Partidos por competición y temporada:",
          dict(sorted(Counter(f"{m['comp']}_{m['season']}" for m in ms).items())))
    engine = mu.Engine(hist, tdiv)
    rows = mu.backtest_rows(engine, ms, counts)
    print("Recuentos:", dict(sorted(counts.items())))
    sides = Counter()
    for m in ms:
        for t in (m["home"], m["away"]):
            sides[(tdiv.get((t, m["season"])) or tdiv.get((t, mp.prev_season(m["season"]))) or mu.OTHER)
                  if t else mu.OTHER] += 1
    print("Lados europeos por liga (CSV) o sin liga:", dict(sides.most_common()))
    print("Amarillas ausentes (lados de partido):", mc.yellow_diag(docs))
    out: dict = {"counts": dict(counts)}
    for variant in mu.VARIANTS:
        for cov in (*mu.COVERAGES, "todos"):
            sub = (rows[f"{variant}|ambos"] + rows[f"{variant}|un_lado"]) if cov == "todos" else rows[f"{variant}|{cov}"]
            met = print_metrics(f"{variant} · {cov}", sub, by_comp=(variant == "e1_vs_comp" and cov == "ambos"),
                                n_boot=n_boot)
            if variant == "e1_vs_comp" and cov == "ambos":
                out["metrics"] = met
    engine.refit()
    print("\nFactor de liga al final (exp β, ataque / defensa) — solo ligas, sin equipos:")
    for s in mp.STATS:
        b = engine.beta.get(s) or {}
        lgs = sorted({lg for _, lg in b})
        print(f"  {s:8} " + " · ".join(f"{lg} {math.exp(b[('att', lg)]):.2f}/{math.exp(b[('def', lg)]):.2f}"
                                       for lg in lgs))
    out["labels"] = mu.labels_from_backtest(out.get("metrics") or {})
    out["comp_labels"] = mu.comp_labels(out.get("metrics") or {})
    print("\nEtiquetas (mp-e1 frente a la competición, los dos equipos con datos, todas las competiciones):",
          out["labels"])
    print("Etiquetas por competición (≥ 300 partidos):", out["comp_labels"] or "ninguna competición llega")
    return out


def main() -> int:
    from shared.firestore_client import iter_all
    docs = [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "uefa"))]
    league = [d.to_dict() or {} for d in iter_all("hl_matches")]
    print(f"Partidos europeos leídos: {len(docs)} · partidos de liga de Highlightly: {len(league)}")
    if not docs:
        print("Sin partidos del grupo uefa en hl_team_stats: falta la carga (job uefa). Nada que medir.")
        return 0
    last_end = max((int(mc.season_of(d["date"])[2:]) + 2000 for d in docs if d.get("date")), default=2027)
    matches, failed = asyncio.run(_history(last_end))
    print(f"Historial de liga (CSV): {len(matches)} partidos · CSV que faltan: {failed or 'ninguno'}")
    out = report(docs, league, matches)
    if os.environ.get("CONFIRMAR") == "ESCRIBIR":
        from datetime import datetime, timezone
        from shared.firestore_client import col
        col("match_preview_metrics").document(DOC_ID).set({
            "model_version": mu.MODEL_VERSION, "computed_at": datetime.now(timezone.utc).isoformat(),
            "reference": "frecuencia de la competición", "coverage": "ambos equipos con datos de liga",
            "counts": out["counts"], "labels": out["labels"], "comp_labels": out["comp_labels"],
            "metrics": {k: v for k, v in (out.get("metrics") or {}).items() if v["div"] == "ALL"},
            "stars_after_live": mc.STARS_AFTER_LIVE})
        print(f"Escrito match_preview_metrics/{DOC_ID} (métricas agregadas y etiquetas).")
    else:
        print("Sin escribir (solo lectura).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
