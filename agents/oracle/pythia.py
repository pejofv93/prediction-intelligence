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
    ("CoinDesk",        "https://feeds.feedburner.com/CoinDesk"),
    ("CoinTelegraph",   "https://cointelegraph.com/rss"),
    ("Decrypt",         "https://decrypt.co/feed"),
    ("TheDefiant",      "https://thedefiant.io/feed"),
    ("TheBlock",        "https://www.theblock.co/rss.xml"),
    ("BitcoinMagazine", "https://bitcoinmagazine.com/.rss/full/"),
    ("CryptoBriefing",  "https://cryptobriefing.com/feed/"),
    ("CoinDeskAlt",     "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("GoogleNews-BTC",  "https://news.google.com/rss/search?q=bitcoin+crypto&hl=es&gl=ES&ceid=ES:es"),
    ("GoogleNews-ETH",  "https://news.google.com/rss/search?q=ethereum+defi&hl=es&gl=ES&ceid=ES:es"),
    # Nitter RSS — tweets de influencers clave crypto
    # Nitter es un frontend open-source de Twitter que expone RSS sin autenticación
    ("Nitter-Saylor",   "https://nitter.net/saylor/rss"),
    ("Nitter-CZ",       "https://nitter.net/cz_binance/rss"),
    ("Nitter-PlanB",    "https://nitter.net/100trillionusd/rss"),
    ("Nitter-Pomp",     "https://nitter.net/APompliano/rss"),
]

