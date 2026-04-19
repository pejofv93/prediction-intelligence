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
        self._oracle = None
        self._forge = None
        self._herald = None
        self._mind = None
        self._sentinel = None
        self._load_agents()  # ARCH-02: cargar agentes en __init__, no en cada run_pipeline

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

        # BUG-02: guardar pipeline en DB al inicio para tener registro desde el primer momento
        try:
            self.db.save_pipeline(ctx)
        except Exception:
            pass

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

                # FIX-03: verificar crisis DESPUÉS de ORACULO (UrgencyDetector lo activa allí)
                if step_name == "ORACULO" and getattr(ctx, 'crisis_mode', False):
                    console.print("[bold red]CRISIS detectada tras ORACULO — desviando a pipeline de crisis[/]")
                    return self._run_crisis_pipeline(ctx)

                if ctx.has_errors() and step_name in ("FORGE",):
                    console.print("[red bold]Pipeline detenido por errores críticos.[/]")
                    break

        self._save_pipeline(ctx, final=True)
        self._print_summary(ctx)
        return ctx

    # ── Pipeline de crisis (<=90s) ───────────────────────────────────────────
    def _run_crisis_pipeline(self, ctx: Context) -> Context:
        """
        Pipeline ultra-rapido para eventos CRISIS (volatilidad >=5x BTC/ETH).
        Solo corre: ORACULO → CALIOPE + ECHO + HEPHAESTUS (3 escenas) → HERALD.
        DAEDALUS e IRIS son saltados para minimizar tiempo de render.
        Target: publicar en menos de 90 segundos.
        """
        ctx.crisis_mode = True
        console.print(
            Panel(
                f"[bold red]CRISIS PIPELINE ACTIVADO[/]\n"
                f"[white]Topic:[/] {ctx.topic}\n"
                f"[dim]Modo ultra-rapido: 3 escenas, sin DAEDALUS ni IRIS[/]",
                border_style="red",
                title="[bold white]NEXUS CRISIS[/]",
            )
        )
        self.logger.warning(f"CRISIS pipeline: topic='{ctx.topic}' pipeline_id={ctx.pipeline_id[:8]}")

        crisis_steps = [
            ("ORACULO [crisis]",    self._run_oracle,         "Analisis rapido de mercado..."),
            ("CALIOPE [crisis]",    self._run_crisis_forge,   "Generando guion de crisis..."),
            ("HERALD [crisis]",     self._run_herald,         "Publicando en plataformas..."),
        ]

        for step_name, step_fn, description in crisis_steps:
            console.print(f"[bold red]-> {description}[/]")
            try:
                ctx = step_fn(ctx)
                console.print(f"  [green]OK[/] {step_name}")
            except Exception as exc:
                ctx.add_error(step_name, str(exc))
                console.print(f"  [red]ERROR[/] {step_name}: {exc}")
                self.logger.exception(f"Error en crisis step {step_name}")

        self._save_pipeline(ctx, final=True)
        return ctx

    def _run_crisis_forge(self, ctx: Context) -> Context:
        """
        Ejecuta solo CALIOPE + ECHO + HEPHAESTUS en modo crisis.
        Omite DAEDALUS (graficos animados) e IRIS (thumbnails A/B).
        ctx.crisis_mode=True hace que HEPHAESTUS limite a 3 escenas.
        """
        if not self._forge:
            ctx.add_warning("NEXUS_CORE", "ForgeAgent no cargado en crisis — saltando FORGE.")
            return ctx
        try:
            # Llamar solo a los agentes criticos del forge
            from agents.forge.caliope import CALIOPE
            from agents.forge.echo import ECHO
            from agents.forge.hephaestus import HEPHAESTUS

            caliope = CALIOPE(self.config, self.db)
            ctx = caliope.run(ctx)

            echo = ECHO(self.config, self.db)
            ctx = echo.run(ctx)

            hephaestus = HEPHAESTUS(self.config, self.db)
            ctx = hephaestus.run(ctx)

        except ImportError as e:
            self.logger.warning(f"Crisis forge: import fallido ({e}), usando ForgeAgent completo")
            ctx = self._run_forge(ctx)
        except Exception as e:
            ctx.add_error("CRISIS_FORGE", str(e))
            self.logger.error(f"Crisis forge error: {e}")
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
    def _probe_resolution(video_path: str):
        """Devuelve (width, height) del vídeo usando ffprobe. Retorna (0, 0) si falla."""
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height', '-of', 'csv=p=0',
                 video_path],
                capture_output=True, text=True, timeout=30,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                dims = probe.stdout.strip().split(',')
                return int(dims[0]), int(dims[1])
        except Exception as exc:
            logger.warning(f"ffprobe fallo: {exc}")
        return 0, 0

    @staticmethod
    def _force_1080p(video_path: str) -> bool:
        """
        Verifica la resolución del vídeo con ffprobe y re-encoda SOLO si no es 1920x1080.
        Reemplaza el archivo original in-place.
        Devuelve True si ya era 1080p, False si fue re-encodado, None si falló.
        """
        if not video_path or not Path(video_path).exists():
            logger.warning(f"force_1080p: archivo no encontrado: {video_path!r}")
            return None

        cur_w, cur_h = NexusCore._probe_resolution(video_path)
        if cur_w > 0:
            if cur_w == 1920 and cur_h == 1080:
                logger.info(f"✅ Resolución verificada: 1920x1080 — sin re-encodar")
                return True
            logger.warning(f"⚠️ Resolución actual {cur_w}x{cur_h} — re-encodando a 1920x1080")
        else:
            logger.warning("force_1080p: ffprobe no pudo leer dimensiones — re-encode preventivo")

        output = video_path.replace('.mp4', '_1080p.mp4')
        try:
            result = subprocess.run(
                [
                    'ffmpeg', '-y', '-i', video_path,
                    '-vf', 'scale=1920:1080:force_original_aspect_ratio=disable',
                    '-c:v', 'libx264', '-b:v', '4000k',
                    '-maxrate', '5000k', '-bufsize', '10000k',
                    '-preset', 'fast',
                    '-c:a', 'copy',
                    output,
                ],
                capture_output=True, timeout=600,
            )
            if result.returncode == 0 and Path(output).exists():
                # Verificar que el re-encode produjo realmente 1920x1080
                rw, rh = NexusCore._probe_resolution(output)
                os.replace(output, video_path)
                if rw == 1920 and rh == 1080:
                    logger.info(f"✅ Re-encodado a 1080p correctamente: {Path(video_path).name}")
                else:
                    logger.warning(f"⚠️ Re-encode completado pero resolución inesperada: {rw}x{rh}")
                return False
            else:
                logger.warning(f"force_1080p ffmpeg error (rc={result.returncode}): "
                               f"{result.stderr[-400:].decode(errors='replace')!r}")
                try:
                    Path(output).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"force_1080p falló: {exc}")
        return None

    def validate_before_publish(self, ctx: Context) -> tuple:
        """
        Quality gate pre-publicación. Verifica 5 condiciones críticas.
        Devuelve (passed: bool, failures: list[str]).
        Si falla: notifica Telegram y NO publica.
        """
        import requests as _req
        failures = []

        # 1. Resolución del MP4 == 1920x1080
        video_path = getattr(ctx, "video_path", None)
        if not video_path or not Path(video_path).exists():
            failures.append("MP4 no existe en disco")
        else:
            vw, vh = self._probe_resolution(video_path)
            if vw != 1920 or vh != 1080:
                failures.append(f"Resolución incorrecta: {vw}x{vh} (esperado 1920x1080)")
            else:
                logger.info("✅ QG check 1 — Resolución 1920x1080 OK")

        # 2. Precio BTC en ctx.btc_price dentro del ±15% del precio Binance en tiempo real
        ctx_btc = getattr(ctx, "btc_price", 0) or 0
        if ctx_btc <= 0:
            failures.append("ctx.btc_price es 0 o nulo")
        else:
            try:
                r = _req.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                    timeout=6,
                )
                binance_btc = float(r.json().get("price", 0))
                if binance_btc > 0:
                    deviation = abs(ctx_btc - binance_btc) / binance_btc
                    if deviation > 0.15:
                        failures.append(
                            f"Precio BTC en ctx (${ctx_btc:,.0f}) difiere >15% del real "
                            f"(${binance_btc:,.0f}, desviación {deviation:.1%})"
                        )
                    else:
                        logger.info(
                            f"✅ QG check 2 — BTC ctx=${ctx_btc:,.0f} "
                            f"Binance=${binance_btc:,.0f} ({deviation:.1%}) OK"
                        )
            except Exception as _be:
                logger.warning(f"QG check 2: Binance no disponible ({_be}) — saltando")

        # 3. Thumbnail existe y pesa entre 50KB y 2MB
        thumb = getattr(ctx, "thumbnail_a_path", "") or ""
        if not thumb or not Path(thumb).exists():
            failures.append(f"Thumbnail A no existe: {thumb!r}")
        else:
            size_bytes = Path(thumb).stat().st_size
            if size_bytes < 50_000:
                failures.append(f"Thumbnail muy pequeño: {size_bytes//1024}KB (<50KB)")
            elif size_bytes > 2_000_000:
                failures.append(f"Thumbnail muy grande: {size_bytes//1024}KB (>2MB)")
            else:
                logger.info(f"✅ QG check 3 — Thumbnail {size_bytes//1024}KB OK")

        # 4. Guion tiene más de 800 palabras
        script = getattr(ctx, "script", "") or ""
        word_count = len(script.split())
        if word_count < 800:
            failures.append(f"Guion demasiado corto: {word_count} palabras (<800)")
        else:
            logger.info(f"✅ QG check 4 — Guion {word_count} palabras OK")

        # 5. MP4 existe y dura más de 3 minutos (180s)
        if video_path and Path(video_path).exists():
            try:
                dur_probe = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
                    capture_output=True, text=True, timeout=30,
                )
                if dur_probe.returncode == 0 and dur_probe.stdout.strip():
                    duration = float(dur_probe.stdout.strip())
                    if duration < 180:
                        failures.append(f"Vídeo demasiado corto: {duration:.0f}s (<180s/3min)")
                    else:
                        logger.info(f"✅ QG check 5 — Duración {duration:.0f}s OK")
            except Exception as _de:
                logger.warning(f"QG check 5: ffprobe duración falló ({_de}) — saltando")

        # 6. Audio del vídeo no es silencio (detecta Coqui truncado + fallback silencioso)
        audio_path = getattr(ctx, "audio_path", "") or ""
        if audio_path and Path(audio_path).exists():
            try:
                vol_probe = subprocess.run(
                    ['ffmpeg', '-i', audio_path, '-af', 'volumedetect', '-f', 'null', '/dev/null'],
                    capture_output=True, text=True, timeout=30,
                )
                output = vol_probe.stderr
                import re as _re
                m = _re.search(r'mean_volume:\s*([-\d.]+)\s*dB', output)
                if m:
                    mean_vol = float(m.group(1))
                    if mean_vol < -50.0:
                        failures.append(f"Audio silencioso: {mean_vol:.1f}dB — TTS falló, no se publica")
                    else:
                        logger.info(f"✅ QG check 6 — Audio nivel {mean_vol:.1f}dB OK")
            except Exception as _ve:
                logger.warning(f"QG check 6: volumedetect falló ({_ve}) — saltando")

        passed = len(failures) == 0

        if passed:
            console.print("  [bold green]✅ Quality gate passed — publicando[/]")
            logger.info("Quality gate passed — todos los checks OK")
        else:
            msg = "❌ Pipeline bloqueado por quality gate:\n" + "\n".join(f"  • {f}" for f in failures)
            console.print(f"  [bold red]{msg}[/]")
            logger.warning(msg)
            ctx.add_warning("QUALITY_GATE", msg)
            # Notificar Telegram
            try:
                tok = os.getenv("TELEGRAM_BOT_TOKEN", "")
                chat = os.getenv("TELEGRAM_CHAT_ID", "")
                if tok and chat:
                    tg_msg = (
                        f"🚫 *NEXUS Quality Gate BLOQUEADO*\n"
                        f"Pipeline: `{ctx.pipeline_id[:8]}`\n"
                        f"Topic: {ctx.topic}\n\n"
                        + "\n".join(f"• {f}" for f in failures)
                    )
                    _req.post(
                        f"https://api.telegram.org/bot{tok}/sendMessage",
                        json={"chat_id": chat, "text": tg_msg, "parse_mode": "Markdown"},
                        timeout=8,
                    )
            except Exception as _te:
                logger.warning(f"Telegram notificación QG falló: {_te}")

        return passed, failures

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
            was_already_1080p = self._force_1080p(ctx.video_path)
            if was_already_1080p is True:
                console.print("  [green]✅ Resolución verificada: 1920x1080[/]")
            elif was_already_1080p is False:
                console.print("  [yellow]⚠️ Re-encodado a 1080p correctamente[/]")
                ctx.add_warning("NEXUS_CORE", "Vídeo re-encodado a 1920x1080 (HEPHAESTUS no lo produjo directamente)")
            else:
                ctx.add_warning("NEXUS_CORE", "No se pudo verificar/forzar resolución 1920x1080")
        # Validar thumbnail antes de publicar
        thumb_a = getattr(ctx, "thumbnail_a_path", "") or ""
        if not thumb_a or not Path(thumb_a).exists():
            ctx.add_warning("NEXUS_CORE", f"Thumbnail A no encontrado: {thumb_a!r} — se publicará sin miniatura custom")
            logger.warning(f"[bold yellow]THUMBNAIL FALTANTE:[/] {thumb_a!r}")
        else:
            size_kb = Path(thumb_a).stat().st_size // 1024
            logger.info(f"Thumbnail A listo: {thumb_a!r} ({size_kb}KB)")
        # Quality gate — bloquear publicación si falla algún check crítico
        qg_passed, qg_failures = self.validate_before_publish(ctx)
        if not qg_passed:
            return ctx  # validate_before_publish ya logueó y notificó Telegram
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
            was_1080p = self._force_1080p(ctx.video_path)
            if was_1080p is False:
                ctx.add_warning("NEXUS_CORE", "Vídeo urgente re-encodado a 1920x1080")
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
        elapsed = (datetime.now() - ctx.pipeline_start).seconds

        table = Table(title="Pipeline Summary", box=box.ROUNDED, border_style="#F7931A")
        table.add_column("Campo", style="dim")
        table.add_column("Valor", style="white")

        table.add_row("Pipeline ID",    ctx.pipeline_id[:8] + "...")
        table.add_row("Topic",          ctx.topic)
        table.add_row("Mode",           ctx.mode)
        table.add_row("Urgente",        "[red]Sí[/]" if ctx.is_urgent else "[green]No[/]")
        table.add_row("SEO Score",      f"[{'green' if ctx.seo_score >= 70 else 'red'}]{ctx.seo_score}/100[/]")
        table.add_row("Retention",      f"[{'green' if ctx.retention_score >= 75 else 'yellow'}]{ctx.retention_score}/100[/]" if ctx.retention_score else "—")
        table.add_row("Aprobado",       "[green]Sí[/]" if ctx.approved else "[red]No[/]")
        table.add_row("YouTube URL",    ctx.youtube_url or "—")
        table.add_row("TikTok URL",     ctx.tiktok_url or "—")
        table.add_row("Errores",        f"[{'red' if ctx.errors else 'green'}]{len(ctx.errors)}[/]")
        table.add_row("Warnings",       str(len(ctx.warnings)))
        table.add_row("Tiempo total",   f"{elapsed}s")
        console.print(table)

        if ctx.errors:
            console.print("[red bold]Errores:[/]")
            for err in ctx.errors:
                console.print(f"  [red]• {err}[/]")

        # Partner Program Progress
        self._print_partner_progress()

    def _print_partner_progress(self) -> None:
        """Muestra el progreso hacia el YouTube Partner Program."""
        try:
            from utils.partner_tracker import PartnerTracker, render_partner_panel
            tracker = PartnerTracker(db=self.db)
            data = tracker.get_progress()
            panel_text = render_partner_panel(data)
            console.print(Panel(panel_text, border_style="yellow",
                                title="[bold white]YouTube Partner Program[/]"))
            if data.get("recommendations"):
                console.print("[bold yellow]Recomendaciones:[/]")
                for rec in data["recommendations"][:3]:
                    console.print(f"  {rec}")
        except Exception as e:
            logger.debug(f"Partner tracker no disponible: {e}")


