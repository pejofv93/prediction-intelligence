from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
mercury.py
MERCURY — Publicador Telegram de NEXUS.
Envía notificaciones al canal público y al bot privado via python-telegram-bot (async).
Prioridad de mensaje: ctx.telegram_message > mensaje automático > sin envío.
"""

import asyncio
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

TELEGRAM_RATE_RETRY_WAIT = 30  # segundos de espera ante FloodWait


class MERCURY(BaseAgent):
    """
    Envía el resumen del pipeline al canal de Telegram.
    Requiere en .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    Fallback sin credenciales: log warning, sin crash.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("MERCURY")

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold magenta]MERCURY[/] iniciado")
        try:
            token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHANNEL_ID", "")

            if not token:
                self.logger.warning(
                    "[yellow]MERCURY[/] TELEGRAM_BOT_TOKEN no configurado — notificación omitida"
                )
                return ctx

            if not chat_id:
                self.logger.warning(
                    "[yellow]MERCURY[/] TELEGRAM_CHAT_ID no configurado — notificación omitida"
                )
                return ctx

            # Determinar texto del mensaje
            message_text = self._resolve_message(ctx)
            if not message_text:
                self.logger.warning(
                    "[yellow]MERCURY[/] sin contenido para notificar — se omite envío"
                )
                return ctx

            # Enviar
            message_id = asyncio.run(self._send(token, chat_id, message_text))
            ctx.telegram_message_id = message_id

            self.logger.info(
                f"[green]MERCURY[/] mensaje enviado (chat={chat_id}, id={message_id})"
            )

            # Persistir en DB
            self._persist(ctx, chat_id, message_id, message_text)

        except Exception as exc:
            self.logger.error(f"[red]MERCURY error:[/] {exc}")
            ctx.add_error("MERCURY", str(exc))
        return ctx

    # ── resolución del mensaje ────────────────────────────────────────────────
    def _resolve_message(self, ctx: Context) -> str:
        """
        Prioridad:
        1. ctx.telegram_message (puesto por OLYMPUS u otro agente)
        2. Mensaje automático si ctx.youtube_url existe
        3. Mensaje completo enriquecido (comportamiento original)
        """
        # 1. Mensaje preformateado de otro agente
        telegram_msg = getattr(ctx, "telegram_message", "")
        if telegram_msg:
            return telegram_msg

        # 2. Mensaje automático mínimo si hay URL de YouTube
        if ctx.youtube_url:
            hora = datetime.now().strftime("%H:%M")
            seo_score = getattr(ctx, "seo_score", "N/A")
            title = self._md_escape(ctx.seo_title) if ctx.seo_title else "Sin título"
            return (
                f"🎬 Nuevo vídeo en CryptoVerdad\n"
                f"📺 {title}\n"
                f"🔗 {ctx.youtube_url}\n"
                f"📊 SEO Score: {seo_score}/100\n"
                f"⏰ {hora}"
            )

        # 3. Mensaje enriquecido (pipeline completo sin URL todavía)
        return self._build_rich_message(ctx)

    # ── mensaje enriquecido (pipeline completo) ───────────────────────────────
    def _build_rich_message(self, ctx: Context) -> str:
        lines = []

        # Cabecera
        if ctx.is_urgent:
            lines.append("🚨 *ALERTA URGENTE — CRYPTOVERDAD* 🚨")
            titulo = ctx.seo_title.upper() if ctx.seo_title else ""
        else:
            lines.append("📢 *NUEVO VÍDEO — CryptoVerdad*")
            titulo = ctx.seo_title

        if titulo:
            lines.append(f"\n*{self._md_escape(titulo)}*\n")

        # Precios actuales
        if ctx.prices:
            btc = ctx.prices.get("bitcoin", {})
            eth = ctx.prices.get("ethereum", {})
            btc_price = btc.get("usd", "N/A")
            eth_price = eth.get("usd", "N/A")
            btc_change = btc.get("usd_24h_change", 0.0)
            eth_change = eth.get("usd_24h_change", 0.0)
            btc_arrow = "🟢" if btc_change >= 0 else "🔴"
            eth_arrow = "🟢" if eth_change >= 0 else "🔴"
            lines.append("📊 *Precios actuales:*")
            lines.append(
                f"  {btc_arrow} BTC: `${btc_price:,.0f}` ({btc_change:+.2f}%)"
                if isinstance(btc_price, (int, float))
                else f"  {btc_arrow} BTC: `{btc_price}`"
            )
            lines.append(
                f"  {eth_arrow} ETH: `${eth_price:,.0f}` ({eth_change:+.2f}%)"
                if isinstance(eth_price, (int, float))
                else f"  {eth_arrow} ETH: `{eth_price}`"
            )
            lines.append("")

        # Resumen del guión
        if ctx.script:
            resumen = ctx.script[:280].replace("\n", " ").strip()
            if len(ctx.script) > 280:
                resumen += "..."
            lines.append("📝 *Resumen:*")
            lines.append(self._md_escape(resumen))
            lines.append("")

        # Links
        if ctx.youtube_url:
            lines.append(f"▶️ [Ver en YouTube]({ctx.youtube_url})")
        if ctx.tiktok_url:
            lines.append(f"🎵 [Ver en TikTok]({ctx.tiktok_url})")

        lines.append("\n_CryptoVerdad — Crypto sin humo. Análisis real, opinión directa._")
        return "\n".join(lines)

    # ── envío async ───────────────────────────────────────────────────────────
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
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                )
                return msg.message_id
            except RetryAfter as exc:
                wait = int(exc.retry_after) + 1
                self.logger.warning(
                    f"[yellow]MERCURY[/] Telegram FloodWait — esperando {wait}s"
                )
                await asyncio.sleep(wait)
                attempt += 1
                if attempt > 3:
                    raise RuntimeError(
                        "MERCURY: demasiados retries por rate limit"
                    ) from exc
            except TelegramError as exc:
                raise RuntimeError(f"MERCURY: Telegram error: {exc}") from exc

    # ── persistencia ──────────────────────────────────────────────────────────
    def _persist(
        self, ctx: Context, chat_id: str, message_id: int, message_text: str
    ) -> None:
        try:
            self.db.save_telegram_notification(
                pipeline_id=ctx.pipeline_id,
                chat_id=chat_id,
                message_id=message_id,
                message_text=message_text,
            )
        except Exception as exc:
            self.logger.warning(
                f"[yellow]MERCURY[/] no se pudo persistir notificación en DB: {exc}"
            )

    # ── volume health check ───────────────────────────────────────────────────
    def volume_health_check(self) -> dict:
        """
        Verifica uso del volumen /app/output y notifica Telegram según umbrales:
          >70% → aviso amarillo
          >85% → alerta roja + lanza cleanup automático
          >95% → CRISIS, alerta urgente
        Retorna dict con status, pct, free_gb, used_gb, total_gb.
        """
        output_dir = Path(os.getenv("OUTPUT_DIR", "/app/output"))
        if not output_dir.exists():
            output_dir = Path(__file__).resolve().parents[2] / "output"

        result = {"status": "ok", "pct": 0.0, "free_gb": 0.0, "used_gb": 0.0, "total_gb": 0.0}

        try:
            usage = shutil.disk_usage(output_dir)
            pct = usage.used / usage.total * 100
            result.update({
                "pct":      pct,
                "free_gb":  usage.free  / 1e9,
                "used_gb":  usage.used  / 1e9,
                "total_gb": usage.total / 1e9,
            })
            self.logger.info(
                f"MERCURY volume: {pct:.1f}% usado, {result['free_gb']:.2f}GB libres"
            )
        except Exception as e:
            self.logger.warning(f"MERCURY: no se pudo leer disco: {e}")
            return result

        if result["pct"] < 70:
            return result

        # Top 3 consumidores (para el mensaje)
        top_lines = []
        try:
            items = []
            for item in output_dir.iterdir():
                size = (
                    sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    if item.is_dir() else item.stat().st_size
                )
                items.append((item.name, size))
            for name, size in sorted(items, key=lambda x: x[1], reverse=True)[:3]:
                label = f"{size/1e9:.2f}GB" if size > 1e9 else f"{size/1e6:.0f}MB"
                top_lines.append(f"  - {name}: {label}")
        except Exception:
            pass

        action = "ninguna"
        if result["pct"] >= 95:
            result["status"] = "crisis"
            emoji = "🚨"
            level = "CRISIS — pipeline puede caer"
        elif result["pct"] >= 85:
            result["status"] = "critical"
            emoji = "🔴"
            level = "CRITICO — limpieza automática"
            try:
                scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
                subprocess.run(
                    [sys.executable, str(scripts_dir / "cleanup_volume.py"), "--confirm"],
                    timeout=180, capture_output=True,
                )
                action = "cleanup automático ejecutado"
                self.logger.info("MERCURY: cleanup automático completado")
            except Exception as ce:
                action = f"cleanup falló: {ce}"
                self.logger.warning(f"MERCURY: cleanup falló: {ce}")
        else:
            result["status"] = "warning"
            emoji = "⚠️"
            level = "ADVERTENCIA"

        top_text = "\n".join(top_lines) if top_lines else "  (no disponible)"
        msg = (
            f"{emoji} *NEXUS Volume Alert*\n"
            f"Nivel: *{level}*\n"
            f"Uso: `{result['pct']:.1f}%` "
            f"({result['used_gb']:.2f}GB / {result['total_gb']:.1f}GB)\n"
            f"Top consumidores:\n{top_text}\n"
            f"Accion: {action}"
        )
        result["message"] = msg
        self.logger.warning(f"MERCURY volume alert: {level} ({result['pct']:.1f}%)")

        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHANNEL_ID", "")
        if token and chat_id:
            try:
                asyncio.run(self._send(token, chat_id, msg))
            except Exception as te:
                self.logger.warning(f"MERCURY volume alert Telegram falló: {te}")

        return result

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _md_escape(text: str) -> str:
        """Escapa caracteres especiales para Markdown de Telegram (modo legacy)."""
        for ch in ("_", "*", "`", "["):
            text = text.replace(ch, f"\\{ch}")
        return text
