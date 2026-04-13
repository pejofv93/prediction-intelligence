from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
kairos.py
KAIROS — Optimizador de horarios de publicación de NEXUS.
Analiza datos históricos y calcula el mejor momento para publicar.
"""

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

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold blue]KAIROS[/] iniciado")
        try:
            # Limpiar defaults obsoletos y re-sembrar con horas actuales.
            # Garantiza que cambios de DEFAULT_HOURS se aplican en producción.
            self._reset_stale_defaults()
            self._update_optimal_hours_from_history()
            today = datetime.now().weekday()  # 0=Lun … 6=Dom
            optimal_hour = self.db.get_optimal_hour(today)
            ctx.optimal_publish_hour = optimal_hour
            self.logger.info(
                f"[green]KAIROS[/] hora óptima para hoy "
                f"(día {today}): [bold]{optimal_hour}:00[/]"
            )
            console.print(self._build_schedule_table())
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

