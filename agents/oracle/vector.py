from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
vector.py
VECTOR — Análisis de Tendencias
Capa ORÁCULO · NEXUS v1.0 · CryptoVerdad

Obtiene tendencias de búsqueda de Google Trends (España/Latam)
vía RSS y pytrends, filtra las relacionadas con cripto/finanzas
y detecta si el topic del pipeline está en trending.
"""

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import feedparser
import httpx
from rich.console import Console
from rich.table import Table

from core.base_agent import BaseAgent
from core.context import Context
from utils.logger import get_logger

console = Console()

TRENDS_TABLE = "oracle_trends"

# RSS de Google Trends por región
TRENDS_RSS_URLS = [
    ("ES", "https://trends.google.com/trends/trendingsearches/daily/rss?geo=ES"),
    ("MX", "https://trends.google.com/trends/trendingsearches/daily/rss?geo=MX"),
    ("AR", "https://trends.google.com/trends/trendingsearches/daily/rss?geo=AR"),
]

# Palabras que indican relación cripto/finanzas
CRYPTO_KEYWORDS = {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "criptomoneda", "blockchain",
    "solana", "sol", "bnb", "binance", "defi", "nft", "web3", "altcoin",
    "inversión", "inversion", "bolsa", "finanzas", "trading", "stablecoin",
    "ripple", "xrp", "cardano", "ada", "doge", "dogecoin", "litecoin",
    "criptoactivo", "token", "mineria", "minería",
}


class VECTOR(BaseAgent):
    """Analizador de tendencias virales en España y Latam."""

    def __init__(self, config: dict, db):
        self.config = config
        self.db = db
        self.logger = get_logger("VECTOR")

    # ── Fetch tendencias RSS ───────────────────────────────────────────────

    def _fetch_trends_rss(self, geo: str, url: str) -> List[str]:
        """Parsea feed RSS de Google Trends y devuelve lista de términos."""
        try:
            feed = feedparser.parse(url)
            terms = []
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                if title:
                    terms.append(title)
            self.logger.debug(f"Trends RSS {geo}: {len(terms)} entradas")
            return terms
        except Exception as exc:
            self.logger.warning(f"Trends RSS {geo} falló: {exc}")
            return []

    # ── Fetch tendencias pytrends ─────────────────────────────────────────

    def _fetch_pytrends(self, topic: str) -> List[str]:
        """
        Intenta usar pytrends para buscar tendencias relacionadas con el topic.
        Si pytrends no está instalado o falla, devuelve lista vacía.
        """
        try:
            from pytrends.request import TrendReq
            pt = TrendReq(hl="es-ES", tz=60, timeout=(10, 25))
            pt.build_payload(
                [topic, "bitcoin", "crypto"],
                cat=0,
                timeframe="now 7-d",
                geo="ES",
            )
            related = pt.related_queries()
            terms = []
            for kw, df_dict in related.items():
                top = df_dict.get("top")
                if top is not None and not top.empty:
                    terms.extend(top["query"].tolist()[:5])
            return list(set(terms))
        except ImportError:
            self.logger.debug("pytrends no instalado — usando solo RSS")
            return []
        except Exception as exc:
            self.logger.warning(f"pytrends falló: {exc}")
            return []

    # ── Filtrado cripto ───────────────────────────────────────────────────

    def _is_crypto_related(self, term: str) -> bool:
        term_lower = term.lower()
        return any(kw in term_lower for kw in CRYPTO_KEYWORDS)

    # ── Detección topic en trending ───────────────────────────────────────

    def _topic_in_trending(self, topic: str, trends: List[str]) -> bool:
        topic_words = set(re.findall(r"\w+", topic.lower()))
        for trend in trends:
            trend_words = set(re.findall(r"\w+", trend.lower()))
            if topic_words & trend_words:
                return True
        return False

    # ── DB ────────────────────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {TRENDS_TABLE} (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        pipeline_id  TEXT,
                        trend        TEXT,
                        geo          TEXT,
                        is_crypto    INTEGER DEFAULT 0,
                        recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
        except Exception as exc:
            self.logger.warning(f"No se pudo crear tabla {TRENDS_TABLE}: {exc}")

    def _persist(self, pipeline_id: str, trends: List[Dict]) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                for t in trends:
                    conn.execute(
                        f"""
                        INSERT INTO {TRENDS_TABLE}
                            (pipeline_id, trend, geo, is_crypto)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            pipeline_id,
                            t.get("term", ""),
                            t.get("geo", "ES"),
                            1 if t.get("is_crypto", False) else 0,
                        ),
                    )
        except Exception as exc:
            self.logger.warning(f"Persistencia de tendencias fallida: {exc}")

    def _print_table(self, trends: List[Dict], topic_trending: bool) -> None:
        table = Table(
            title="[bold #F7931A]VECTOR — Tendencias España/Latam[/]",
            style="bold white",
            border_style="#F7931A",
            show_header=True,
            header_style="bold #F7931A",
        )
        table.add_column("Región",  justify="center", style="dim white", width=8)
        table.add_column("Tendencia", justify="left", style="bold white", width=40)
        table.add_column("Cripto",  justify="center", width=8)

        for t in trends[:15]:
            is_c = t.get("is_crypto", False)
            crypto_marker = "[bold #4CAF50]SI[/]" if is_c else "[dim]no[/]"
            table.add_row(t.get("geo", "ES"), t.get("term", ""), crypto_marker)

        console.print(table)
        if topic_trending:
            console.print(
                "[bold #4CAF50]TOPIC EN TRENDING[/] — urgency_score +20\n"
            )

    # ── run ────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("VECTOR iniciado — analizando tendencias...")
        try:
            self._ensure_table()
            topic = ctx.topic or "bitcoin"

            all_trends: List[Dict] = []

            # 1. Google Trends RSS por región
            for geo, url in TRENDS_RSS_URLS:
                terms = self._fetch_trends_rss(geo, url)
                for term in terms:
                    all_trends.append({
                        "term":      term,
                        "geo":       geo,
                        "is_crypto": self._is_crypto_related(term),
                    })

            # 2. pytrends complementario
            pt_terms = self._fetch_pytrends(topic)
            for term in pt_terms:
                all_trends.append({
                    "term":      term,
                    "geo":       "ES",
                    "is_crypto": self._is_crypto_related(term),
                })

            # Priorizar tendencias cripto
            crypto_trends = [t for t in all_trends if t["is_crypto"]]
            other_trends  = [t for t in all_trends if not t["is_crypto"]]
            ordered = crypto_trends + other_trends

            # Guardar lista de strings en ctx (sin duplicados)
            seen = set()
            trend_strings = []
            for t in ordered:
                term = t["term"]
                if term not in seen:
                    seen.add(term)
                    trend_strings.append(term)

            ctx.trends = trend_strings[:20]

            # 3. Detectar si el topic está en trending
            topic_trending = self._topic_in_trending(topic, trend_strings)
            if topic_trending:
                ctx.urgency_score += 20.0
                self.logger.info(
                    f"[bold #4CAF50]Topic '{topic}' detectado en trending[/] — +20 urgency_score"
                )

            self._print_table(ordered, topic_trending)
            self._persist(ctx.pipeline_id, ordered[:20])

            self.logger.info(
                f"VECTOR completado. {len(ctx.trends)} tendencias. "
                f"Cripto: {len(crypto_trends)}. Urgency score: {ctx.urgency_score}"
            )

        except Exception as e:
            self.logger.error(f"VECTOR error: {e}")
            ctx.add_error("VECTOR", str(e))

        return ctx

