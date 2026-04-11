from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
pythia.py
PYTHIA — Oráculo de Noticias
Capa ORÁCULO · NEXUS v1.0 · CryptoVerdad

Agrega noticias RSS de fuentes cripto de referencia, filtra las últimas
24h, puntúa relevancia respecto al topic del pipeline y detecta eventos
de urgencia (hack, exploit, SEC, crash).
"""

import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import feedparser
from rich.console import Console
from rich.table import Table

from core.base_agent import BaseAgent
from core.context import Context
from utils.logger import get_logger

console = Console()

# ── Configuración de fuentes RSS ──────────────────────────────────────────────
RSS_SOURCES = [
    ("CoinDesk",      "https://feeds.feedburner.com/CoinDesk"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",       "https://decrypt.co/feed"),
]

URGENCY_KEYWORDS = {
    "hack", "exploit", "hacked", "sec", "crash", "ban", "banned",
    "attack", "breach", "rugpull", "rug pull", "sanction", "sanciones",
    "regulacion", "quiebra", "bankruptcy", "collapse", "colapso",
}

NEWS_TABLE = "oracle_news"


class PYTHIA(BaseAgent):
    """Oráculo de noticias cripto con scoring de relevancia."""

    def __init__(self, config: dict, db):
        self.config = config
        self.db = db
        self.logger = get_logger("PYTHIA")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _google_news_url(self, topic: str) -> str:
        safe = topic.replace(" ", "+")
        return (
            f"https://news.google.com/rss/search"
            f"?q={safe}+crypto&hl=es&gl=ES&ceid=ES:es"
        )

    def _parse_date(self, entry) -> datetime:
        """Extrae fecha del entry; devuelve datetime aware UTC."""
        import email.utils
        for attr in ("published_parsed", "updated_parsed"):
            val = getattr(entry, attr, None)
            if val:
                try:
                    import time as _time
                    ts = _time.mktime(val)
                    return datetime.fromtimestamp(ts, tz=timezone.utc)
                except Exception:
                    pass
        # Fallback: published string
        raw = getattr(entry, "published", "") or getattr(entry, "updated", "")
        if raw:
            try:
                tup = email.utils.parsedate_to_datetime(raw)
                if tup.tzinfo is None:
                    tup = tup.replace(tzinfo=timezone.utc)
                return tup
            except Exception:
                pass
        return datetime.now(tz=timezone.utc)

    def _score_relevance(self, title: str, summary: str, topic: str) -> int:
        """
        Puntúa relevancia 0-100 de una noticia respecto al topic.
        Simple TF heurístico por palabras clave.
        """
        text = (title + " " + summary).lower()
        topic_words = re.findall(r"\w+", topic.lower())
        crypto_keywords = {
            "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
            "crypto", "blockchain", "defi", "nft", "altcoin", "web3",
            "binance", "bnb", "stablecoin", "usdt", "usdc",
        }
        score = 0
        for word in topic_words:
            if len(word) > 2 and word in text:
                score += 25
        for kw in crypto_keywords:
            if kw in text:
                score += 5
        return min(score, 100)

    def _has_urgency(self, title: str, summary: str) -> bool:
        text = (title + " " + summary).lower()
        return any(kw in text for kw in URGENCY_KEYWORDS)

    def _extract_article_image(self, entry) -> str:
        """Extrae URL de imagen del artículo RSS. Devuelve str vacío si no hay."""
        try:
            # feedparser: media_content
            if hasattr(entry, "media_content") and entry.media_content:
                for media in entry.media_content:
                    if media.get("medium") == "image" or media.get("type", "").startswith("image"):
                        url = media.get("url", "")
                        if url:
                            return url
            # feedparser: enclosures
            if hasattr(entry, "enclosures") and entry.enclosures:
                for enc in entry.enclosures:
                    if enc.get("type", "").startswith("image"):
                        href = enc.get("url", "") or enc.get("href", "")
                        if href:
                            return href
            # feedparser: links con rel=enclosure
            if hasattr(entry, "links"):
                for link in entry.links:
                    if link.get("rel") == "enclosure" and "image" in link.get("type", ""):
                        return link.get("href", "")
            # Buscar <img src="..."> en summary o content HTML
            content = getattr(entry, "summary", "") or ""
            if not content and hasattr(entry, "content") and entry.content:
                content = entry.content[0].get("value", "")
            if content:
                match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return ""

    def _fetch_feed(self, name: str, url: str) -> List[Dict[str, Any]]:
        """Parsea un feed RSS y devuelve lista de items normalizados."""
        try:
            feed = feedparser.parse(url)
            items = []
            for entry in feed.entries:
                items.append({
                    "title":     getattr(entry, "title", "Sin título"),
                    "url":       getattr(entry, "link", ""),
                    "source":    name,
                    "summary":   getattr(entry, "summary", ""),
                    "published": self._parse_date(entry),
                    "image_url": self._extract_article_image(entry),
                })
            return items
        except Exception as exc:
            self.logger.warning(f"Feed {name} falló: {exc}")
            return []

    def _ensure_table(self) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {NEWS_TABLE} (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        pipeline_id  TEXT,
                        title        TEXT,
                        url          TEXT,
                        source       TEXT,
                        published    TEXT,
                        relevance    INTEGER,
                        is_urgent    INTEGER DEFAULT 0,
                        image_url    TEXT    DEFAULT '',
                        recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                # Migración: añadir columna image_url si la tabla ya existe sin ella
                try:
                    conn.execute(f"ALTER TABLE {NEWS_TABLE} ADD COLUMN image_url TEXT DEFAULT ''")
                except Exception:
                    pass  # La columna ya existe — ignorar
        except Exception as exc:
            self.logger.warning(f"No se pudo crear tabla {NEWS_TABLE}: {exc}")

    def _persist(self, pipeline_id: str, news: List[Dict]) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                for item in news:
                    conn.execute(
                        f"""
                        INSERT INTO {NEWS_TABLE}
                            (pipeline_id, title, url, source, published, relevance, is_urgent, image_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pipeline_id,
                            item.get("title", ""),
                            item.get("url", ""),
                            item.get("source", ""),
                            item.get("published", ""),
                            item.get("relevance", 0),
                            1 if item.get("urgent", False) else 0,
                            item.get("image_url", ""),
                        ),
                    )
        except Exception as exc:
            self.logger.warning(f"Persistencia de noticias fallida: {exc}")

    def _print_table(self, news: List[Dict]) -> None:
        table = Table(
            title="[bold #F7931A]PYTHIA — Top Noticias[/]",
            style="bold white",
            border_style="#F7931A",
            show_header=True,
            header_style="bold #F7931A",
        )
        table.add_column("Fuente",     justify="left",  style="dim white", width=14)
        table.add_column("Título",     justify="left",  style="bold white", width=52)
        table.add_column("Relevancia", justify="center", width=12)
        table.add_column("Publicado",  justify="center", style="dim white", width=18)

        for item in news:
            rel = item.get("relevance", 0)
            rel_color = "#4CAF50" if rel >= 60 else ("#F7931A" if rel >= 30 else "#888888")
            pub = item.get("published", "")
            if isinstance(pub, datetime):
                pub = pub.strftime("%d/%m %H:%M")
            table.add_row(
                item.get("source", ""),
                item.get("title", "")[:50],
                f"[{rel_color}]{rel}/100[/]",
                pub,
            )
        console.print(table)

    # ── run ────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("PYTHIA iniciada — agregando noticias RSS...")
        try:
            self._ensure_table()
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
            topic = ctx.topic or "crypto bitcoin"

            sources = list(RSS_SOURCES) + [
                ("Google News", self._google_news_url(topic))
            ]

            all_items: List[Dict[str, Any]] = []
            for name, url in sources:
                self.logger.debug(f"Fetching {name}...")
                items = self._fetch_feed(name, url)
                all_items.extend(items)

            # Filtrar últimas 24h
            recent = [
                i for i in all_items
                if i["published"] >= cutoff
            ]
            self.logger.info(
                f"Noticias últimas 24h: {len(recent)} / total {len(all_items)}"
            )

            # Scoring y detección urgencia
            urgent_found = False
            for item in recent:
                item["relevance"] = self._score_relevance(
                    item["title"], item.get("summary", ""), topic
                )
                item["urgent"] = self._has_urgency(
                    item["title"], item.get("summary", "")
                )
                if item["urgent"]:
                    urgent_found = True
                    self.logger.warning(
                        f"[bold #F44336]URGENTE:[/] {item['title'][:60]}"
                    )

            # Top 5 por relevancia
            top5 = sorted(recent, key=lambda x: x["relevance"], reverse=True)[:5]

            # Formatear para ctx (serializable)
            ctx.news = [
                {
                    "title":     i["title"],
                    "url":       i["url"],
                    "source":    i["source"],
                    "published": i["published"].isoformat() if isinstance(i["published"], datetime) else str(i["published"]),
                    "relevance": i["relevance"],
                    "image_url": i.get("image_url", ""),
                }
                for i in top5
            ]

            # Asignar imagen de la noticia más relevante al contexto para HEPHAESTUS
            for item in top5:
                img_url = item.get("image_url", "")
                if img_url:
                    ctx.news_image_url = img_url
                    self.logger.info(f"Imagen artículo encontrada: {img_url[:80]}")
                    break

            if urgent_found:
                ctx.is_urgent = True
                ctx.urgency_score += 25.0

            self._print_table(top5)
            self._persist(ctx.pipeline_id, top5)

            self.logger.info(
                f"PYTHIA completada. Top {len(ctx.news)} noticias. Urgente={ctx.is_urgent}"
            )

        except Exception as e:
            self.logger.error(f"PYTHIA error: {e}")
            ctx.add_error("PYTHIA", str(e))

        return ctx

