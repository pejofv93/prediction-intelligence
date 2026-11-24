"""Solo lectura: por qué job_runner (GitHub Actions) no puede escribir en Cloud Logging
(ResourceExhausted en todas las pasadas del analyze del 24-nov, 0 líneas escritas).

Lee Cloud Monitoring y Service Usage; no escribe nada. Al log PÚBLICO solo salen nombres de
método/cuota, códigos de respuesta, límites y recuentos por hora: ni ids de cuenta, ni correos,
ni números de proyecto.

Entorno: TOKEN (gcloud auth print-access-token), PROJECT, DAYS, GOOGLE_APPLICATION_CREDENTIALS.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

TOKEN = os.environ["TOKEN"]
PROJECT = os.environ.get("PROJECT", "prediction-intelligence")
DAYS = int(os.environ.get("DAYS", "2"))
LOGGING = "logging.googleapis.com"


def _get(url: str, params: list[tuple[str, str]] | None = None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error", {})
        except Exception:
            err = {}
        return {"error": {"code": e.code, "status": err.get("status", "")}}


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error", {})
        except Exception:
            err = {}
        return {"error": {"code": e.code, "status": err.get("status", "")}}


def _series(metric_filter: str, aligner: str, group_by: list[str]) -> dict:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=DAYS)
    params = [("filter", metric_filter),
              ("interval.startTime", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
              ("interval.endTime", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
              ("aggregation.alignmentPeriod", "3600s"),
              ("aggregation.perSeriesAligner", aligner),
              ("aggregation.crossSeriesReducer", "REDUCE_SUM")]
    params += [("aggregation.groupByFields", g) for g in group_by]
    return _get(f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries", params)


def _val(p: dict) -> float:
    v = p["value"]
    return float(v.get("int64Value", v.get("doubleValue", 0)))


def _print_hours(title: str, d: dict, label_keys: list[tuple[str, str]]) -> None:
    print(f"== {title}")
    if "error" in d:
        print(f"  sin acceso: {d['error'].get('code')} {d['error'].get('status')}")
        return
    rows = defaultdict(float)
    for s in d.get("timeSeries", []):
        labels = [s.get(kind, {}).get("labels", {}).get(k, "?") for kind, k in label_keys]
        for p in s.get("points", []):
            hour = p["interval"]["endTime"][:13]
            v = _val(p)
            if v:
                rows[(hour, *labels)] += v
    if not rows:
        print("  sin datos en el periodo")
        return
    for key in sorted(rows):
        print(f"  {key[0]}:00  " + " · ".join(key[1:]) + f"  → {rows[key]:,.0f}")


def main() -> None:
    # 1) ¿De qué proyecto es la cuenta de servicio? Las cuotas de la API se cargan al proyecto
    #    de la cuenta (consumidor), no al proyecto donde se escribe.
    print("== Cuenta de servicio del runner")
    try:
        cred = json.load(open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]))
        sa_project = cred.get("client_email", "").split("@")[-1].split(".")[0]
        print(f"  proyecto de la cuenta = proyecto de destino: {'sí' if sa_project == PROJECT else 'NO (otro proyecto)'}")
        print(f"  quota_project_id fijado en la credencial: {'sí' if cred.get('quota_project_id') else 'no'}")
    except Exception as e:
        print(f"  no se pudo leer la credencial ({type(e).__name__})")

    # 1b) ¿Tiene la cuenta permiso para escribir entradas de log? (testIamPermissions y roles
    #     de la cuenta en el proyecto; solo nombres de permiso y de rol).
    print("== Permisos de la cuenta de servicio en el proyecto")
    perms = ["logging.logEntries.create", "logging.logEntries.list", "logging.logs.list"]
    d = _post(f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}:testIamPermissions",
              {"permissions": perms})
    if "error" in d:
        print(f"  testIamPermissions sin acceso: {d['error'].get('code')} {d['error'].get('status')}")
    else:
        have = set(d.get("permissions", []))
        for perm in perms:
            print(f"  {perm}: {'sí' if perm in have else 'NO'}")
    try:
        email = json.load(open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])).get("client_email", "")
    except Exception:
        email = ""
    d = _post(f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}:getIamPolicy", {})
    if "error" in d:
        print(f"  roles: sin acceso ({d['error'].get('code')} {d['error'].get('status')})")
    else:
        roles = sorted(b["role"] for b in d.get("bindings", [])
                       if f"serviceAccount:{email}" in b.get("members", []))
        print("  roles de la cuenta: " + (", ".join(roles) if roles else "ninguno directo"))

    # 2) Peticiones a la API de Logging por hora, método y código (solo las que se cargan a
    #    este proyecto como consumidor).
    _print_hours(
        "Peticiones a logging.googleapis.com cargadas a este proyecto (por hora UTC, método, código)",
        _series(f'metric.type="serviceruntime.googleapis.com/api/request_count" AND '
                f'resource.type="consumed_api" AND resource.labels.service="{LOGGING}"',
                "ALIGN_SUM", ["resource.labels.method", "metric.labels.response_code",
                              "metric.labels.grpc_status_code"]),
        [("resource", "method"), ("metric", "response_code"), ("metric", "grpc_status_code")])

    # 3) Cuotas superadas y uso por cuota.
    _print_hours(
        "Cuotas de logging.googleapis.com superadas (minutos con exceso por hora)",
        _series(f'metric.type="serviceruntime.googleapis.com/quota/exceeded" AND '
                f'resource.type="consumer_quota" AND resource.labels.service="{LOGGING}"',
                "ALIGN_COUNT_TRUE", ["metric.labels.quota_metric", "metric.labels.limit_name"]),
        [("metric", "quota_metric"), ("metric", "limit_name")])
    _print_hours(
        "Uso de cuota de logging.googleapis.com (por hora UTC y cuota)",
        _series(f'metric.type="serviceruntime.googleapis.com/quota/rate/net_usage" AND '
                f'resource.type="consumer_quota" AND resource.labels.service="{LOGGING}"',
                "ALIGN_SUM", ["metric.labels.quota_metric"]),
        [("metric", "quota_metric")])

    # 4) Límites efectivos de las cuotas de Logging en este proyecto.
    print("== Límites de cuota de logging.googleapis.com en este proyecto")
    d = _get(f"https://serviceusage.googleapis.com/v1beta1/projects/{PROJECT}/services/{LOGGING}"
             f"/consumerQuotaMetrics", [("view", "BASIC")])
    if "error" in d:
        print(f"  sin acceso: {d['error'].get('code')} {d['error'].get('status')}")
    for m in d.get("metrics", []):
        for lim in m.get("consumerQuotaLimits", []):
            buckets = lim.get("quotaBuckets", [])
            eff = buckets[0].get("effectiveLimit", "?") if buckets else "?"
            print(f"  {m.get('displayName', m.get('metric'))} · {lim.get('unit', '')}: {eff}")


if __name__ == "__main__":
    main()
