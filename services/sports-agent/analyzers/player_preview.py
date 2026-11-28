"""
services/sports-agent/analyzers/player_preview.py
Jugadores en las fichas de fútbol de Tendencias (pp-v1) — modelo puro: sin red ni Firestore.
Decisión del usuario (28-nov-2026): remates totales, a puerta, fuera, goles y asistencias por
jugador, con línea principal y % de más; quién juega por minutos recientes, enseñando la fecha del
último partido; ⭐ con la regla de Europa; en Telegram como mucho 1 píldora de jugador por partido.

Datos: hl_match_players (box score de Highlightly de las 8 ligas, temporadas 2023/24 a la actual).
Solo en la web privada y en Telegram (los términos de Highlightly prohíben exponerlos como API o
exportación).

Modelo, por jugador y partido (walk-forward por fecha: solo partidos de días anteriores):
  - Juega si fue titular en al menos 2 de los 3 últimos partidos de liga de su equipo y jugó en el
    último (PLAYS_*). Sin al menos MIN_APPS apariciones en esta temporada y la anterior: muestra
    insuficiente (se calcula, nunca lleva ⭐).
  - Minutos esperados: media de sus últimas MIN_LAST apariciones (tope 90).
  - Ritmo por 90 minutos en esta temporada y la anterior, encogido hacia el de su posición en la liga
    (RATE_K noventas de la posición).
  - Rival: lo que concede por partido en esa estadística frente a la media de la liga (encogido con
    OPP_K partidos de la media). Casa/fuera: cociente de la liga.
  - λ = ritmo × minutos/90 × rival × casa/fuera; P(más de la línea) con Poisson.
Referencias del backtest (las dos de la regla): la media por partido del propio jugador (mismo
historial) y la media por partido de su posición en la liga (titulares).
Remates fuera = totales − a puerta (la misma definición en todos los partidos; el campo propio de
Highlightly solo está en los docs con fields_version 2). Asistencias: solo docs con fields_version 2.
"""
import math
from collections import defaultdict, deque
from typing import Iterable, Optional

MODEL_VERSION = "pp-v1"
METRICS_DOC = "pp-v1_backtest"

# mercado → (estadística, líneas fijas)
MARKETS: dict[str, tuple[str, tuple[float, ...]]] = {
    "shots_player": ("shots", (0.5, 1.5, 2.5, 3.5)),
    "sot_player": ("sot", (0.5, 1.5, 2.5)),
    "off_player": ("off", (0.5, 1.5, 2.5)),
    "goals_player": ("goals", (0.5, 1.5)),
    "assists_player": ("assists", (0.5,)),
}
STATS = ("shots", "sot", "off", "goals", "assists")
STAT_NAMES = {"shots": "Remates", "sot": "A puerta", "off": "Fuera", "goals": "Goles", "assists": "Asistencias"}
POSITIONS = ("Defender", "Midfielder", "Forward")      # porteros fuera
PLAYS_LAST_N, PLAYS_MIN_STARTS = 3, 2
MIN_LAST = 5
MIN_APPS = 5
RATE_K = 4.0          # noventas de la posición
OPP_K = 8.0           # partidos de la media de la liga
EVAL_SEASONS = ("2425", "2526", "2627")
RECENT_SEASONS = ("2526", "2627")
SEASONS = ("2324", "2425", "2526", "2627")


def prev_season(s: str) -> str:
    """'2526' → '2425'."""
    a = int(s[:2]) - 1
    return f"{a:02d}{a + 1:02d}"


def row_stats(p: dict, fields_version: int) -> dict:
    """Fila de jugador de hl_match_players → {estadística: valor o None si no hay dato}."""
    shots, sot = int(p.get("shots_total") or 0), int(p.get("shots_on_target") or 0)
    return {"shots": shots, "sot": sot, "off": max(shots - sot, 0), "goals": int(p.get("goals") or 0),
            "assists": int(p.get("assists") or 0) if fields_version >= 2 and "assists" in p else None}


def games_from_docs(docs: Iterable[dict]) -> list[dict]:
    """Docs de hl_match_players → partidos ordenados por fecha con lo que usa el modelo."""
    out = []
    for d in docs:
        teams = d.get("teams") or []
        if len(teams) != 2 or not d.get("date") or not d.get("season") or not d.get("div"):
            continue
        fv = int(d.get("fields_version") or 1)
        sides = {}
        for t in teams:
            players = []
            for p in t.get("players") or []:
                if not p.get("player_id") or int(p.get("minutes") or 0) <= 0:
                    continue
                players.append({"pid": p["player_id"], "name": p.get("name"), "pos": p.get("position"),
                                "start": not p.get("is_substitute"), "min": min(int(p["minutes"]), 120),
                                "st": row_stats(p, fv)})
            sides[t.get("side")] = {"team_id": t.get("team_id"), "team": t.get("team"), "players": players}
        if set(sides) != {"home", "away"}:
            continue
        for side, opp in (("home", "away"), ("away", "home")):
            tot = {}
            for s in STATS:
                vals = [p["st"][s] for p in sides[side]["players"]]
                tot[s] = None if any(v is None for v in vals) else sum(vals)
            sides[side]["tot"] = tot
        out.append({"match_id": d.get("match_id"), "div": d["div"], "season": str(d["season"]),
                    "date": str(d["date"])[:10], "home": sides["home"], "away": sides["away"]})
    return sorted(out, key=lambda g: (g["date"], str(g["match_id"])))


