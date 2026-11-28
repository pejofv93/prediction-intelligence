"""Prueba SIN ESCRIBIR de los jugadores en las fichas de fútbol (pp-v1). Calcula el backtest en memoria
(scripts/ops/player_backtest.py, sin guardarlo), monta el historial con hl_match_players y, para los
partidos de las fichas de fútbol (match_previews) de los próximos DIAS días de las 8 ligas, saca los
jugadores que juegan (regla de minutos recientes) con su línea principal y % de más por mercado, la
fecha de su último partido, las ⭐ (mercados con ventaja medida, sin muestra insuficiente) y la píldora
de Telegram (como mucho 1 de jugador por partido).

Salida: fichero OUT_FILE (va CIFRADO en el workflow: lleva nombres de jugadores, que son datos de
Highlightly y solo pueden verse en privado). Log: solo recuentos. No escribe nada en Firestore ni
pide nada a Highlightly.
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "ops"))
sys.path.insert(0, ROOT)

import player_backtest as pb  # noqa: E402
from analyzers import player_preview as pp  # noqa: E402
from analyzers.match_preview import LABEL_EDGE  # noqa: E402

SHOW_PER_TEAM = 5
PILL_MIN, PILL_MAX = 0.60, 0.90
MAP_MIN_SIM = 0.5
NOUN = {"shots": "remates", "sot": "remates a puerta", "off": "remates fuera", "goals": "goles",
        "assists": "asistencias"}


def player_block(hist: pp.History, names: dict, div: str, season: str, side: str, tid, oid, labels: dict) -> list[dict]:
    out = []
    for pid in hist.candidates(tid):
        pred = hist.predict(div, season, side, tid, oid, pid)
        if pred is None:
            continue
        markets = {}
        for mk, (s, lines) in pp.MARKETS.items():
            sp = pred["stats"].get(s)
            if sp is None:
                continue
            probs = {ln: pp.poisson_over(sp["lam"], ln) for ln in lines}
            star = labels.get(mk) == LABEL_EDGE and not pred["low_sample"]
            markets[s] = {"exp": round(sp["lam"], 2), "main": pp.main_line(probs),
                          "probs": {str(k): round(v, 3) for k, v in probs.items()}, "star": star}
        out.append({"pid": pid, "name": names.get(pid, "?"), "pos": pred["pos"], "last_date": pred["last_date"],
                    "exp_min": pred["exp_min"], "low_sample": pred["low_sample"], "markets": markets})
    out.sort(key=lambda p: -(p["markets"].get("shots", {}).get("exp") or 0))
    return out[:SHOW_PER_TEAM]


def player_pill(players_by_team: dict) -> dict | None:
    """Como mucho 1 píldora de jugador por partido: entre los mercados con ⭐, en cada uno la línea
    MÁS ALTA entre el 60 % y el 90 %; de todas, la de mayor %."""
    best = None
    for team, players in players_by_team.items():
        for p in players:
            for s, m in p["markets"].items():
                if not m["star"]:
                    continue
                ok = [(float(ln), pr) for ln, pr in m["probs"].items() if PILL_MIN <= pr <= PILL_MAX]
                if not ok:
                    continue
                ln, pr = max(ok)
                if best is None or pr > best["p"]:
                    best = {"player": p["name"], "team": team, "stat": s, "line": ln, "p": pr}
    return best


def pill_text(x: dict) -> str:
    ln = f"{x['line']:.1f}".replace(".", ",")
    if x["stat"] == "goals" and x["line"] == 0.5:
        body = f"{x['player']} ({x['team']}) <b>marca</b>"
    elif x["stat"] == "assists" and x["line"] == 0.5:
        body = f"{x['player']} ({x['team']}) da al menos 1 asistencia"
    else:
        body = f"{x['player']} ({x['team']}) más de <b>{ln}</b> {NOUN[x['stat']]}"
    return f"⭐ {body} · <b>{round(x['p'] * 100)} %</b>"


def _upcoming(now: datetime, days: int) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    q = (col("match_previews").where(filter=FieldFilter("kickoff_utc", ">=", now.isoformat()))
         .where(filter=FieldFilter("kickoff_utc", "<=", (now + timedelta(days=days)).isoformat())))
    return [s.to_dict() for s in q.stream()]


def main() -> int:
    from collectors.highlightly_players import HL_LEAGUES, team_similarity
    now = datetime.now(timezone.utc)
    days = int(os.environ.get("DIAS") or 3)
    out_file = os.environ.get("OUT_FILE") or "fichas_jugadores.txt"
    docs = pb.load_docs()
    games = pp.games_from_docs(docs)
    res = pb.compute(games)
    labels = res["labels"]
    edge = sorted(mk for mk, lab in labels.items() if lab == LABEL_EDGE)
    print(f"Backtest en memoria (sin guardar): {res['n_rows']} filas; ⭐ en {edge}")
    today = now.date().isoformat()
    hist, names = pp.History(), {}
    teams_by_div = defaultdict(dict)          # div → {team_id: nombre}, temporada más reciente
    season_by_div = {}
    for g in games:
        if g["date"] >= today:
            continue
        hist.update(g)
        season_by_div[g["div"]] = g["season"]
        for side in ("home", "away"):
            for p in g[side]["players"]:
                names[p["pid"]] = p["name"]
    for g in games:
        if g["season"] == season_by_div.get(g["div"]):
            for side in ("home", "away"):
                teams_by_div[g["div"]][g[side]["team_id"]] = g[side]["team"]

    fixtures = [f for f in _upcoming(now, days) if f.get("div") in HL_LEAGUES]
    counts = Counter()
    lines = [f"Jugadores en las fichas de fútbol (prueba sin escribir, pp-v1), {now.isoformat()}",
             f"Backtest en memoria: ⭐ en {edge or 'ningún mercado'}",
             *[ln for ln in res["report"] if ln.startswith(("Recuentos", "Etiquetas", "Evaluadas"))], ""]
    web = []
    for f in sorted(fixtures, key=lambda x: (x["kickoff_utc"], x["div"])):
        div = f["div"]
        cand = teams_by_div.get(div) or {}

        def best(name):
            sc = sorted(((team_similarity(name, n), tid) for tid, n in cand.items()), reverse=True)
            return sc[0][1] if sc and sc[0][0] >= MAP_MIN_SIM else None
        hid, aid = best(f["home_team"]), best(f["away_team"])
        if hid is None or aid is None:
            counts["sin emparejar en Highlightly"] += 1
            continue
        season = season_by_div[div]
        by_team = {f["home_team"]: player_block(hist, names, div, season, "home", hid, aid, labels),
                   f["away_team"]: player_block(hist, names, div, season, "away", aid, hid, labels)}
        counts["partidos"] += 1
        counts["jugadores"] += sum(len(v) for v in by_team.values())
        pill = player_pill(by_team)
        counts["con píldora de jugador"] += pill is not None
        lines.append(f"== {f['kickoff_utc'][:16]} {div} · {f['home_team']} – {f['away_team']}")
        for team, players in by_team.items():
            lines.append(f"  {team}:")
            for p in players:
                cells = []
                for s in pp.STATS:
                    m = p["markets"].get(s)
                    if not m:
                        cells.append(f"{pp.STAT_NAMES[s]} —")
                        continue
                    ln = m["main"]
                    cells.append(f"{'⭐' if m['star'] else ''}{pp.STAT_NAMES[s]} +{ln}: {m['probs'][str(ln)]:.0%}")
                low = " · muestra insuficiente" if p["low_sample"] else ""
                lines.append(f"    {p['name']} ({p['pos']}, ~{p['exp_min']:.0f} min, último partido: "
                             f"{p['last_date']}{low}) · " + " · ".join(cells))
        lines.append("  Telegram: " + (pill_text(pill) if pill else "(sin píldora de jugador)"))
        web.append({"id": f.get("match_id"), "players": {t: [{k: v for k, v in p.items() if k != "pid"} for p in ps]
                                                          for t, ps in by_team.items()}})
    lines.append("\n---- JSON propuesto para la web (sección «Jugadores» de cada ficha) ----")
    lines.append(json.dumps(web[:3], ensure_ascii=False, indent=1))
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"Fichas de fútbol en la ventana ({days} días, 8 ligas): {len(fixtures)} · {dict(counts)}. "
          "Nada escrito en Firestore.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
