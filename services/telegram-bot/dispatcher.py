"""
Repartidor: lanza por workflow_dispatch los workflows a los que les toca según
shared/dispatch_schedule.py. Lo llaman Cloud Scheduler (cada 5 min) y, de respaldo, un
workflow de GitHub cada hora; las dos llamadas pueden solaparse sin duplicar nada.

Una franja = un documento `dispatch_claims/{tarea}_{AAAAMMDDHHMM}` creado con create()
(falla si ya existe → otra llamada ya la reclamó). Si GitHub rechaza el lanzamiento, se
borra el documento para que la siguiente llamada lo reintente dentro del retraso máximo.
En modo dry_run se usa la colección `dispatch_claims_dry` y no se llama a GitHub.
Nunca lanza: devuelve un resumen (tareas y franjas, sin tokens).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from shared import dispatch_schedule as ds

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REPO = os.environ.get("DISPATCH_REPO", "pejofv93/prediction-intelligence")
REF = "main"
CLAIM_KEEP_DAYS = 3


def _claims(mode: str):
    from shared.firestore_client import col
    return col("dispatch_claims" if mode == "live" else "dispatch_claims_dry")


def _claim(coll, name: str, slot: datetime, now: datetime) -> bool:
    """True si esta llamada se queda la franja (el documento no existía)."""
    try:
        coll.document(f"{name}_{slot:%Y%m%d%H%M}").create(
            {"task": name, "slot": slot, "claimed_at": now, "status": "claimed"})
        return True
    except Exception as exc:
        # google.api_core.exceptions.AlreadyExists/Conflict (por nombre: así no depende del import)
        if type(exc).__name__ in ("AlreadyExists", "Conflict"):
            return False
        raise


def _fire(workflow: str, inputs: dict, token: str) -> tuple[bool, str]:
    try:
        r = httpx.post(f"{GITHUB_API}/repos/{REPO}/actions/workflows/{workflow}/dispatches",
                       headers={"Authorization": f"Bearer {token}",
                                "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"},
                       json={"ref": REF, "inputs": inputs}, timeout=15.0)
        return r.status_code == 204, f"HTTP {r.status_code}"
    except Exception as exc:  # red, DNS...
        return False, type(exc).__name__


def _prune(coll, now: datetime) -> int:
    """Borra reclamaciones de hace más de CLAIM_KEEP_DAYS días (una vez al día, a las 04:0x)."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    n = 0
    cutoff = now - timedelta(days=CLAIM_KEEP_DAYS)
    for d in coll.where(filter=FieldFilter("claimed_at", "<", cutoff)).limit(500).stream():
        d.reference.delete()
        n += 1
    from shared.firestore_client import col
    for d in col("dispatch_heartbeat").where(filter=FieldFilter("updated_at", "<", cutoff)).limit(200).stream():
        d.reference.delete()
        n += 1
    return n


def source_of(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "google-cloud-scheduler" in ua:
        return "scheduler"
    if ua.startswith("curl"):
        return "respaldo"
    return "otro"


def _heartbeat(now: datetime, source: str) -> dict:
    """Anota la llamada (un doc por hora) y cuenta las de los últimos 60 min por origen."""
    from google.cloud.firestore_v1 import ArrayUnion
    from shared.firestore_client import col
    hb = col("dispatch_heartbeat")
    hb.document(f"{now:%Y%m%d%H}").set(
        {"calls": ArrayUnion([f"{now:%M}:{source}"]), "updated_at": now}, merge=True)
    by_hour = {}
    for h in (now - timedelta(hours=1), now):
        snap = hb.document(f"{h:%Y%m%d%H}").get()
        by_hour[h] = ((snap.to_dict() or {}).get("calls") or []) if snap.exists else []
    return count_calls(by_hour, now)


def count_calls(by_hour: dict, now: datetime) -> dict[str, int]:
    """{hora: ["MM:origen", ...]} → llamadas por origen en los últimos 60 minutos."""
    counts: dict[str, int] = {}
    for h, calls in by_hour.items():
        for c in calls:
            minute, src = c.split(":", 1)
            t = h.replace(minute=int(minute), second=0, microsecond=0)
            if now - timedelta(minutes=60) < t <= now:
                counts[src] = counts.get(src, 0) + 1
    return counts


def run_dispatch(now: datetime | None = None, mode: str | None = None, source: str = "otro") -> dict:
    now = now or datetime.now(timezone.utc)
    mode = mode or ds.DISPATCH_MODE
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
    out = {"mode": mode, "now": now.isoformat(timespec="minutes"), "origen": source,
           "lanzadas": [], "ya_lanzadas": 0, "fallidas": [], "podadas": 0}
    try:
        out["llamadas_ultima_hora"] = _heartbeat(now, source)
    except Exception:
        logger.warning("dispatch: no se pudo anotar el latido", exc_info=True)
    if mode == "live" and not token:
        out["error"] = "falta GITHUB_DISPATCH_TOKEN"
        logger.error("dispatch: modo live sin token — no se lanza nada")
        return out
    try:
        coll = _claims(mode)
        for name, slot in ds.due(now):
            if not _claim(coll, name, slot, now):
                out["ya_lanzadas"] += 1
                continue
            label = f"{name}@{slot:%H:%M}"
            if mode != "live":
                out["lanzadas"].append(label)
                continue
            task = ds.TASKS[name]
            ok, why = _fire(task.workflow, task.inputs, token)
            ref = coll.document(f"{name}_{slot:%Y%m%d%H%M}")
            if ok:
                ref.update({"status": "fired", "fired_at": now})
                out["lanzadas"].append(label)
            else:
                ref.delete()          # la siguiente llamada lo reintenta
                out["fallidas"].append(f"{label} ({why})")
        if now.hour == 4 and now.minute < 10:
            out["podadas"] = _prune(coll, now)
    except Exception as exc:
        logger.error("dispatch: error", exc_info=True)
        out["error"] = type(exc).__name__
    logger.info("dispatch: %s", out)
    return out
