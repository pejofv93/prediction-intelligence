"""
aurora.py
AURORA — Publicador Instagram de NEXUS.
Sube el vídeo corto como Reel usando instagrapi.
"""

import os
from pathlib import Path

from rich.console import Console

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

HASHTAGS_CRYPTO = (
    "#bitcoin #crypto #criptomonedas #ethereum #btc #eth #inversión "
    "#blockchain #cryptoverdad #análisiscrypto"
)
MAX_CAPTION = 2200


class AURORA(BaseAgent):
    """
    Publica el vídeo (short_video_path o video_path) como Reel en Instagram.
    Usa INSTAGRAM_USERNAME e INSTAGRAM_PASSWORD del entorno.
    Si falla → warning, nunca detiene el pipeline.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("AURORA")

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold magenta]AURORA[/] iniciado")
        try:
            username = os.getenv("INSTAGRAM_USERNAME", "")
            password = os.getenv("INSTAGRAM_PASSWORD", "")
            if not username or not password:
                raise EnvironmentError(
                    "INSTAGRAM_USERNAME o INSTAGRAM_PASSWORD no configurados"
                )

            video_path = ctx.short_video_path or ctx.video_path
            if not video_path or not Path(video_path).exists():
                raise FileNotFoundError(
                    f"Vídeo para Instagram no encontrado: {video_path!r}"
                )

            caption    = self._build_caption(ctx)
            cover_path = getattr(ctx, 'thumbnail_a_path', '') or ''
            instagram_url = self._upload_reel(
                username, password, video_path, caption, cover_path=cover_path
            )
            ctx.instagram_url = instagram_url
            self.logger.info(
                f"[green]AURORA[/] Reel publicado en Instagram: {instagram_url}"
            )
            self._persist(ctx)
        except Exception as exc:
            msg = str(exc).lower()
            # Instagram bloquea IPs de nube habitualmente — silenciar error técnico
            if any(k in msg for k in ("login", "challenge", "blocked", "ip", "network",
                                       "connection", "timeout", "credentials", "username",
                                       "not configured", "not found")):
                self.logger.info("Instagram no disponible desde Railway — omitiendo")
            else:
                self.logger.warning(f"[yellow]AURORA[/] Instagram omitido: {exc}")
            ctx.add_warning("AURORA", "Instagram no disponible — omitido")
        return ctx

    # ── construcción de caption ───────────────────────────────────────────────
    def _build_caption(self, ctx: Context) -> str:
        title       = (getattr(ctx, 'seo_title', '') or ctx.topic or '')[:100]
        youtube_url = ctx.youtube_url or ''

        # Añadir link a YouTube en la caption para redirigir tráfico
        yt_line = f"\nAnálisis completo: {youtube_url}" if youtube_url else ""

        caption = f"{title}{yt_line}\n\n{HASHTAGS_CRYPTO}"
        if len(caption) > MAX_CAPTION:
            allowed = MAX_CAPTION - len(HASHTAGS_CRYPTO) - len(yt_line) - 4
            caption = title[:allowed].rstrip() + f"...{yt_line}\n\n{HASHTAGS_CRYPTO}"
        return caption

    # ── subida con instagrapi ─────────────────────────────────────────────────
    def _upload_reel(
        self,
        username: str,
        password: str,
        video_path: str,
        caption: str,
        cover_path: str = "",
    ) -> str:
        try:
            from instagrapi import Client  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Instala instagrapi: pip install instagrapi"
            ) from exc

        cl = Client()
        cl.login(username, password)
        self.logger.info("[bold magenta]AURORA[/] sesión Instagram activa, subiendo Reel...")

        # Usar thumbnail A como cover si existe
        if cover_path and Path(cover_path).exists():
            media = cl.video_upload(
                Path(video_path),
                caption=caption,
                thumbnail=Path(cover_path),
            )
        else:
            media = cl.video_upload(Path(video_path), caption=caption)

        # instagrapi devuelve objeto Media; construimos URL del Reel si hay code
        media_code = getattr(media, "code", None)
        media_pk   = getattr(media, "pk", None) or getattr(media, "id", None)
        if media_code:
            return f"https://www.instagram.com/reel/{media_code}/"
        if media_pk:
            return f"https://www.instagram.com/reel/{media_pk}/"
        return f"https://www.instagram.com/{username}/"

    # ── persistencia ──────────────────────────────────────────────────────────
    def _persist(self, ctx: Context) -> None:
        import uuid
        if not getattr(ctx, "instagram_url", ""):
            return
        try:
            self.db.save_video({
                "id": str(uuid.uuid4()),
                "pipeline_id": ctx.pipeline_id,
                "platform": "instagram",
                "video_id": "",
                "title": ctx.seo_title,
                "url": ctx.instagram_url,
            })
        except Exception as exc:
            self.logger.warning(f"[yellow]AURORA[/] no se pudo persistir en DB: {exc}")
