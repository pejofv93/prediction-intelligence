"""SOLO LECTURA. ¿Fallaban las fuentes de props de jugadores (understat y goleadores de
football-data) también cuando sports-analyze corría en Cloud Run, o empezaron en GitHub Actions?

Lee LOGS_FILE (gcloud logging read --format json) con las líneas de analyzers.player_props de
sports-agent y cuenta por día UTC y origen (actions = revisión «github-actions»; cloud-run = el
resto): cargas correctas (y cuántas con 0 jugadores), fallos, caché caducada, «understat vacío» y
los códigos HTTP de los fallos. Ni ligas, ni partidos, ni jugadores: solo recuentos.
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

KINDS = (
    ("understat ok", re.compile(r"understat\(.*\): (\d+) atacantes cargados")),
    ("understat falla", re.compile(r"understat\(.*\): fetch falló")),
    ("understat caché caducada", re.compile(r"understat\(.*\): usando caché expirada")),
    ("understat vacío → fdorg", re.compile(r"understat vacío")),
    ("fdorg ok", re.compile(r"fdorg_scorers\(.*\): (\d+) jugadores cargados")),
    ("fdorg falla", re.compile(r"fdorg_scorers\(.*\): fetch falló")),
)
HTTP = re.compile(r"HTTP(?: Error)? (\d{3})")   # «HTTP Error 429» (antes) y «HTTP 429» (27-nov)


def summarize(entries: list[dict]) -> list[str]:
    by: dict = defaultdict(Counter)
    for e in entries:
        text = e.get("textPayload") or json.dumps(e.get("jsonPayload") or {}, ensure_ascii=False)
        rev = ((e.get("resource") or {}).get("labels") or {}).get("revision_name") or ""
        key = ((e.get("timestamp") or "")[:10], "actions" if rev == "github-actions" else "cloud-run")
        for name, rx in KINDS:
            m = rx.search(text)
            if m:
                by[key][name] += 1
                if m.groups() and int(m.group(1)) == 0:
                    by[key][name + " con 0"] += 1
                if "falla" in name:
                    for code in HTTP.findall(text)[:1]:
                        by[key][f"{name.split()[0]} HTTP {code}"] += 1
                break
    out = [f"{len(entries)} entradas"]
    for (day, origin) in sorted(by):
        out.append(f"{day} {origin:9} " + ", ".join(f"{k}={v}" for k, v in sorted(by[(day, origin)].items())))
    return out


def main() -> int:
    with open(os.environ["LOGS_FILE"], encoding="utf-8") as f:
        entries = json.load(f)
    for line in summarize(entries):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
