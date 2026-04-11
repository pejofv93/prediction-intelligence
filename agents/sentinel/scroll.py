from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
"""
scroll.py
SCROLL — Newsletter semanal de CryptoVerdad via Telegram.
Genera y envía cada lunes a las 10:00 UTC un digest con:
  - Top 3 vídeos de la semana por vistas
  - Precios BTC/ETH/SOL con variación 7d (CoinGecko)
  - Próximos temas del canal
Guarda el envío en telegram_notifications para no duplicar.
"""

import asyncio
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from rich.console import Console
from rich.panel import Panel

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# ── Constantes ────────────────────────────────────────────────────────────────
COINGECKO_BASE_URL = os.getenv(
    "COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3"
)
SCROLL_SEND_WEEKDAY = 0   # lunes (Monday = 0 en Python)
SCROLL_SEND_HOUR_UTC = 10  # 10:00 UTC
CHANNEL_URL = "https://youtube.com/@CryptoVerdad"

# Temas genéricos de relleno si no hay ctx.articles
_GENERIC_TOPICS = [
    "Análisis técnico de Bitcoin esta semana",
    "Ethereum y el mercado DeFi",
    "Altcoins con mayor potencial del mes",
    "Novedades de regulación cripto en Europa",
    "Cómo leer el gráfico de dominancia BTC",
]


