"""SOLO LECTURA. Repartidor en modo de prueba frente al cron de GitHub, últimas HOURS horas.

Por tarea de shared/dispatch_schedule.TASKS:
  - franjas que tocaban (las que ya han pasado su retraso máximo, o están reclamadas);
  - reclamadas por el repartidor en pruebas (dispatch_claims_dry) y a qué hora, con el retraso
    sobre la franja en minutos;
  - saltadas (tocaban y nadie las reclamó dentro del margen);
  - fuera de horario (reclamaciones que no corresponden a ninguna franja: no debería haber);
  - a qué hora arrancó de verdad el cron de GitHub (event=schedule), desde gh_runs.json.
Solo nombres de tarea y horas UTC. No escribe nada.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from shared import dispatch_schedule as ds  # noqa: E402
from shared.firestore_client import col  # noqa: E402


def _dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def slots_between(task, start, end):
    t = start.replace(second=0, microsecond=0)
    out = []
    while t <= end:
        if ds.matches(task.cron, t):
            out.append(t)
        t += timedelta(minutes=1)
    return out


def main() -> int:
    hours = int(os.environ.get("HOURS") or 24)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    gh = json.load(open(os.environ.get("GH_RUNS", "gh_runs.json"), encoding="utf-8"))

    claims: dict[str, dict[datetime, datetime]] = {}
    for d in col("dispatch_claims_dry").where(filter=FieldFilter("claimed_at", ">=", start)).stream():
        x = d.to_dict() or {}
        if "task" not in x:
            continue
        claims.setdefault(x["task"], {})[_dt(x["slot"])] = _dt(x["claimed_at"])

    print(f"Ventana: {start:%d-%m %H:%M} → {now:%d-%m %H:%M} UTC ({hours} h)")
    tot = {"tocaban": 0, "reclamadas": 0, "saltadas": 0, "fuera": 0, "gh": 0}
    for name, task in ds.TASKS.items():
        exp = [s for s in slots_between(task, start, now)
               if s <= now - timedelta(minutes=task.max_late_min) or s in claims.get(name, {})]
        got = claims.get(name, {})
        late = {s: round((got[s] - s).total_seconds() / 60) for s in exp if s in got}
        skipped = [s for s in exp if s not in got]
        outside = [s for s in got if s not in set(slots_between(task, start - timedelta(hours=6), now))]
        runs = sorted(_dt(r) for r in gh.get(task.workflow, []) if _dt(r) >= start)
        for k, v in (("tocaban", len(exp)), ("reclamadas", len(late)), ("saltadas", len(skipped)),
                     ("fuera", len(outside)), ("gh", len(runs))):
            tot[k] += v
        lat = sorted(late.values())
        print(f"\n{name}  ({task.cron}, margen {task.max_late_min} min)")
        print(f"  tocaban {len(exp)} · repartidor {len(late)}"
              + (f" (retraso mediana {lat[len(lat) // 2]} min, máx {lat[-1]})" if lat else "")
              + f" · saltadas {len(skipped)} · fuera de horario {len(outside)} · cron GitHub {len(runs)}")
        print("  repartidor: " + (", ".join(f"{s:%H:%M}→{got[s]:%H:%M}" for s in exp if s in got) or "—"))
        if skipped:
            print("  saltadas:   " + ", ".join(f"{s:%H:%M}" for s in skipped))
        print("  cron GitHub: " + (", ".join(f"{r:%H:%M}" for r in runs) or "—"))
    print(f"\nTOTAL: tocaban {tot['tocaban']} · repartidor {tot['reclamadas']} · saltadas {tot['saltadas']}"
          f" · fuera de horario {tot['fuera']} · arranques del cron de GitHub {tot['gh']}")
    print("Duplicados: imposibles por diseño (un documento por tarea y franja); "
          "'fuera de horario' detectaría reclamaciones anómalas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
