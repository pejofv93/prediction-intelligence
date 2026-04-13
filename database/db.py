"""
db.py
Gestión de la base de datos SQLite de NEXUS.
"""

import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from utils.logger import get_logger

logger = get_logger("DB_MANAGER")

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class DBManager:
    """
    Gestor de la base de datos SQLite de NEXUS.
    Usa una conexión persistente para soportar :memory: en tests y
    check_same_thread=False para uso desde hilos del servidor web.
    """

    def __init__(self, db_path: str = "cryptoverdad.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.execute_schema()
        logger.info(f"DB inicializada en: {self.db_path}")

    # ── Conexión ──────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        return self._conn

    # ── Inicialización del schema ─────────────────────────────────────────────
    def execute_schema(self) -> None:
        """Crea las tablas si no existen."""
        try:
            schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
            self._conn.executescript(schema_sql)
            self._conn.commit()
            logger.info("Schema SQL ejecutado correctamente.")
        except Exception as exc:
            logger.error(f"Error ejecutando schema: {exc}")
            raise

    # ── Pipelines ─────────────────────────────────────────────────────────────
    def save_pipeline(self, ctx) -> None:
        """Inserta o actualiza un pipeline desde un Context."""
        try:
            errors_json = json.dumps(ctx.errors, ensure_ascii=False)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO pipelines (id, topic, mode, status, youtube_url, tiktok_url, seo_score, errors)
                    VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        youtube_url  = excluded.youtube_url,
                        tiktok_url   = excluded.tiktok_url,
                        seo_score    = excluded.seo_score,
                        errors       = excluded.errors
                    """,
                    (
                        ctx.pipeline_id,
                        ctx.topic,
                        ctx.mode,
                        ctx.youtube_url or None,
                        ctx.tiktok_url or None,
                        ctx.seo_score,
                        errors_json,
                    ),
                )
            logger.debug(f"Pipeline {ctx.pipeline_id[:8]} guardado.")
        except Exception as exc:
            logger.error(f"Error guardando pipeline: {exc}")
            raise

    def update_pipeline_status(self, pipeline_id: str, status: str) -> None:
        """Actualiza el status de un pipeline y marca completed_at si termina."""
        try:
            completed_at = datetime.now().isoformat() if status.startswith("completed") else None
            with self._connect() as conn:
                conn.execute(
                    "UPDATE pipelines SET status=?, completed_at=? WHERE id=?",
                    (status, completed_at, pipeline_id),
                )
            logger.debug(f"Pipeline {pipeline_id[:8]} → status={status}")
        except Exception as exc:
            logger.error(f"Error actualizando status: {exc}")
            raise

    def get_pipeline(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """Devuelve un pipeline por su ID o None si no existe."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM pipelines WHERE id=?", (pipeline_id,)
                ).fetchone()
            if row:
                return dict(row)
            return None
        except Exception as exc:
            logger.error(f"Error obteniendo pipeline: {exc}")
            return None

    def list_pipelines(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Devuelve los últimos N pipelines."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM pipelines ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error(f"Error listando pipelines: {exc}")
            return []

    # ── Videos ───────────────────────────────────────────────────────────────
    def save_video(self, video_data: Dict[str, Any]) -> None:
        """
        Inserta un vídeo publicado.
        video_data debe contener: id, pipeline_id, platform, video_id, title, url
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO videos
                        (id, pipeline_id, platform, video_id, title, url)
                    VALUES (:id, :pipeline_id, :platform, :video_id, :title, :url)
                    """,
                    video_data,
                )
            logger.debug(f"Vídeo {video_data.get('id','?')[:8]} guardado.")
        except Exception as exc:
            logger.error(f"Error guardando vídeo: {exc}")
            raise

    def update_video_stats(
        self, video_id: str, views: int, likes: int, thumbnail_winner: Optional[str] = None
    ) -> None:
        """Actualiza las estadísticas de un vídeo."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE videos SET views=?, likes=?, thumbnail_winner=? WHERE id=?",
                    (views, likes, thumbnail_winner, video_id),
                )
        except Exception as exc:
            logger.error(f"Error actualizando stats de vídeo: {exc}")
            raise

    # ── Learning data ─────────────────────────────────────────────────────────
    def save_learning_data(self, video_id: str, metric: str, value: float) -> None:
        """Registra una métrica de aprendizaje para un vídeo."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO learning_data (video_id, metric, value) VALUES (?,?,?)",
                    (video_id, metric, value),
                )
        except Exception as exc:
            logger.error(f"Error guardando learning_data: {exc}")
            raise

    def get_learning_data(self, video_id: str) -> List[Dict[str, Any]]:
        """Devuelve todas las métricas de aprendizaje de un vídeo."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM learning_data WHERE video_id=?", (video_id,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error(f"Error obteniendo learning_data: {exc}")
            return []

    # ── Optimal hours ─────────────────────────────────────────────────────────
    def upsert_optimal_hour(
        self, day_of_week: int, hour: int, avg_views: float, sample_size: int
    ) -> None:
        """Inserta o actualiza la hora óptima de publicación para un día."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO optimal_hours (day_of_week, hour, avg_views, sample_size, updated_at)
                    VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(day_of_week, hour) DO UPDATE SET
                        avg_views   = excluded.avg_views,
                        sample_size = excluded.sample_size,
                        updated_at  = CURRENT_TIMESTAMP
                    """,
                    (day_of_week, hour, avg_views, sample_size),
                )
        except Exception as exc:
            logger.error(f"Error upserting optimal_hour: {exc}")
            raise

    # ── Memoria de vídeos (OLYMPUS) ───────────────────────────────────────────
    def save_memoria_video(
        self,
        video_id: str,
        title: str,
        url: str,
        seo_score: int,
        privacy_status: str,
    ) -> None:
        """Registra un vídeo publicado en YouTube en la tabla memoria_videos."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memoria_videos
                        (video_id, title, url, seo_score, published_at, privacy_status)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    """,
                    (video_id, title, url, seo_score, privacy_status),
                )
            logger.debug(f"memoria_videos: {video_id} guardado.")
        except Exception as exc:
            logger.error(f"Error guardando memoria_video: {exc}")
            raise

    def save_telegram_notification(
        self,
        pipeline_id: str,
        chat_id: str,
        message_id: int,
        message_text: str,
    ) -> None:
        """Registra una notificación de Telegram enviada."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO telegram_notifications
                        (pipeline_id, chat_id, message_id, message_text)
                    VALUES (?, ?, ?, ?)
                    """,
                    (pipeline_id, chat_id, message_id, message_text),
                )
            logger.debug(f"Telegram notificación guardada (pipeline={pipeline_id[:8]}).")
        except Exception as exc:
            logger.error(f"Error guardando telegram_notification: {exc}")
            raise

    # ── Precios de mercado (fallback genérico por coin_id) ────────────────────
    def save_coin_price(self, coin_id: str, price: float) -> None:
        """Guarda el precio de cualquier coin en market_prices (INSERT OR REPLACE)."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO market_prices
                           (coin_id, price_usd, updated_at)
                           VALUES (?, ?, datetime('now'))""",
                    (coin_id, price),
                )
            logger.debug(f"market_prices: {coin_id} ${price:,.2f} guardado.")
        except Exception as exc:
            logger.warning(f"save_coin_price({coin_id}): {exc}")

    def get_last_coin_price(self, coin_id: str) -> float:
        """Devuelve el ultimo precio guardado para coin_id, o 0.0 si no hay datos."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT price_usd FROM market_prices
                           WHERE coin_id=?
                           ORDER BY updated_at DESC LIMIT 1""",
                    (coin_id,),
                ).fetchone()
            return float(row[0]) if row else 0.0
        except Exception as exc:
            logger.warning(f"get_last_coin_price({coin_id}): {exc}")
            return 0.0

    # Alias de compatibilidad (usados en argos.py legacy)
    def save_btc_price(self, price: float) -> None:
        self.save_coin_price("bitcoin", price)

    def get_last_btc_price(self) -> float:
        return self.get_last_coin_price("bitcoin")

    # ── LLM Usage tracking ───────────────────────────────────────────────────

    def save_llm_usage(self, provider: str, tokens: int) -> None:
        """Registra tokens consumidos por un proveedor LLM hoy (UTC)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO llm_usage (provider, tokens, day) VALUES (?, ?, ?)",
                    (provider, tokens, today),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"save_llm_usage error (no crítico): {exc}")

    def get_llm_usage_today(self, provider: str) -> int:
        """Devuelve total de tokens usados hoy (UTC) por un proveedor."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0) as total FROM llm_usage "
                    "WHERE provider=? AND day=?",
                    (provider, today),
                ).fetchone()
                return int(row["total"]) if row else 0
        except Exception as exc:
            logger.warning(f"get_llm_usage_today error (no crítico): {exc}")
            return 0

    def get_llm_usage_summary(self) -> list:
        """Devuelve resumen de uso hoy por proveedor (para el panel web)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT provider, SUM(tokens) as total FROM llm_usage "
                    "WHERE day=? GROUP BY provider ORDER BY total DESC",
                    (today,),
                ).fetchall()
                return [{"provider": r["provider"], "tokens": r["total"]} for r in rows]
        except Exception as exc:
            logger.warning(f"get_llm_usage_summary error: {exc}")
            return []

    def get_optimal_hour(self, day_of_week: int, min_samples: int = 3) -> Optional[int]:
        """
        Devuelve la hora con más vistas promedio para el día dado.
        Solo usa datos históricos si hay al menos min_samples muestras reales.
        Si no hay datos suficientes, devuelve 10 (10:00 UTC = 12:00 España).
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT hour, sample_size FROM optimal_hours
                    WHERE day_of_week=? AND sample_size >= ?
                    ORDER BY avg_views DESC
                    LIMIT 1
                    """,
                    (day_of_week, min_samples),
                ).fetchone()
            if row:
                return row["hour"]
            return 10  # fallback 10:00 UTC (12:00 España)
        except Exception as exc:
            logger.error(f"Error obteniendo optimal_hour: {exc}")
            return 10