class SCROLL(BaseAgent):
    """
    Genera y envía el resumen semanal de CryptoVerdad al canal de Telegram.
    Solo envía si es lunes a las 10:00 UTC o si ctx.metadata['scroll_force'] = True.
    Verifica la BD para no duplicar el envío dentro de la misma semana.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("SCROLL")
        self._ensure_table()

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold cyan]SCROLL[/] iniciado")
        try:
            forced = ctx.metadata.get("scroll_force", False)
            now_utc = datetime.now(timezone.utc)

            # 1. Verificar si toca enviar
            if not forced and not self._is_send_time(now_utc):
                ctx.metadata["scroll_skipped"] = "not_monday"
                self.logger.info(
                    "[cyan]SCROLL[/] no es lunes 10:00 UTC — digest omitido"
                )
                return ctx

            # 2. Verificar que no se haya enviado ya esta semana
            if self._already_sent_this_week():
                ctx.metadata["scroll_skipped"] = "already_sent_this_week"
                self.logger.info(
                    "[cyan]SCROLL[/] ya se envió el digest esta semana — omitido"
                )
                return ctx

            # 3. Credenciales Telegram
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            chat_id = (
                os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
                or os.getenv("TELEGRAM_CHAT_ID", "").strip()
            )

            if not token or not chat_id:
                self.logger.warning(
                    "[yellow]SCROLL[/] Telegram no configurado "
                    "(TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID) — skip silencioso"
                )
                ctx.metadata["scroll_skipped"] = "telegram_not_configured"
                return ctx

            # 4. Generar digest
            week_start, week_end = self._week_range(now_utc)
            top_videos = self._fetch_top_videos(week_start)
            prices = self._fetch_prices(ctx)
            next_topics = self._resolve_next_topics(ctx)

            message = self._build_message(
                week_start, week_end, top_videos, prices, next_topics
            )

            # 5. Enviar
            message_id = asyncio.run(self._send(token, chat_id, message))
            ctx.metadata["scroll_sent"] = True
            ctx.metadata["scroll_message_id"] = message_id

            # 6. Persistir en BD
            self._persist(ctx, chat_id, message_id, message, week_start)

            try:
                console.print(
                    Panel(
                        f"[bold green]SCROLL[/] Digest semanal enviado\n"
                        f"Chat: {chat_id} | Message ID: {message_id}",
                        border_style="green",
                    )
                )
            except Exception:
                pass

            self.logger.info(
                f"[green]SCROLL[/] digest enviado correctamente (id={message_id})"
            )

        except Exception as exc:
            self.logger.error(f"[red]SCROLL error:[/] {exc}")
            ctx.add_error("SCROLL", str(exc))
            ctx.metadata["scroll_sent"] = False

        return ctx

    # ── Comprobación de tiempo y duplicado ────────────────────────────────────
    def _is_send_time(self, now_utc: datetime) -> bool:
        """Devuelve True si es lunes y la hora UTC es 10 (± tolerancia de 30 min)."""
        is_monday = now_utc.weekday() == SCROLL_SEND_WEEKDAY
        is_ten_utc = now_utc.hour == SCROLL_SEND_HOUR_UTC
        return is_monday and is_ten_utc

    def _already_sent_this_week(self) -> bool:
        """Comprueba en BD si ya se registró un envío SCROLL esta semana."""
        try:
            # Lunes de la semana actual en UTC
            today = datetime.now(timezone.utc).date()
            monday = today - timedelta(days=today.weekday())
            monday_str = monday.isoformat()

            with self.db._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) as cnt
                    FROM scroll_digests
                    WHERE week_start = ?
                    """,
                    (monday_str,),
                ).fetchone()
            return (row["cnt"] if row else 0) > 0
        except Exception as exc:
            self.logger.warning(
                f"[yellow]SCROLL[/] no se pudo verificar envío previo: {exc}"
            )
            return False

    # ── Rango de la semana ────────────────────────────────────────────────────
    @staticmethod
    def _week_range(now_utc: datetime):
        """Devuelve (lunes, domingo) de la semana en curso."""
        today = now_utc.date()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    # ── Datos: vídeos top ─────────────────────────────────────────────────────
    def _fetch_top_videos(self, week_start) -> List[Dict[str, Any]]:
        """
        Recupera los 3 vídeos con más vistas publicados en los últimos 7 días.
        Fallback: usa los últimos 3 pipelines completados si no hay datos de analytics.
        """
        try:
            week_start_str = week_start.isoformat()
            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT title, url, views, likes
                    FROM videos
                    WHERE created_at >= ?
                    ORDER BY views DESC
                    LIMIT 3
                    """,
                    (week_start_str,),
                ).fetchall()

            if rows:
                return [dict(r) for r in rows]

            # Fallback: pipelines completados
            self.logger.info(
                "[cyan]SCROLL[/] sin datos analytics — usando pipelines como proxy"
            )
            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT topic as title, youtube_url as url,
                           0 as views, 0 as likes
                    FROM pipelines
                    WHERE status LIKE 'completed%'
                      AND youtube_url IS NOT NULL
                    ORDER BY completed_at DESC
                    LIMIT 3
                    """,
                ).fetchall()
            return [dict(r) for r in rows]

        except Exception as exc:
            self.logger.warning(
                f"[yellow]SCROLL[/] error obteniendo vídeos: {exc}"
            )
            return []

    # ── Datos: precios CoinGecko ──────────────────────────────────────────────
    def _fetch_prices(self, ctx: Context) -> Optional[Dict[str, Any]]:
        """
        Intenta obtener precios de ctx.prices primero.
        Si no hay datos, llama a CoinGecko /simple/price (sin key).
        Devuelve None si falla todo.
        """
        # 1. Desde ctx
        if ctx.prices:
            return self._normalize_prices(ctx.prices)

        # 2. CoinGecko
        try:
            import urllib.request
            import json as _json

            url = (
                f"{COINGECKO_BASE_URL}/simple/price"
                "?ids=bitcoin,ethereum,solana"
                "&vs_currencies=usd"
                "&include_7d_change=true"
            )
            req = urllib.request.Request(
                url, headers={"User-Agent": "NEXUS-CryptoVerdad/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            self.logger.info("[cyan]SCROLL[/] precios obtenidos desde CoinGecko")
            return self._normalize_prices(data)

        except Exception as exc:
            self.logger.warning(
                f"[yellow]SCROLL[/] CoinGecko no disponible: {exc} — sección precios omitida"
            )
            return None

    @staticmethod
    def _normalize_prices(raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normaliza el dict de precios (acepta tanto CoinGecko directo como ctx.prices).
        Devuelve { 'BTC': {'price': ..., 'change_7d': ...}, ... }
        """
        mapping = {
            "bitcoin": "BTC",
            "ethereum": "ETH",
            "solana": "SOL",
        }
        result = {}
        for coin_id, symbol in mapping.items():
            entry = raw.get(coin_id, {})
            if not entry:
                continue
            price = (
                entry.get("usd")
                or entry.get("price_usd")
                or entry.get("usd_price")
            )
            change = (
                entry.get("usd_7d_change")
                or entry.get("usd_price_change_percentage_7d")
                or entry.get("price_change_7d")
                or 0.0
            )
            if price is not None:
                result[symbol] = {"price": price, "change_7d": float(change)}
        return result

    # ── Datos: próximos temas ─────────────────────────────────────────────────
    def _resolve_next_topics(self, ctx: Context) -> List[str]:
        """
        Usa los títulos/resúmenes de ctx.articles[:3] si están disponibles.
        Fallback: lista predefinida genérica.
        """
        articles = getattr(ctx, "articles", None) or ctx.news
        if articles:
            topics = []
            for art in articles[:3]:
                title = (
                    art.get("title") or art.get("headline") or art.get("summary", "")
                )
                if title:
                    # Limitar longitud y limpiar
                    title = re.sub(r"\s+", " ", title).strip()[:80]
                    topics.append(title)
            if topics:
                return topics

        # Fallback genérico
        return _GENERIC_TOPICS[:3]

    # ── Construcción del mensaje ──────────────────────────────────────────────
    def _build_message(
        self,
        week_start,
        week_end,
        top_videos: List[Dict[str, Any]],
        prices: Optional[Dict[str, Any]],
        next_topics: List[str],
    ) -> str:
        lines: List[str] = []

        # Cabecera
        lunes_str = self._format_date_es(week_start)
        domingo_str = self._format_date_es(week_end)
        lines.append("📊 *SEMANA EN CRYPTOVERDAD*")
        lines.append(f"_Del {lunes_str} al {domingo_str}_")
        lines.append("")

        # Sección 1: Top vídeos
        lines.append("🏆 *Lo más visto esta semana:*")
        if top_videos:
            for i, v in enumerate(top_videos, start=1):
                title = self._md_escape(str(v.get("title") or "Sin título")[:55])
                url = v.get("url") or ""
                views = v.get("views") or 0
                if url:
                    if views:
                        lines.append(f"{i}\\. [{title}]({url}) — {views:,} views")
                    else:
                        lines.append(f"{i}\\. [{title}]({url})")
                else:
                    lines.append(f"{i}\\. {title}")
        else:
            lines.append("_Todavía no hay vídeos registrados esta semana\\._")
        lines.append("")

        # Sección 2: Precios
        if prices:
            lines.append("💰 *Precios al cierre:*")
            for symbol in ("BTC", "ETH", "SOL"):
                data = prices.get(symbol)
                if not data:
                    continue
                price = data["price"]
                change = data["change_7d"]
                arrow = "🟢" if change >= 0 else "🔴"
                sign = "+" if change >= 0 else ""
                if isinstance(price, float) and price < 1:
                    price_str = f"${price:.4f}"
                else:
                    price_str = f"${price:,.0f}" if isinstance(price, (int, float)) else str(price)
                lines.append(
                    f"• {arrow} {symbol}: {price_str} \\({sign}{change:.2f}% 7d\\)"
                )
            lines.append("")

        # Sección 3: Próximos temas
        lines.append("🔮 *Esta semana en CryptoVerdad:*")
        for topic in next_topics:
            lines.append(f"• {self._md_escape(topic)}")
        lines.append("")

        # Footer
        lines.append(
            "🔔 Activa la campanita para no perderte nada\\."
        )
        lines.append(f"[Ver canal →]({CHANNEL_URL})")
        lines.append("")
        lines.append(
            "_CryptoVerdad — Crypto sin humo\\. Análisis real, opinión directa\\._"
        )

        return "\n".join(lines)

    # ── Envío async (reutiliza patrón MERCURY) ────────────────────────────────
    async def _send(self, token: str, chat_id: str, text: str) -> int:
        try:
            from telegram import Bot
            from telegram.error import RetryAfter, TelegramError
        except ImportError as exc:
            raise ImportError(
                "Instala python-telegram-bot: pip install python-telegram-bot"
            ) from exc

        bot = Bot(token=token)
        attempt = 0
        while True:
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=False,
                )
                return msg.message_id
            except RetryAfter as exc:
                wait = int(exc.retry_after) + 1
                self.logger.warning(
                    f"[yellow]SCROLL[/] Telegram FloodWait — esperando {wait}s"
                )
                await asyncio.sleep(wait)
                attempt += 1
                if attempt > 3:
                    raise RuntimeError(
                        "SCROLL: demasiados retries por rate limit de Telegram"
                    ) from exc
            except TelegramError as exc:
                raise RuntimeError(
                    f"SCROLL: Telegram error al enviar digest: {exc}"
                ) from exc

    # ── Persistencia ──────────────────────────────────────────────────────────
    def _ensure_table(self) -> None:
        """Crea la tabla scroll_digests si no existe."""
        try:
            with self.db._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scroll_digests (
                        id           INTEGER   PRIMARY KEY AUTOINCREMENT,
                        pipeline_id  TEXT,
                        chat_id      TEXT,
                        message_id   INTEGER,
                        week_start   DATE      NOT NULL,
                        message_text TEXT,
                        sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
        except Exception as exc:
            self.logger.error(
                f"[red]SCROLL[/] no se pudo crear tabla scroll_digests: {exc}"
            )
            raise

    def _persist(
        self,
        ctx: Context,
        chat_id: str,
        message_id: int,
        message_text: str,
        week_start,
    ) -> None:
        """Guarda el digest en scroll_digests y también en telegram_notifications."""
        try:
            week_start_str = week_start.isoformat()
            with self.db._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO scroll_digests
                        (pipeline_id, chat_id, message_id, week_start, message_text)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ctx.pipeline_id, chat_id, message_id, week_start_str, message_text),
                )
            # También en la tabla genérica de notificaciones Telegram
            self.db.save_telegram_notification(
                pipeline_id=ctx.pipeline_id,
                chat_id=chat_id,
                message_id=message_id,
                message_text=message_text,
            )
            self.logger.debug(
                f"[cyan]SCROLL[/] digest persistido (semana={week_start_str})"
            )
        except Exception as exc:
            self.logger.warning(
                f"[yellow]SCROLL[/] no se pudo persistir digest en BD: {exc}"
            )

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _md_escape(text: str) -> str:
        """
        Escapa caracteres especiales para MarkdownV2 de Telegram.
        Referencia: https://core.telegram.org/bots/api#markdownv2-style
        """
        # Caracteres que MarkdownV2 requiere escapar
        special = r"\_*[]()~`>#+-=|{}.!"
        result = []
        for ch in text:
            if ch in special:
                result.append(f"\\{ch}")
            else:
                result.append(ch)
        return "".join(result)

    @staticmethod
    def _format_date_es(d) -> str:
        """Formatea una fecha en español abreviado: '7 abr'."""
        meses = {
            1: "ene", 2: "feb", 3: "mar", 4: "abr",
            5: "may", 6: "jun", 7: "jul", 8: "ago",
            9: "sep", 10: "oct", 11: "nov", 12: "dic",
        }
        return f"{d.day} {meses[d.month]}"
