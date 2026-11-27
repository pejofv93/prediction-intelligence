"""Prueba SIN ESCRIBIR de las fichas de Europa (mp-e1: Champions, Europa League, Conference League)
con las etiquetas calculadas en el momento: hace el backtest (scripts/ops/uefa_backtest.report) en
memoria, sin guardar match_preview_metrics/mp-e1_backtest, y con él corre
run_uefa_previews(dry_run=True) sobre los partidos de la próxima jornada europea.

Partidos: los que ya están en hl_team_stats (grupo uefa) sin terminar (la carga de Europa guarda el
listado entero de la temporada): 0 peticiones a Highlightly. Próxima jornada = desde el primer
partido por jugar (o desde DESDE) hasta 2 días después; con DIAS > 0, esa ventana en días.
Lee hl_team_stats, hl_matches y los CSV públicos de football-data.co.uk. No escribe nada en
Firestore: la función de escritura del módulo de fichas se sustituye por una que solo cuenta.

Log público: solo agregados (backtest) y recuentos por fecha y competición. Las fichas (equipos,
medias, líneas, ⭐, el resumen de Telegram tal como saldría y el JSON de la web) van a OUT_FILE, que el
workflow cifra con la clave pública que se le pasa antes de subirlo.
"""
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts", "ops"))
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, ROOT)

import uefa_backtest as ub  # noqa: E402
from analyzers import match_preview as mp  # noqa: E402
from analyzers import match_preview_cups as mc  # noqa: E402
from analyzers import match_preview_uefa as mu  # noqa: E402
from analyzers import match_preview_uefa_live as ul  # noqa: E402

MATCHDAY_SPAN = 2          # una jornada europea: martes-jueves


def upcoming(docs: list[dict], now: datetime) -> list[dict]:
    """Partidos europeos por jugar (hl_team_stats sin terminar, con hora y los dos ids) en forma de
    fixture del estado de las fichas."""
    out = []
    for d in docs:
        if d.get("finished") or d.get("comp") not in mu.COMPS or not d.get("kickoff_utc"):
            continue
        if d.get("home_team_id") is None or d.get("away_team_id") is None:
            continue
        try:
            ko = mp_parse(d["kickoff_utc"])
        except (TypeError, ValueError):
            continue
        if ko < now:
            continue
        out.append({"hl_match_id": d.get("match_id"), "league_id": d.get("league_id"), "comp": d["comp"],
                    "date": ko.date().isoformat(), "kickoff_utc": ko.isoformat(),
                    "hl_home": d.get("home_team"), "hl_away": d.get("away_team"),
                    "hl_home_id": d["home_team_id"], "hl_away_id": d["away_team_id"]})
    return sorted(out, key=lambda f: (f["kickoff_utc"], f["comp"], str(f["hl_match_id"])))


def mp_parse(s: str) -> datetime:
    from analyzers.match_preview_store import _parse
    return _parse(s)


def pick_window(fixtures: list[dict], desde: Optional[date], dias: int) -> tuple[list[dict], str, str]:
    """Partidos de la ventana: [desde o primer partido, + dias) si dias > 0; si no, la jornada que
    empieza en el primer partido desde `desde` (MATCHDAY_SPAN días más)."""
    fx = [f for f in fixtures if desde is None or f["date"] >= desde.isoformat()]
    if not fx:
        return [], "", ""
    lo = desde.isoformat() if (desde and dias > 0) else fx[0]["date"]
    span = dias - 1 if dias > 0 else MATCHDAY_SPAN
    hi = (date.fromisoformat(lo) + timedelta(days=span)).isoformat()
    return [f for f in fx if lo <= f["date"] <= hi], lo, hi


def ficha_text(p: dict) -> list[str]:
    """Una ficha en texto: esperados con factor de liga y, por mercado, P(más) del modelo frente a la
    frecuencia de la competición en cada línea; ⭐ = ventaja medida."""
    t = p["teams"]
    out = [f"\n== {p['kickoff_utc'][:16]} UTC · {p['comp_name']} · {p['home_team']} – {p['away_team']} "
           f"({p['match_id']}, datos hasta {p.get('data_cutoff')})",
           f"   local: liga {t['home']['div'] or 'sin liga'} · {t['home']['n_cur']} partidos esta temporada"
           f"{'' if p['home_resolved'] else ' · SIN EMPAREJAR'} | visitante: liga {t['away']['div'] or 'sin liga'} · "
           f"{t['away']['n_cur']} partidos{'' if p['away_resolved'] else ' · SIN EMPAREJAR'}"]
    if p.get("low_sample"):
        out.append("   muestra insuficiente (un equipo sin datos de liga o con pocos partidos): sin ⭐")
    for s in mp.STATS:
        b = p["stats"][s]
        if not b.get("available"):
            out.append(f"   {s}: no disponible ({b.get('reason')})")
            continue
        out.append(f"   {s}: modelo {b['exp_home']} – {b['exp_away']} (total {b['exp_total']}; factor de liga "
                   f"{b['factor_home']} / {b['factor_away']}) · competición {b['league_mean_home']} – "
                   f"{b['league_mean_away']}")
        for side, name in (("home", "local"), ("away", "visitante"), ("total", "total")):
            blk = b["lines"][side]
            star = "⭐" if blk["highlight"] else "  "
            cells = []
            for ln in sorted(blk["lines"].values(), key=lambda x: x["line"]):
                mark = "*" if ln["line"] == blk["main_line"] else ""
                extra = " (extra)" if ln.get("extra") else ""
                pl = "—" if ln["p_league"] is None else f"{ln['p_league']:.0%}"
                cells.append(f"+{ln['line']}{mark}{extra}: {ln['p_model']:.0%} (comp. {pl})")
            out.append(f"     {star} {name:9} [{blk['label']}] · " + " · ".join(cells))
    return out