# Palabras clave crypto mínimas para filtrar tweets de fuentes Nitter
# (evita que tweets sobre política o deportes entren en el contexto del pipeline)
_NITTER_CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "crypto", "ethereum", "eth", "market", "price", "análisis", "$",
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
        Mantenido por compatibilidad — internamente delega a _score_article_v2.
        """
        article = {"title": title, "summary": summary}
        return self._score_article_v2(article, topic)

    def _score_article_v2(self, article: dict, topic: str) -> int:
        """
        Scoring multidimensional 0-100:
        - Relevancia temporal: artículos de las últimas 4h reciben bonus +20
        - Densidad de datos: menciona precios concretos, %, estadísticas → +15
        - Impacto potencial: keywords de alta señal (SEC, ETF, hack, halving) → +30
        - Longitud: artículos >200 palabras en summary → +10
        - Fuente: CoinDesk/Decrypt > fuentes desconocidas → +10
        - Urgencia: keywords de crisis → score mínimo garantizado de 70
        - Topic match: palabras del topic en texto → bonus hasta +15
        """
        import time as _time_mod
        score = 0
        title = (article.get("title") or "").lower()
        summary = (article.get("summary") or "").lower()
        full_text = title + " " + summary
        published = article.get("published_parsed") or article.get("updated_parsed")

        # 1. Relevancia temporal
        if published:
            try:
                age_hours = (_time_mod.time() - _time_mod.mktime(published)) / 3600
                if age_hours < 2:
                    score += 20
                elif age_hours < 6:
                    score += 12
                elif age_hours < 24:
                    score += 5
            except Exception:
                pass

        # 2. Densidad de datos (precios, porcentajes, millones/billones)
        data_patterns = re.findall(
            r'\$[\d,]+|\d+[\.,]\d*%|\d+\s*(?:million|billion|millones|millardos|btc|eth)',
            full_text
        )
        score += min(15, len(data_patterns) * 5)

        # 3. Keywords de alto impacto (señal real)
        high_signal = [
            "sec", "etf", "hack", "exploit", "halving", "blackrock",
            "fbi", "treasury", "fed ", "interest rate", "binance ban",
            "coinbase", "regulation", "bill", "congress", "senate",
            "microstrategy", "el salvador", "cbdc", "taproot",
        ]
        hits = sum(1 for kw in high_signal if kw in full_text)
        score += min(30, hits * 10)

        # 4. Longitud del summary (más contenido = más analizable)
        word_count = len(summary.split())
        if word_count > 200:
            score += 10
        elif word_count > 100:
            score += 5

        # 5. Fuente de calidad
        source = (
            article.get("source", {}).get("href", "")
            if isinstance(article.get("source"), dict)
            else str(article.get("source", ""))
        )
        source = (source + " " + (article.get("link", "") or "")).lower()
        quality_sources = ["coindesk", "cointelegraph", "decrypt", "theblock",
                           "bloomberg", "reuters", "ft.com", "wsj"]
        if any(s in source for s in quality_sources):
            score += 10

        # 6. Topic match
        topic_words = [w for w in re.findall(r"\w+", topic.lower()) if len(w) > 2]
        topic_hits = sum(1 for w in topic_words if w in full_text)
        score += min(15, topic_hits * 5)

        # 7. Urgencia mínima garantizada
        crisis_words = ["hack", "exploit", "crash", "ban", "seized", "arrested",
                        "fud", "collapse", "liquidated", "emergency"]
        if any(w in full_text for w in crisis_words):
            score = max(score, 70)

        return min(100, score)

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

    def _deduplicate_articles(self, articles: list) -> list:
        """
        Elimina artículos duplicados por similitud de título (>70% palabras en común).
        Ordena por score descendente antes de deduplicar: se queda la copia
        con mayor puntuación cuando varias fuentes cubren la misma noticia.
        """
        seen_titles: list = []
        unique: list = []
        for art in sorted(articles, key=lambda x: x.get("relevance", 0), reverse=True):
            title_words = set(re.findall(r"\w+", art.get("title", "").lower()))
            if not title_words:
                unique.append(art)
                continue
            is_dup = any(
                len(title_words & set(re.findall(r"\w+", seen))) / max(len(title_words), 1) > 0.7
                for seen in seen_titles
            )
            if not is_dup:
                seen_titles.append(art.get("title", "").lower())
                unique.append(art)
        return unique

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

            # RSS_SOURCES ya incluye los 10 feeds fijos; Google News dinámico se añade aquí
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

            # Filtro Nitter: solo aceptar tweets que contengan al menos una
            # palabra clave crypto (evita política, deportes, etc.)
            def _is_nitter_relevant(item: dict) -> bool:
                title = (item.get("title") or "").lower()
                return any(kw in title for kw in _NITTER_CRYPTO_KEYWORDS)

            recent = [
                i for i in recent
                if not i.get("source", "").startswith("Nitter") or _is_nitter_relevant(i)
            ]
            self.logger.info(
                f"Noticias últimas 24h: {len(recent)} / total {len(all_items)}"
            )

            # Scoring v2 multidimensional + detección urgencia
            urgent_found = False
            for item in recent:
                # Pasar el dict del feed directamente para que _score_article_v2
                # pueda leer published_parsed si feedparser lo expone en el original.
                # El item ya normalizado no tiene ese campo, así que usamos
                # la interfaz pública que acepta title+summary.
                item["relevance"] = self._score_article_v2(
                    {"title": item["title"], "summary": item.get("summary", ""),
                     "link": item.get("url", ""), "source": item.get("source", "")},
                    topic
                )
                item["urgent"] = self._has_urgency(
                    item["title"], item.get("summary", "")
                )
                if item["urgent"]:
                    urgent_found = True
                    self.logger.warning(
                        f"[bold #F44336]URGENTE:[/] {item['title'][:60]}"
                    )

            # Deduplicar antes de seleccionar top noticias
            recent_unique = self._deduplicate_articles(recent)
            self.logger.info(
                f"Tras deduplicación: {len(recent_unique)} artículos únicos "
                f"(eliminados {len(recent) - len(recent_unique)} duplicados)"
            )

            # Top 5 por relevancia
            top5 = sorted(recent_unique, key=lambda x: x["relevance"], reverse=True)[:5]

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

