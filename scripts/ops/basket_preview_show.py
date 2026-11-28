"""Prueba SIN ESCRIBIR de las fichas de la Euroliga (bp-v1) con las etiquetas calculadas en el
momento: hace el backtest (scripts/ops/basket_backtest.compute) en memoria, sin guardar
match_preview_metrics/bp-v1-EUR_backtest, y con él corre run_basket_previews(dry_run=True).

Lee eur_games y match_previews_basket de Firestore y el calendario del feed público de la
Euroliga. No escribe nada en Firestore ni gasta cuota de Highlightly.

Log público: solo recuentos (partidos, mercados con ventaja, acciones). Las fichas (equipos,
medias, líneas, píldoras ⭐ y el resumen de Telegram tal como saldría) van a OUT_FILE, que el
workflow cifra con la clave pública que se le pasa antes de subirlo.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts", "ops"))
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, ROOT)

import basket_backtest as bb  # noqa: E402
from analyzers import basket_preview as bp  # noqa: E402
from analyzers import basket_preview_live as bl  # noqa: E402


def ficha_text(p: dict) -> list[str]:
    """Una ficha en texto: medias del modelo y de la liga, y por mercado la línea principal con
    P(más) del modelo frente a la frecuencia de la liga y del equipo; ⭐ = ventaja medida."""
    out = [f"\n== {p['kickoff_utc'][:16]} UTC · {p['home_team']} – {p['away_team']} "
           f"({p['match_id']}, jornada {p.get('round')}, datos hasta {p.get('data_cutoff')})"]
    if p.get("low_sample"):
        out.append("   muestra insuficiente (algún equipo con menos de 5 partidos esta temporada)")
    out.append(f"   posesiones esperadas: {p.get('exp_poss')}")
    for s in bp.STATS:
        b = p["stats"][s]
        out.append(f"   {bp.STAT_NAMES[s]}: modelo {b['exp_home']} – {b['exp_away']} (total {b['exp_total']}; "
                   f"sesgo corregido equipo {b.get('bias_team'):+} / total {b.get('bias_total'):+}) · "
                   f"liga {b['league_mean_home']} – {b['league_mean_away']}")
        for side, name in (("home", "local"), ("away", "visitante"), ("total", "total")):
            blk = b["lines"][side]
            star = "⭐" if blk["highlight"] else "  "
            cells = []
            for ln in sorted(blk["lines"].values(), key=lambda x: x["line"]):
                mark = "*" if ln["line"] == blk["main_line"] else ""
                extra = " (extra)" if ln.get("extra") else ""
                pl = "—" if ln["p_league"] is None else f"{ln['p_league']:.0%}"
                pt = "—" if ln["p_team"] is None else f"{ln['p_team']:.0%}"
                cells.append(f"+{ln['line']}{mark}{extra}: {ln['p_model']:.0%} (liga {pl}, equipo {pt})")
            out.append(f"     {star} {name:9} [{blk['label']}] sd {blk['sd']} · " + " · ".join(cells))
    return out


def telegram_preview(previews: list[dict]) -> list[str]:
    """El resumen de Telegram tal como saldría con estas fichas (por día de Madrid)."""
    sys.path.insert(0, os.path.join(ROOT, "services", "telegram-bot"))
    import trends_summary as ts
    out = []
    days = sorted({ts.madrid(ts._parse(p["kickoff_utc"])).date() for p in previews})
    for d in days:
        day_p = [p for p in previews if ts.madrid(ts._parse(p["kickoff_utc"])).date() == d]
        out.append(f"\n---- Telegram, {d} (solo baloncesto) ----")
        for m in ts.build_messages(day_p, d):
            out.append(m)
    return out


def main() -> int:
    league = (os.environ.get("LIGA") or "EUR").upper()      # EUR (Euroliga) o NBA
    if league not in bl.HISTORY_COLL:
        print(f"LIGA debe ser una de {sorted(bl.HISTORY_COLL)}")
        return 1
    name = bl.LEAGUE_NAMES[league]
    out_file = os.environ.get("OUT_FILE") or "fichas_baloncesto.txt"
    docs = bb.load_docs(league)
    games, _ = bb.filter_phases(bp.games_from_docs(docs, league), league)
    res = bb.compute(games, league)
    doc = bb.backtest_doc(res, league, datetime.now(timezone.utc).isoformat())
    edge = sorted(mk for mk, lab in doc["labels"].items() if lab == bp.LABEL_EDGE)
    print(f"Backtest en memoria (sin guardar): {res['n_matches']} partidos evaluados; "
          f"mercados con ventaja medida: {edge}")
    sesgo = next((ln for ln in res["report"] if ln.startswith("Sesgo medio")), "")
    print(sesgo)

    collected: dict = {}
    days = int(os.environ.get("DIAS") or bl.WINDOW_DAYS)   # ventana de la prueba (las fichas reales: 3 días)
    summary = asyncio.run(bl.run_basket_previews(dry_run=True, backtest=doc, collect=collected, days=days,
                                                 league=league))
    print("Prueba sin escribir: " + json.dumps({k: summary.get(k) for k in
                                                 ("ok", "dry_run", "days", "fixtures_window", "history_games", "counts",
                                                  "would_write", "edge_markets", "errors", "aviso")},
                                                ensure_ascii=False))
    summary["days"] = days
    previews = sorted(collected.values(), key=lambda p: (p["kickoff_utc"], p["match_id"]))
    lines = [f"Fichas de {name} que se escribirían ({len(previews)}), {summary.get('now')}",
             f"Etiquetas calculadas en el momento (backtest en memoria, sin guardar): ventaja medida en {edge}",
             sesgo]
    for p in previews:
        lines += ficha_text(p)
    if previews:
        try:
            lines += telegram_preview(previews)
        except Exception as e:      # el resumen es un extra: la prueba vale sin él
            lines.append(f"\n(resumen de Telegram no disponible: {type(e).__name__})")
    lines.append("\n---- JSON de la web (match_previews_basket → web_match) ----")
    from analyzers.match_preview_store import web_match
    lines.append(json.dumps([web_match(p) for p in previews], ensure_ascii=False, indent=1))
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Fichas: {len(previews)} (en el fichero cifrado). Nada escrito en Firestore.")
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
