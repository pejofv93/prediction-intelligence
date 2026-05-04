from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
kairos.py
KAIROS — Optimizador de horarios de publicación de NEXUS.
Analiza datos históricos y calcula el mejor momento para publicar.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from rich.console import Console
from rich.table import Table

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# Defaults: 10:00 UTC (12:00 España verano / 11:00 invierno)
# Elegido para que cualquier redeploy nocturno tenga margen suficiente.
VOLUME_GUARDIAN_HOUR_UTC = 3  # Hora UTC en que KAIROS ejecuta VOLUME_GUARDIAN

DEFAULT_HOURS: Dict[int, int] = {
    0: 10,  # Lunes
    1: 10,  # Martes
    2: 10,  # Miércoles
    3: 10,  # Jueves
    4: 10,  # Viernes
    5: 10,  # Sábado
    6: 10,  # Domingo
}

# Mínimo de muestras por slot para considerarlo fiable
MIN_SAMPLE_SIZE = 3


class KAIROS(BaseAgent):
    """
    Analiza el historial de publicaciones vs views y determina
    el horario óptimo para cada día de la semana.
    Actualiza ctx.optimal_publish_hour.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("KAIROS")
        # Limpiar defaults obsoletos al instanciar — garantiza que
        # schedule_next_publish() usa DEFAULT_HOURS actuales desde el primer tick.
        self._reset_stale_defaults()

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold blue]KAIROS[/] iniciado")
        try:
            self._update_optimal_hours_from_history()
            today = datetime.now().weekday()  # 0=Lun … 6=Dom
            optimal_hour = self.db.get_optimal_hour(today)
            ctx.optimal_publish_hour = optimal_hour
            self.logger.info(
                f"[green]KAIROS[/] hora óptima para hoy "
                f"(día {today}): [bold]{optimal_hour}:00[/]"
            )
            console.print(self._build_schedule_table())
            # Procesar cola de verificaciones A/B pendientes
            self._process_ab_swap_queue()
            # VOLUME_GUARDIAN: limpieza diaria 03:00 UTC
            if self._should_run_volume_guardian():
                self._volume_guardian()
            # Volume health check: alertas cada 6h via MERCURY
            self._maybe_run_volume_health_check()
        except Exception as exc:
            self.logger.error(f"[red]KAIROS error:[/] {exc}")
            ctx.add_error("KAIROS", str(exc))
            ctx.optimal_publish_hour = DEFAULT_HOURS.get(datetime.now().weekday(), 10)
        return ctx

    # ── actualización de la tabla optimal_hours ───────────────────────────────
    def _update_optimal_hours_from_history(self) -> None:
        """
        Lee los vídeos publicados, agrupa por (día_semana, hora),
        calcula views promedio y actualiza la tabla optimal_hours.
        """
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT v.views,
                           ld.value AS publish_hour,
                           strftime('%w', v.created_at) AS dow_sqlite
                    FROM videos v
                    JOIN learning_data ld
                         ON ld.video_id = v.id AND ld.metric = 'publish_hour'
                    WHERE v.platform = 'youtube'
                      AND v.views IS NOT NULL
                    """
                ).fetchall()
        except Exception as exc:
            self.logger.warning(f"[yellow]KAIROS[/] no se pudo leer historial: {exc}")
            return

        if not rows:
            self.logger.info("[yellow]KAIROS[/] sin datos históricos, usando defaults")
            self._seed_defaults()
            return

        # Agregamos: slot = (day_of_week, hour) → lista de views
        slots: Dict[tuple, List[float]] = {}
        for r in rows:
            try:
                # SQLite strftime('%w') devuelve 0=Dom … 6=Sáb
                # Convertimos a Python: 0=Lun … 6=Dom
                sqlite_dow = int(r["dow_sqlite"])
                python_dow = (sqlite_dow - 1) % 7  # 0=Sun→6, 1=Mon→0 …
                hour = int(float(r["publish_hour"]))
                views = float(r["views"] or 0)
                slots.setdefault((python_dow, hour), []).append(views)
            except (ValueError, TypeError):
                continue

        for (dow, hour), views_list in slots.items():
            if len(views_list) >= MIN_SAMPLE_SIZE:
                avg = sum(views_list) / len(views_list)
                self.db.upsert_optimal_hour(dow, hour, avg, len(views_list))

    def _seed_defaults(self) -> None:
        """Siembra los defaults si la tabla está vacía."""
        for dow, hour in DEFAULT_HOURS.items():
            try:
                self.db.upsert_optimal_hour(dow, hour, 0.0, 0)
            except Exception:
                pass

    def _reset_stale_defaults(self) -> None:
        """
        Elimina filas con sample_size=0 (sem datos reales) y re-siembra
        con los DEFAULT_HOURS actuales. Se llama en cada arranque para
        garantizar que cambios de DEFAULT_HOURS se aplican en producción.
        """
        try:
            with self.db._connect() as conn:
                conn.execute("DELETE FROM optimal_hours WHERE sample_size = 0")
            self.logger.info("KAIROS: defaults obsoletos eliminados de optimal_hours")
        except Exception as exc:
            self.logger.warning(f"KAIROS: no se pudo limpiar optimal_hours: {exc}")
        self._seed_defaults()

    # ── cola A/B swap ─────────────────────────────────────────────────────────
    def _process_ab_swap_queue(self) -> None:
        """
        Procesa la cola de verificaciones A/B thumbnail.
        Para cada entrada en ab_swap_queue con status='pending' y check_at <= ahora:
        - Obtiene la decisión de swap y marca como procesado.
        - El swap real lo ejecutaría OLYMPUS._set_thumbnail() en una futura iteración.
        """
        import sqlite3

        try:
            with sqlite3.connect(self.db.db_path) as conn:
                # Verificar si la tabla existe antes de operar
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='ab_swap_queue'"
                ).fetchone()
                if not exists:
                    return

                pending = conn.execute("""
                    SELECT id, pipeline_id, youtube_video_id, current_thumbnail
                    FROM ab_swap_queue
                    WHERE status = 'pending'
                      AND check_at <= datetime('now')
                    LIMIT 5
                """).fetchall()

            for row_id, pipeline_id, yt_vid_id, current_thumb in pending:
                try:
                    pid_short = (pipeline_id or "")[:8]
                    self.logger.info(
                        f"KAIROS A/B check: pipeline={pid_short} "
                        f"video={yt_vid_id} thumbnail={current_thumb}"
                    )
                    with sqlite3.connect(self.db.db_path) as conn:
                        conn.execute(
                            "UPDATE ab_swap_queue SET status='checked' WHERE id=?",
                            (row_id,),
                        )
                except Exception as e:
                    self.logger.warning(f"A/B swap check {row_id}: {e}")

        except Exception as e:
            self.logger.debug(f"_process_ab_swap_queue: {e}")

    # ── schedule_next_publish ─────────────────────────────────────────────────
    def schedule_next_publish(self) -> datetime:
        """
        Devuelve el próximo datetime óptimo para publicar.
        Si la hora óptima de hoy ya pasó, calcula el día siguiente.
        """
        now = datetime.now()
        dow = now.weekday()
        optimal_hour = self.db.get_optimal_hour(dow)

        candidate = now.replace(hour=optimal_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            # Buscar el próximo día con hora óptima
            for delta in range(1, 8):
                next_dow = (dow + delta) % 7
                next_hour = self.db.get_optimal_hour(next_dow)
                candidate = (now + timedelta(days=delta)).replace(
                    hour=next_hour, minute=0, second=0, microsecond=0
                )
                break  # primera ocurrencia válida

        return candidate

    # ── volume guardian ───────────────────────────────────────────────────────
    def _should_run_volume_guardian(self) -> bool:
        """True si el VOLUME_GUARDIAN debe ejecutarse ahora (03:00-03:59 UTC, una vez por día)."""
        now_utc = datetime.utcnow()
        if now_utc.hour != VOLUME_GUARDIAN_HOUR_UTC:
            return False
        today = now_utc.date().isoformat()
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM volume_cleanup_log WHERE date(ran_at) = ? LIMIT 1",
                    (today,),
                ).fetchone()
                return row is None
        except Exception:
            return False

    def _volume_guardian(self) -> None:
        """
        Job diario 03:00 UTC: limpia archivos viejos en el volumen Railway
        y registra el resultado en volume_cleanup_log.
        """
        self.logger.info("KAIROS: VOLUME_GUARDIAN iniciado")
        output_dir = Path(os.getenv("OUTPUT_DIR", "/app/output"))
        if not output_dir.exists():
            output_dir = Path(__file__).resolve().parents[3] / "output"

        freed_bytes = 0
        action = "none"
        disk_pct = 0.0

        try:
            before = shutil.disk_usage(output_dir)
            disk_pct = before.used / before.total * 100

            scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
            result = subprocess.run(
                [sys.executable, str(scripts_dir / "cleanup_volume.py"), "--confirm"],
                capture_output=True, text=True, timeout=300,
            )

            after = shutil.disk_usage(output_dir)
            freed_bytes = max(0, before.used - after.used)
            disk_pct_after = after.used / after.total * 100
            action = "cleanup"

            self.logger.info(
                f"KAIROS VOLUME_GUARDIAN: liberados {freed_bytes/1e6:.1f}MB, "
                f"disco {disk_pct:.1f}% → {disk_pct_after:.1f}%"
            )

            if result.stderr:
                self.logger.debug(f"VOLUME_GUARDIAN stderr: {result.stderr[-400:]}")

        except Exception as exc:
            self.logger.error(f"KAIROS VOLUME_GUARDIAN error: {exc}")
            action = f"error: {exc}"

        self._log_volume_cleanup(freed_bytes, action, disk_pct)

    def _maybe_run_volume_health_check(self) -> None:
        """Ejecuta MERCURY.volume_health_check() si han pasado >6h desde el último check."""
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT ran_at FROM volume_cleanup_log
                    ORDER BY ran_at DESC LIMIT 1
                    """
                ).fetchone()

            if row:
                last_check = datetime.fromisoformat(row[0])
                if (datetime.utcnow() - last_check).total_seconds() < 6 * 3600:
                    return  # checked recently enough

            from agents.herald.mercury import MERCURY
            mercury = MERCURY(self.config, self.db)
            result = mercury.volume_health_check()
            self.logger.info(
                f"KAIROS: volume health check completado — "
                f"status={result.get('status')} pct={result.get('pct', 0):.1f}%"
            )
            # Registrar que se hizo el check
            self._log_volume_cleanup(0, f"health_check:{result.get('status','ok')}")
        except Exception as exc:
            self.logger.debug(f"KAIROS: volume health check falló: {exc}")

    def _log_volume_cleanup(self, freed_bytes: int, action: str, disk_pct: float = 0.0) -> None:
        """Registra resultado en volume_cleanup_log."""
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO volume_cleanup_log (freed_bytes, action, disk_pct)
                    VALUES (?, ?, ?)
                    """,
                    (freed_bytes, action, disk_pct),
                )
        except Exception as exc:
            self.logger.warning(f"KAIROS: no se pudo loguear cleanup en DB: {exc}")

    # ── tabla rich ────────────────────────────────────────────────────────────
    def _build_schedule_table(self) -> Table:
        day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        today = datetime.now().weekday()

        table = Table(
            title="[bold blue]KAIROS[/] Horario óptimo de publicación",
            show_header=True,
            header_style="bold white on #0A0A0A",
        )
        table.add_column("Día", style="cyan", min_width=12)
        table.add_column("Hora óptima", justify="center", style="white")
        table.add_column("Fuente")

        for dow, name in enumerate(day_names):
            hour = self.db.get_optimal_hour(dow)
            is_today = dow == today

            # Verificar si es dato real o default
            try:
                with self.db._connect() as conn:
                    row = conn.execute(
                        "SELECT sample_size FROM optimal_hours "
                        "WHERE day_of_week=? ORDER BY avg_views DESC LIMIT 1",
                        (dow,),
                    ).fetchone()
                sample = row["sample_size"] if row else 0
            except Exception:
                sample = 0

            fuente = f"[green]histórico ({sample} vídeos)[/]" if sample >= MIN_SAMPLE_SIZE else "[yellow]default[/]"
            day_label = f"[bold]{name}[/] ◄ HOY" if is_today else name
            table.add_row(day_label, f"{hour:02d}:00", fuente)

        next_dt = self.schedule_next_publish()
        table.caption = f"Próxima publicación óptima: [bold yellow]{next_dt.strftime('%a %d/%m/%Y %H:%M')}[/]"
        return table

