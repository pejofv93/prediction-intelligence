"""
scripts/trend_variant_backtest.py

Backtest de variantes de las reglas de RACHA del feed de tendencias (Ambos marcan,
goles 2+, goles 3+, hándicap -2) sobre los partidos pasados de team_stats.raw_matches.
Solo lectura: no escribe nada en Firestore ni toca el feed.

Menú CERRADO: las variantes están definidas abajo como datos y su hash va fijado en
MENU_SHA256. El script se niega a correr si el menú (o las reglas de selección) no
coinciden con el hash: cambiar el menú obliga a cambiar también el hash en un commit
visible, después del de este archivo. El menú se commiteó antes de la primera ejecución.

Datos: une los raw_matches de todos los equipos de fútbol (cada doc guarda ~20 últimos
partidos) y reconstruye la historia de cada equipo en orden cronológico. Cada caso solo
ve partidos anteriores a su fecha.

Partición por fecha (fija):
  ajuste       — partidos anteriores a SPLIT_DATE: aquí se ELIGE la variante ganadora
  comprobación — partidos desde SPLIT_DATE: aquí solo se MIDE la ganadora ya elegida

Regla de elección (fija): entre las variantes con al menos DEV_MIN_N casos de ajuste,
la de mayor límite inferior de Wilson al 95% en ajuste (desempate: más casos).
Veredicto (fijo): pasa si en comprobación tiene al menos TEST_MIN_N casos y el límite
inferior de Wilson al 95% es >= objetivo - TEST_TOLERANCE.

Mercados del modelo (doble oportunidad, empate no apuesta, total exacto, margen): fuera,
no hay xG ni Poisson históricos guardados — solo se pueden probar en sombra hacia delante.

Uso:
    python scripts/trend_variant_backtest.py                      # local (REST)
    python scripts/trend_variant_backtest.py --transport grpc     # CI / Cloud Run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fsrest import get_db  # noqa: E402

SPLIT_DATE = "2025-07-01"
DEV_MIN_N = 30
TEST_MIN_N = 300
TEST_TOLERANCE = 0.03

# Objetivo propio de cada mercado (mismos valores que shared/config.py TREND_MARKET_TARGET).
TARGETS = {"btts": 0.70, "team_goals_over": 0.70, "team_goals_over_3": 0.50, "handicap": 0.55}

# Condición: who = team | opp (mercados de equipo) · any | both | home | away (Ambos marcan)
#            stat = qué cuenta en cada partido de la historia · rate = mínimo
#            W = ventana · min_n = partidos mínimos en la ventana · venue = solo partidos
#            en la misma condición (local en casa, visitante fuera).
# v1 = la regla que está hoy en producción (para comparar).
MENU = {
    "btts": [
        {"id": "v1", "desc": "actual: un equipo >=70% (10 partidos)",
         "conds": [{"who": "any", "stat": "btts", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "B2", "desc": "los dos equipos >=70% (10)",
         "conds": [{"who": "both", "stat": "btts", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "B3", "desc": "los dos equipos >=70% (15)",
         "conds": [{"who": "both", "stat": "btts", "rate": 0.70, "W": 15, "min_n": 8}]},
        {"id": "B4", "desc": "los dos equipos >=70% (20)",
         "conds": [{"who": "both", "stat": "btts", "rate": 0.70, "W": 20, "min_n": 8}]},
        {"id": "B5", "desc": "los dos equipos >=60% (10)",
         "conds": [{"who": "both", "stat": "btts", "rate": 0.60, "W": 10, "min_n": 8}]},
        {"id": "B6", "desc": "los dos marcan >=80% y los dos encajan >=80% (10)",
         "conds": [{"who": "both", "stat": "score", "rate": 0.80, "W": 10, "min_n": 8},
                   {"who": "both", "stat": "concede", "rate": 0.80, "W": 10, "min_n": 8}]},
        {"id": "B7", "desc": "local en casa >=70% y visitante fuera >=70% (10)",
         "conds": [{"who": "home", "stat": "btts", "rate": 0.70, "W": 10, "min_n": 5, "venue": True},
                   {"who": "away", "stat": "btts", "rate": 0.70, "W": 10, "min_n": 5, "venue": True}]},
        {"id": "B8", "desc": "un equipo >=80% (10)",
         "conds": [{"who": "any", "stat": "btts", "rate": 0.80, "W": 10, "min_n": 8}]},
    ],
    "team_goals_over": [
        {"id": "v1", "desc": "actual: equipo marca 2+ >=70% (10)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "G2", "desc": "equipo 2+ >=70% (15)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 15, "min_n": 8}]},
        {"id": "G3", "desc": "equipo 2+ >=70% (20)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 20, "min_n": 8}]},
        {"id": "G4", "desc": "equipo 2+ >=70% en su condición (10)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 10, "min_n": 5, "venue": True}]},
        {"id": "G5", "desc": "equipo 2+ >=70% y rival encaja 2+ >=50% (10)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "concede2", "rate": 0.50, "W": 10, "min_n": 8}]},
        {"id": "G6", "desc": "equipo 2+ >=70% y rival encaja 2+ >=40% (10)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "concede2", "rate": 0.40, "W": 10, "min_n": 8}]},
        {"id": "G7", "desc": "equipo 2+ >=80% (10)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.80, "W": 10, "min_n": 8}]},
        {"id": "G8", "desc": "equipo 2+ >=70% (10) jugando en casa",
         "home_only": True,
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 10, "min_n": 8}]},
    ],
    "team_goals_over_3": [
        {"id": "v1", "desc": "actual: equipo marca 3+ >=70% (10)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "T2", "desc": "equipo 3+ >=50% (10)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.50, "W": 10, "min_n": 8}]},
        {"id": "T3", "desc": "equipo 3+ >=60% (10)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.60, "W": 10, "min_n": 8}]},
        {"id": "T4", "desc": "equipo 3+ >=50% (15)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.50, "W": 15, "min_n": 8}]},
        {"id": "T5", "desc": "equipo 3+ >=50% y rival encaja 3+ >=30% (10)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.50, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "concede3", "rate": 0.30, "W": 10, "min_n": 8}]},
        {"id": "T6", "desc": "equipo 3+ >=50% en su condición (10)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.50, "W": 10, "min_n": 5, "venue": True}]},
        {"id": "T7", "desc": "equipo 3+ >=50% (10) jugando en casa",
         "home_only": True,
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.50, "W": 10, "min_n": 8}]},
    ],
    "handicap": [
        {"id": "v1", "desc": "actual: gana por 2+ >=70% (10)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "H2", "desc": "gana por 2+ >=50% (10)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.50, "W": 10, "min_n": 8}]},
        {"id": "H3", "desc": "gana por 2+ >=60% (15)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.60, "W": 15, "min_n": 8}]},
        {"id": "H4", "desc": "gana por 2+ >=50% y rival pierde por 2+ >=40% (10)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.50, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "lose2", "rate": 0.40, "W": 10, "min_n": 8}]},
        {"id": "H5", "desc": "gana por 2+ >=50% y rival pierde por 2+ >=30% (10)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.50, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "lose2", "rate": 0.30, "W": 10, "min_n": 8}]},
        {"id": "H6", "desc": "gana por 2+ >=60% en su condición (10)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.60, "W": 10, "min_n": 5, "venue": True}]},
        {"id": "H7", "desc": "gana por 2+ >=60% (10) jugando en casa",
         "home_only": True,
         "conds": [{"who": "team", "stat": "win2", "rate": 0.60, "W": 10, "min_n": 8}]},
    ],
}

_FROZEN = {"menu": MENU, "targets": TARGETS, "split": SPLIT_DATE, "dev_min_n": DEV_MIN_N,
           "test_min_n": TEST_MIN_N, "test_tolerance": TEST_TOLERANCE}
MENU_SHA256 = "3fdd95f95796cd99181a200fc7dcf15191c2f65498dd79e0d5522033452be32b"


def menu_hash() -> str:
    return hashlib.sha256(json.dumps(_FROZEN, sort_keys=True).encode()).hexdigest()


# Estadística por partido de la historia de un equipo: (fecha, gf, ga, en_casa)
STATS = {
    "btts": lambda gf, ga: gf > 0 and ga > 0,
    "score": lambda gf, ga: gf > 0,
    "concede": lambda gf, ga: ga > 0,
    "goals2": lambda gf, ga: gf >= 2,
    "goals3": lambda gf, ga: gf >= 3,
    "win2": lambda gf, ga: gf - ga >= 2,
    "concede2": lambda gf, ga: ga >= 2,
    "concede3": lambda gf, ga: ga >= 3,
    "lose2": lambda gf, ga: ga - gf >= 2,
}

# Resultado del caso: (goles del equipo del caso, goles del rival)
OUTCOMES = {
    "btts": lambda gf, ga: gf > 0 and ga > 0,
    "team_goals_over": lambda gf, ga: gf >= 2,
    "team_goals_over_3": lambda gf, ga: gf >= 3,
    "handicap": lambda gf, ga: gf - ga >= 2,
}
MATCH_LEVEL = {"btts"}  # un caso por partido; el resto, un caso por equipo


def _passes(hist: list[tuple], cond: dict, at_home: bool) -> bool:
    if cond.get("venue"):
        hist = [h for h in hist if h[3] == at_home]
    window = hist[-cond["W"]:]
    if len(window) < cond["min_n"]:
        return False
    fn = STATS[cond["stat"]]
    return sum(1 for _, gf, ga, _h in window if fn(gf, ga)) / len(window) >= cond["rate"]


def _fires(variant: dict, team_hist, opp_hist, team_home: bool, match_level: bool) -> bool:
    if variant.get("home_only") and not team_home:
        return False
    for c in variant["conds"]:
        who = c["who"]
        if match_level:  # team_hist = local, opp_hist = visitante
            home_ok = _passes(team_hist, c, True)
            away_ok = _passes(opp_hist, c, False)
            ok = {"any": home_ok or away_ok, "both": home_ok and away_ok,
                  "home": home_ok, "away": away_ok}[who]
        elif who == "team":
            ok = _passes(team_hist, c, team_home)
        else:  # opp
            ok = _passes(opp_hist, c, not team_home)
        if not ok:
            return False
    return True


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def load_matches(db) -> list[tuple]:
    """Partidos únicos (fecha, local, visitante, goles local, goles visitante)."""
    uniq: dict[tuple, tuple[int, int]] = {}
    for doc in db.read_collection("team_stats"):
        if str(doc.get("_id", "")).startswith("bball_"):
            continue
        for m in doc.get("raw_matches") or []:
            try:
                key = (str(m.get("date") or m.get("match_date"))[:10],
                       str(m["home_team_id"]), str(m["away_team_id"]))
                uniq[key] = (int(m["goals_home"]), int(m["goals_away"]))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted((k[0], k[1], k[2], v[0], v[1]) for k, v in uniq.items() if len(k[0]) == 10)


def run(matches: list[tuple]) -> dict:
    # tallies[market][variant_id][part] = [aciertos, casos]
    tallies = {mk: {v["id"]: {"dev": [0, 0], "test": [0, 0]} for v in vs} for mk, vs in MENU.items()}
    base = {mk: {"dev": [0, 0], "test": [0, 0]} for mk in MENU}
    hist: dict[str, list[tuple]] = defaultdict(list)

    for date, home, away, gh, ga in matches:
        part = "dev" if date < SPLIT_DATE else "test"
        # Solo partidos de días ANTERIORES: hay ~300 equipos con dos partidos el mismo
        # día (el mismo partido guardado con el rival bajo dos ids) y, sin este corte,
        # la segunda copia veía el resultado de la primera.
        hh = [h for h in hist[home] if h[0] < date]
        ah = [h for h in hist[away] if h[0] < date]
        for mk, variants in MENU.items():
            if mk in MATCH_LEVEL:
                cases = [(hh, ah, True, gh, ga)]
            else:
                cases = [(hh, ah, True, gh, ga), (ah, hh, False, ga, gh)]
            for t_hist, o_hist, t_home, gf, gc in cases:
                outcome = OUTCOMES[mk](gf, gc)
                base[mk][part][0] += outcome
                base[mk][part][1] += 1
                for v in variants:
                    if _fires(v, t_hist, o_hist, t_home, mk in MATCH_LEVEL):
                        tallies[mk][v["id"]][part][0] += outcome
                        tallies[mk][v["id"]][part][1] += 1
        hist[home].append((date, gh, ga, True))
        hist[away].append((date, ga, gh, False))
    return {"tallies": tallies, "base": base}


def choose_winner(market: str, tallies: dict) -> str | None:
    eligible = [(vid, t["dev"]) for vid, t in tallies[market].items() if t["dev"][1] >= DEV_MIN_N]
    if not eligible:
        return None
    return max(eligible, key=lambda e: (wilson(*e[1])[0], e[1][1]))[0]


def _fmt(t: list[int]) -> str:
    hits, n = t
    if n == 0:
        return "—"
    lo, hi = wilson(hits, n)
    return f"{hits}/{n} {hits / n * 100:.0f}% [{lo * 100:.0f}–{hi * 100:.0f}]"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["rest", "grpc"], default="rest")
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", "prediction-intelligence"))
    ap.add_argument("--prefix", default=os.environ.get("FIRESTORE_COLLECTION_PREFIX", "prod"))
    ap.add_argument("--account", default=os.environ.get("GCLOUD_ACCOUNT"))
    args = ap.parse_args()

    if menu_hash() != MENU_SHA256:
        print(f"ABORTADO: el menú no coincide con el congelado.\n  actual   {menu_hash()}\n"
              f"  esperado {MENU_SHA256}", file=sys.stderr)
        return 2

    db = get_db(args.transport, args.project, args.prefix, args.account)
    matches = load_matches(db)
    dev_n = sum(1 for m in matches if m[0] < SPLIT_DATE)
    print(f"Menú {MENU_SHA256[:12]} · {len(matches)} partidos ({matches[0][0]} → {matches[-1][0]}) · "
          f"ajuste {dev_n} antes de {SPLIT_DATE} · comprobación {len(matches) - dev_n}")
    res = run(matches)

    for mk, variants in MENU.items():
        target = TARGETS[mk]
        b = res["base"][mk]
        print(f"\n== {mk} · objetivo {target * 100:.0f}% · base comprobación "
              f"{b['test'][0] / max(b['test'][1], 1) * 100:.0f}%")
        winner = choose_winner(mk, res["tallies"])
        for v in variants:
            t = res["tallies"][mk][v["id"]]
            mark = "★" if v["id"] == winner else " "
            print(f" {mark} {v['id']:3} {v['desc']:52} ajuste {_fmt(t['dev']):>22} · "
                  f"comprobación {_fmt(t['test']):>24}")
        if winner is None:
            print(f"   → sin ganadora: ninguna variante llega a {DEV_MIN_N} casos de ajuste")
            continue
        hits, n = res["tallies"][mk][winner]["test"]
        lo, _ = wilson(hits, n)
        ok = n >= TEST_MIN_N and lo >= target - TEST_TOLERANCE
        print(f"   → ganadora {winner}: {'PASA' if ok else 'NO PASA'} "
              f"(comprobación n={n}, límite inferior {lo * 100:.0f}% vs objetivo-{TEST_TOLERANCE * 100:.0f} = "
              f"{(target - TEST_TOLERANCE) * 100:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