def poisson_over(lam: float, line: float) -> float:
    """P(X > line) con X ~ Poisson(λ)."""
    k = int(math.floor(line))
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k + 1):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _acc():
    return {s: 0.0 for s in STATS}


class History:
    """Acumulados walk-forward por temporada. update(g) después de predecir el día de g."""

    def __init__(self):
        self.player = defaultdict(lambda: defaultdict(lambda: {"n": 0, "min": 0.0, "sum": _acc(), "n_ok": _acc()}))
        self.player_last = defaultdict(lambda: deque(maxlen=MIN_LAST))    # (fecha, minutos, equipo)
        self.player_pos = {}
        self.team_last = defaultdict(lambda: deque(maxlen=PLAYS_LAST_N))  # {pid: titular}
        self.team_last_date = {}
        self.conc = defaultdict(lambda: defaultdict(lambda: {"n": _acc(), "sum": _acc()}))  # equipo→temp
        self.league = defaultdict(lambda: {"n": _acc(), "sum": _acc(), "home": _acc(), "nh": _acc(),
                                           "away": _acc(), "na": _acc()})                    # (div, temp)
        self.pos = defaultdict(lambda: {"min": _acc(), "sum": _acc(), "apps": _acc()})       # (div, temp, pos)

    def update(self, g: dict) -> None:
        key = (g["div"], g["season"])
        for side, opp in (("home", "away"), ("away", "home")):
            t, o = g[side], g[opp]
            lg = self.league[key]
            for s in STATS:
                v = t["tot"][s]
                if v is None:
                    continue
                lg["sum"][s] += v
                lg["n"][s] += 1
                lg[side][s] += v
                lg["nh" if side == "home" else "na"][s] += 1
                c = self.conc[o["team_id"]][g["season"]]
                c["sum"][s] += v
                c["n"][s] += 1
            self.team_last[t["team_id"]].append({p["pid"]: p["start"] for p in t["players"]})
            self.team_last_date[t["team_id"]] = g["date"]
            for p in t["players"]:
                acc = self.player[p["pid"]][g["season"]]
                acc["n"] += 1
                acc["min"] += p["min"]
                for s in STATS:
                    if p["st"][s] is not None:
                        acc["sum"][s] += p["st"][s]
                        acc["n_ok"][s] += 1
                self.player_last[p["pid"]].append((g["date"], p["min"], t["team_id"]))
                if p["pos"]:
                    self.player_pos[p["pid"]] = p["pos"]
                if p["start"] and p["pos"] in POSITIONS:
                    pa = self.pos[(g["div"], g["season"], p["pos"])]
                    for s in STATS:
                        if p["st"][s] is not None:
                            pa["sum"][s] += p["st"][s]
                            pa["min"][s] += p["min"]
                            pa["apps"][s] += 1

    # ── consultas ──
    def plays(self, team_id, pid) -> bool:
        last = list(self.team_last.get(team_id) or [])
        if len(last) < PLAYS_LAST_N:
            return False
        starts = sum(1 for m in last if m.get(pid))
        return starts >= PLAYS_MIN_STARTS and pid in last[-1]

    def same_team(self, pid, team_id) -> bool:
        """Sus últimas PLAYS_LAST_N apariciones de liga (box score, por id de equipo, no por nombre)
        fueron todas con este equipo: fuera los traspasados que aún salen en partidos del anterior."""
        apps = list(self.player_last.get(pid) or [])[-PLAYS_LAST_N:]
        return bool(apps) and all(a[2] == team_id for a in apps)

    def recent_apps(self, pid, n: int = PLAYS_LAST_N) -> list[tuple]:
        """[(fecha, minutos, id de equipo)] de sus últimas n apariciones de liga."""
        return list(self.player_last.get(pid) or [])[-n:]

    def candidates(self, team_id) -> list:
        """Jugadores de campo que cumplen la regla de «juega» (de los últimos partidos del equipo) y
        cuyas últimas apariciones fueron todas con este equipo."""
        last = list(self.team_last.get(team_id) or [])
        pids = {pid for m in last for pid in m}
        return sorted(pid for pid in pids if self.player_pos.get(pid) in POSITIONS and self.plays(team_id, pid)
                      and self.same_team(pid, team_id))

    def _two(self, d: dict, season: str):
        return [d[s] for s in (season, prev_season(season)) if s in d]

    def predict(self, div: str, season: str, side: str, team_id, opp_id, pid) -> Optional[dict]:
        """{estadística: {lam, ref_player, ref_pos}} + muestra, o None si no hay con qué."""
        pos = self.player_pos.get(pid)
        if pos not in POSITIONS:
            return None
        accs = self._two(self.player[pid], season)
        n_apps = sum(a["n"] for a in accs)
        mins = sum(a["min"] for a in accs)
        last = list(self.player_last[pid])
        if not n_apps or not last or mins <= 0:
            return None
        exp_min = min(90.0, sum(a[1] for a in last) / len(last))
        lgs = [self.league[(div, s)] for s in (season, prev_season(season)) if (div, s) in self.league]
        poss = [self.pos[(div, s, pos)] for s in (season, prev_season(season)) if (div, s, pos) in self.pos]
        concs = self._two(self.conc[opp_id], season)
        out = {"low_sample": n_apps < MIN_APPS, "n_apps": n_apps, "exp_min": round(exp_min, 1),
               "last_date": last[-1][0], "pos": pos, "stats": {}}
        for s in STATS:
            n_ok = sum(a["n_ok"][s] for a in accs)
            ln = sum(l["n"][s] for l in lgs)
            pmin = sum(p["min"][s] for p in poss)
            papps = sum(p["apps"][s] for p in poss)
            if not n_ok or not ln or not pmin or not papps:
                continue
            # minutos de las apariciones con dato (asistencias: solo docs con fields_version 2)
            share = n_ok / n_apps
            mins_ok = mins * share
            psum = sum(a["sum"][s] for a in accs)
            pos_rate = sum(p["sum"][s] for p in poss) / pmin * 90.0
            rate = (psum + RATE_K * pos_rate) / (mins_ok / 90.0 + RATE_K)
            lmean = sum(l["sum"][s] for l in lgs) / ln
            cn = sum(c["n"][s] for c in concs)
            csum = sum(c["sum"][s] for c in concs)
            opp = ((csum + OPP_K * lmean) / (cn + OPP_K)) / lmean if lmean > 0 else 1.0
            sh = sum(l[side][s] for l in lgs)
            shn = sum(l["nh" if side == "home" else "na"][s] for l in lgs)
            side_f = (sh / shn) / lmean if shn and lmean > 0 else 1.0
            lam = rate * exp_min / 90.0 * opp * side_f
            out["stats"][s] = {"lam": lam, "ref_player": psum / n_ok,
                               "ref_pos": sum(p["sum"][s] for p in poss) / papps}
        return out if out["stats"] else None


