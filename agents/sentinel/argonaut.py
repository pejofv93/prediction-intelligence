from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

"""
argonaut.py
ARGONAUT — Auditor del sistema NEXUS CryptoVerdad.

Responsabilidades:
  1. Marcar pipelines caducados (pending/running > 2h) como 'timeout'
  2. Archivar archivos huérfanos en output/ sin registro en BD (>7 días)
  3. Archivar thumbnails sin uso en assets/thumbnails/ (>30 días)
  4. Calcular estadísticas de salud del sistema (últimos 7 días)
  5. Limpiar learning_data antigua (>90 días) y hacer VACUUM periódico

Reglas:
  - Nunca borrar archivos, solo moverlos a output/archive/
  - Nunca crash silencioso
  - Todo output de terminal usa rich
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# ── Constantes ────────────────────────────────────────────────────────────────
PIPELINE_TIMEOUT_HOURS = 2
ORPHAN_FILE_DAYS = 7
THUMBNAIL_STALE_DAYS = 30
LEARNING_DATA_RETENTION_DAYS = 90
VACUUM_INTERVAL_DAYS = 7

_ORPHAN_EXTENSIONS = {".mp4", ".wav", ".png", ".mp3", ".srt"}

# Archivo que registra la última vez que se hizo VACUUM
_VACUUM_STAMP = Path(__file__).resolve().parent.parent.parent / ".argonaut_last_vacuum"


class ARGONAUT(BaseAgent):
    """
    Auditor del sistema NEXUS.
    Detecta contenido caducado, archivos huérfanos y mantiene la salud de la BD.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("ARGONAUT")
        self._root = Path(__file__).resolve().parent.parent.parent
        self._output_dir = self._root / "output"
        self._archive_dir = self._output_dir / "archive"
        self._thumbnails_dir = self._root / "assets" / "thumbnails"

    # ── Punto de entrada ──────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold cyan]ARGONAUT[/] iniciado — auditoría del sistema")
        try:
            self._ensure_archive_dirs()

            pipelines_fixed = self._fix_stale_pipelines()
            files_archived = self._archive_orphan_files()
            thumbs_archived = self._archive_stale_thumbnails()
            health = self._compute_health()
            db_cleaned = self._cleanup_database()

            total_archived = files_archived + thumbs_archived
            warnings = self._build_warnings(pipelines_fixed, total_archived, health)

            audit_result: Dict[str, Any] = {
                "pipelines_fixed": pipelines_fixed,
                "files_archived": total_archived,
                "db_cleaned": db_cleaned,
                "health_score": health["score"],
                "warnings": warnings,
            }
            ctx.metadata["argonaut_audit"] = audit_result
            ctx.metadata["argonaut_pipelines_fixed"] = pipelines_fixed
            ctx.metadata["argonaut_health"] = health

            self._print_report(audit_result, health)

        except Exception as exc:
            self.logger.error(f"[red]ARGONAUT error:[/] {exc}")
            ctx.add_error("ARGONAUT", str(exc))

        return ctx

    # ── 1. Pipelines caducados ────────────────────────────────────────────────
    def _fix_stale_pipelines(self) -> int:
        """Marca como 'timeout' los pipelines pending/running con >2h de antigüedad."""
        fixed = 0
        cutoff = datetime.now() - timedelta(hours=PIPELINE_TIMEOUT_HOURS)
        cutoff_iso = cutoff.isoformat()
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, status, created_at FROM pipelines
                    WHERE status IN ('pending', 'running')
                    AND created_at < ?
                    """,
                    (cutoff_iso,),
                ).fetchall()

                for row in rows:
                    conn.execute(
                        "UPDATE pipelines SET status='timeout' WHERE id=?",
                        (row["id"],),
                    )
                    self.logger.info(
                        f"[yellow]ARGONAUT[/] Pipeline {row['id'][:8]}... "
                        f"(status={row['status']}) marcado como timeout"
                    )
                    fixed += 1

            if fixed:
                self.logger.info(f"[yellow]ARGONAUT[/] {fixed} pipeline(s) marcados como timeout")
        except Exception as exc:
            self.logger.error(f"[red]ARGONAUT[/] error auditando pipelines: {exc}")
        return fixed

    # ── 2. Archivos huérfanos en output/ ──────────────────────────────────────
    def _archive_orphan_files(self) -> int:
        """Mueve a output/archive/ archivos de output/ sin registro en BD de >7 días."""
        if not self._output_dir.exists():
            return 0

        archived = 0
        cutoff = datetime.now() - timedelta(days=ORPHAN_FILE_DAYS)

        # Recopilar rutas conocidas en BD (videos y pipelines con rutas)
        known_paths = self._get_known_file_paths()

        # Directorios que NO se deben escanear para no crear bucles
        skip_dirs = {"archive", "latsync", "prometheus", "sadtalker", "sadtalker_test"}

        for file_path in self._output_dir.rglob("*"):
            # Saltar directorios y la propia carpeta archive
            if not file_path.is_file():
                continue
            if any(part in skip_dirs for part in file_path.parts):
                continue
            if file_path.suffix.lower() not in _ORPHAN_EXTENSIONS:
                continue

            # Comprobar antigüedad
            try:
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
            except OSError:
                continue
            if mtime >= cutoff:
                continue

            # Comprobar si está referenciado en la BD
            file_str = str(file_path)
            if any(known in file_str or file_str in known for known in known_paths):
                continue

            # Mover a archive manteniendo la sub-estructura relativa
            try:
                rel = file_path.relative_to(self._output_dir)
                dest = self._archive_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(file_path), str(dest))
                self.logger.info(f"[dim]ARGONAUT[/] Archivado huérfano: {rel}")
                archived += 1
            except Exception as exc:
                self.logger.error(f"[red]ARGONAUT[/] no se pudo archivar {file_path}: {exc}")

        return archived

    def _get_known_file_paths(self) -> List[str]:
        """Devuelve todas las rutas de archivos registradas en la BD."""
        paths: List[str] = []
        try:
            with self.db._connect() as conn:
                # videos tabla
                rows = conn.execute("SELECT url FROM videos WHERE url IS NOT NULL").fetchall()
                for r in rows:
                    if r["url"]:
                        paths.append(r["url"])

                # pipelines — no almacena rutas de archivo directamente,
                # pero recogemos youtube_url/tiktok_url como referencia
                rows2 = conn.execute(
                    "SELECT youtube_url, tiktok_url FROM pipelines "
                    "WHERE youtube_url IS NOT NULL OR tiktok_url IS NOT NULL"
                ).fetchall()
                for r in rows2:
                    if r["youtube_url"]:
                        paths.append(r["youtube_url"])
                    if r["tiktok_url"]:
                        paths.append(r["tiktok_url"])
        except Exception as exc:
            self.logger.error(f"[red]ARGONAUT[/] error obteniendo paths de BD: {exc}")
        return paths

    # ── 3. Thumbnails sin uso ─────────────────────────────────────────────────
    def _archive_stale_thumbnails(self) -> int:
        """Mueve thumbnails de assets/thumbnails/ sin referencia en BD y >30 días."""
        if not self._thumbnails_dir.exists():
            return 0

        archived = 0
        cutoff = datetime.now() - timedelta(days=THUMBNAIL_STALE_DAYS)
        thumb_archive = self._archive_dir / "thumbnails"
        thumb_archive.mkdir(parents=True, exist_ok=True)

        # Rutas referenciadas en BD (tabla videos: thumbnail_winner)
        referenced: List[str] = []
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT thumbnail_winner FROM videos WHERE thumbnail_winner IS NOT NULL"
                ).fetchall()
                for r in rows:
                    if r["thumbnail_winner"]:
                        referenced.append(r["thumbnail_winner"])
        except Exception as exc:
            self.logger.error(f"[red]ARGONAUT[/] error consultando thumbnails en BD: {exc}")

        for img_path in self._thumbnails_dir.rglob("*"):
            if not img_path.is_file():
                continue
            if img_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue

            try:
                mtime = datetime.fromtimestamp(img_path.stat().st_mtime)
            except OSError:
                continue
            if mtime >= cutoff:
                continue

            # Comprobar si está referenciado
            img_str = str(img_path)
            img_name = img_path.name
            if any(img_str in ref or img_name in ref for ref in referenced):
                continue

            try:
                dest = thumb_archive / img_path.name
                # Evitar colisiones de nombre
                if dest.exists():
                    stem = img_path.stem
                    suffix = img_path.suffix
                    dest = thumb_archive / f"{stem}_{int(datetime.now().timestamp())}{suffix}"
                shutil.move(str(img_path), str(dest))
                self.logger.info(f"[dim]ARGONAUT[/] Thumbnail archivado: {img_path.name}")
                archived += 1
            except Exception as exc:
                self.logger.error(f"[red]ARGONAUT[/] no se pudo archivar thumbnail {img_path}: {exc}")

        return archived

    # ── 4. Estadísticas de salud ──────────────────────────────────────────────
    def _compute_health(self) -> Dict[str, Any]:
        """Calcula métricas de salud del sistema en los últimos 7 días."""
        health: Dict[str, Any] = {
            "score": 100,
            "completed": 0,
            "failed": 0,
            "timeout": 0,
            "total": 0,
            "success_rate_pct": 100.0,
            "period_days": 7,
        }
        try:
            cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) as cnt
                    FROM pipelines
                    WHERE created_at >= ?
                    GROUP BY status
                    """,
                    (cutoff,),
                ).fetchall()

            counts: Dict[str, int] = {}
            for r in rows:
                counts[r["status"]] = r["cnt"]

            completed = counts.get("completed", 0) + counts.get("completed_dry_run", 0)
            failed = counts.get("failed", 0) + counts.get("error", 0)
            timeout = counts.get("timeout", 0)
            total = sum(counts.values())

            success_rate = (completed / total * 100.0) if total > 0 else 100.0

            health.update({
                "completed": completed,
                "failed": failed,
                "timeout": timeout,
                "total": total,
                "success_rate_pct": round(success_rate, 1),
                "score": int(success_rate),
            })
        except Exception as exc:
            self.logger.error(f"[red]ARGONAUT[/] error calculando salud: {exc}")

        return health

    # ── 5. Limpieza de BD ─────────────────────────────────────────────────────
    def _cleanup_database(self) -> bool:
        """Elimina learning_data antigua y hace VACUUM periódico."""
        cleaned = False
        try:
            # Eliminar learning_data de hace >90 días
            cutoff = (datetime.now() - timedelta(days=LEARNING_DATA_RETENTION_DAYS)).isoformat()
            with self.db._connect() as conn:
                result = conn.execute(
                    "DELETE FROM learning_data WHERE recorded_at < ?",
                    (cutoff,),
                )
                deleted = result.rowcount
                if deleted:
                    self.logger.info(
                        f"[yellow]ARGONAUT[/] {deleted} registro(s) de learning_data eliminados (>90 días)"
                    )
            cleaned = True
        except Exception as exc:
            self.logger.error(f"[red]ARGONAUT[/] error limpiando learning_data: {exc}")

        # VACUUM periódico (máximo una vez cada 7 días)
        if self._should_vacuum():
            try:
                # VACUUM no puede ejecutarse dentro de una transacción
                conn = self.db._connect()
                conn.isolation_level = None  # autocommit
                conn.execute("VACUUM")
                conn.isolation_level = ""    # restaurar
                _VACUUM_STAMP.write_text(datetime.now().isoformat(), encoding="utf-8")
                self.logger.info("[green]ARGONAUT[/] VACUUM SQLite completado")
            except Exception as exc:
                self.logger.error(f"[red]ARGONAUT[/] error en VACUUM: {exc}")

        return cleaned

    def _should_vacuum(self) -> bool:
        """Devuelve True si han pasado >7 días desde el último VACUUM."""
        if not _VACUUM_STAMP.exists():
            return True
        try:
            last = datetime.fromisoformat(_VACUUM_STAMP.read_text(encoding="utf-8").strip())
            return (datetime.now() - last).days >= VACUUM_INTERVAL_DAYS
        except Exception:
            return True

    # ── Utilidades ────────────────────────────────────────────────────────────
    def _ensure_archive_dirs(self) -> None:
        """Crea output/archive/ y output/archive/thumbnails/ si no existen."""
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        (self._archive_dir / "thumbnails").mkdir(parents=True, exist_ok=True)

    def _build_warnings(
        self,
        pipelines_fixed: int,
        files_archived: int,
        health: Dict[str, Any],
    ) -> List[str]:
        """Genera lista de advertencias según los resultados de la auditoría."""
        warnings: List[str] = []
        if pipelines_fixed > 0:
            warnings.append(f"{pipelines_fixed} pipeline(s) marcados como timeout (>2h sin completar)")
        if files_archived > 0:
            warnings.append(f"{files_archived} archivo(s) huérfanos movidos a archive/")
        score = health.get("score", 100)
        if score < 80:
            warnings.append(
                f"Salud del sistema baja: {score}% de tasa de éxito en los últimos 7 días"
            )
        if health.get("timeout", 0) >= 3:
            warnings.append(
                f"{health['timeout']} pipelines en timeout en los últimos 7 días — revisar estabilidad"
            )
        return warnings

    # ── Reporte rich ──────────────────────────────────────────────────────────
    def _print_report(self, audit: Dict[str, Any], health: Dict[str, Any]) -> None:
        """Imprime un resumen de la auditoría con rich."""
        score = health.get("score", 100)
        if score >= 90:
            score_style = "[green]"
        elif score >= 70:
            score_style = "[yellow]"
        else:
            score_style = "[red]"

        table = Table(
            title="[bold cyan]ARGONAUT[/] Auditoría del Sistema",
            show_header=True,
            header_style="bold white on #0A0A0A",
        )
        table.add_column("Métrica", style="cyan", min_width=35)
        table.add_column("Resultado", justify="right", style="white")

        table.add_row("Pipelines marcados timeout", str(audit["pipelines_fixed"]))
        table.add_row("Archivos archivados (huérfanos + thumbnails)", str(audit["files_archived"]))
        table.add_row("Limpieza BD completada", "Si" if audit["db_cleaned"] else "No")
        table.add_row(
            "Tasa de éxito (7 días)",
            f"{score_style}{health.get('success_rate_pct', 100.0):.1f}%[/]",
        )
        table.add_row(
            "Health score",
            f"{score_style}{score}/100[/]",
        )
        table.add_row("Pipelines completados (7d)", str(health.get("completed", 0)))
        table.add_row("Pipelines fallidos (7d)", str(health.get("failed", 0)))
        table.add_row("Pipelines timeout (7d)", str(health.get("timeout", 0)))

        console.print(table)

        warnings = audit.get("warnings", [])
        if warnings:
            warning_text = "\n".join(f"  - {w}" for w in warnings)
            console.print(
                Panel(
                    f"[yellow]{warning_text}[/]",
                    title="[bold yellow]ARGONAUT Advertencias[/]",
                    border_style="yellow",
                )
            )
        else:
            console.print(
                Panel(
                    "[green]Sistema en buen estado. Sin advertencias.[/]",
                    title="[bold cyan]ARGONAUT[/]",
                    border_style="green",
                )
            )


# ── Ejecución standalone ──────────────────────────────────────────────────────
if __name__ == "__main__":
    """
    Uso: python -m agents.sentinel.argonaut
    Ejecuta la auditoría completa sobre la BD real y muestra el informe.
    """
    import sys
    from pathlib import Path as _Path

    # Asegurar que el root del proyecto está en sys.path
    _root = _Path(__file__).resolve().parent.parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

    from database.db import DBManager
    from core.context import Context

    console.print("[bold cyan]ARGONAUT[/] — modo standalone")

    _db_path = _root / "cryptoverdad.db"
    _db = DBManager(str(_db_path))
    _config: dict = {}
    _ctx = Context(topic="argonaut_audit", mode="standard")

    _agent = ARGONAUT(config=_config, db=_db)
    _ctx = _agent.run(_ctx)

    if _ctx.errors:
        console.print(f"[red]Errores:[/] {_ctx.errors}")
    else:
        console.print("[green]Auditoría completada sin errores.[/]")
