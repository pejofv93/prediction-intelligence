#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
NEXUS v1.0 — Sistema Autónomo CryptoVerdad
Punto de entrada CLI completo con rich + flags para automatización.
"""

import argparse
import os
import sys
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.columns import Columns
from rich import box

# ── Configuración de paths ──────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

console = Console()

# ── Banner ASCII ────────────────────────────────────────────────────────────────
BANNER = """[bold #F7931A]
███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
[/bold #F7931A][white]v1.0 · CryptoVerdad · "Crypto sin humo."[/white]"""

MENU = """
[bold white]  [1][/] Crear vídeo        [dim](pipeline completo)[/]
[bold white]  [2][/] Modo urgente       [dim](noticia crítica — sin revisión)[/]
[bold white]  [3][/] Panel web          [dim](puerto 8080)[/]
[bold white]  [4][/] Estado del sistema
[bold white]  [5][/] Test de componentes
[bold white]  [6][/] Salir
"""


# ── Carga de config y DB ────────────────────────────────────────────────────────

def load_config() -> dict:
    import yaml
    config_path = ROOT / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_db(config: dict):
    from database.db import DBManager
    db_path = config.get("database", {}).get("path", "cryptoverdad.db")
    if not Path(db_path).is_absolute():
        db_path = str(ROOT / db_path)
    return DBManager(db_path)


# ── Acciones del menú ───────────────────────────────────────────────────────────

def _print_dry_run_artifacts(ctx) -> None:
    """Muestra en una tabla los artefactos generados en dry-run."""
    from pathlib import Path as _P
    table = Table(
        title="[bold yellow]Artefactos generados (dry-run)[/]",
        box=box.ROUNDED, border_style="yellow", show_header=True,
    )
    table.add_column("Campo",    style="bold cyan",  min_width=20)
    table.add_column("Valor",    style="white",      min_width=40)
    table.add_column("Existe",   min_width=6)

    def _row(label, value):
        if not value:
            table.add_row(label, "[dim](vacío)[/]", "—")
            return
        exists = "[green]✓[/]" if _P(value).exists() else "[red]✗[/]"
        table.add_row(label, str(value)[-60:], exists)

    table.add_row("Título SEO",   str(ctx.seo_title or "")[:60],  "—")
    table.add_row("SEO Score",    str(ctx.seo_score),             "—")
    _row("Audio",       ctx.audio_path)
    _row("Vídeo",       ctx.video_path)
    _row("Short",       ctx.short_video_path)
    _row("Thumbnail A", ctx.thumbnail_a_path)
    _row("Thumbnail B", ctx.thumbnail_b_path)
    _row("Gráfico",     ctx.chart_path)

    if ctx.errors:
        table.add_row("[red]Errores[/]", "\n".join(ctx.errors[:3]), "—")
    if ctx.warnings:
        table.add_row("[yellow]Avisos[/]", "\n".join(ctx.warnings[:3]), "—")

    console.print(table)


def action_create_video(config: dict, db, topic: str = "", mode: str = "",
                        dry_run: bool = False, interactive: bool = True) -> None:
    """Lanza el pipeline completo."""
    from core.nexus_core import NexusCore

    if not topic:
        topic = Prompt.ask("[bold #F7931A]Tema del vídeo[/]")
    if not mode and interactive:
        mode = Prompt.ask(
            "[dim]Modo[/] (standard/urgente/short/analisis/opinion/tutorial)",
            default="standard",
        )
    if not mode:
        mode = "standard"

    dry_label = "  [bold yellow]⚑ DRY-RUN — no se publicará[/]\n" if dry_run else ""
    console.print(Panel(
        f"{dry_label}"
        f"[bold]Lanzando pipeline[/]\n"
        f"[dim]Tema:[/]  {topic}\n"
        f"[dim]Modo:[/]  [cyan]{mode}[/]",
        border_style="#F7931A" if not dry_run else "yellow",
        title="[bold white]NEXUS PIPELINE[/]",
    ))

    nexus = NexusCore(config, db)
    ctx = nexus.run_pipeline(topic, mode, dry_run=dry_run)

    if ctx.has_errors():
        console.print("[red bold]Pipeline completado con errores. Revisa los logs.[/]")
    elif dry_run:
        console.print("[bold yellow]Dry-run completado — contenido generado, nada publicado.[/]")
        _print_dry_run_artifacts(ctx)
    else:
        console.print("[green bold]Pipeline completado exitosamente.[/]")


def action_urgent(config: dict, db, topic: str = "") -> None:
    """Modo urgente: publicación rápida, TikTok primero."""
    from core.nexus_core import NexusCore

    if not topic:
        topic = Prompt.ask("[bold red]Noticia urgente[/]")

    console.print(Panel(
        f"[bold red]MODO URGENTE ACTIVADO[/]\n[white]{topic}[/]",
        border_style="red",
        title="NEXUS URGENT",
    ))

    nexus = NexusCore(config, db)
    ctx = nexus.run_urgent_pipeline(topic)

    if ctx.has_errors():
        console.print("[red]Pipeline urgente completado con errores.[/]")
    else:
        console.print("[green bold]Publicación urgente completada.[/]")


def action_web_panel(config: dict) -> None:
    """Arranca el servidor FastAPI del panel web."""
    import uvicorn

    port = int(config.get("web", {}).get("port", 8080))
    pin  = os.getenv("WEB_PIN") or str(config.get("web", {}).get("pin", "1234"))

    console.print(Panel(
        f"[bold]Panel web iniciando…[/]\n"
        f"  URL: [link]http://localhost:{port}[/link]\n"
        f"  PIN: [bold #F7931A]{pin}[/]\n"
        f"[dim]Ctrl+C para detener[/]",
        border_style="#F7931A",
        title="[bold white]NEXUS WEB PANEL[/]",
    ))

    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="warning",   # reducir ruido; rich maneja el output
    )


def action_system_status(config: dict, db) -> None:
    """Muestra el estado completo del sistema en una tabla rich."""
    console.print(Panel("[bold]Estado del Sistema NEXUS[/]", border_style="#F7931A"))

    table = Table(box=box.ROUNDED, border_style="#F7931A", show_header=True)
    table.add_column("Componente", style="bold cyan", min_width=20)
    table.add_column("Estado", min_width=12)
    table.add_column("Detalle", style="dim")

    # Config YAML
    try:
        _ = load_config()
        table.add_row("Config YAML", "[green]OK[/]", str(ROOT / "config.yaml"))
    except Exception as e:
        table.add_row("Config YAML", "[red]ERROR[/]", str(e))

    # SQLite
    try:
        db_path = config.get("database", {}).get("path", "cryptoverdad.db")
        if not Path(db_path).is_absolute():
            db_path = str(ROOT / db_path)
        conn = sqlite3.connect(db_path)
        pipelines_count = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
        videos_count    = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        conn.close()
        table.add_row("SQLite", "[green]OK[/]", f"{pipelines_count} pipelines · {videos_count} vídeos")
    except Exception as e:
        table.add_row("SQLite", "[red]ERROR[/]", str(e))

    # LLM Client
    try:
        from utils.llm_client import LLMClient
        llm = LLMClient(config)
        health = llm.health_check()
        groq_ok   = health.get("groq", False)
        ollama_ok = health.get("ollama", False)
        table.add_row("Groq (primary)",   "[green]OK[/]" if groq_ok   else "[red]OFFLINE[/]",   "llama-3.3-70b-versatile")
        table.add_row("Ollama (fallback)", "[green]OK[/]" if ollama_ok else "[yellow]OFFLINE[/]", "llama3.2")
    except Exception as e:
        table.add_row("LLM Client", "[red]ERROR[/]", str(e))

    # Variables de entorno clave
    env_vars = [
        ("GROQ_API_KEY",        "Groq API"),
        ("PEXELS_API_KEY",      "Pexels"),
        ("TELEGRAM_BOT_TOKEN",  "Telegram Bot"),
        ("YOUTUBE_CLIENT_ID",   "YouTube OAuth"),
        ("WEB_PIN",             "Web PIN"),
    ]
    for var, label in env_vars:
        val = os.getenv(var)
        if val:
            table.add_row(label, "[green]Configurado[/]", f"{var[:6]}****")
        else:
            table.add_row(label, "[yellow]No configurado[/]", f"${var}")

    console.print(table)

    # Pipelines recientes
    try:
        recent = db.list_pipelines(limit=5)
    except Exception:
        recent = []

    if recent:
        ptable = Table(
            title="Últimos 5 Pipelines",
            box=box.SIMPLE_HEAVY,
            border_style="dim",
        )
        ptable.add_column("ID",     style="dim",  width=10)
        ptable.add_column("Tema",   max_width=42)
        ptable.add_column("Modo",   style="cyan", width=10)
        ptable.add_column("Estado", width=22)
        ptable.add_column("Creado", style="dim",  width=16)

        for p in recent:
            status = str(p.get("status") or "pending")
            if "completed" in status and "error" not in status:
                status_str = f"[green]{status}[/]"
            elif "error" in status or status == "error":
                status_str = f"[red]{status}[/]"
            elif status == "running":
                status_str = f"[yellow]{status}[/]"
            else:
                status_str = f"[dim]{status}[/]"

            ptable.add_row(
                str(p["id"])[:8] + "…",
                str(p["topic"])[:42],
                str(p["mode"]),
                status_str,
                str(p.get("created_at", ""))[:16],
            )
        console.print(ptable)
    else:
        console.print("[dim]No hay pipelines registrados.[/]")


# ── Test de componentes ─────────────────────────────────────────────────────────

def _test_context() -> str:
    from core.context import Context
    ctx = Context(topic="test_topic", mode="standard")
    assert ctx.pipeline_id, "pipeline_id vacío"
    assert ctx.topic == "test_topic"
    ctx.add_error("TEST", "error de prueba")
    assert ctx.has_errors()
    return f"pipeline_id={ctx.pipeline_id[:8]}"


def _test_database() -> str:
    from database.db import DBManager
    from core.context import Context
    db = DBManager(":memory:")
    db.execute_schema()
    ctx = Context(topic="test_db", mode="standard")
    db.save_pipeline(ctx)
    p = db.get_pipeline(ctx.pipeline_id)
    assert p is not None, "Pipeline no encontrado tras INSERT"
    assert p["topic"] == "test_db"
    db.update_pipeline_status(ctx.pipeline_id, "completed")
    p2 = db.get_pipeline(ctx.pipeline_id)
    assert p2["status"] == "completed"
    return "INSERT + SELECT + UPDATE OK (SQLite en memoria)"


def _test_llm_client(config: dict) -> str:
    from utils.llm_client import LLMClient
    llm = LLMClient(config)
    assert llm.primary in ("groq", "ollama"), "primary inválido"
    return f"primary={llm.primary}, model={llm.model}"


def _test_config(config: dict) -> str:
    assert "channel" in config,  "Falta sección 'channel'"
    assert "llm"     in config,  "Falta sección 'llm'"
    assert "video"   in config,  "Falta sección 'video'"
    assert "web"     in config,  "Falta sección 'web'"
    ch = config["channel"]["name"]
    return f"canal={ch}, llm={config['llm']['model']}"


def _test_coingecko() -> str:
    import urllib.request
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    req = urllib.request.Request(url, headers={"User-Agent": "NEXUS/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        import json
        data = json.loads(resp.read())
    btc_price = data["bitcoin"]["usd"]
    return f"BTC=${btc_price:,.0f} USD"


def _test_rss_feedparser() -> str:
    import feedparser
    feed = feedparser.parse("https://feeds.feedburner.com/CoinDesk")
    if not feed.entries:
        # Fallback: Google News crypto
        feed = feedparser.parse("https://news.google.com/rss/search?q=bitcoin&hl=es&gl=ES&ceid=ES:es")
    assert feed.entries, "Feed vacío"
    title = feed.entries[0].get("title", "sin título")[:40]
    return f"{len(feed.entries)} items · '{title}…'"


def _test_agent_imports() -> str:
    """Verifica que los módulos de agentes se pueden importar sin ejecutar."""
    imported = []
    failed   = []

    agent_modules = [
        ("core.context",         "Context"),
        ("core.nexus_core",      "NexusCore"),
        ("core.urgency_detector","UrgencyDetector"),
        ("utils.logger",         "get_logger"),
        ("utils.llm_client",     "LLMClient"),
        ("database.db",          "DBManager"),
    ]

    # Agentes opcionales (pueden no estar implementados aún)
    optional_modules = [
        ("agents.oracle.oracle_agent",  "OracleAgent"),
        ("agents.forge.forge_agent",    "ForgeAgent"),
        ("agents.herald.herald_agent",  "HeraldAgent"),
        ("agents.mind.mind_agent",      "MindAgent"),
    ]

    for mod, cls in agent_modules:
        try:
            m = __import__(mod, fromlist=[cls])
            getattr(m, cls)
            imported.append(mod.split(".")[-1])
        except Exception as e:
            failed.append(f"{mod}:{e}")

    for mod, cls in optional_modules:
        try:
            m = __import__(mod, fromlist=[cls])
            getattr(m, cls)
            imported.append(f"[opt]{mod.split('.')[-1]}")
        except ImportError:
            pass   # Opcional, no cuenta como fallo
        except Exception as e:
            failed.append(f"[opt]{mod}:{e}")

    if failed:
        raise ImportError("; ".join(failed))

    return f"{len(imported)} módulos OK: {', '.join(imported)}"


def _test_edge_tts() -> str:
    try:
        import edge_tts
        return f"edge-tts {edge_tts.__version__} disponible"
    except ImportError:
        raise ImportError("edge-tts no instalado (pip install edge-tts)")


def _test_web_app() -> str:
    """Verifica que FastAPI y el app de NEXUS se pueden importar."""
    import importlib
    fastapi = importlib.import_module("fastapi")
    jinja2  = importlib.import_module("jinja2")
    uvicorn = importlib.import_module("uvicorn")
    # Verificar que templates existen
    tpl_dir = ROOT / "web" / "templates"
    templates = list(tpl_dir.glob("*.html"))
    assert templates, "No hay templates HTML en web/templates/"
    return f"FastAPI {fastapi.__version__} · {len(templates)} templates"


def action_test(config: dict, db) -> None:
    """Test completo de todos los componentes del sistema."""
    console.print(Panel(
        "[bold]Ejecutando test de componentes NEXUS...[/]",
        border_style="cyan",
        title="[bold cyan]TEST SUITE[/]",
    ))

    tests = [
        ("Context",           lambda: _test_context()),
        ("Database (SQLite)", lambda: _test_database()),
        ("Config YAML",       lambda: _test_config(config)),
        ("LLM Client",        lambda: _test_llm_client(config)),
        ("CoinGecko API",     lambda: _test_coingecko()),
        ("RSS feedparser",    lambda: _test_rss_feedparser()),
        ("Agent Imports",     lambda: _test_agent_imports()),
        ("edge-tts",          lambda: _test_edge_tts()),
        ("Web App (FastAPI)", lambda: _test_web_app()),
    ]

    table = Table(
        title="Resultados del Test",
        box=box.ROUNDED,
        border_style="#F7931A",
        show_header=True,
    )
    table.add_column("Componente",  style="bold",  min_width=22)
    table.add_column("Estado",      min_width=10)
    table.add_column("Detalle",     style="dim")

    ok_count = 0
    results  = []

    for name, test_fn in tests:
        try:
            detail = test_fn()
            results.append((name, "OK", detail))
            ok_count += 1
        except Exception as exc:
            results.append((name, "ERROR", str(exc)))

    for name, status, detail in results:
        if status == "OK":
            status_str = "[green]OK[/]"
        else:
            status_str = "[red]FAIL[/]"
        table.add_row(name, status_str, str(detail)[:80])

    console.print(table)

    total = len(results)
    if ok_count == total:
        console.print(f"\n[bold green]Todos los tests pasaron ({ok_count}/{total})[/]")
    else:
        console.print(f"\n[bold yellow]{ok_count}/{total} tests OK · {total - ok_count} fallaron[/]")


# ── Menú interactivo ────────────────────────────────────────────────────────────

def show_menu() -> None:
    console.print(BANNER)
    console.print(Panel(
        MENU.strip(),
        title="[bold #F7931A]MENÚ PRINCIPAL[/bold #F7931A]",
        border_style="#F7931A",
        box=box.ROUNDED,
    ))


def interactive_menu(config: dict, db) -> None:
    show_menu()

    while True:
        try:
            choice = Prompt.ask(
                "[bold #F7931A]NEXUS >[/]",
                choices=["1", "2", "3", "4", "5", "6"],
                default="1",
            )

            if choice == "1":
                action_create_video(config, db)
            elif choice == "2":
                action_urgent(config, db)
            elif choice == "3":
                action_web_panel(config)
            elif choice == "4":
                action_system_status(config, db)
            elif choice == "5":
                action_test(config, db)
            elif choice == "6":
                console.print("[dim]Hasta luego. · NEXUS v1.0[/]")
                break

            # Re-mostrar menú tras cada acción
            console.print(Panel(MENU.strip(), border_style="#F7931A", box=box.ROUNDED))

        except KeyboardInterrupt:
            console.print("\n[dim]Ctrl+C recibido. Saliendo...[/]")
            break
        except Exception as exc:
            console.print(f"[red bold]Error inesperado:[/] {exc}")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="NEXUS v1.0 · CryptoVerdad · Motor autónomo de contenido crypto",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  python main.py --topic 'Bitcoin ATH' --mode standard\n"
            "  python main.py --topic 'Hack Bybit' --mode urgente\n"
            "  python main.py --server\n"
            "  python main.py --test\n"
        ),
    )
    parser.add_argument(
        "--topic", "--tema",
        dest="topic",
        type=str,
        help="Tema del vídeo (lanza pipeline directamente)",
    )
    parser.add_argument(
        "--mode", "--modo",
        dest="mode",
        type=str,
        default="standard",
        choices=["standard", "urgente", "short", "analisis", "opinion", "tutorial"],
        help="Modo de producción (default: standard)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Genera guión/audio/vídeo pero NO publica en ninguna plataforma",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Iniciar únicamente el panel web (puerto 8080)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Ejecutar test completo de todos los componentes",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Modo servidor Railway: panel web (8080) + scheduler automático KAIROS",
    )
    return parser.parse_args()


# ── Modo automático Railway ──────────────────────────────────────────────────────

def action_auto(config: dict, db) -> None:
    """Modo servidor: panel web en hilo principal + KAIROS scheduler en background."""
    import threading
    import time
    from datetime import datetime

    def _scheduler_loop() -> None:
        """Bucle KAIROS: lanza pipeline en la hora óptima del día."""
        from agents.mind.kairos import Kairos
        from core.nexus_core import NexusCore

        kairos = Kairos(config, db)
        console.print("[bold #F7931A]KAIROS scheduler activo[/]")

        while True:
            try:
                next_run = kairos.schedule_next_publish()
                now = datetime.now()
                wait_sec = (next_run - now).total_seconds()

                if wait_sec > 60:
                    console.print(
                        f"[dim]KAIROS: próximo pipeline {next_run.strftime('%Y-%m-%d %H:%M')} "
                        f"(en {wait_sec/3600:.1f}h)[/]"
                    )
                    time.sleep(min(wait_sec - 60, 3600))
                    continue

                # Hora de producir
                console.print("[bold #F7931A]KAIROS: lanzando pipeline automático...[/]")
                nexus = NexusCore(config, db)
                ctx = nexus.run_pipeline("análisis crypto diario", "standard", dry_run=False)

                if ctx.has_errors():
                    console.print("[red]Pipeline automático con errores — ver logs.[/]")
                else:
                    console.print("[green]Pipeline automático completado.[/]")

                # Dormir 1h para no re-ejecutar el mismo slot
                time.sleep(3600)

            except Exception as exc:
                console.print(f"[red]KAIROS error:[/] {exc}")
                time.sleep(300)

    # KAIROS en hilo daemon (muere si el proceso principal muere)
    scheduler = threading.Thread(target=_scheduler_loop, daemon=True, name="kairos")
    scheduler.start()

    # Panel web en hilo principal (bloquea; Railway detecta /health)
    action_web_panel(config)


def main():
    args = parse_args()

    # ── Carga config ──────────────────────────────────────────────────────────
    try:
        config = load_config()
    except FileNotFoundError:
        console.print("[red bold]Error:[/] config.yaml no encontrado en el directorio de NEXUS.")
        sys.exit(1)
    except Exception as exc:
        console.print(f"[red bold]Error cargando config.yaml:[/] {exc}")
        sys.exit(1)

    # ── Init DB ───────────────────────────────────────────────────────────────
    try:
        db = get_db(config)
    except Exception as exc:
        console.print(f"[red bold]Error inicializando base de datos:[/] {exc}")
        sys.exit(1)

    # ── Modos directos via flags ───────────────────────────────────────────────
    if args.test:
        action_test(config, db)
        return

    if args.auto:
        action_auto(config, db)
        return

    if args.server:
        action_web_panel(config)
        return

    if args.topic and args.mode == "urgente":
        action_urgent(config, db, topic=args.topic)
        return

    if args.topic:
        action_create_video(
            config, db,
            topic=args.topic,
            mode=args.mode,
            dry_run=args.dry_run,
            interactive=False,
        )
        return

    # ── Menú interactivo ───────────────────────────────────────────────────────
    interactive_menu(config, db)


if __name__ == "__main__":
    main()
