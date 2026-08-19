"""
Sonda de la fuente UEFA (allsportsapi2): descubre la temporada por /seasons y comprueba
que devuelve fixtures y resultados. No escribe nada — solo lee e imprime.

Coste: 3 requests por competición (seasons + next + last) de los 100/día de la clave.

Uso:
    python scripts/probe_uefa.py                 # las tres competiciones
    python scripts/probe_uefa.py --leagues CL    # solo una

La clave sale de FOOTBALL_RAPID_API_KEY; si no está en el entorno se lee de las variables
del servicio de Cloud Run con gcloud (cómodo desde el puesto de trabajo, donde no hay .env).
Local: usar Python 3.11 con SSL_CERT_FILE apuntando al bundle de Norton, o el TLS falla.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

_HOST = "allsportsapi2.p.rapidapi.com"
_TOURNAMENTS = {"CL": 7, "EL": 679, "ECL": 17015}


def _season_label(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    start = now.year if now.month >= 8 else now.year - 1
    return f"{start % 100:02d}/{(start + 1) % 100:02d}"


def _api_key() -> str:
    key = os.environ.get("FOOTBALL_RAPID_API_KEY", "")
    if key:
        return key
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        sys.exit("FOOTBALL_RAPID_API_KEY no está en el entorno y gcloud no está disponible")
    out = subprocess.run(
        [exe, "run", "services", "describe", "sports-agent",
         "--project", os.environ.get("GOOGLE_CLOUD_PROJECT", "prediction-intelligence"),
         "--region", os.environ.get("CLOUD_RUN_REGION", "europe-west1"),
         "--format", "json(spec.template.spec.containers[0].env)"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"no se pudo leer la clave de Cloud Run: {out.stderr[:200]}")
    env = json.loads(out.stdout)["spec"]["template"]["spec"]["containers"][0]["env"]
    for e in env:
        if e["name"] == "FOOTBALL_RAPID_API_KEY":
            return e["value"]
    sys.exit("FOOTBALL_RAPID_API_KEY no está entre las variables del servicio")


def _get(path: str, key: str) -> tuple[dict, dict]:
    req = urllib.request.Request(f"https://{_HOST}{path}")
    req.add_header("x-rapidapi-key", key)
    req.add_header("x-rapidapi-host", _HOST)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), dict(r.headers)


def probe(league: str, key: str) -> None:
    tid = _TOURNAMENTS[league]
    label = _season_label()

    data, headers = _get(f"/api/tournament/{tid}/seasons", key)
    seasons = data.get("seasons", [])
    match = next((s for s in seasons if str(s.get("year")) == label), None)
    chosen = match or (seasons[0] if seasons else None)
    if not chosen:
        print(f"{league}: /seasons vacío")
        return

    print(f"\n=== {league} (tid={tid}) — {len(seasons)} temporadas, campaña buscada {label}")
    for s in seasons[:3]:
        marca = " <-- elegida" if s is chosen else ""
        print(f"    {s.get('year'):>6}  id={s.get('id'):<8} {s.get('name','')}{marca}")
    if not match:
        print(f"    AVISO: ninguna temporada con year={label}; se usa la primera de la lista")

    sid = chosen["id"]
    for direction in ("next", "last"):
        d, headers = _get(f"/api/tournament/{tid}/season/{sid}/matches/{direction}/0", key)
        events = d.get("events", [])
        rondas: dict[str, int] = {}
        for e in events:
            r = (e.get("roundInfo") or {}).get("name", "?")
            rondas[r] = rondas.get(r, 0) + 1
        print(f"    /{direction}/0: {len(events):>2} eventos  hasNextPage={d.get('hasNextPage')}  {rondas}")
        for e in events[:2]:
            ts = e.get("startTimestamp")
            cuando = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                      if ts else "?")
            ht, at = e.get("homeTeam", {}), e.get("awayTeam", {})
            gh = (e.get("homeScore") or {}).get("current")
            ga = (e.get("awayScore") or {}).get("current")
            marcador = f"{gh}-{ga}" if gh is not None else "vs"
            print(f"        {cuando} | {ht.get('name')}({ht.get('id')}) {marcador} "
                  f"{at.get('name')}({at.get('id')})")
    rem = headers.get("X-RateLimit-Requests-Remaining")
    if rem:
        print(f"    cuota restante hoy: {rem}/{headers.get('X-RateLimit-Requests-Limit', '?')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--leagues", default="CL,EL,ECL", help="competiciones separadas por coma")
    args = ap.parse_args()

    key = _api_key()
    for lg in [x.strip().upper() for x in args.leagues.split(",") if x.strip()]:
        if lg not in _TOURNAMENTS:
            print(f"{lg}: desconocida, se omite")
            continue
        try:
            probe(lg, key)
        except Exception as e:
            print(f"{lg}: ERROR {type(e).__name__} {e}")


if __name__ == "__main__":
    main()
