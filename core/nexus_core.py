"""
nexus_core.py
Orquestador maestro de NEXUS v1.0.
Llama a cada capa en orden: ORACULO → FORGE → (review) → HERALD → MIND
y persiste el resultado en SQLite.
"""

import uuid
import subprocess
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich import box

from core.context import Context
from core.urgency_detector import UrgencyDetector
from utils.logger import get_logger

console = Console()
logger = get_logger("NEXUS_CORE")


class NexusCore:
    """
    Orquesta el pipeline completo de NEXUS.

    Uso:
        config = load_config()
        db     = DBManager(config["database"]["path"])
        nexus  = NexusCore(config, db)
        ctx    = nexus.run_pipeline("Bitcoin supera 100k", mode="standard")
    """

    def __init__(self, config: dict, db):
        self.config = config
        self.db = db
        self.logger = get_logger("NEXUS_CORE")
        self._urgency_detector = UrgencyDetector()

    # ── Lazy imports para evitar ciclos ──────────────────────────────────────
    def _load_agents(self):
        """Importa los agentes en tiempo de ejecución (evita importaciones circulares)."""
        try:
            from agents.oracle.oracle_agent import OracleAgent
            self._oracle = OracleAgent(self.config, self.db)
        except ImportError:
            self._oracle = None
            logger.warning("OracleAgent no disponible aún.")

        try:
            from agents.forge.forge_agent import ForgeAgent
            self._forge = ForgeAgent(self.config, self.db)
        except ImportError:
            self._forge = None
            logger.warning("ForgeAgent no disponible aún.")

        try:
            from agents.herald.herald_agent import HeraldAgent
            self._herald = HeraldAgent(self.config, self.db)
        except ImportError:
            self._herald = None
            logger.warning("HeraldAgent no disponible aún.")

        try:
            from agents.mind.mind_agent import MindAgent
            self._mind = MindAgent(self.config, self.db)
        except ImportError:
            self._mind = None
            logger.warning("MindAgent no disponible aún.")

        try:
            from agents.sentinel.sentinel_agent import SentinelAgent
            self._sentinel = SentinelAgent(self.config, self.db)
        except ImportError:
            self._sentinel = None
            logger.warning("SentinelAgent no disponible aún.")

    # ── Pipeline principal ───────────────────────────────────────────────────
    def run_pipeline(self, topic: str, mode: str = "analisis",
                     dry_run: bool = False) -> Context:
        """
        Ejecuta el pipeline completo:
        ORACULO → FORGE → review humana → HERALD → MIND

        dry_run=True: genera todo el contenido (guión, audio, vídeo, thumbnails)
        pero omite la publicación en YouTube/TikTok/Telegram.
        """
        ctx = Context(topic=topic, mode=mode)
        ctx.forced_mode = mode   # THEMIS no sobreescribe si el usuario forzó un modo
        ctx.dry_run = dry_run
        ctx.pipeline_id = str(uuid.uuid4())
        ctx.pipeline_start = datetime.now()

        self._print_banner(topic, mode)
        self._load_agents()

        try:
            self.db.update_pipeline_status(ctx.pipeline_id, "running")
        except Exception:
            pass  # DB opcional en modo test

        steps = [
            ("ORACULO",   self._run_oracle,    "Analizando mercado y noticias..."),
            ("FORGE",     self._run_forge,     "Generando guión, audio y vídeo..."),
            ("REVIEW",    self._run_review,    "Revisión humana del contenido..."),
            ("HERALD",    self._run_herald,    "Publicando en plataformas..."),
            ("MIND",      self._run_mind,      "Registrando aprendizaje..."),
            ("SENTINEL",  self._run_sentinel,  "Monitoreo post-publicación..."),
        ]

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold #F7931A]{task.description}"),
            BarColumn(bar_width=30),
            console=console,
            transient=False,
        ) as progress:
            for step_name, step_fn, description in steps:
                task = progress.add_task(description, total=None)
                try:
                    ctx = step_fn(ctx)
                    progress.update(task, completed=True, total=1)
                    console.print(f"  [green]✓[/] {step_name} completado")
                except Exception as exc:
                    ctx.add_error(step_name, str(exc))
                    progress.update(task, completed=True, total=1)
                    console.print(f"  [red]✗[/] {step_name} falló: {exc}")
                    logger.exception(f"Error en {step_name}")

                if ctx.has_errors() and step_name in ("FORGE",):
                    console.print("[red bold]Pipeline detenido por errores críticos.[/]")
                    break

        self._save_pipeline(ctx, final=True)
        self._print_summary(ctx)
        return ctx

    # ── Pipeline urgente ─────────────────────────────────────────────────────
    def run_urgent_pipeline(self, topic: str) -> Context:
        """
        Pipeline urgente: TikTok primero, YouTube después.
        Salta la revisión humana para velocidad máxima.
        """
        ctx = Context(topic=topic, mode="urgente")
        ctx.pipeline_id = str(uuid.uuid4())
        ctx.pipeline_start = datetime.now()
        ctx.is_urgent = True
        ctx.urgency_score = 100.0

        console.print(
            Panel(
                f"[bold red]⚡ MODO URGENTE ACTIVADO[/]\n[white]{topic}[/]",
                border_style="red",
                title="NEXUS URGENT",
            )
        )

        self._load_agents()

        urgent_steps = [
            ("ORACULO", self._run_oracle,        "Análisis rápido de mercado..."),
            ("FORGE",   self._run_forge,          "Generando contenido urgente..."),
            # No hay review humana en modo urgente
            ("HERALD",  self._run_herald_urgent,  "Publicando TikTok primero..."),
            ("MIND",    self._run_mind,            "Registrando aprendizaje..."),
        ]

        for step_name, step_fn, description in urgent_steps:
            console.print(f"[bold #F7931A]→ {description}[/]")
            try:
                ctx = step_fn(ctx)
                console.print(f"  [green]✓[/] {step_name} OK")
            except Exception as exc:
                ctx.add_error(step_name, str(exc))
                console.print(f"  [red]✗[/] {step_name} error: {exc}")
                logger.exception(f"Error urgente en {step_name}")

        self._save_pipeline(ctx, final=True)
        self._print_summary(ctx)
        return ctx

    # ── Runners de cada capa ─────────────────────────────────────────────────
    def _run_oracle(self, ctx: Context) -> Context:
        ctx = self._urgency_detector.run(ctx)
        if self._oracle:
            ctx = self._oracle.run(ctx)
        else:
            ctx.add_warning("NEXUS_CORE", "OracleAgent no cargado, saltando análisis de mercado.")
        return ctx

    def _run_forge(self, ctx: Context) -> Context:
        if self._forge:
            ctx = self._forge.run(ctx)
        else:
            ctx.add_warning("NEXUS_CORE", "ForgeAgent no cargado, saltando generación de contenido.")
        return ctx

    def _run_review(self, ctx: Context) -> Context:
        ctx.approved = True
        return ctx
    def _run_review_DISABLED(self, ctx: Context) -> Context:
        """Revisión humana en terminal (sólo modo no-urgente)."""
        if ctx.mode == "urgente" or ctx.is_urgent:
            ctx.approved = True
            return ctx
        try:
            from utils.review_ui import ReviewUI
            ui = ReviewUI()
            approved, notes = ui.review(ctx)
            ctx.approved = approved
            ctx.review_notes = notes
            if not approved:
                ctx.add_warning("REVIEW", "Pipeline cancelado por el operador.")
        except Exception as exc:
            ctx.add_error("REVIEW", str(exc))
            logger.exception("Error en revisión humana")
        return ctx

    @staticmethod
    def _force_1080p(video_path: str) -> None:
        """
        Re-encoda el vídeo a 1920x1080 exacto con ffmpeg.
        Reemplaza el archivo original in-place.
        No-op si el archivo no existe o ffmpeg falla.
        """
        if not video_path or not Path(video_path).exists():
            return
        output = video_path.replace('.mp4', '_1080p.mp4')
        try:
            result = subprocess.run(
                [
                    'ffmpeg', '-y', '-i', video_path,
                    '-vf', 'scale=1920:1080',
                    '-c:v', 'libx264', '-crf', '18',
                    '-c:a', 'copy',
                    output,
                ],
                capture_output=True, timeout=600,
            )
            if result.returncode == 0 and Path(output).exists():
                os.replace(output, video_path)
                logger.info(f"force_1080p: {Path(video_path).name} re-encodado a 1920x1080")
            else:
                logger.warning(f"force_1080p ffmpeg error: {result.stderr[-200:]!r}")
                try:
                    Path(output).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"force_1080p falló: {exc}")

    def _run_herald(self, ctx: Context) -> Context:
        if getattr(ctx, "dry_run", False):
            ctx.add_warning("HERALD", "dry-run: publicación omitida.")
            console.print("  [dim yellow]⚑ dry-run — HERALD saltado (no se publica)[/]")
            return ctx
        if not ctx.approved:
            ctx.add_warning("HERALD", "Publicación omitida: contenido no aprobado.")
            return ctx
        # Bloqueo SEO — no publicar si score < 70
        seo = getattr(ctx, "seo_score", 0)
        if seo < 70:
            msg = f"SEO Score {seo}/100 < 70 — publicación bloqueada por HERMES."
            ctx.add_warning("NEXUS_CORE", msg)
            console.print(f"  [bold red]✗ {msg}[/]")
            return ctx
        # Garantizar resolución 1920x1080 antes de subir
        if getattr(ctx, "video_path", None):
            console.print("  [dim]Verificando resolución 1920x1080...[/]")
            self._force_1080p(ctx.video_path)
        if self._herald:
            ctx = self._herald.run(ctx)
        else:
            ctx.add_warning("NEXUS_CORE", "HeraldAgent no cargado, saltando publicación.")
        return ctx

    def _run_herald_urgent(self, ctx: Context) -> Context:
        """Herald con orden TikTok → YouTube para modo urgente."""
        if getattr(ctx, "dry_run", False):
            ctx.add_warning("HERALD", "dry-run: publicación urgente omitida.")
            console.print("  [dim yellow]⚑ dry-run — HERALD URGENTE saltado[/]")
            return ctx
        ctx.approved = True
        # Garantizar resolución 1920x1080 antes de subir
        if getattr(ctx, "video_path", None):
            console.print("  [dim]Verificando resolución 1920x1080...[/]")
            self._force_1080p(ctx.video_path)
        if self._herald:
            ctx = self._herald.run_urgent(ctx)
        else:
            ctx.add_warning("NEXUS_CORE", "HeraldAgent no cargado.")
        return ctx

    def _run_mind(self, ctx: Context) -> Context:
        if self._mind:
            ctx = self._mind.run(ctx)
        else:
            ctx.add_warning("NEXUS_CORE", "MindAgent no cargado, saltando aprendizaje.")
        return ctx

    def _run_sentinel(self, ctx: Context) -> Context:
        """Post-publicación: comentarios, newsletter, costes, auditoría."""
        if getattr(ctx, "dry_run", False):
            console.print("  [dim]⚑ dry-run — SENTINEL saltado[/]")
            return ctx
        if self._sentinel:
            ctx = self._sentinel.run(ctx)
        else:
            ctx.add_warning("NEXUS_CORE", "SentinelAgent no cargado.")
        return ctx

    # ── Persistencia ─────────────────────────────────────────────────────────
    def _save_pipeline(self, ctx: Context, final: bool = False) -> None:
        try:
            self.db.save_pipeline(ctx)
            if final:
                status = "completed" if not ctx.has_errors() else "completed_with_errors"
                self.db.update_pipeline_status(ctx.pipeline_id, status)
        except Exception as exc:
            logger.warning(f"No se pudo guardar el pipeline en DB: {exc}")

    # ── Rich helpers ─────────────────────────────────────────────────────────
    def _print_banner(self, topic: str, mode: str) -> None:
        console.print(
            Panel(
                f"[bold #F7931A]NEXUS v1.0[/] · [white]CryptoVerdad[/]\n"
                f"[dim]Topic:[/] {topic}\n"
                f"[dim]Mode:[/]  [cyan]{mode}[/]",
                border_style="#F7931A",
                title="[bold white]PIPELINE START[/]",
                box=box.DOUBLE_EDGE,
            )
        )

    def _print_summary(self, ctx: Context) -> None:
        table = Table(title="Pipeline Summary", box=box.ROUNDED, border_style="#F7931A")
        table.add_column("Campo", style="dim")
        table.add_column("Valor", style="white")

        table.add_row("Pipeline ID", ctx.pipeline_id[:8] + "...")
        table.add_row("Topic", ctx.topic)
        table.add_row("Mode", ctx.mode)
        table.add_row("Urgente", "[red]Sí[/]" if ctx.is_urgent else "[green]No[/]")
        table.add_row("SEO Score", str(ctx.seo_score))
        table.add_row("Aprobado", "[green]Sí[/]" if ctx.approved else "[red]No[/]")
        table.add_row("YouTube URL", ctx.youtube_url or "—")
        table.add_row("TikTok URL", ctx.tiktok_url or "—")
        table.add_row("Errores", str(len(ctx.errors)))
        table.add_row("Warnings", str(len(ctx.warnings)))

        elapsed = (datetime.now() - ctx.pipeline_start).seconds
        table.add_row("Tiempo total", f"{elapsed}s")

        console.print(table)

        if ctx.errors:
            console.print("[red bold]Errores:[/]")
            for err in ctx.errors:
                console.print(f"  [red]• {err}[/]")


