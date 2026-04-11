"""
review_ui.py
Interfaz rich en terminal para revisión humana del guión antes de publicar.
"""

import os
import tempfile
import subprocess
from typing import Tuple

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.prompt import Prompt
from rich import box

from utils.logger import get_logger

console = Console()
logger = get_logger("REVIEW_UI")


class ReviewUI:
    """
    Presenta el guión al operador y solicita [A]probar / [E]ditar / [C]ancelar.
    Si edita, abre un editor de texto y re-valida el SEO score.
    """

    # Score mínimo requerido para aprobar automáticamente
    MIN_SEO_SCORE = 70

    def review(self, ctx) -> Tuple[bool, str]:
        """
        Muestra la revisión interactiva del guión.

        Returns:
            (approved: bool, notes: str)
        """
        try:
            self._print_script(ctx)
            self._print_seo_table(ctx)
            return self._ask_action(ctx)
        except KeyboardInterrupt:
            console.print("\n[yellow]Revisión interrumpida por el usuario.[/]")
            return False, "Interrumpido por el usuario."
        except Exception as exc:
            logger.exception("Error en ReviewUI.review()")
            return False, f"Error en revisión: {exc}"

    # ── Display helpers ───────────────────────────────────────────────────────
    def _print_script(self, ctx) -> None:
        script_display = ctx.script if ctx.script else "[dim](sin guión generado)[/dim]"
        console.print(
            Panel(
                script_display,
                title=f"[bold #F7931A]GUIÓN · {ctx.seo_title or ctx.topic}[/]",
                border_style="#F7931A",
                padding=(1, 2),
                box=box.ROUNDED,
            )
        )

    def _print_seo_table(self, ctx) -> None:
        score = ctx.seo_score
        score_color = "green" if score >= self.MIN_SEO_SCORE else "red"

        table = Table(title="SEO Analysis", box=box.SIMPLE_HEAVY, border_style="dim")
        table.add_column("Campo", style="dim", width=20)
        table.add_column("Valor")

        table.add_row("Score", f"[bold {score_color}]{score}/100[/]")
        table.add_row(
            "Título",
            f"{ctx.seo_title or '—'} [dim]({len(ctx.seo_title)} chars)[/dim]",
        )
        desc_words = len(ctx.seo_description.split()) if ctx.seo_description else 0
        table.add_row(
            "Descripción",
            f"[dim]{desc_words} palabras[/dim]",
        )
        table.add_row("Tags", ", ".join(ctx.seo_tags) if ctx.seo_tags else "—")
        table.add_row(
            "Aviso legal",
            "[green]Incluido[/]" if ctx.legal_warning_added else "[red]Falta[/]",
        )

        console.print(table)

    # ── Flujo de acción ───────────────────────────────────────────────────────
    def _ask_action(self, ctx) -> Tuple[bool, str]:
        while True:
            console.print(
                "\n[bold]¿Qué deseas hacer?[/]  "
                "[[bold green]A[/]]probar  "
                "[[bold yellow]E[/]]ditar  "
                "[[bold red]C[/]]ancelar"
            )
            choice = Prompt.ask(
                "[bold #F7931A]>>[/]",
                choices=["a", "e", "c", "A", "E", "C"],
                default="a",
            ).lower()

            if choice == "a":
                notes = Prompt.ask(
                    "Notas opcionales (Enter para omitir)", default=""
                )
                console.print("[green bold]✓ Contenido aprobado. Publicando...[/]")
                return True, notes

            elif choice == "e":
                new_script = self._open_editor(ctx.script)
                if new_script and new_script.strip():
                    ctx.script = new_script
                    console.print("[cyan]Guión actualizado.[/]")
                    # Re-validar SEO básico tras edición
                    self._revalidate_seo(ctx)
                    self._print_script(ctx)
                    self._print_seo_table(ctx)
                else:
                    console.print("[yellow]El guión editado está vacío. Se mantiene el original.[/]")

            elif choice == "c":
                confirm = Prompt.ask(
                    "[red]¿Seguro que deseas cancelar?[/] (s/n)", default="n"
                ).lower()
                if confirm == "s":
                    console.print("[red bold]Pipeline cancelado.[/]")
                    return False, "Cancelado por el operador."

    # ── Editor ────────────────────────────────────────────────────────────────
    def _open_editor(self, current_script: str) -> str:
        """Abre el script en el editor de texto del sistema y devuelve el contenido editado."""
        editor = os.environ.get("EDITOR", "nano")
        # Fallback para Windows
        if os.name == "nt":
            editor = os.environ.get("EDITOR", "notepad")

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(current_script)
                tmp_path = tmp.name

            console.print(f"[dim]Abriendo editor: {editor} {tmp_path}[/]")
            subprocess.call([editor, tmp_path])

            with open(tmp_path, "r", encoding="utf-8") as f:
                new_content = f.read()

            os.unlink(tmp_path)
            return new_content

        except Exception as exc:
            logger.error(f"No se pudo abrir el editor: {exc}")
            console.print(
                "[yellow]No se pudo abrir el editor automático. "
                "Edita el guión directamente en el archivo de salida.[/]"
            )
            return current_script

    # ── Re-validación SEO ─────────────────────────────────────────────────────
    def _revalidate_seo(self, ctx) -> None:
        """Recalcula un score SEO básico tras edición manual."""
        try:
            words = len(ctx.script.split())
            score = 0

            # Criterios básicos
            if words >= 200:
                score += 30
            elif words >= 100:
                score += 15

            if ctx.seo_title and len(ctx.seo_title) <= 60:
                score += 20

            if ctx.seo_tags and len(ctx.seo_tags) >= 5:
                score += 20

            desc_words = len(ctx.seo_description.split()) if ctx.seo_description else 0
            if desc_words >= 300:
                score += 30
            elif desc_words >= 150:
                score += 15

            ctx.seo_score = min(score, 100)
            logger.info(f"SEO re-validado tras edición: {ctx.seo_score}/100")
        except Exception as exc:
            logger.warning(f"No se pudo re-validar SEO: {exc}")
