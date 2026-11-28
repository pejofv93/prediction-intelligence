"""Backtest de Europa (Champions, Europa League, Conference League) con mp-e1:
analyzers/match_preview_uefa.py. Walk-forward por fecha; desglose por temporada evaluada y ⭐ con la
regla de la Euroliga (todo el periodo + temporadas limpias juntas) y, en 2025/26 + 2026/27 juntas, el
modelo no peor que la referencia (mu.star_labels).

SOLO LECTURA por defecto. Con CONFIRMAR=ESCRIBIR (workflow uefa-backtest, confirmar=ESCRIBIR, a mano)
guarda UN documento: match_preview_metrics/mp-e1_backtest (métricas agregadas por mercado y etiquetas
que leen las fichas de Europa), nada más.

Lee hl_team_stats (grupo uefa), hl_matches y hl_league_listing (partidos de liga de Highlightly, para
el mapa id de Highlightly → equipo del CSV) de Firestore, y baja los CSV públicos de football-data.co.uk
de las 8 ligas, las segundas y B1/T1/G1/SC0 desde 2019/20. No pide nada a Highlightly.
Emparejamiento SOLO por id (27-nov). Auditoría: compara con el emparejamiento anterior (id con el mapa
de hl_matches y, si no, parecido de nombre) y cuenta los equipos que iban por nombre, los que el mapa
nuevo lleva a otro club (falsos) y los que se quedan sin emparejar.
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
LEGACY_MIN_SIM = 0.80     # el respaldo por nombre de antes del 27-nov (solo para la auditoría)
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


def legacy_resolver(ids: dict, tdiv: dict, similarity, min_sim: float = LEGACY_MIN_SIM):
    """El emparejamiento de antes del 27-nov (solo para la auditoría): por id y, si no, por parecido
    único de nombre con los equipos con CSV de esa temporada o la anterior. Devuelve (nombre, vía)."""
    by_season: dict = {}
    for t, s in tdiv:
        by_season.setdefault(s, set()).add(t)
    cache: dict = {}

    def resolve(hl_id, hl_name: str, season: str):
        cands = by_season.get(season, set()) | by_season.get(mp.prev_season(season), set())
        n = ids.get(hl_id, ids.get(str(hl_id))) if hl_id is not None else None
        if n in cands:
            return n, "id"
        key = (hl_name, season)
        if key not in cache:
            best = None
            if hl_name:
                scored = sorted(((similarity(hl_name, c), c) for c in cands), reverse=True)
                if scored and scored[0][0] >= min_sim and not (len(scored) > 1 and scored[1][0] == scored[0][0]):
                    best = scored[0][1]
            cache[key] = best
        return (cache[key], "nombre") if cache[key] else (None, None)

    return resolve


def audit_matching(docs: list[dict], old_resolve, new_resolve, ids_new: dict) -> tuple[dict, list[dict]]:
    """Emparejamiento anterior frente al nuevo (solo por id), por equipo de Highlightly distinto (su
    temporada más reciente). Devuelve (recuentos, detalle por equipo dudoso). Categorías:
      falso_otro_club        antes por nombre y el id lleva a otro club (o el club elegido tiene su propio
                             id en el mapa, distinto): el caso del Inter de la Conference
      por_nombre_ahora_igual antes por nombre y el id confirma el mismo club
      por_nombre_sin_id      antes por nombre y ahora sin emparejar (muestra insuficiente)
      id_igual / nuevo_por_id / sin_emparejar
    El detalle (nombres) NO va al log: lo usa el fichero cifrado de la prueba de fichas."""
    owner: dict = {}
    for hid, name in ids_new.items():
        owner.setdefault(name, set()).add(str(hid))
    last: dict = {}
    for d in docs:
        date = (d.get("date") or "")[:10]
        if len(date) != 10 or d.get("comp") not in mu.COMPS:
            continue
        season = mc.season_of(date)
        for side in ("home", "away"):
            hid = d.get(f"{side}_team_id")
            if hid is None:
                continue
            if str(hid) not in last or season > last[str(hid)][2]:
                last[str(hid)] = (hid, d.get(f"{side}_team") or "", season)
    counts, detail = Counter(), []
    for key, (hid, hl_name, season) in last.items():
        old, via = old_resolve(hid, hl_name, season)
        new = new_resolve(hid, hl_name, season)
        if via == "nombre":
            other_owner = bool(owner.get(old, set()) - {key})
            if (new is not None and new != old) or (new is None and other_owner):
                cat = "falso_otro_club"
            elif new == old:
                cat = "por_nombre_ahora_igual"
            else:
                cat = "por_nombre_sin_id"
        elif via == "id":
            cat = "id_igual" if new == old else "id_cambia"
        else:
            cat = "nuevo_por_id" if new is not None else "sin_emparejar"
        counts[cat] += 1
        if via == "nombre" or cat == "id_cambia":
            detail.append({"hl": hl_name, "season": season, "antes": old, "ahora": new, "cat": cat})
    counts["nombres_repetidos_entre_ids"] = len(mu.shared_names(ids_new))
    return dict(sorted(counts.items())), sorted(detail, key=lambda r: (r["cat"], r["hl"]))


def report(docs: list[dict], league: list[dict], matches: list[dict], n_boot: int = mu.N_BOOT,
           league_old: list[dict] | None = None) -> dict:
    """Backtest completo e impresión de agregados. league = partidos de liga de Highlightly para el
    mapa de ids (hl_matches + hl_league_listing); league_old = solo hl_matches (el mapa de antes, para
    la auditoría; None = sin auditoría). Devuelve {"labels", "metrics", "counts", "comp_labels",
    "audit", "audit_detail", …}."""
    from collectors.highlightly_players import team_similarity
    hist = mp.History(matches, prev_divs=mu.ALL_DIVS)
    csv_by_div_day: dict = {}
    for m in matches:
        csv_by_div_day.setdefault((m["div"], m["date"]), []).append(m)
    ids = mc.hl_id_names(league, csv_by_div_day, team_similarity)
    tdiv = mu.team_divs_all(hist)
    print(f"Ids de Highlightly emparejados por votos: {len(ids)} · equipos-temporada con liga en CSV: {len(tdiv)}")
    resolve = mu.make_resolver(ids, tdiv)
    audit, audit_detail = {}, []
    if league_old is not None:
        ids_old = mc.hl_id_names(league_old, csv_by_div_day, team_similarity)
        audit, audit_detail = audit_matching(docs, legacy_resolver(ids_old, tdiv, team_similarity), resolve, ids)
        print(f"Auditoría del emparejamiento (equipos de Highlightly distintos; antes: {len(ids_old)} ids + "
              f"nombre; ahora: {len(ids)} ids, solo id):", audit)
    counts = Counter()
    ms = mu.uefa_matches(docs, resolve, counts)
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
    out: dict = {"counts": dict(counts), "audit": audit, "audit_detail": audit_detail}
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
    main_rows = rows["e1_vs_comp|ambos"]
    by_season = mu.season_metrics(main_rows, n_boot=n_boot)
    print("\nPor temporada (mp-e1 frente a la competición, los dos equipos con datos, las tres juntas)"
          "  mercado: partidos · diferencia [IC95] · etiqueta")
    for season, met in by_season.items():
        print(f"  temporada {season}")
        for v in sorted((v for v in met.values() if v["div"] == "ALL"), key=lambda v: v["market"]):
            print(f"    {v['market']:14} {v['n_matches']:4d} · {v['delta']:+.4f} "
                  f"[{_fmt(v['ci_lo'])}, {_fmt(v['ci_hi'])}] · {v['ci_label']}")
    evals = sorted(by_season)
    clean = mu.clean_seasons(evals)
    met_clean = None
    if clean != evals:
        met_clean = print_metrics(f"temporadas limpias juntas {clean} (sin las de diseño)",
                                  [r for r in main_rows if r[0] in clean], n_boot=n_boot)
    else:
        print(f"\nTemporadas limpias = todas las evaluadas {evals} (ninguna usada para diseñar mp-e1): "
              "la regla de temporadas limpias coincide con la de todo el periodo.")
    recent = [s for s in evals if s in mu.RECENT_SEASONS]
    recent_rows = [r for r in main_rows if r[0] in recent]
    met_recent = print_metrics(f"temporadas recientes juntas {list(mu.RECENT_SEASONS)}", recent_rows,
                               n_boot=n_boot) if recent_rows else {}
    out["eval_seasons"], out["clean_seasons"], out["recent_seasons"] = evals, clean, recent
    out["recent"] = {v["market"]: {f: v[f] for f in ("n_matches", "delta", "ci_lo", "ci_hi")}
                     for v in met_recent.values() if v["div"] == "ALL"}
    out["by_season"] = {s: {v["market"]: {f: v[f] for f in ("n_matches", "delta", "ci_lo", "ci_hi", "ci_label")}
                            for v in met.values() if v["div"] == "ALL"} for s, met in by_season.items()}
    out["labels"] = mu.star_labels(out.get("metrics") or {}, met_clean, met_recent)
    out["comp_labels"] = mu.comp_labels(out.get("metrics") or {})
    edge_seasons = {mk: sum(1 for s in out["by_season"].values() if (s.get(mk) or {}).get("ci_label") == mp.LABEL_EDGE)
                    for mk in out["labels"]}
    print("\nEtiquetas (IC95 entero bajo 0 frente a la competición en todo el periodo y en las temporadas "
          f"limpias juntas, y en {list(mu.RECENT_SEASONS)} juntas diferencia media ≤ 0; los dos equipos con "
          "datos, las tres competiciones):", out["labels"])
    print(f"Temporadas (de {len(evals)}) con el IC95 entero bajo 0, por mercado (informativo):", edge_seasons)
    print("Por competición (≥ 300 partidos, informativo):", out["comp_labels"] or "ninguna competición llega")
    return out


def main() -> int:
    from shared.firestore_client import iter_all
    from collectors.highlightly_loader import LEAGUE_LISTING, league_listing_matches
    docs = [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "uefa"))]
    league_old = [d.to_dict() or {} for d in iter_all("hl_matches")]
    league = league_old + league_listing_matches(d.to_dict() or {} for d in iter_all(LEAGUE_LISTING))
    print(f"Partidos europeos leídos: {len(docs)} · partidos de liga de Highlightly: {len(league_old)} "
          f"(hl_matches) + {len(league) - len(league_old)} (hl_league_listing)")
    if not docs:
        print("Sin partidos del grupo uefa en hl_team_stats: falta la carga (job uefa). Nada que medir.")
        return 0
    last_end = max((int(mc.season_of(d["date"])[2:]) + 2000 for d in docs if d.get("date")), default=2027)
    matches, failed = asyncio.run(_history(last_end))
    print(f"Historial de liga (CSV): {len(matches)} partidos · CSV que faltan: {failed or 'ninguno'}")
    out = report(docs, league, matches, league_old=league_old)
    if os.environ.get("CONFIRMAR") == "ESCRIBIR":
        from datetime import datetime, timezone
        from shared.firestore_client import col
        col("match_preview_metrics").document(DOC_ID).set({
            "model_version": mu.MODEL_VERSION, "computed_at": datetime.now(timezone.utc).isoformat(),
            "reference": "frecuencia de la competición", "coverage": "ambos equipos con datos de liga",
            "counts": out["counts"], "labels": out["labels"], "comp_labels": out["comp_labels"],
            "matching": "solo id de Highlightly (hl_matches + hl_league_listing)", "audit": out["audit"],
            "metrics": {k: v for k, v in (out.get("metrics") or {}).items() if v["div"] == "ALL"},
            "eval_seasons": out["eval_seasons"], "clean_seasons": out["clean_seasons"],
            "design_seasons": list(mu.DESIGN_SEASONS), "by_season": out["by_season"],
            "recent_seasons": out["recent_seasons"], "recent": out["recent"],
            "rule": ("ventaja medida: IC95 por partido de (Brier mp-e1 − Brier de la frecuencia de la "
                     "competición) entero bajo 0, las tres competiciones juntas, en todo el periodo y en las "
                     "temporadas limpias juntas (clean_seasons: sin las de diseño), y en recent_seasons "
                     "juntas diferencia media ≤ 0 (el modelo no peor que la referencia)")})
        print(f"Escrito match_preview_metrics/{DOC_ID} (métricas agregadas y etiquetas).")
    else:
        print("Sin escribir (solo lectura).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
