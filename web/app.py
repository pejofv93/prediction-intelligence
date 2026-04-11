"""
web/app.py
Panel de control NEXUS v1.0 — FastAPI + Jinja2 + Tailwind CDN + Alpine.js CDN
Puerto 8080 · Protegido con PIN (WEB_PIN del .env o config.yaml)
"""

import asyncio
import json
import os
import secrets
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

import yaml
from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from rich.console import Console

# ── Setup ──────────────────────────────────────────────────────────────────────
console = Console()

BASE_DIR = Path(__file__).resolve().parent.parent   # nexus/
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="NEXUS Control Panel", version="1.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ── Configuración ──────────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = BASE_DIR / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_pin() -> str:
    """PIN desde .env > config.yaml > fallback '1234'."""
    pin = os.getenv("WEB_PIN")
    if pin:
        return pin
    try:
        cfg = _load_config()
        return str(cfg.get("web", {}).get("pin", "1234"))
    except Exception:
        return "1234"


def _get_db():
    sys.path.insert(0, str(BASE_DIR))
    from database.db import DBManager
    cfg = _load_config()
    db_path = cfg.get("database", {}).get("path", "cryptoverdad.db")
    # Resolve relative to BASE_DIR
    if not Path(db_path).is_absolute():
        db_path = str(BASE_DIR / db_path)
    return DBManager(db_path)


# ── Sesiones (en memoria, simple dict) ────────────────────────────────────────
_sessions: dict[str, datetime] = {}
SESSION_TTL = timedelta(hours=8)

# ── Pipelines en background ────────────────────────────────────────────────────
_running_pipelines: dict[str, dict] = {}  # pipeline_id → {status, log, ctx_summary}


def _is_authenticated(nexus_auth: Optional[str]) -> bool:
    if not nexus_auth:
        return False
    exp = _sessions.get(nexus_auth)
    if not exp:
        return False
    if datetime.now() > exp:
        _sessions.pop(nexus_auth, None)
        return False
    return True


# ── Helpers de template context ────────────────────────────────────────────────

def _base_ctx(request: Request) -> dict:
    return {"request": request}


# ── 1. Login ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, nexus_auth: Optional[str] = Cookie(default=None)):
    if _is_authenticated(nexus_auth):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {**_base_ctx(request), "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    pin: str = Form(...),
    nexus_auth: Optional[str] = Cookie(default=None),
):
    correct_pin = _get_pin()
    if pin == correct_pin:
        token = secrets.token_hex(32)
        _sessions[token] = datetime.now() + SESSION_TTL
        response = RedirectResponse("/dashboard", status_code=302)
        response.set_cookie(
            key="nexus_auth",
            value=token,
            httponly=True,
            max_age=int(SESSION_TTL.total_seconds()),
            samesite="lax",
        )
        console.print("[green]NEXUS Panel: login exitoso.[/]")
        return response
    console.print("[yellow]NEXUS Panel: intento de login fallido.[/]")
    return templates.TemplateResponse(
        "login.html",
        {**_base_ctx(request), "error": "PIN incorrecto. Inténtalo de nuevo."},
        status_code=401,
    )


@app.get("/logout")
async def logout(nexus_auth: Optional[str] = Cookie(default=None)):
    if nexus_auth:
        _sessions.pop(nexus_auth, None)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("nexus_auth")
    return response


# ── 2. Dashboard ───────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, nexus_auth: Optional[str] = Cookie(default=None)):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    try:
        db = _get_db()
        pipelines = db.list_pipelines(limit=10)
    except Exception as exc:
        console.print(f"[red]Dashboard DB error: {exc}[/]")
        pipelines = []

    # Estadísticas
    total = len(pipelines)
    scores = [p["seo_score"] for p in pipelines if p.get("seo_score")]
    avg_seo = round(sum(scores) / len(scores), 1) if scores else 0

    last_pub = None
    for p in pipelines:
        if p.get("youtube_url") or p.get("tiktok_url"):
            last_pub = p.get("completed_at") or p.get("created_at")
            break

    # Pipelines en memoria (en background)
    for p in pipelines:
        pid = str(p.get("id", ""))
        if pid in _running_pipelines:
            p["status"] = _running_pipelines[pid].get("status", p.get("status"))

    return templates.TemplateResponse("dashboard.html", {
        **_base_ctx(request),
        "pipelines": pipelines,
        "total_videos": total,
        "avg_seo": avg_seo,
        "last_published": last_pub or "—",
    })


