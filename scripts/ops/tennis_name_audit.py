"""SOLO LECTURA. Por qué no emparejan los nombres de tenis del calendario con tennis-data.

Lee upcoming_matches (sport=tennis) y tennis_tournaments (ya cargado) y escribe un JSON con:
  - calendario del circuito principal SIN emparejar: nombre tal cual, liga, torneo, fuente,
    claves probadas y los 3 nombres de tennis-data más parecidos;
  - lado tennis-data: jugadores que jugaron en los últimos 60 días y que ningún nombre del
    calendario empareja (para ver el formato del otro lado).
El JSON va CIFRADO a un artefacto de 1 día (el repo es público): en el log, solo recuentos.

Uso: python scripts/ops/tennis_name_audit.py salida.json
"""
import difflib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from collectors import tennis_data as td  # noqa: E402


def main(out_path: str) -> int:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col

    # Lado tennis-data: clave → nombres tal cual y última fecha
    td_names: dict[str, set[str]] = {}
    last_seen: dict[str, str] = {}
    n_docs = 0
    for snap in col("tennis_tournaments").stream():
        n_docs += 1
        d = snap.to_dict() or {}
        tour = str(d.get("tour") or "").upper()
        for m in d.get("matches") or []:
            for raw in (m.get("winner"), m.get("loser")):
                k = td.td_player_key(raw or "")
                if not k:
                    continue
                td_names.setdefault(k, set()).add(f"{raw} [{tour}]")
                last_seen[k] = max(last_seen.get(k, ""), str(m.get("date") or ""))
    index = set(td_names)
    norm_to_raw = {td._norm(r.split(" [")[0]): r for rs in td_names.values() for r in rs}

    # Lado calendario
    cal = list(col("upcoming_matches").where(filter=FieldFilter("sport", "==", "tennis")).stream())
    unmatched, matched_keys = [], set()
    counts = Counter()
    for snap in cal:
        d = snap.to_dict() or {}
        main_tour = td.is_main_tour(d)
        for side in ("home_team", "away_team"):
            n = str(d.get(side) or "")
            if not n or "/" in n or " & " in n:
                continue
            hit = td.match_player(n, index)
            counts[("principal" if main_tour else "otros", "ok" if hit else "no")] += 1
            if hit:
                matched_keys.add(hit)
                continue
            if not main_tour:
                continue
            close = difflib.get_close_matches(td._norm(n), list(norm_to_raw), n=3, cutoff=0.5)
            unmatched.append({
                "name": n, "league": d.get("league"), "tournament": d.get("tournament"),
                "source": d.get("source"), "date": str(d.get("date") or "")[:10],
                "keys_tried": td.candidate_keys(n),
                "closest_td": [norm_to_raw[c] for c in close],
            })

    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    td_recent_unmatched = sorted(
        r for k, rs in td_names.items() if last_seen.get(k, "") >= cutoff and k not in matched_keys
        for r in rs)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "calendar_main_unmatched": sorted(unmatched, key=lambda x: x["name"]),
                   "td_recent_unmatched": td_recent_unmatched,
                   "leagues_main": Counter(str(x["league"]) for x in unmatched).most_common(),
                   "tournaments_main": Counter(str(x["tournament"]) for x in unmatched).most_common()},
                  f, ensure_ascii=False, indent=1)

    # Log público: solo recuentos
    print(f"tennis_tournaments: {n_docs} docs, {len(index)} claves de jugador")
    print(f"upcoming_matches tenis: {len(cal)} partidos")
    for (grp, res), v in sorted(counts.items()):
        print(f"  {grp} {res}: {v}")
    print(f"sin emparejar (principal) en el JSON: {len(unmatched)}; "
          f"tennis-data recientes sin emparejar: {len(td_recent_unmatched)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
