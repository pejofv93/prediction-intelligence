"""SOLO LECTURA. ¿Por qué solo empareja ~13-19 % de los nombres del calendario de tenis?

El dry-run de tennis-data (24-nov-2026) da ATP 9/196 en «circuito principal» y WTA 36/46,
también con el índice de 2019-2026 entero: no es por los años cargados. Hipótesis: el
calendario (upcoming_matches sport=tennis) mete como «ATP» torneos que no son del circuito
principal — el respaldo de odds-api.io etiqueta «ATP» todo lo que no reconoce y guarda la
competición tal cual en `tournament`; a final de noviembre la temporada ATP ha terminado.

Por cada torneo del calendario que is_main_tour da por principal: fuente, liga, nombres y
cuántos emparejan con las claves de tennis_tournaments (todas las temporadas cargadas).
Los torneos son eventos públicos; ningún nombre de jugador sale al log. No escribe nada.
"""
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from collectors.tennis_data import is_main_tour, match_player, td_player_key  # noqa: E402
from shared.firestore_client import col, iter_all  # noqa: E402


def main() -> int:
    index: set[str] = set()
    variants: dict[str, set] = defaultdict(set)
    last_year: dict[str, int] = {}
    for snap in iter_all("tennis_tournaments"):
        d = snap.to_dict() or {}
        tour = str(d.get("tour") or "")
        for m in d.get("matches") or []:
            for raw in (m.get("winner"), m.get("loser")):
                k = td_player_key(raw)
                if k:
                    index.add(k)
                    variants[k].add(f"{raw}|{tour}")
                    y = int(str(m.get("date") or "0")[:4] or 0)
                    last_year[k] = max(last_year.get(k, 0), y)
    players = {k: len(v) for k, v in variants.items()}
    print(f"claves de tennis_tournaments: {len(index)}")

    rows: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    not_main: Counter = Counter()
    hit_years: Counter = Counter()
    snaps = list(col("upcoming_matches").where(filter=FieldFilter("sport", "==", "tennis")).stream())
    for snap in snaps:
        d = snap.to_dict() or {}
        names = [str(d.get(s) or "") for s in ("home_team", "away_team")]
        names = [n for n in names if n and "/" not in n and " & " not in n]
        if not is_main_tour(d):
            not_main[str(d.get("league") or "?")] += len(names)
            continue
        key = (str(d.get("league") or "?"), str(d.get("source") or "—"),
               str(d.get("tournament") or "—")[:60])
        for n in names:
            rows[key][1] += 1
            k = match_player(n, index, players)
            if k:
                rows[key][0] += 1
                hit_years[last_year.get(k, 0)] += 1
    print(f"partidos de tenis en el calendario: {len(snaps)}")
    print(f"fuera del circuito principal (por liga): {dict(not_main)}")
    print("\nPrincipal según is_main_tour — liga · fuente · torneo: emparejan/nombres")
    for key, (ok, n) in sorted(rows.items(), key=lambda kv: -kv[1][1]):
        print(f"  {key[0]:<4} {key[1]:<20} {key[2]:<60} {ok:3d}/{n:<3d}")
    print(f"\nÚltimo año con partido en tennis-data de los que emparejan: {dict(sorted(hit_years.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
