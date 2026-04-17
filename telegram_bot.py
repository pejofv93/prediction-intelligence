#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot.py
BOT TELEGRAM PRIVADO — NEXUS CryptoVerdad
Comandos de control remoto solo para el admin configurado.

Comandos disponibles:
  /estado   — últimos pipelines + precios actuales + próximo ciclo
  /forzar   — lanza pipeline inmediatamente (topic opcional)
  /urgente  — modo urgente con noticia
  /parar    — detiene el scheduler KAIROS
  /precio   — precios BTC/ETH/SOL en tiempo real

Variables de entorno requeridas:
  TELEGRAM_BOT_TOKEN  — token del bot (el mismo que MERCURY)
  TELEGRAM_ADMIN_ID   — tu user_id de Telegram (para auth)
"""

import asyncio
import os
import threading
import json
import urllib.request
from datetime import datetime
from typing import Optional

from rich.console import Console

from database.db import DBManager
from utils.logger import get_logger

console = Console()
logger = get_logger("TELEGRAM_BOT")

_pipeline_lock = threading.Lock()


class TelegramBot:
    """
    Bot privado de control remoto para NEXUS.
    Corre en un hilo daemon independiente del panel web y KAIROS.
    """

    _last_forced: dict = {}  # user_id → datetime
    _FORCE_COOLDOWN_MINUTES = 30

    def __init__(
        self,
        config: dict,
        db: DBManager,
        stop_event: Optional[threading.Event] = None,
    ):
        self.config = config
        self.db = db
        self._stop_event = stop_event or threading.Event()
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._admin_id = os.getenv("TELEGRAM_ADMIN_ID", "")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _is_admin(self, update) -> bool:
        if not self._admin_id:
            return True
        return str(update.effective_user.id) == str(self._admin_id)

    # ── Comandos ──────────────────────────────────────────────────────────────

    async def cmd_estado(self, update, ctx):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ No autorizado.")
            return

        lines = ["📊 *Estado NEXUS — CryptoVerdad*\n"]

        # Últimos pipelines
        try:
            pipelines = self.db.list_pipelines(limit=5)
            if pipelines:
                lines.append("*Últimos pipelines:*")
                for p in pipelines:
                    status = str(p.get("status", "?"))
                    if "completed" in status and "error" not in status:
                        emoji = "✅"
                    elif "error" in status:
                        emoji = "❌"
                    else:
                        emoji = "⏳"
                    topic = str(p.get("topic", ""))[:35]
                    mode = str(p.get("mode", ""))
                    yt = f"\n   🔗 {p['youtube_url']}" if p.get("youtube_url") else ""
                    seo = f" · SEO {p['seo_score']}" if p.get("seo_score") else ""
                    lines.append(f"{emoji} `{topic}` _{mode}{seo}_{yt}")
        except Exception as exc:
            lines.append(f"_Error leyendo pipelines: {exc}_")

        # Próximo pipeline
        try:
            from agents.mind.kairos import KAIROS
            kairos = KAIROS(self.config, self.db)
            next_run = kairos.schedule_next_publish()
            delta = next_run - datetime.now()
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            lines.append(f"\n⏰ *Próximo pipeline:* {next_run.strftime('%Y\\-m\\-d %H:%M')} UTC _(en {h}h {m}m)_")
        except Exception:
            pass

        # Precios en tiempo real
        prices_text = self._fetch_prices()
        if prices_text:
            lines.append(f"\n{prices_text}")

        # Pipeline en curso
        if _pipeline_lock.locked():
            lines.append("\n⚙️ _Pipeline en curso ahora mismo_")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    async def cmd_precio(self, update, ctx):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ No autorizado.")
            return

        prices_text = self._fetch_prices()
        if prices_text:
            await update.message.reply_text(prices_text, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ No se pudieron obtener precios ahora mismo.")

    async def cmd_forzar(self, update, ctx):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ No autorizado.")
            return

        # Rate limit: 30 min entre /forzar del mismo usuario
        user_id = str(update.effective_user.id)
        last = TelegramBot._last_forced.get(user_id)
        if last:
            elapsed = (datetime.now() - last).total_seconds() / 60
            if elapsed < self._FORCE_COOLDOWN_MINUTES:
                remaining = int(self._FORCE_COOLDOWN_MINUTES - elapsed)
                await update.message.reply_text(
                    f"⏳ Cooldown activo. Próximo /forzar disponible en {remaining} min."
                )
                return
        TelegramBot._last_forced[user_id] = datetime.now()

        topic = " ".join(ctx.args).strip() if ctx.args else "análisis crypto diario"

        if _pipeline_lock.locked():
            await update.message.reply_text(
                "⚠️ Ya hay un pipeline en curso. Espera a que termine."
            )
            return

        await update.message.reply_text(
            f"🚀 *Pipeline iniciado*\n📝 {topic}\n\n_Esto tardará 5\\-10 minutos\\.\\.\\._",
            parse_mode="MarkdownV2",
        )

        loop = asyncio.get_running_loop()
        chat_id = update.effective_chat.id

        async def _send(text: str):
            await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

        def _run():
            with _pipeline_lock:
                try:
                    from core.nexus_core import NexusCore
                    nexus = NexusCore(self.config, self.db)
                    result = nexus.run_pipeline(topic, "analisis", dry_run=False)
                    if result.has_errors():
                        errors = "\n".join(result.errors[:3])
                        msg = f"❌ Pipeline con errores:\n`{errors}`"
                    else:
                        yt = result.youtube_url or "sin URL aún"
                        seo = result.seo_score or 0
                        msg = f"✅ *Pipeline completado*\n📺 {yt}\n📊 SEO: {seo}/100"
                except Exception as exc:
                    msg = f"❌ Error inesperado: {exc}"
            asyncio.run_coroutine_threadsafe(_send(msg), loop)

        threading.Thread(target=_run, daemon=True, name="bot-forced-pipeline").start()

    async def cmd_urgente(self, update, ctx):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ No autorizado.")
            return

        # Rate limit: 30 min entre /urgente del mismo usuario
        user_id = str(update.effective_user.id)
        last = TelegramBot._last_forced.get(user_id)
        if last:
            elapsed = (datetime.now() - last).total_seconds() / 60
            if elapsed < self._FORCE_COOLDOWN_MINUTES:
                remaining = int(self._FORCE_COOLDOWN_MINUTES - elapsed)
                await update.message.reply_text(
                    f"⏳ Cooldown activo. Próximo /urgente disponible en {remaining} min."
                )
                return
        TelegramBot._last_forced[user_id] = datetime.now()

        topic = " ".join(ctx.args).strip() if ctx.args else ""
        if not topic:
            await update.message.reply_text(
                "⚠️ Uso: `/urgente <noticia>`\nEjemplo: `/urgente Bitcoin cae 10% en 1 hora`",
                parse_mode="Markdown",
            )
            return

        if _pipeline_lock.locked():
            await update.message.reply_text("⚠️ Pipeline en curso. Espera.")
            return

        await update.message.reply_text(
            f"🚨 *MODO URGENTE ACTIVADO*\n📢 {topic}",
            parse_mode="Markdown",
        )

        loop = asyncio.get_running_loop()
        chat_id = update.effective_chat.id

        async def _send(text: str):
            await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

        def _run():
            with _pipeline_lock:
                try:
                    from core.nexus_core import NexusCore
                    nexus = NexusCore(self.config, self.db)
                    result = nexus.run_urgent_pipeline(topic)
                    if result.has_errors():
                        errors = "\n".join(result.errors[:3])
                        msg = f"❌ Urgente con errores:\n`{errors}`"
                    else:
                        yt = result.youtube_url or "sin URL aún"
                        msg = f"✅ *Urgente publicado*\n📺 {yt}"
                except Exception as exc:
                    msg = f"❌ Error urgente: {exc}"
            asyncio.run_coroutine_threadsafe(_send(msg), loop)

        threading.Thread(target=_run, daemon=True, name="bot-urgent-pipeline").start()

    async def cmd_parar(self, update, ctx):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ No autorizado.")
            return

        self._stop_event.set()
        await update.message.reply_text(
            "🛑 *Señal de parada enviada*\n"
            "KAIROS dejará de programar nuevos pipelines.\n"
            "_El pipeline actual (si lo hay) terminará antes de detenerse._",
            parse_mode="Markdown",
        )

    async def cmd_help(self, update, ctx):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ No autorizado.")
            return

        msg = (
            "🤖 *NEXUS Bot — Comandos disponibles*\n\n"
            "*/estado* — Últimos pipelines, precios y próximo ciclo\n"
            "*/precio* — Precios BTC/ETH/SOL en tiempo real\n"
            "*/forzar \\[topic\\]* — Lanza un pipeline inmediatamente\n"
            "*/urgente <noticia>* — Pipeline urgente con prioridad máxima\n"
            "*/parar* — Detiene el scheduler KAIROS\n"
            "*/help* — Esta ayuda"
        )
        await update.message.reply_text(msg, parse_mode="MarkdownV2")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_prices(self) -> str:
        try:
            url = (
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "NEXUS/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())

            lines = ["💰 *Precios actuales:*"]
            for coin_id, symbol in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
                d = data.get(coin_id, {})
                price = d.get("usd", 0)
                change = d.get("usd_24h_change", 0) or 0
                arrow = "🟢" if change >= 0 else "🔴"
                lines.append(f"{arrow} {symbol}: `${price:,.0f}` ({change:+.2f}%)")
            return "\n".join(lines)
        except Exception:
            return ""

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Arranca el bot (bloqueante). Llama desde un hilo daemon."""
        if not self._token:
            logger.warning(
                "TELEGRAM_BOT_TOKEN no configurado — bot privado desactivado"
            )
            return

        if not self._admin_id:
            logger.warning(
                "TELEGRAM_ADMIN_ID no configurado — el bot acepta comandos de CUALQUIER usuario"
            )

        try:
            from telegram.ext import Application, CommandHandler
        except ImportError:
            logger.error(
                "python-telegram-bot no instalado. "
                "Ejecuta: pip install python-telegram-bot"
            )
            return

        app = Application.builder().token(self._token).build()
        app.add_handler(CommandHandler("estado",  self.cmd_estado))
        app.add_handler(CommandHandler("precio",  self.cmd_precio))
        app.add_handler(CommandHandler("forzar",  self.cmd_forzar))
        app.add_handler(CommandHandler("urgente", self.cmd_urgente))
        app.add_handler(CommandHandler("parar",   self.cmd_parar))
        app.add_handler(CommandHandler("help",    self.cmd_help))
        app.add_handler(CommandHandler("start",   self.cmd_help))

        logger.info(
            "[bold green]BOT TELEGRAM privado activo[/] — "
            "/estado /precio /forzar /urgente /parar"
        )
        console.print(
            "[bold green]BOT TELEGRAM[/] arrancado — admin_id="
            f"{'(todos)' if not self._admin_id else self._admin_id}"
        )

        app.run_polling(stop_signals=None, close_loop=False)

    def start_in_thread(self) -> threading.Thread:
        """Arranca el bot en un hilo daemon y devuelve el hilo."""
        t = threading.Thread(target=self.start, daemon=True, name="telegram-bot")
        t.start()
        return t
