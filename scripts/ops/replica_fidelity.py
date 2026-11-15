"""¿Cuánto se parece la réplica de v1 (v1_replica.py) al modelo real? SOLO LECTURA.

Lee las predicciones de fútbol 1X2 de v1 desde SINCE (colección predictions), las cruza con los
CSV de football-data.co.uk (mismo partido: liga, fecha ±1 día y equipos traducidos con el
emparejamiento de las fichas) y calcula la réplica justo antes de cada partido. Compara, para el
lado que eligió v1, la probabilidad y la confianza. No escribe nada. Imprime solo agregados
(recuentos y diferencias medias por liga): nunca equipos, partidos ni ids.
Lo lanza el workflow manual sports-replica-fidelity.
"""
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, os.path.dirname(__file__))

import v1_replica as vr  # noqa: E402

SINCE = datetime(2026, 10, 22, tzinfo=timezone.utc)
SEASONS = ["1718", "1819", "1920", "2021", "2122", "2223", "2324", "2425", "2526", "2627"]


def _dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def read_predictions() -> list[dict]:
    from shared.firestore_client import iter_all
    out, seen = [], set()
    for d in iter_all("predictions", where=("sport", "==", "football")):
        p = d.to_dict() or {}
        created = _dt(p.get("created_at"))
        if created is None or created < SINCE or (p.get("market_type") or "h2h") != "h2h":
            continue
        key = (p.get("match_id"), p.get("team_to_back"))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def download_rows() -> tuple[list[dict], int]:
    import httpx
    rows, failed = [], 0
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        for div in vr.V1_DIVS.values():
            for s in SEASONS:
                try:
                    r = c.get(vr.FDCO_URL.format(season=s, div=div))
                    if r.status_code != 200:
                        failed += 1
                        continue
                    rows += vr.parse_csv(r.content.decode("latin-1"), div, s)
                except Exception:
                    failed += 1
    return rows, failed


def stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    s = sorted(abs(v) for v in vals)
    return {"n": len(vals), "mean": sum(vals) / len(vals), "mae": sum(s) / len(s),
            "median_abs": s[len(s) // 2], "p90_abs": s[int(0.9 * (len(s) - 1))],
            "within_3pts": sum(1 for v in s if v <= 0.03) / len(s)}


def corr(a: list[float], b: list[float]):
    if len(a) < 3:
        return None
    import numpy as np
    return float(np.corrcoef(a, b)[0, 1])


def main() -> int:
    from collectors.match_fixtures import map_team

    preds = read_predictions()
    print(f"predicciones de v1 (fútbol 1X2, desde {SINCE:%d/%m/%Y}, una por partido y lado): {len(preds)}")
    rows, failed = download_rows()
    print(f"partidos de football-data.co.uk cargados: {len(rows)} (CSV sin descargar: {failed})")

    teams_by_div = defaultdict(set)
    for m in rows:
        if m["season"] in ("2526", "2627"):
            teams_by_div[m["div"]] |= {m["home"], m["away"]}
    by_div_date = defaultdict(list)
    for m in rows:
        by_div_date[(m["div"], m["date"])].append(m)

    why = Counter()
    wanted = {}
    for p in preds:
        div = vr.V1_DIVS.get(p.get("league"))
        if not div:
            why["liga fuera de las 8 de football-data.co.uk"] += 1
            continue
        h, okh = map_team(str(p.get("home_team") or ""), teams_by_div[div])
        a, oka = map_team(str(p.get("away_team") or ""), teams_by_div[div])
        if not (okh and oka):
            why["equipo sin emparejar"] += 1
            continue
        try:
            d0 = date.fromisoformat(str(p.get("match_date"))[:10])
        except ValueError:
            why["sin fecha"] += 1
            continue
        hit = None
        for off in (0, -1, 1):
            dd = date.fromordinal(d0.toordinal() + off).isoformat()
            hit = next((m for m in by_div_date.get((div, dd), []) if m["home"] == h and m["away"] == a), None)
            if hit:
                break
        if not hit:
            why["partido aún no está en el CSV"] += 1
            continue
        side = "home" if str(p.get("team_to_back")) == str(p.get("home_team")) else "away"
        wanted[(hit["div"], hit["date"], hit["home"], hit["away"])] = {**p, "_side": side, "_div": div}

    states = vr.Replica(rows).states_before(wanted)
    dp, dc, pv, pr, same_fav, ev_ok = [], [], [], [], 0, 0
    by_div = defaultdict(list)
    for key, p in wanted.items():
        st = states.get(key)
        rp = vr.replica_probs(st) if st else None
        if rp is None:
            why["réplica sin Poisson (pocos partidos previos)"] += 1
            continue
        side = p["_side"]
        r_p, v_p = rp["p"][side], float(p.get("calculated_prob") or 0)
        dp.append(r_p - v_p)
        pv.append(v_p)
        pr.append(r_p)
        by_div[p["_div"]].append(r_p - v_p)
        if p.get("confidence") is not None:
            dc.append(rp["conf"][side] - float(p["confidence"]))
        other = "away" if side == "home" else "home"
        same_fav += rp["p"][side] > rp["p"][other]
        odds = float(p.get("odds") or 0)
        ev_ok += odds > 1 and r_p * odds - 1 > 0.08
    n = len(dp)
    print(f"comparadas: {n}")
    print("no comparadas:", dict(why))
    if not n:
        return 0
    s = stats(dp)
    print(f"\nProbabilidad del lado elegido (réplica − v1): media {s['mean']:+.3f} · error medio {s['mae']:.3f} · "
          f"mediana {s['median_abs']:.3f} · p90 {s['p90_abs']:.3f} · a ≤ 3 puntos {s['within_3pts']:.0%} · "
          f"correlación {corr(pr, pv):.2f}")
    c = stats(dc)
    if c["n"]:
        print(f"Confianza (réplica − v1): media {c['mean']:+.3f} · error medio {c['mae']:.3f} · "
              f"a ≤ 3 puntos {c['within_3pts']:.0%} (n={c['n']})")
    print(f"La réplica también ve favorito el lado elegido por v1: {same_fav / n:.0%}")
    print(f"La réplica también da EV > 8 % a la cuota de v1: {ev_ok / n:.0%}")
    print("\nPor liga (n · error medio de la probabilidad):")
    names = {v: k for k, v in vr.V1_DIVS.items()}
    for div, v in sorted(by_div.items()):
        st = stats(v)
        print(f"  {names[div]:<4} n={st['n']:>3} · error medio {st['mae']:.3f} · media {st['mean']:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