def main_line(probs: dict) -> Optional[float]:
    """Línea principal: la de P(más) más cercana al 50 %."""
    return min(probs, key=lambda ln: (abs(probs[ln] - 0.5), ln)) if probs else None


def backtest_rows(games: list[dict]) -> tuple[list[tuple], list[tuple], dict]:
    """Walk-forward por día. Filas (temporada, liga, mercado, línea, y, p_ref, p_modelo, partido):
    frente a la media del jugador y frente a la de su posición. Solo jugadores que cumplían la regla
    de «juega» antes del partido Y jugaron (si no juega, la apuesta se anula), sin muestra
    insuficiente, en EVAL_SEASONS."""
    hist = History()
    rows_player, rows_pos = [], []
    counts = defaultdict(int)
    by_day = defaultdict(list)
    for g in games:
        by_day[g["date"]].append(g)
    for day in sorted(by_day):
        day_games = by_day[day]
        for g in day_games:
            if g["season"] not in EVAL_SEASONS:
                continue
            for side, opp in (("home", "away"), ("away", "home")):
                t, o = g[side], g[opp]
                played = {p["pid"]: p for p in t["players"]}
                for pid in hist.candidates(t["team_id"]):
                    counts["candidatos"] += 1
                    if pid not in played:
                        counts["no jugó (anulada)"] += 1
                        continue
                    pred = hist.predict(g["div"], g["season"], side, t["team_id"], o["team_id"], pid)
                    if pred is None:
                        counts["sin historial"] += 1
                        continue
                    if pred["low_sample"]:
                        counts["muestra insuficiente"] += 1
                        continue
                    counts["evaluados"] += 1
                    actual = played[pid]["st"]
                    for mk, (s, lines) in MARKETS.items():
                        sp = pred["stats"].get(s)
                        if sp is None or actual[s] is None:
                            continue
                        for ln in lines:
                            y = int(actual[s] > ln)
                            pm = poisson_over(sp["lam"], ln)
                            base = (g["season"], g["div"], mk, ln, y)
                            rows_player.append(base + (poisson_over(sp["ref_player"], ln), pm, g["match_id"]))
                            rows_pos.append(base + (poisson_over(sp["ref_pos"], ln), pm, g["match_id"]))
        for g in day_games:
            hist.update(g)
    return rows_player, rows_pos, dict(counts)