def telegram_preview(previews: list[dict]) -> list[str]:
    """El resumen de Telegram tal como saldría con estas fichas (por día de Madrid, solo Europa)."""
    sys.path.insert(0, os.path.join(ROOT, "services", "telegram-bot"))
    import trends_summary as ts
    out = []
    for d in sorted({ts.madrid(ts._parse(p["kickoff_utc"])).date() for p in previews}):
        day_p = [p for p in previews if ts.madrid(ts._parse(p["kickoff_utc"])).date() == d]
        out.append(f"\n---- Telegram, {d} (solo Europa) ----")
        out += ts.build_messages(day_p, d)
    return out


def main() -> int:
    from collectors.highlightly_players import team_similarity
    from shared.firestore_client import iter_all
    out_file = os.environ.get("OUT_FILE") or "fichas_europa.txt"
    dias = int(os.environ.get("DIAS") or 0)
    desde = date.fromisoformat(os.environ["DESDE"]) if os.environ.get("DESDE") else None
    now = datetime.now(timezone.utc)

    docs = [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "uefa"))]
    league = [d.to_dict() or {} for d in iter_all("hl_matches")]
    print(f"Partidos europeos leídos: {len(docs)} · partidos de liga de Highlightly: {len(league)}")
    last_end = max((int(mc.season_of(d["date"])[2:]) + 2000 for d in docs if d.get("date")), default=2027)
    matches, failed = asyncio.run(ub._history(last_end))
    print(f"Historial de liga (CSV): {len(matches)} partidos · CSV que faltan: {failed or 'ninguno'}")
    res = ub.report(docs, league, matches)
    backtest = {"model_version": mu.MODEL_VERSION, "labels": res["labels"], "clean_seasons": res["clean_seasons"]}
    edge = sorted(mk for mk, lab in res["labels"].items() if lab == mp.LABEL_EDGE)
    print(f"\nBacktest en memoria (sin guardar): ⭐ en {edge}")

    fx_all = upcoming(docs, now)
    print("Partidos por jugar por fecha y competición:",
          dict(sorted(Counter(f"{f['date']} {f['comp']}" for f in fx_all).items())[:20]))
    fx, lo, hi = pick_window(fx_all, desde, dias)
    print(f"Ventana de la prueba: {lo} → {hi} · {len(fx)} partidos")

    # Como la pasada real: historial y partidos europeos de esta temporada y la anterior; mapa de ids
    # de hoy (así no se recalcula ni se guarda el estado).
    season = mc.season_of(now.date().isoformat())
    keep = {season, mp.prev_season(season)}
    hist_m = [m for m in matches if m["season"] in keep]
    hist = mp.History(hist_m, prev_divs=mu.ALL_DIVS)
    csv_by_div_day: dict = {}
    for m in hist_m:
        csv_by_div_day.setdefault((m["div"], m["date"]), []).append(m)
    ids = {str(k): v for k, v in mc.hl_id_names(league, csv_by_div_day, team_similarity).items()}
    state = {"listed": {}, "fixtures": fx, "id_names": {"updated_at": now.isoformat(), "map": ids}}
    uefa_docs = [d for d in docs if d.get("date") and mc.season_of(d["date"][:10]) in keep]

    writes = []
    ul._set_docs = lambda coll, data: writes.append(coll)       # prueba: nada llega a Firestore
    collected: dict = {}
    summary = asyncio.run(ul.run_uefa_previews(dry_run=True, now=now, highlightly=False, state=state,
                                               matches=hist_m, uefa_docs=uefa_docs, backtest=backtest, live={},
                                               existing={}, collect=collected))
    print("Prueba sin escribir: " + json.dumps({k: summary.get(k) for k in
                                                 ("ok", "dry_run", "fixtures_window", "counts", "by_comp", "engine",
                                                  "id_map", "would_write", "errors", "aviso")}, ensure_ascii=False))
    previews = sorted(collected.values(), key=lambda p: (p["kickoff_utc"], p["comp"], p["match_id"]))
    stars = sum(1 for p in previews for s in mp.STATS for b in [p["stats"][s]] if b.get("available")
                for side in ("home", "away") if b["lines"][side]["highlight"])
    print(f"Fichas: {len(previews)} · con muestra insuficiente: {sum(1 for p in previews if p['low_sample'])} · "
          f"bloques con ⭐ (equipo): {stars} · escrituras bloqueadas: {len(writes)}")

    lines = [f"Fichas de Europa que se escribirían ({len(previews)}), {now.isoformat()}, ventana {lo} → {hi}",
             f"Etiquetas calculadas en el momento (backtest en memoria, sin guardar): ⭐ en {edge}",
             f"Temporadas limpias: {res['clean_seasons']}"]
    for p in previews:
        lines += ficha_text(p)
    if previews:
        try:
            lines += telegram_preview(previews)
        except Exception as e:      # el resumen es un extra: la prueba vale sin él
            lines.append(f"\n(resumen de Telegram no disponible: {type(e).__name__}: {e})")
        lines.append("\n---- JSON de la web (match_previews_uefa → web_match) ----")
        try:
            from analyzers.match_preview_store import web_match
            lines.append(json.dumps([web_match(p) for p in previews], ensure_ascii=False, indent=1))
        except Exception as e:
            lines.append(f"(web no disponible: {type(e).__name__}: {e})")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("Fichas en el fichero cifrado. Nada escrito en Firestore.")
    return 0 if summary.get("ok") and not writes else 1


if __name__ == "__main__":
    sys.exit(main())
