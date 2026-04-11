from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
croesus.py
CROESUS — Controlador de costes y uso de APIs de NEXUS.
Registra consumo en SQLite y avisa cuando se acercan los límites.
"""

import sqlite3
from datetime import datetime, date
from typing import Dict, Any

from rich.console import Console
from rich.table import Table

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# ── Límites por API (unidades/día salvo indicación) ───────────────────────────
API_LIMITS: Dict[str, Dict[str, Any]] = {
    "groq": {
        "label": "Groq API (tokens/día)",
        "daily_limit": 1_000_000,
        "unit": "tokens",
        "warn_pct": 80,
    },
    "pexels": {
        "label": "Pexels API (req/hora)",
        "daily_limit": 200,          # por hora, vigilamos diario como proxy
        "unit": "requests",
        "warn_pct": 80,
    },
    "coingecko": {
        "label": "CoinGecko (req/min)",
        "daily_limit": 30,            # por minuto; aquí registramos el máximo diario razonable
        "unit": "requests",
        "warn_pct": 80,
    },
    "youtube": {
        "label": "YouTube API (quota units/día)",
        "daily_limit": 10_000,
        "unit": "units",
        "warn_pct": 80,
    },
    "edge_tts": {
        "label": "edge-tts (chars/día)",
        "daily_limit": 999_999,       # gratis, límite muy alto
        "unit": "chars",
        "warn_pct": 90,
    },
}

# Coste en quota units de operaciones YouTube comunes
YOUTUBE_QUOTA_COSTS = {
    "videos.insert": 1600,
    "thumbnails.set": 50,
    "videos.list": 1,
    "channels.list": 1,
    "search.list": 100,
}


class CROESUS(BaseAgent):
    """
    Estima y registra el coste de cada pipeline.
    Avisa con rich si se acercan los límites de API.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("CROESUS")
        self._ensure_table()

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold green]CROESUS[/] iniciado")
        try:
            usage = self._estimate_usage(ctx)
            self._record_usage(usage, ctx.pipeline_id)
            self._check_limits(ctx, usage)
            console.print(self.get_daily_report())
        except Exception as exc:
            self.logger.error(f"[red]CROESUS error:[/] {exc}")
            ctx.add_error("CROESUS", str(exc))
        return ctx

    # ── estimación de uso ─────────────────────────────────────────────────────
    def _estimate_usage(self, ctx: Context) -> Dict[str, float]:
        """Estima el consumo de APIs para este pipeline."""
        usage: Dict[str, float] = {}

        # Groq: tokens aproximados del guión (1 token ≈ 4 chars)
        if ctx.script:
            usage["groq"] = len(ctx.script) / 4.0
        else:
            usage["groq"] = 0.0

        # YouTube: insert + thumbnail
        yt_cost = 0
        if ctx.video_path:
            yt_cost += YOUTUBE_QUOTA_COSTS["videos.insert"]
        if ctx.thumbnail_a_path:
            yt_cost += YOUTUBE_QUOTA_COSTS["thumbnails.set"]
        usage["youtube"] = float(yt_cost)

        # Pexels: 1 req por clip de stock (aprox 5 clips por vídeo)
        usage["pexels"] = 5.0

        # CoinGecko: 1 req por pipeline
        usage["coingecko"] = 1.0

        # edge-tts: chars del guión
        usage["edge_tts"] = float(len(ctx.script)) if ctx.script else 0.0

        return usage

    # ── registro en SQLite ────────────────────────────────────────────────────
    def _ensure_table(self) -> None:
        """Crea la tabla api_usage si no existe."""
        try:
            with self.db._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_usage (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        api_name    TEXT    NOT NULL,
                        pipeline_id TEXT,
                        amount      REAL    NOT NULL,
                        unit        TEXT,
                        recorded_at DATE    DEFAULT (date('now'))
                    )
                    """
                )
        except Exception as exc:
            self.logger.error(f"[red]CROESUS[/] no se pudo crear tabla api_usage: {exc}")
            raise

    def _record_usage(self, usage: Dict[str, float], pipeline_id: str) -> None:
        try:
            with self.db._connect() as conn:
                for api_name, amount in usage.items():
                    unit = API_LIMITS.get(api_name, {}).get("unit", "units")
                    conn.execute(
                        "INSERT INTO api_usage (api_name, pipeline_id, amount, unit) "
                        "VALUES (?, ?, ?, ?)",
                        (api_name, pipeline_id, amount, unit),
                    )
        except Exception as exc:
            self.logger.error(f"[red]CROESUS[/] error registrando uso: {exc}")
            raise

    # ── verificación de límites ───────────────────────────────────────────────
    def _get_daily_totals(self) -> Dict[str, float]:
        """Devuelve el total consumido hoy por cada API."""
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT api_name, SUM(amount) as total "
                    "FROM api_usage "
                    "WHERE recorded_at = date('now') "
                    "GROUP BY api_name"
                ).fetchall()
            return {row["api_name"]: row["total"] for row in rows}
        except Exception as exc:
            self.logger.error(f"[red]CROESUS[/] error obteniendo totales: {exc}")
            return {}

    def _check_limits(self, ctx: Context, current_usage: Dict[str, float]) -> None:
        daily_totals = self._get_daily_totals()

        for api_name, cfg in API_LIMITS.items():
            total = daily_totals.get(api_name, 0.0)
            limit = cfg["daily_limit"]
            pct = (total / limit * 100) if limit > 0 else 0.0
            warn_pct = cfg["warn_pct"]

            if pct >= warn_pct:
                msg = (
                    f"{cfg['label']}: {total:.0f}/{limit} "
                    f"({pct:.1f}% del límite diario)"
                )
                ctx.add_warning("CROESUS", msg)
                icon = "🔴" if pct >= 95 else "🟡"
                console.print(
                    f"[bold yellow]CROESUS[/] {icon} ATENCIÓN — {msg}"
                )

    # ── reporte diario ────────────────────────────────────────────────────────
    def get_daily_report(self) -> Table:
        """Devuelve una tabla rich con el uso diario de APIs."""
        daily_totals = self._get_daily_totals()

        table = Table(
            title=f"[bold yellow]CROESUS[/] Uso de APIs — {date.today().isoformat()}",
            show_header=True,
            header_style="bold white on #0A0A0A",
        )
        table.add_column("API", style="cyan", min_width=30)
        table.add_column("Usado hoy", justify="right", style="white")
        table.add_column("Límite", justify="right", style="white")
        table.add_column("Uso %", justify="right")
        table.add_column("Estado")

        for api_name, cfg in API_LIMITS.items():
            total = daily_totals.get(api_name, 0.0)
            limit = cfg["daily_limit"]
            pct = (total / limit * 100) if limit > 0 else 0.0

            if pct >= 95:
                estado = "[red]CRÍTICO[/]"
                pct_str = f"[red]{pct:.1f}%[/]"
            elif pct >= cfg["warn_pct"]:
                estado = "[yellow]ADVERTENCIA[/]"
                pct_str = f"[yellow]{pct:.1f}%[/]"
            else:
                estado = "[green]OK[/]"
                pct_str = f"[green]{pct:.1f}%[/]"

            table.add_row(
                cfg["label"],
                f"{total:,.0f} {cfg['unit']}",
                f"{limit:,.0f} {cfg['unit']}",
                pct_str,
                estado,
            )

        return table