# ── 3. Nuevo pipeline — formulario ─────────────────────────────────────────────

@app.get("/pipeline/new", response_class=HTMLResponse)
async def pipeline_new(
    request: Request,
    topic: str = "",
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("pipeline_new.html", {
        **_base_ctx(request),
        "prefill_topic": topic,
    })


# ── 4. Lanzar pipeline ─────────────────────────────────────────────────────────

def _run_pipeline_bg(pipeline_id: str, topic: str, mode: str, config: dict) -> None:
    """Ejecuta el pipeline en un hilo separado y actualiza _running_pipelines."""
    sys.path.insert(0, str(BASE_DIR))
    try:
        from database.db import DBManager
        from core.nexus_core import NexusCore

        db_path = config.get("database", {}).get("path", "cryptoverdad.db")
        if not Path(db_path).is_absolute():
            db_path = str(BASE_DIR / db_path)
        db = DBManager(db_path)

        _running_pipelines[pipeline_id]["status"] = "running"
        _running_pipelines[pipeline_id]["log"].append(
            f"[{datetime.now().strftime('%H:%M:%S')}] Pipeline iniciado"
        )

        nexus = NexusCore(config, db)
        if mode == "urgente":
            ctx = nexus.run_urgent_pipeline(topic)
        else:
            ctx = nexus.run_pipeline(topic, mode)

        # Override pipeline_id para consistencia
        ctx.pipeline_id = pipeline_id

        final_status = "completed" if not ctx.has_errors() else "completed_with_errors"
        _running_pipelines[pipeline_id]["status"] = final_status
        _running_pipelines[pipeline_id]["ctx_summary"] = ctx.summary()

        for err in ctx.errors:
            _running_pipelines[pipeline_id]["log"].append(f"[ERROR] {err}")
        for w in ctx.warnings:
            _running_pipelines[pipeline_id]["log"].append(f"[WARN]  {w}")

        _running_pipelines[pipeline_id]["log"].append(
            f"[{datetime.now().strftime('%H:%M:%S')}] Pipeline {final_status}"
        )
        _running_pipelines[pipeline_id]["script"] = ctx.script
        _running_pipelines[pipeline_id]["approved"] = ctx.approved

    except Exception as exc:
        _running_pipelines[pipeline_id]["status"] = "error"
        _running_pipelines[pipeline_id]["log"].append(f"[FATAL] {exc}")
        console.print(f"[red]Pipeline BG error: {exc}[/]")


@app.post("/pipeline/start")
async def pipeline_start(
    request: Request,
    topic: str = Form(...),
    mode: str = Form("standard"),
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    if not topic.strip():
        return RedirectResponse("/pipeline/new", status_code=302)

    pipeline_id = str(uuid.uuid4())

    try:
        config = _load_config()
        db = _get_db()
        from core.context import Context
        ctx = Context(topic=topic, mode=mode)
        ctx.pipeline_id = pipeline_id
        db.save_pipeline(ctx)
    except Exception as exc:
        console.print(f"[red]Error creando pipeline en DB: {exc}[/]")

    _running_pipelines[pipeline_id] = {
        "status": "pending",
        "log": [f"[{datetime.now().strftime('%H:%M:%S')}] Pipeline encolado: {topic}"],
        "ctx_summary": {},
        "script": "",
        "approved": False,
        "topic": topic,
        "mode": mode,
    }

    try:
        config = _load_config()
        t = threading.Thread(
            target=_run_pipeline_bg,
            args=(pipeline_id, topic, mode, config),
            daemon=True,
        )
        t.start()
        console.print(f"[bold #F7931A]Pipeline lanzado:[/] {pipeline_id[:8]} | {topic}")
    except Exception as exc:
        _running_pipelines[pipeline_id]["status"] = "error"
        _running_pipelines[pipeline_id]["log"].append(f"[FATAL] No se pudo lanzar: {exc}")
        console.print(f"[red]Error lanzando thread: {exc}[/]")

    return RedirectResponse(f"/pipeline/{pipeline_id}", status_code=302)


# ── 5. Estado de pipeline individual ──────────────────────────────────────────

@app.get("/pipeline/{pipeline_id}", response_class=HTMLResponse)
async def pipeline_detail(
    request: Request,
    pipeline_id: str,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    # Buscar en memoria primero, luego en DB
    mem = _running_pipelines.get(pipeline_id)

    db_pipeline = None
    try:
        db = _get_db()
        db_pipeline = db.get_pipeline(pipeline_id)
    except Exception:
        pass

    if not mem and not db_pipeline:
        raise HTTPException(status_code=404, detail="Pipeline no encontrado")

    pipeline_data = {}
    if db_pipeline:
        pipeline_data.update(db_pipeline)
    if mem:
        pipeline_data.update({
            "status": mem.get("status", pipeline_data.get("status")),
            "log": mem.get("log", []),
            "script": mem.get("script", ""),
            "approved": mem.get("approved", False),
            "topic": mem.get("topic", pipeline_data.get("topic", "")),
            "mode": mem.get("mode", pipeline_data.get("mode", "")),
        })

    return templates.TemplateResponse("pipeline.html", {
        **_base_ctx(request),
        "pipeline": pipeline_data,
        "pipeline_id": pipeline_id,
    })


# ── 6. Aprobar / Rechazar pipeline ─────────────────────────────────────────────

@app.post("/pipeline/{pipeline_id}/approve")
async def pipeline_approve(
    pipeline_id: str,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        raise HTTPException(status_code=401)
    if pipeline_id in _running_pipelines:
        _running_pipelines[pipeline_id]["approved"] = True
        _running_pipelines[pipeline_id]["log"].append(
            f"[{datetime.now().strftime('%H:%M:%S')}] Contenido APROBADO por operador"
        )
    try:
        db = _get_db()
        db.update_pipeline_status(pipeline_id, "approved")
    except Exception:
        pass
    return RedirectResponse(f"/pipeline/{pipeline_id}", status_code=302)


@app.post("/pipeline/{pipeline_id}/reject")
async def pipeline_reject(
    pipeline_id: str,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        raise HTTPException(status_code=401)
    if pipeline_id in _running_pipelines:
        _running_pipelines[pipeline_id]["approved"] = False
        _running_pipelines[pipeline_id]["status"] = "rejected"
        _running_pipelines[pipeline_id]["log"].append(
            f"[{datetime.now().strftime('%H:%M:%S')}] Contenido RECHAZADO por operador"
        )
    try:
        db = _get_db()
        db.update_pipeline_status(pipeline_id, "rejected")
    except Exception:
        pass
    return RedirectResponse(f"/pipeline/{pipeline_id}", status_code=302)


# ── 7. API JSON de estado ──────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status(nexus_auth: Optional[str] = Cookie(default=None)):
    if not _is_authenticated(nexus_auth):
        raise HTTPException(status_code=401, detail="No autenticado")

    errors_list = []
    pipelines_today = 0
    last_video = "—"

    try:
        db = _get_db()
        all_pipelines = db.list_pipelines(limit=100)

        today = datetime.now().date()
        for p in all_pipelines:
            created = str(p.get("created_at", ""))[:10]
            if created == str(today):
                pipelines_today += 1
            if not last_video or last_video == "—":
                if p.get("youtube_url") or p.get("tiktok_url"):
                    last_video = p.get("seo_title") or p.get("topic") or "—"

        for p in all_pipelines[:5]:
            errs = p.get("errors")
            if errs:
                try:
                    parsed = json.loads(errs)
                    errors_list.extend(parsed[:3])
                except Exception:
                    pass
    except Exception as exc:
        errors_list.append(str(exc))

    # Groq quota (aproximado — sin API real de quota)
    groq_quota = "N/A"
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        groq_quota = "sin configurar"

    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "pipelines_today": pipelines_today,
        "pipelines_running": sum(
            1 for v in _running_pipelines.values() if v.get("status") == "running"
        ),
        "last_video": last_video,
        "groq_quota_used": groq_quota,
        "errors": errors_list[:10],
    })


# ── 8. API JSON de pipeline (polling) ─────────────────────────────────────────

@app.get("/api/pipeline/{pipeline_id}")
async def api_pipeline_status(
    pipeline_id: str,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        raise HTTPException(status_code=401)

    mem = _running_pipelines.get(pipeline_id)
    if mem:
        return JSONResponse({
            "pipeline_id": pipeline_id,
            "status": mem.get("status", "unknown"),
            "log": mem.get("log", []),
            "script": mem.get("script", ""),
            "approved": mem.get("approved", False),
        })

    try:
        db = _get_db()
        p = db.get_pipeline(pipeline_id)
        if p:
            return JSONResponse({
                "pipeline_id": pipeline_id,
                "status": p.get("status", "unknown"),
                "log": [],
                "script": "",
                "approved": False,
            })
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Pipeline no encontrado")


# ── 9. Galería de vídeos ───────────────────────────────────────────────────────

@app.get("/videos", response_class=HTMLResponse)
async def videos_gallery(
    request: Request,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    videos = []
    try:
        db = _get_db()
        # Obtener vídeos publicados desde pipelines con URL
        pipelines = db.list_pipelines(limit=50)
        for p in pipelines:
            if p.get("youtube_url") or p.get("tiktok_url"):
                videos.append({
                    "id": p.get("id", ""),
                    "title": p.get("topic", "Sin título"),
                    "youtube_url": p.get("youtube_url", ""),
                    "tiktok_url": p.get("tiktok_url", ""),
                    "seo_score": p.get("seo_score", 0),
                    "created_at": str(p.get("created_at", ""))[:16],
                    "status": p.get("status", ""),
                })
    except Exception as exc:
        console.print(f"[red]Videos gallery DB error: {exc}[/]")

    return templates.TemplateResponse("videos.html", {
        **_base_ctx(request),
        "videos": videos,
    })


# ── 10. Health (Railway) ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexus"}


# ── 11. Pipeline status (24 agentes + logs) ───────────────────────────────────

_AGENTS_REGISTRY = [
    # (name, layer)
    ("ARGOS",    "ORACULO"), ("PYTHIA",  "ORACULO"), ("RECON",   "ORACULO"),
    ("VECTOR",   "ORACULO"), ("THEMIS",  "ORACULO"),
    ("CALIOPE",  "FORGE"),   ("HERMES",  "FORGE"),   ("ECHO",    "FORGE"),
    ("HEPHAESTUS","FORGE"),  ("IRIS",    "FORGE"),    ("DAEDALUS","FORGE"),
    ("OLYMPUS",  "HERALD"),  ("RAPID",   "HERALD"),   ("AURORA",  "HERALD"),
    ("MERCURY",  "HERALD"),  ("PROTEUS", "HERALD"),
    ("AGORA",    "SENTINEL"),("SCROLL",  "SENTINEL"), ("CROESUS", "SENTINEL"),
    ("ARGONAUT", "SENTINEL"),
    ("MNEME",    "MIND"),    ("KAIROS",  "MIND"),     ("ALETHEIA","MIND"),
]


def _agent_status_from_db(db) -> dict:
    """Detecta el ultimo estado de cada agente desde pipelines recientes."""
    try:
        pipelines = db.list_pipelines(limit=5)
        if not pipelines:
            return {}
        last = pipelines[0]
        errors_raw = last.get("errors", "[]") or "[]"
        try:
            errors = json.loads(errors_raw)
        except Exception:
            errors = []
        # Detectar que agentes aparecen en errores
        agent_errors = {}
        for e in errors:
            for name, _ in _AGENTS_REGISTRY:
                if name in str(e).upper():
                    agent_errors[name] = True
        return agent_errors
    except Exception:
        return {}


@app.get("/pipeline-status", response_class=HTMLResponse)
async def pipeline_status_page(
    request: Request,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    try:
        db = _get_db()
        agent_errors = _agent_status_from_db(db)
    except Exception:
        agent_errors = {}

    agents = []
    for name, layer in _AGENTS_REGISTRY:
        # Determinar si hay pipeline corriendo con este agente
        running_any = any(
            v.get("status") == "running" for v in _running_pipelines.values()
        )
        if name in agent_errors:
            status = "ERROR"
        elif running_any:
            status = "RUNNING"
        else:
            status = "OK"

        agents.append({"name": name, "layer": layer, "status": status})

    return templates.TemplateResponse("pipeline_status.html", {
        **_base_ctx(request),
        "agents": agents,
    })


# ── 12. API logs ───────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_logs(nexus_auth: Optional[str] = Cookie(default=None)):
    if not _is_authenticated(nexus_auth):
        raise HTTPException(status_code=401)

    log_lines = []
    try:
        # Recopilar logs de todos los pipelines en memoria
        for pid, data in list(_running_pipelines.items())[-5:]:
            for line in data.get("log", [])[-20:]:
                # Parsear formato: [HH:MM:SS] mensaje o [LEVEL] mensaje
                level = "INFO"
                agent = "NEXUS"
                message = line
                if line.startswith("[ERROR]") or "ERROR" in line[:10]:
                    level = "ERROR"
                elif line.startswith("[WARN]"):
                    level = "WARNING"
                log_lines.append({
                    "ts": line[:11] if line.startswith("[") else "",
                    "level": level,
                    "agent": agent,
                    "message": message,
                })
    except Exception as exc:
        log_lines.append({"ts": "", "level": "ERROR", "agent": "WEB", "message": str(exc)})

    # Leer el log file si existe
    log_file = BASE_DIR / "nexus.log"
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-50:]:
                parts = line.split(" - ", 3)
                log_lines.append({
                    "ts": parts[0][:19] if parts else "",
                    "level": parts[2].strip() if len(parts) > 2 else "INFO",
                    "agent": parts[1].strip() if len(parts) > 1 else "NEXUS",
                    "message": parts[3].strip() if len(parts) > 3 else line,
                })
        except Exception:
            pass

    return JSONResponse(log_lines[-50:])


# ── 13. Calendario ─────────────────────────────────────────────────────────────

@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(
    request: Request,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    scheduled = []
    try:
        db = _get_db()
        pipelines = db.list_pipelines(limit=50)
        for p in pipelines:
            scheduled.append({
                "id": p.get("id", ""),
                "title": p.get("topic", "Sin titulo"),
                "topic": p.get("topic", ""),
                "mode": p.get("mode", ""),
                "scheduled_time": p.get("created_at", ""),
                "status": p.get("status", "pending"),
                "youtube_url": p.get("youtube_url", ""),
                "tiktok_url": p.get("tiktok_url", ""),
                "seo_score": p.get("seo_score"),
            })
    except Exception as exc:
        console.print(f"[red]Calendar DB error: {exc}[/]")

    return templates.TemplateResponse("calendar.html", {
        **_base_ctx(request),
        "scheduled": scheduled,
    })


# ── 14. Historial ──────────────────────────────────────────────────────────────

@app.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    filter: str = "todos",
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    videos = []
    try:
        db = _get_db()
        all_pipelines = db.list_pipelines(limit=200)
        today = datetime.now().date()

        for p in all_pipelines:
            created_str = str(p.get("created_at", ""))[:10]
            try:
                created_date = datetime.strptime(created_str, "%Y-%m-%d").date()
            except Exception:
                created_date = today

            if filter == "hoy" and created_date != today:
                continue
            if filter == "semana":
                from datetime import timedelta
                if created_date < (today - timedelta(days=7)):
                    continue
            if filter == "mes":
                if created_date.year != today.year or created_date.month != today.month:
                    continue

            videos.append({
                "id": p.get("id", ""),
                "topic": p.get("topic", ""),
                "mode": p.get("mode", ""),
                "created_at": str(p.get("created_at", "")),
                "youtube_url": p.get("youtube_url", ""),
                "tiktok_url": p.get("tiktok_url", ""),
                "seo_score": p.get("seo_score"),
                "status": p.get("status", ""),
            })
    except Exception as exc:
        console.print(f"[red]History DB error: {exc}[/]")

    return templates.TemplateResponse("history.html", {
        **_base_ctx(request),
        "videos": videos,
        "filter": filter,
    })


# ── 15. Ideas ──────────────────────────────────────────────────────────────────

@app.get("/ideas", response_class=HTMLResponse)
async def ideas_page(
    request: Request,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    ideas = []
    try:
        db = _get_db()
        conn = db._connect()
        # Intentar tablas donde PYTHIA/THEMIS guardan ideas
        for table in ("noticias", "ideas", "topics", "news_items"):
            try:
                rows = conn.execute(
                    f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    ideas.append({
                        "title": d.get("title") or d.get("topic") or d.get("headline", ""),
                        "topic": d.get("topic") or d.get("title", ""),
                        "score": d.get("score") or d.get("relevance_score"),
                        "source": d.get("source") or d.get("feed_url", ""),
                        "created_at": str(d.get("created_at", ""))[:16],
                        "detected_at": str(d.get("detected_at", ""))[:16],
                        "status": d.get("status", "nueva"),
                    })
                if ideas:
                    break
            except Exception:
                continue
    except Exception as exc:
        console.print(f"[red]Ideas DB error: {exc}[/]")

    return templates.TemplateResponse("ideas.html", {
        **_base_ctx(request),
        "ideas": ideas,
    })


# ── 16. Crear pipeline desde idea ─────────────────────────────────────────────

@app.post("/pipeline/from-idea")
async def pipeline_from_idea(
    request: Request,
    topic: str = Form(...),
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)
    # Redirige al formulario con el topic precargado
    return RedirectResponse(
        f"/pipeline/new?topic={topic}", status_code=302
    )


# ── 17. Agentes ────────────────────────────────────────────────────────────────

@app.get("/agents", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    try:
        db = _get_db()
        agent_errors = _agent_status_from_db(db)
    except Exception:
        agent_errors = {}

    running_any = any(
        v.get("status") == "running" for v in _running_pipelines.values()
    )

    agents_by_layer: dict = {}
    prompts_dir = BASE_DIR / "prompts"

    for name, layer in _AGENTS_REGISTRY:
        if name in agent_errors:
            status = "ERROR"
        elif running_any:
            status = "RUNNING"
        else:
            status = "OK"

        # Detectar si tiene prompt
        prompt_file = prompts_dir / f"{name.lower()}.txt"
        has_prompt = prompt_file.exists()

        agent_info = {
            "name": name,
            "layer": layer,
            "status": status,
            "has_prompt": has_prompt,
            "file": f"agents/{layer.lower()}/{name.lower()}.py",
        }

        agents_by_layer.setdefault(layer, []).append(agent_info)

    return templates.TemplateResponse("agents.html", {
        **_base_ctx(request),
        "agents_by_layer": agents_by_layer,
    })


# ── 18. API prompt de agente ───────────────────────────────────────────────────

@app.get("/api/agent-prompt/{agent_name}")
async def api_agent_prompt(
    agent_name: str,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        raise HTTPException(status_code=401)

    # Sanitizar nombre: solo letras mayusculas/minusculas
    safe_name = "".join(c for c in agent_name if c.isalpha()).lower()
    prompt_file = BASE_DIR / "prompts" / f"{safe_name}.txt"

    content = ""
    if prompt_file.exists():
        try:
            content = prompt_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            content = f"Error leyendo prompt: {exc}"
    else:
        content = f"No se encontro prompts/{safe_name}.txt"

    return JSONResponse({"agent": agent_name, "content": content})


# ── 19. Aprendizaje (MNEME) ───────────────────────────────────────────────────

@app.get("/learning", response_class=HTMLResponse)
async def learning_page(
    request: Request,
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    learnings = []
    try:
        db = _get_db()
        conn = db._connect()
        # Intentar tablas donde MNEME guarda aprendizajes
        for table in ("learnings", "learning", "mneme_data", "learning_data"):
            try:
                rows = conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 100"
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    learnings.append({
                        "key": d.get("key") or d.get("metric") or d.get("pattern", ""),
                        "value": d.get("value") or d.get("insight") or d.get("learning", ""),
                        "category": d.get("category") or d.get("agent") or d.get("video_id", ""),
                        "created_at": str(d.get("recorded_at") or d.get("created_at") or d.get("updated_at", ""))[:16],
                    })
                if learnings:
                    break
            except Exception:
                continue
    except Exception as exc:
        console.print(f"[red]Learning DB error: {exc}[/]")

    return templates.TemplateResponse("learning.html", {
        **_base_ctx(request),
        "learnings": learnings,
    })


# ── 20. Configuracion ─────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    nexus_auth: Optional[str] = Cookie(default=None),
    saved: bool = False,
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    settings_data = _load_settings_from_db()
    return templates.TemplateResponse("settings.html", {
        **_base_ctx(request),
        "settings": settings_data,
        "saved": saved,
        "error": None,
    })


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    groq_api_key: str = Form(default=""),
    pexels_api_key: str = Form(default=""),
    telegram_bot_token: str = Form(default=""),
    telegram_chat_id: str = Form(default=""),
    web_pin: str = Form(default=""),
    auto_mode: str = Form(default=""),
    pipeline_schedule: str = Form(default="0 9,14,20 * * *"),
    nexus_auth: Optional[str] = Cookie(default=None),
):
    if not _is_authenticated(nexus_auth):
        return RedirectResponse("/", status_code=302)

    error = None
    try:
        db = _get_db()
        conn = db._connect()

        # Crear tabla de settings si no existe
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nexus_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

        def _upsert(key: str, value: str):
            if value.strip():
                conn.execute(
                    "INSERT INTO nexus_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value.strip()),
                )

        if groq_api_key.strip():
            _upsert("groq_api_key", groq_api_key)
        if pexels_api_key.strip():
            _upsert("pexels_api_key", pexels_api_key)
        if telegram_bot_token.strip():
            _upsert("telegram_bot_token", telegram_bot_token)
        if telegram_chat_id.strip():
            _upsert("telegram_chat_id", telegram_chat_id)
        if web_pin.strip():
            _upsert("web_pin", web_pin)
        _upsert("auto_mode", "1" if auto_mode == "1" else "0")
        _upsert("pipeline_schedule", pipeline_schedule)
        conn.commit()
        console.print("[green]Settings guardados en DB.[/]")
    except Exception as exc:
        error = str(exc)
        console.print(f"[red]Error guardando settings: {exc}[/]")

    settings_data = _load_settings_from_db()
    return templates.TemplateResponse("settings.html", {
        **_base_ctx(request),
        "settings": settings_data,
        "saved": error is None,
        "error": error,
    })


def _load_settings_from_db() -> dict:
    """Carga configuracion guardada en DB. Devuelve dict con valores masked."""
    result = {
        "groq_api_key": "",
        "pexels_api_key": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "web_pin_masked": "****",
        "auto_mode": False,
        "pipeline_schedule": "0 9,14,20 * * *",
    }
    try:
        db = _get_db()
        conn = db._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nexus_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        rows = conn.execute("SELECT key, value FROM nexus_settings").fetchall()
        for row in rows:
            k, v = row["key"], row["value"]
            if k == "groq_api_key" and v:
                result["groq_api_key"] = v[:6] + "***" if len(v) > 6 else "***"
            elif k == "pexels_api_key" and v:
                result["pexels_api_key"] = v[:4] + "***" if len(v) > 4 else "***"
            elif k == "telegram_bot_token" and v:
                result["telegram_bot_token"] = v[:8] + "***" if len(v) > 8 else "***"
            elif k == "telegram_chat_id":
                result["telegram_chat_id"] = v
            elif k == "web_pin" and v:
                result["web_pin_masked"] = "*" * len(v)
            elif k == "auto_mode":
                result["auto_mode"] = v == "1"
            elif k == "pipeline_schedule":
                result["pipeline_schedule"] = v
    except Exception as exc:
        console.print(f"[red]Error cargando settings: {exc}[/]")
    return result


# ── 21. API precios CoinGecko (polling dashboard) ─────────────────────────────

@app.get("/api/prices")
async def api_prices(nexus_auth: Optional[str] = Cookie(default=None)):
    if not _is_authenticated(nexus_auth):
        raise HTTPException(status_code=401)

    try:
        import urllib.request
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana&vs_currencies=usd"
            "&include_24hr_change=true"
        )
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return JSONResponse({
            "BTC": {
                "price": data.get("bitcoin", {}).get("usd"),
                "change24h": data.get("bitcoin", {}).get("usd_24h_change"),
            },
            "ETH": {
                "price": data.get("ethereum", {}).get("usd"),
                "change24h": data.get("ethereum", {}).get("usd_24h_change"),
            },
            "SOL": {
                "price": data.get("solana", {}).get("usd"),
                "change24h": data.get("solana", {}).get("usd_24h_change"),
            },
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
