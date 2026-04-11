from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
recon.py
RECON — Espía de Competidores
Capa ORÁCULO · NEXUS v1.0 · CryptoVerdad

Busca los vídeos más recientes de competidores sobre el topic,
identifica el gap editorial (qué ángulo no han cubierto) y persiste
los resultados en SQLite.
"""

import json
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx
from rich.console import Console
from rich.table import Table

from core.base_agent import BaseAgent
from core.context import Context
from utils.logger import get_logger

console = Console()

COMPETITORS_TABLE = "oracle_competitors"

# YouTube RSS base (no requiere API key)
YT_RSS_BASE = "https://www.youtube.com/feeds/videos.xml?search_query={query}"
YT_API_SEARCH = "https://www.googleapis.com/youtube/v3/search"


class RECON(BaseAgent):
    """Espía de competidores — analiza vídeos recientes sobre el topic."""

    def __init__(self, config: dict, db):
        self.config = config
        self.db = db
        self.logger = get_logger("RECON")
        self.yt_api_key: Optional[str] = os.getenv(
            "YOUTUBE_API_KEY",
            config.get("youtube", {}).get("api_key", ""),
        )

    # ── YouTube API v3 ─────────────────────────────────────────────────────

    def _search_youtube_api(self, topic: str) -> List[Dict[str, Any]]:
        """
        Busca los 5 vídeos más recientes con YouTube Data API v3.
        Requiere YOUTUBE_API_KEY en env o config.
        """
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        params = {
            "part":           "snippet",
            "q":              f"{topic} crypto",
            "type":           "video",
            "order":          "date",
            "publishedAfter": cutoff,
            "maxResults":     5,
            "relevanceLanguage": "es",
            "key":            self.yt_api_key,
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(YT_API_SEARCH, params=params)
            resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("items", []):
            snip = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId", "")
            results.append({
                "title":     snip.get("title", ""),
                "channel":   snip.get("channelTitle", ""),
                "published": snip.get("publishedAt", ""),
                "url":       f"https://youtube.com/watch?v={video_id}",
                "thumbnail": snip.get("thumbnails", {}).get("medium", {}).get("url", ""),
                "views":     0,      # requiere videos.list con statistics
                "duration":  "N/A",  # requiere videos.list con contentDetails
                "source":    "yt_api",
            })
        return results

    # ── YouTube RSS fallback ───────────────────────────────────────────────

    def _search_youtube_rss(self, topic: str) -> List[Dict[str, Any]]:
        """
        Busca vídeos vía Invidious/Piped RSS público como fallback.
        Usa la API de búsqueda no oficial con feedparser.
        """
        import feedparser
        query = urllib.parse.quote(f"{topic} crypto")
        # Canal de Piped como fallback de búsqueda
        urls_to_try = [
            f"https://www.youtube.com/feeds/videos.xml?search_query={query}",
        ]
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=48)
        results = []

        for url in urls_to_try:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:10]:
                    import email.utils
                    import time as _t
                    pub = datetime.now(tz=timezone.utc)
                    for attr in ("published_parsed", "updated_parsed"):
                        val = getattr(entry, attr, None)
                        if val:
                            try:
                                pub = datetime.fromtimestamp(_t.mktime(val), tz=timezone.utc)
                            except Exception:
                                pass
                            break

                    if pub < cutoff:
                        continue

                    video_url = getattr(entry, "link", "")
                    results.append({
                        "title":     getattr(entry, "title", "Sin título"),
                        "channel":   getattr(entry, "author", "Desconocido"),
                        "published": pub.isoformat(),
                        "url":       video_url,
                        "thumbnail": "",
                        "views":     0,
                        "duration":  "N/A",
                        "source":    "rss",
                    })
                if results:
                    break
            except Exception as exc:
                self.logger.warning(f"RSS YouTube fallido ({url[:40]}): {exc}")

        return results[:5]

    # ── Gap analysis ──────────────────────────────────────────────────────

    def _identify_gap(self, topic: str, competitors: List[Dict]) -> str:
        """
        Heurístico simple: detecta qué sub-ángulos están cubiertos
        y sugiere el ángulo no tratado.
        """
        if not competitors:
            return f"Nadie ha cubierto '{topic}' en las últimas 48h — primera mover advantage."

        all_titles = " ".join(c.get("title", "") for c in competitors).lower()

        angles = {
            "precio/predicción":    ["precio", "price", "prediction", "prediccion", "target"],
            "análisis técnico":     ["analisis", "analysis", "chart", "soporte", "resistencia"],
            "noticias/fundamentos": ["noticia", "news", "fundament", "por qué", "why"],
            "tutorial/educativo":   ["como", "cómo", "tutorial", "guia", "guide", "aprende"],
            "opinión/debate":       ["opinion", "debate", "creo", "think", "view"],
        }
        covered = [name for name, kws in angles.items() if any(k in all_titles for k in kws)]
        uncovered = [name for name in angles if name not in covered]

        if uncovered:
            return (
                f"Ángulos no cubiertos por competidores: {', '.join(uncovered)}. "
                f"Recomendado: enfoque en '{uncovered[0]}'."
            )
        return (
            "Todos los ángulos básicos cubiertos. "
            "Diferenciarse con datos exclusivos, perspectiva latinoamericana o ángulo regulatorio español."
        )

    # ── DB ────────────────────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {COMPETITORS_TABLE} (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        pipeline_id  TEXT,
                        title        TEXT,
                        channel      TEXT,
                        url          TEXT,
                        views        INTEGER DEFAULT 0,
                        duration     TEXT,
                        thumbnail    TEXT,
                        published    TEXT,
                        gap_analysis TEXT,
                        recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
        except Exception as exc:
            self.logger.warning(f"No se pudo crear tabla {COMPETITORS_TABLE}: {exc}")

    def _persist(self, pipeline_id: str, competitors: List[Dict], gap: str) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                for c in competitors:
                    conn.execute(
                        f"""
                        INSERT INTO {COMPETITORS_TABLE}
                            (pipeline_id, title, channel, url, views, duration, thumbnail, published, gap_analysis)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pipeline_id,
                            c.get("title", ""),
                            c.get("channel", ""),
                            c.get("url", ""),
                            c.get("views", 0),
                            c.get("duration", "N/A"),
                            c.get("thumbnail", ""),
                            c.get("published", ""),
                            gap,
                        ),
                    )
        except Exception as exc:
            self.logger.warning(f"Persistencia competidores fallida: {exc}")

    def _print_table(self, competitors: List[Dict], gap: str) -> None:
        table = Table(
            title="[bold #F7931A]RECON — Competidores recientes[/]",
            style="bold white",
            border_style="#F7931A",
            show_header=True,
            header_style="bold #F7931A",
        )
        table.add_column("#",       justify="center", style="dim white", width=4)
        table.add_column("Canal",   justify="left",  style="bold white", width=20)
        table.add_column("Título",  justify="left",  style="white", width=46)
        table.add_column("Views",   justify="right", style="dim white", width=10)
        table.add_column("Duración",justify="center", style="dim white", width=10)

        for idx, c in enumerate(competitors, 1):
            views = c.get("views", 0)
            views_str = f"{views:,}" if views else "N/A"
            table.add_row(
                str(idx),
                c.get("channel", "")[:18],
                c.get("title", "")[:44],
                views_str,
                c.get("duration", "N/A"),
            )

        console.print(table)
        console.print(f"[bold #F7931A]GAP detectado:[/] [white]{gap}[/]\n")

    # ── run ────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("RECON iniciado — analizando competidores...")
        try:
            self._ensure_table()
            topic = ctx.topic or "bitcoin"

            competitors: List[Dict] = []

            # Intentar API v3 primero, RSS como fallback
            if self.yt_api_key:
                self.logger.debug("Usando YouTube Data API v3...")
                try:
                    competitors = self._search_youtube_api(topic)
                except Exception as api_exc:
                    self.logger.warning(f"API v3 fallida: {api_exc}. Usando RSS...")
                    competitors = self._search_youtube_rss(topic)
            else:
                self.logger.info("Sin YOUTUBE_API_KEY — usando RSS fallback...")
                competitors = self._search_youtube_rss(topic)

            gap = self._identify_gap(topic, competitors)

            ctx.competitors = competitors

            self._print_table(competitors, gap)
            self._persist(ctx.pipeline_id, competitors, gap)

            self.logger.info(
                f"RECON completado. {len(competitors)} competidores. Gap: {gap[:60]}..."
            )

        except Exception as e:
            self.logger.error(f"RECON error: {e}")
            ctx.add_error("RECON", str(e))

        return ctx

