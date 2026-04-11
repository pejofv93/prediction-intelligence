from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
mneme.py
MNEME — Motor de memoria y aprendizaje de NEXUS.
Corre al INICIO del pipeline para inyectar learning_context en ctx antes de CALÍOPE.
"""

from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# Mínimo de vídeos para considerar un ajuste confirmado
MIN_VIDEOS_TO_CONFIRM = 5
# Máximo de cambio relativo por iteración (15 %)
MAX_ADJUST_FACTOR = 0.15

# Defaults cuando no hay datos
DEFAULT_LEARNING_CONTEXT: Dict[str, Any] = {
    "preferred_script_style": "educativo",
    "best_hook_patterns": [
        "¿Sabías que…?",
        "Esto va a cambiar TODO sobre…",
        "Nadie te está contando la verdad sobre…",
    ],
    "avoid_patterns": [],
    "thumbnail_winner_style": "A",
    "avg_optimal_hour": 18,
}


class MNEME(BaseAgent):
    """
    Lee el historial de SQLite y calcula los ajustes de estilo, hooks
    y thumbnails que han maximizado las visualizaciones.
    Inyecta el resultado en ctx.learning_context.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("MNEME")
        self._ensure_table()

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold purple]MNEME[/] iniciado")
        try:
            videos = self._get_historical_videos()
            if len(videos) < MIN_VIDEOS_TO_CONFIRM:
                self.logger.info(
                    f"[yellow]MNEME[/] solo {len(videos)} vídeos históricos "
                    f"(mínimo {MIN_VIDEOS_TO_CONFIRM}). Usando defaults."
                )
                ctx.learning_context = DEFAULT_LEARNING_CONTEXT.copy()
                return ctx

            learning = self._compute_learning(videos)
            ctx.learning_context = learning
            self._log_summary(learning)
        except Exception as exc:
            self.logger.error(f"[red]MNEME error:[/] {exc}")
            ctx.add_error("MNEME", str(exc))
            ctx.learning_context = DEFAULT_LEARNING_CONTEXT.copy()
        return ctx

    # ── cálculo de aprendizaje ────────────────────────────────────────────────
    def _compute_learning(self, videos: List[Dict]) -> Dict[str, Any]:
        # ── Estilo de guión con más views ────────────────────────────────────
        style_views: Dict[str, List[int]] = {}
        for v in videos:
            style = v.get("script_style") or "educativo"
            views = v.get("views", 0) or 0
            style_views.setdefault(style, []).append(views)

        style_avgs = {
            s: sum(vv) / len(vv) for s, vv in style_views.items() if vv
        }
        best_style = max(style_avgs, key=lambda k: style_avgs[k]) if style_avgs else "educativo"

        # ── Thumbnail winner ─────────────────────────────────────────────────
        a_views = [v.get("views", 0) for v in videos if v.get("thumbnail_winner") == "A"]
        b_views = [v.get("views", 0) for v in videos if v.get("thumbnail_winner") == "B"]
        avg_a = sum(a_views) / len(a_views) if a_views else 0
        avg_b = sum(b_views) / len(b_views) if b_views else 0
        thumbnail_winner = "A" if avg_a >= avg_b else "B"

        # ── Hora óptima promedio ─────────────────────────────────────────────
        hours = [v.get("publish_hour") for v in videos if v.get("publish_hour") is not None]
        if hours:
            # Agrupamos por hora y buscamos la de mayor views
            hour_views: Dict[int, List[int]] = {}
            for v in videos:
                h = v.get("publish_hour")
                if h is not None:
                    hour_views.setdefault(h, []).append(v.get("views", 0) or 0)
            best_hour = max(hour_views, key=lambda k: sum(hour_views[k]) / len(hour_views[k]))
        else:
            best_hour = 18

        # ── Hooks ganadores / patrones a evitar ──────────────────────────────
        hooks_all = self._get_hook_metrics()
        best_hooks = [h["pattern"] for h in hooks_all[:5] if h.get("pattern")]
        avoid = [h["pattern"] for h in hooks_all if h.get("avg_views", 0) < 100 and h.get("pattern")]

        # ── Aplicar regla de ajuste máximo 15 % ──────────────────────────────
        prev = DEFAULT_LEARNING_CONTEXT
        final_hour = self._clamp_adjust(
            int(prev["avg_optimal_hour"]), best_hour, MAX_ADJUST_FACTOR
        )

        return {
            "preferred_script_style": best_style,
            "best_hook_patterns": best_hooks if best_hooks else DEFAULT_LEARNING_CONTEXT["best_hook_patterns"],
            "avoid_patterns": avoid,
            "thumbnail_winner_style": thumbnail_winner,
            "avg_optimal_hour": final_hour,
        }

    def _clamp_adjust(self, old_val: int, new_val: int, factor: float) -> int:
        """Limita el cambio al MAX_ADJUST_FACTOR del valor anterior."""
        max_delta = max(1, round(abs(old_val) * factor))
        delta = new_val - old_val
        delta = max(-max_delta, min(max_delta, delta))
        return old_val + delta

    # ── acceso a datos históricos ─────────────────────────────────────────────
    def _get_historical_videos(self) -> List[Dict]:
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT v.id, v.views, v.thumbnail_winner,
                           ld_style.value   AS script_style_raw,
                           ld_hour.value    AS publish_hour
                    FROM videos v
                    LEFT JOIN learning_data ld_style
                           ON ld_style.video_id = v.id AND ld_style.metric = 'script_style'
                    LEFT JOIN learning_data ld_hour
                           ON ld_hour.video_id  = v.id AND ld_hour.metric  = 'publish_hour'
                    WHERE v.platform = 'youtube'
                    ORDER BY v.created_at DESC
                    LIMIT 100
                    """
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                # script_style_raw es un float (index) — lo convertimos a nombre
                # Si se guardó como índice numérico hacemos lookup básico
                raw = d.get("script_style_raw")
                styles = ["educativo", "opinion", "analisis", "short", "tutorial", "urgente", "thread"]
                if raw is not None:
                    try:
                        idx = int(raw)
                        d["script_style"] = styles[idx] if 0 <= idx < len(styles) else "educativo"
                    except (ValueError, TypeError):
                        d["script_style"] = "educativo"
                else:
                    d["script_style"] = "educativo"
                if d.get("publish_hour") is not None:
                    d["publish_hour"] = int(d["publish_hour"])
                result.append(d)
            return result
        except Exception as exc:
            self.logger.error(f"[red]MNEME[/] error leyendo vídeos históricos: {exc}")
            return []

    def _get_hook_metrics(self) -> List[Dict]:
        """Devuelve hooks ordenados por avg_views desc."""
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ld.value AS pattern,
                           AVG(v.views) AS avg_views
                    FROM learning_data ld
                    JOIN videos v ON v.id = ld.video_id
                    WHERE ld.metric = 'hook_pattern'
                    GROUP BY ld.value
                    ORDER BY avg_views DESC
                    LIMIT 20
                    """
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── tabla creación ────────────────────────────────────────────────────────
    def _ensure_table(self) -> None:
        """La tabla learning_data ya existe en schema.sql; no hacemos nada extra."""
        pass

    # ── log de resumen ────────────────────────────────────────────────────────
    def _log_summary(self, lc: Dict[str, Any]) -> None:
        table = Table(
            title="[bold purple]MNEME[/] Learning Context",
            show_header=True,
            header_style="bold white on #0A0A0A",
        )
        table.add_column("Clave", style="cyan")
        table.add_column("Valor", style="white")
        for k, v in lc.items():
            table.add_row(str(k), str(v))
        console.print(table)

