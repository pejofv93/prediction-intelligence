"""SOLO LECTURA (23-nov-2026). ¿De dónde sale el tiempo facturable de sports-agent?

Compara, por día, el tiempo de instancia facturable (Cloud Monitoring) con la suma de la
duración de las peticiones (log de peticiones de Cloud Run) y reparte esa suma por ruta.
Lo que sobre entre una cosa y otra es tiempo de instancia sin petición en curso (arranques,
apagados, tareas en segundo plano si la CPU no se limita fuera de las peticiones).

Entrada (ficheros que deja el workflow): BILL_FILE (timeSeries de Monitoring, por día),
REQ_FILE (gcloud logging read … run.googleapis.com/requests, json), SVC_FILE (gcloud run
services describe, json). Log público: solo rutas propias del servicio, recuentos y segundos.
"""
import json
import os
import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

_SAFE_PATH = re.compile(r"^/[a-z0-9/_-]{0,40}$")


def _secs(lat) -> float:
    try:
        return float(str(lat).rstrip("s"))
    except (TypeError, ValueError):
        return 0.0


def _path(url: str) -> str:
    p = urlparse(url or "").path or "/"
    return p if _SAFE_PATH.match(p) else "(otras rutas)"


def billable_by_day(path: str) -> dict[str, float]:
    try:
        d = json.load(open(path))
    except Exception as e:
        print(f"  Monitoring: sin datos ({type(e).__name__})")
        return {}
    if "error" in d:
        print("  Monitoring: sin acceso —", d["error"].get("status"))
        return {}
    out: dict[str, float] = defaultdict(float)
    for s in d.get("timeSeries", []):
        for p in s.get("points", []):
            day = p["interval"]["endTime"][:10]
            v = p["value"]
            out[day] += float(v.get("doubleValue", v.get("int64Value", 0)))
    return dict(out)


def main() -> None:
    print("== Configuración del servicio (solo los ajustes que cambian la factura)")
    try:
        svc = json.load(open(os.environ["SVC_FILE"]))
        ann = svc["spec"]["template"]["metadata"].get("annotations", {})
        spec = svc["spec"]["template"]["spec"]
        for k in ("run.googleapis.com/cpu-throttling", "run.googleapis.com/startup-cpu-boost",
                  "autoscaling.knative.dev/minScale", "autoscaling.knative.dev/maxScale"):
            print(f"  {k.split('/')[-1]}: {ann.get(k, '(sin fijar: valor por defecto)')}")
        print(f"  containerConcurrency: {spec.get('containerConcurrency')} · "
              f"timeoutSeconds: {spec.get('timeoutSeconds')}")
    except Exception as e:
        print(f"  sin acceso ({type(e).__name__})")

    bill = billable_by_day(os.environ["BILL_FILE"])

    try:
        entries = json.load(open(os.environ["REQ_FILE"]))
    except Exception as e:
        print(f"peticiones: sin datos ({type(e).__name__})")
        entries = []
    by_day: dict[str, float] = defaultdict(float)
    by_path: dict[str, list[float]] = defaultdict(list)
    codes: dict[str, Counter] = defaultdict(Counter)
    instances: dict[str, set] = defaultdict(set)
    for e in entries:
        hr = e.get("httpRequest") or {}
        day = str(e.get("timestamp", ""))[:10]
        lat = _secs(hr.get("latency"))
        p = _path(hr.get("requestUrl", ""))
        by_day[day] += lat
        by_path[p].append(lat)
        codes[p][str(hr.get("status", "?"))] += 1
        inst = (e.get("labels") or {}).get("instanceId")
        if inst:
            instances[day].add(inst)

    print(f"\n== Por día: facturable frente a suma de peticiones ({len(entries)} peticiones leídas)")
    print("  día         facturable  peticiones  sin petición  instancias")
    for day in sorted(set(bill) | set(by_day)):
        b, r = bill.get(day, 0.0) / 60, by_day.get(day, 0.0) / 60
        print(f"  {day}  {b:8.0f} min  {r:7.0f} min  {b - r:9.0f} min  {len(instances.get(day, ())):5d}")

    days = max(len(by_day), 1)
    print(f"\n== Por ruta (media por día en {days} días con peticiones; latencia = duración entera)")
    print("  ruta                              peticiones/día   min/día   máx (min)  códigos")
    for p, lats in sorted(by_path.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {p:34s} {len(lats) / days:10.1f} {sum(lats) / 60 / days:9.1f} {max(lats) / 60:10.1f}"
              f"  {dict(codes[p].most_common(4))}")


if __name__ == "__main__":
    main()
