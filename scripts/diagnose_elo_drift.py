"""
Diagnóstico: ¿cuánto se ha desviado el ELO guardado por re-aplicar los mismos resultados?

Contexto: hasta 2026-08-19 `update_all_elos` no llevaba registro de lo ya aplicado y los
llamantes le pasaban las mismas listas en cada ciclo (collect cada 6h con 30 días de
partidos terminados). Cada resultado entraba decenas de veces con K=32.

Qué mide:
  1. Repeticiones en elo_history — prueba directa (mismo partido varias veces en el mismo
     equipo). Es un suelo: el historial está recortado a 10 entradas.
  2. ELO guardado vs recomputado en UNA pasada sobre el mismo universo de partidos.
     OJO: la columna "1 pasada" no es "el ELO verdadero" — es lo que dan los partidos
     actualmente accesibles partiendo todos de 1500, y sale comprimida por tener pocos
     partidos por equipo. Sirve para medir la distorsión, no para sustituir al ELO.
  3. Daño al ORDEN (Spearman) y probabilidades que produce elo_win_probability.

Uso:
    python scripts/diagnose_elo_drift.py                      # local (REST, gRPC bloqueado)
    python scripts/diagnose_elo_drift.py --transport grpc     # CI / Cloud Run
    python scripts/diagnose_elo_drift.py --elo-collection team_elo_backup_20260819
"""
import argparse
import os
import statistics
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fsrest import get_db  # noqa: E402

K_FACTOR, HOME_ADVANTAGE, DEFAULT_ELO = 32, 100, 1500.0

_WATCH = ["real madrid", "barcelona", "bayern", "liverpool", "manchester city", "paris",
          "inter", "atletico", "arsenal", "napoli", "juventus", "dortmund", "chelsea",
          "milan", "sevilla", "roma", "tottenham", "valencia", "athletic", "leipzig"]

_GENERIC = {"fc", "cf", "ac", "as", "sc", "club", "de", "the", "calcio", "ssc", "ss",
            "us", "afc", "rc", "rcd", "cd", "ud"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower().strip())
    s = s.encode("ascii", "ignore").decode()
    s = "".join(c if c.isalnum() else " " for c in s)
    return " ".join(w for w in s.split() if w not in _GENERIC)


