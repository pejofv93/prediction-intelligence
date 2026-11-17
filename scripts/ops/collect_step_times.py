"""SOLO LECTURA. Tiempo de cada paso de sports-collect a partir de los logs de Cloud Run
(sports-agent), leídos por el workflow con `gcloud logging read` en LOGS_FILE (JSON).

Cada pasada empieza en "collect: iniciando pipeline". El tiempo de un paso es lo que va desde
su primer mensaje hasta el primer mensaje del paso siguiente. Solo nombre del paso, minutos y
recuentos (los números de los mensajes "N partidos ..."): nada de equipos, URLs ni errores.
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

STEP = re.compile(r"(collect(?:\.[a-z0-9_]+)?): ")
COUNT = re.compile(r": (\d+) (partidos|resultados|ligas)")


def main() -> int:
    raw = json.load(open(os.environ.get("LOGS_FILE", "logs.json"), encoding="utf-8"))
    ev = []
    for e in raw:
        msg = e.get("textPayload") or (e.get("jsonPayload") or {}).get("message") or ""
        m = STEP.search(msg)
        if not m:
            continue
        rest = msg[m.end():]
        c = COUNT.match(": " + rest)
        tag = "inicio" if rest.startswith("iniciando") else "fin" if rest.startswith("completado") else ""
        ev.append((datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")), m.group(1), tag,
                   f"{c.group(1)} {c.group(2)}" if c else ""))
    ev.sort()
    runs, cur = [], None
    for t, step, tag, cnt in ev:
        if step == "collect" and tag == "inicio":
            cur = {"start": t, "events": []}
            runs.append(cur)
        elif cur is not None:
            cur["events"].append((t, step, tag, cnt))
    print(f"Pasadas encontradas: {len(runs)}")
    tot = defaultdict(list)
    for r in runs:
        marks, seen = [], set()
        for t, step, tag, cnt in r["events"]:
            if step not in seen:
                seen.add(step)
                marks.append([t, step, []])
            if cnt:
                marks[[m[1] for m in marks].index(step)][2].append(cnt)
        end = next((t for t, s, tag, _ in r["events"] if s == "collect" and tag == "fin"), None)
        parts = []
        for i, (t, step, cnts) in enumerate(marks):
            if step == "collect":
                continue
            nxt = next((m[0] for m in marks[i + 1:]), end)
            mins = ((nxt - t).total_seconds() / 60) if nxt else None
            if step != "collect.cleanup":
                parts.append(f"{step.split('.', 1)[-1]} {mins:.1f}" if mins is not None else
                             f"{step.split('.', 1)[-1]} ¿sin fin?")
                if mins is not None:
                    tot[step].append(mins)
            if cnts:
                parts[-1] += f" ({', '.join(cnts[:2])})"
        total = f"{(end - r['start']).total_seconds() / 60:.1f} min" if end else "SIN TERMINAR"
        print(f"\n{r['start']:%a %d-%m %H:%M}  total {total}")
        print("   " + " · ".join(parts))
    print("\nMediana por paso (min):")
    for step, v in sorted(tot.items(), key=lambda kv: -sorted(kv[1])[len(kv[1]) // 2]):
        v = sorted(v)
        print(f"   {step:32} mediana {v[len(v) // 2]:5.1f} · máx {v[-1]:5.1f} · n={len(v)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
