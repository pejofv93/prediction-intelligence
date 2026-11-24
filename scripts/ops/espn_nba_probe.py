"""SOLO LECTURA. Sonda de ESPN para la NBA en Tendencias (antes de cargar nba_games).

Pide el scoreboard de los últimos días (hasta 10, parando al tener 3 partidos terminados de
temporada regular o playoffs) y el `summary` de esos 3 partidos. Comprueba:
  - código HTTP y tiempo de cada petición;
  - nombres de los campos de estadística de equipo (boxscore.teams[].statistics[].name) y si
    vienen rellenos, por equipo (índice 0/1, sin nombre);
  - si el parser del colector (collectors/nba_espn.build_game_doc) saca el doc o por qué no;
  - cuántos cuartos trae linescores (para la prórroga).
Peticiones: ≤10 scoreboards + 3 summaries, a ESPN (sin clave). No escribe nada.
El log es público: nada de equipos, marcadores ni cifras.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from collectors import nba_espn as ne  # noqa: E402

MAX_DAYS_BACK = 10
WANT = 3


def main() -> int:
    today = datetime.now(timezone.utc).date()
    picked: list[tuple[str, str]] = []
    for back in range(1, MAX_DAYS_BACK + 1):
        day = (today - timedelta(days=back)).strftime("%Y%m%d")
        sb, code, secs = ne.http_get_json(ne.SCOREBOARD_URL.format(day=day))
        evs = ne.events_from_scoreboard(sb) if sb else []
        ok = [e for e in evs if ne.wanted(e) and e["completed"]]
        print(f"scoreboard día -{back}: HTTP {code} · {secs * 1000:.0f} ms · partidos {len(evs)} · "
              f"terminados de regular/playoffs {len(ok)}")
        for e in ok:
            if len(picked) < WANT:
                picked.append((e["id"], day))
        if len(picked) >= WANT:
            break
    if not picked:
        print("Sin partidos terminados en los últimos días: nada que probar.")
        return 1
    all_ok = True
    for n, (event_id, day) in enumerate(picked, 1):
        sm, code, secs = ne.http_get_json(ne.SUMMARY_URL.format(event=event_id))
        print(f"\nsummary partido {n}: HTTP {code} · {secs * 1000:.0f} ms")
        if not sm:
            all_ok = False
            continue
        print(f"  claves de primer nivel: {sorted(sm)}")
        comps = ((sm.get("header") or {}).get("competitions") or [{}])[0].get("competitors") or []
        print(f"  competidores {len(comps)} · homeAway {sorted(c.get('homeAway') or '?' for c in comps)} · "
              f"cuartos en linescores {[len(c.get('linescores') or []) for c in comps]} · "
              f"con marcador {sum(1 for c in comps if c.get('score') not in (None, ''))}")
        for t in ne.field_report(sm):
            filled = sum(1 for _, f in t["fields"] if f)
            print(f"  equipo {t['team_index']}: {len(t['fields'])} campos, {filled} rellenos")
            print("    " + ", ".join(f"{name}{'' if f else ' (vacío)'}" for name, f in t["fields"]))
        doc, why = ne.build_game_doc(sm, event_id, day, ne.season_code(today))
        print(f"  parser del colector: {'doc OK' if doc else 'sin doc: ' + str(why)}")
        all_ok = all_ok and doc is not None
    print("\nSin escribir nada (solo lectura).")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