def expected(a: float, b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((b - a) / 400.0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--transport", choices=["rest", "grpc"], default="rest")
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", "prediction-intelligence"))
    ap.add_argument("--prefix", default=os.environ.get("FIRESTORE_COLLECTION_PREFIX", "prod"))
    ap.add_argument("--account", default=os.environ.get("GCLOUD_ACCOUNT"))
    ap.add_argument("--elo-collection", default="team_elo",
                    help="colección de ELO a auditar (permite comparar contra una copia)")
    ap.add_argument("--compare-with", default="",
                    help="otra colección de ELO (p.ej. una copia previa al rebuild): "
                         "imprime antes/después por equipo y el cambio de orden")
    args = ap.parse_args()

    db = get_db(args.transport, args.project, args.prefix, args.account)
    stats = db.read_collection("team_stats")
    elos = db.read_collection(args.elo_collection)
    results = db.read_collection("match_results")

    names: dict[str, str] = {}
    name2id: dict[str, str] = {}
    for t in stats:
        tid = t.get("team_id")
        if tid is None:
            continue
        names[str(tid)] = t.get("team_name", f"Team_{tid}")
        name2id.setdefault(norm(t.get("team_name", "")), str(tid))
    for e in elos:
        tid = e.get("team_id")
        if tid is not None:
            names.setdefault(str(tid), e.get("team_name", f"Team_{tid}"))

    # ── Universo de partidos ────────────────────────────────────────────────
    universe: dict[str, dict] = {}
    from_raw = from_res = unresolved = 0
    for t in stats:
        for m in t.get("raw_matches") or []:
            mid = str(m.get("match_id", ""))
            if not mid or m.get("goals_home") is None or m.get("goals_away") is None:
                continue
            if mid not in universe:
                universe[mid] = {"date": m.get("date", ""), "h": str(m.get("home_team_id")),
                                 "a": str(m.get("away_team_id")),
                                 "gh": m.get("goals_home"), "ga": m.get("goals_away")}
                from_raw += 1
    for r in results:
        mid = str(r.get("match_id", ""))
        if not mid or mid in universe:
            continue
        h, a = name2id.get(norm(r.get("home_team", ""))), name2id.get(norm(r.get("away_team", "")))
        if not h or not a:
            unresolved += 1
            continue
        universe[mid] = {"date": r.get("match_date", ""), "h": h, "a": a,
                         "gh": r.get("goals_home"), "ga": r.get("goals_away")}
        from_res += 1

    print(f"universo: {len(universe)} partidos únicos ({from_raw} de team_stats.raw_matches, "
          f"{from_res} de match_results, {unresolved} sin id resoluble)")

    # ── Recomputo de una sola pasada ────────────────────────────────────────
    rec: dict[str, float] = {}
    for m in sorted(universe.values(), key=lambda x: str(x["date"])):
        h, a = m["h"], m["a"]
        if not h or not a or m["gh"] is None or m["ga"] is None:
            continue
        eh, ea = rec.get(h, DEFAULT_ELO), rec.get(a, DEFAULT_ELO)
        score = 1.0 if m["gh"] > m["ga"] else 0.0 if m["ga"] > m["gh"] else 0.5
        p = expected(eh + HOME_ADVANTAGE, ea)
        rec[h] = (eh + HOME_ADVANTAGE) + K_FACTOR * (score - p) - HOME_ADVANTAGE
        rec[a] = ea + K_FACTOR * ((1.0 - score) - (1.0 - p))

    # ── 1. Repeticiones en elo_history ──────────────────────────────────────
    dup_teams = tot = uniq = solo1 = 0
    for e in elos:
        hist = e.get("elo_history") or []
        keys = [(h.get("date"), h.get("opponent_id"), h.get("result")) for h in hist]
        if not keys:
            continue
        tot += len(keys)
        uniq += len(set(keys))
        if len(set(keys)) < len(keys):
            dup_teams += 1
        if len({(k[0], k[1]) for k in keys}) == 1:
            solo1 += 1
    print(f"elo_history: {dup_teams}/{len(elos)} equipos con entradas repetidas | "
          f"{tot} entradas → {uniq} partidos distintos ({tot / max(uniq, 1):.1f}x) | "
          f"{solo1} equipos cuyo historial entero es un solo partido repetido")

    cur = {str(e.get("team_id")): float(e.get("elo", DEFAULT_ELO))
           for e in elos if e.get("team_id") is not None}
    comunes = sorted(t for t in cur if t in rec)
    if not comunes:
        print("sin equipos comparables — nada que medir")
        return

    # ── 2. Tabla de desvíos ─────────────────────────────────────────────────
    print(f"\nequipos comparables: {len(comunes)} de {len(cur)} con ELO guardado")
    print(f"\n{'equipo':<26}{'ELO guardado':>13}{'1 pasada':>11}{'desvío':>9}")
    print("-" * 60)
    filas = [(cur[t] - rec[t], names.get(t, t), cur[t], rec[t]) for t in comunes
             if any(w in norm(names.get(t, "")) for w in _WATCH)]
    for d, n, c, r in sorted(filas, key=lambda x: -abs(x[0]))[:20]:
        print(f"{n[:25]:<26}{c:>13.0f}{r:>11.0f}{d:>+9.0f}")

    difs = sorted(abs(cur[t] - rec[t]) for t in comunes)
    n = len(difs)
    print(f"\ndesvío absoluto: mediana {difs[n // 2]:.0f} | p90 {difs[int(n * 0.9)]:.0f} | "
          f"máx {difs[-1]:.0f}")
    print(f"desviación típica: guardado {statistics.pstdev([cur[t] for t in comunes]):.0f} "
          f"vs 1 pasada {statistics.pstdev([rec[t] for t in comunes]):.0f}")
    print(f"amplitud: guardado {max(cur[t] for t in comunes) - min(cur[t] for t in comunes):.0f} "
          f"vs 1 pasada {max(rec[t] for t in comunes) - min(rec[t] for t in comunes):.0f}")
    print(f"equipos con |ELO-1500|>300: guardado "
          f"{sum(1 for t in comunes if abs(cur[t] - 1500) > 300)} vs 1 pasada "
          f"{sum(1 for t in comunes if abs(rec[t] - 1500) > 300)}")

    # ── 3. Daño al orden ────────────────────────────────────────────────────
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        out = [0] * len(vals)
        for pos, i in enumerate(order):
            out[i] = pos
        return out

    rc, rr = rank([cur[t] for t in comunes]), rank([rec[t] for t in comunes])
    d2 = sum((rc[i] - rr[i]) ** 2 for i in range(n))
    rho = 1 - 6 * d2 / (n * (n ** 2 - 1)) if n > 2 else float("nan")
    print(f"\nSpearman guardado vs 1 pasada: rho={rho:.3f} "
          f"(1.0 = mismo orden de fuerza; por debajo de ~0.9 el ranking está roto)")

    if args.compare_with:
        compare(db, args.compare_with, args.elo_collection, elos, names)


def compare(db, otra_col: str, esta_col: str, elos: list[dict], names: dict) -> None:
    """Antes/después por equipo entre dos colecciones de ELO, con puesto en el ranking."""
    otros = db.read_collection(otra_col)
    antes = {str(e.get("team_id")): float(e.get("elo", DEFAULT_ELO))
             for e in otros if e.get("team_id") is not None}
    ahora = {str(e.get("team_id")): float(e.get("elo", DEFAULT_ELO))
             for e in elos if e.get("team_id") is not None}
    for e in otros:
        if e.get("team_id") is not None:
            names.setdefault(str(e["team_id"]), e.get("team_name", ""))

    def puestos(d):
        return {t: i + 1 for i, (t, _) in
                enumerate(sorted(d.items(), key=lambda kv: -kv[1]))}

    p_antes, p_ahora = puestos(antes), puestos(ahora)
    comunes = [t for t in ahora if t in antes]

    print(f"\n\n=== {otra_col} (antes) vs {esta_col} (ahora) — {len(comunes)} equipos en ambas")
    print(f"{'equipo':<26}{'antes':>8}{'ahora':>8}{'cambio':>9}"
          f"{'puesto antes':>14}{'puesto ahora':>14}")
    print("-" * 79)
    filas = [t for t in comunes if any(w in norm(names.get(t, "")) for w in _WATCH)]
    for t in sorted(filas, key=lambda t: -ahora[t]):
        print(f"{names.get(t, t)[:25]:<26}{antes[t]:>8.0f}{ahora[t]:>8.0f}"
              f"{ahora[t] - antes[t]:>+9.0f}"
              f"{p_antes[t]:>10}/{len(antes):<3}{p_ahora[t]:>10}/{len(ahora):<3}")


def expected_prob(home_elo: float, away_elo: float) -> float:
    return expected(home_elo + HOME_ADVANTAGE, away_elo)


if __name__ == "__main__":
    main()
