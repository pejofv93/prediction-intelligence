"""
agents/forge/helios.py
HELIOS v3 -- Avatar lip-sync via fal.ai + Kling Avatar v2 Pro

Sube imagen PNG + audio MP3/WAV a fal.ai y devuelve video MP4 con lip-sync.
Requiere: pip install fal-client
          FAL_KEY en .env
"""

import os
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import fal_client
from rich.console import Console
from rich.panel import Panel

from core.context import Context
from utils.logger import get_logger

console = Console()
logger  = get_logger("HELIOS")

_ROOT       = Path(__file__).resolve().parents[2]
_OUTPUT_DIR = _ROOT / "output" / "prometheus"
_ASSETS_DIR = _ROOT / "assets"
_AVATAR_PATHS = [
    _ASSETS_DIR / "avatar_carlos_base.png",
    _ASSETS_DIR / "avatar_base.png",
]


class HELIOS:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self.logger = logger

    def run(self, ctx: Context) -> Context:
        self.logger.info("HELIOS v3 iniciado")
        try:
            ctx = self._generate(ctx)
        except Exception as e:
            self.logger.error(f"HELIOS error: {e}")
            ctx.errors.append(str(e))
        return ctx

    def _generate(self, ctx: Context) -> Context:
        # ── Localizar avatar ─────────────────────────────────────────────────
        avatar_path = next((p for p in _AVATAR_PATHS if p.exists()), None)
        if not avatar_path:
            raise FileNotFoundError(
                "No se encontró avatar PNG en assets/. "
                "Genera avatar_carlos_base.png primero."
            )

        # ── Localizar audio ──────────────────────────────────────────────────
        audio_path = getattr(ctx, "audio_path", None)
        if not audio_path or not Path(audio_path).exists():
            raise FileNotFoundError(f"Audio no encontrado: {audio_path}")
        audio_path = Path(audio_path)

        self.logger.info(
            f"Avatar: {avatar_path.name} ({avatar_path.stat().st_size//1024}KB)"
            f" | Audio: {audio_path.name} ({audio_path.stat().st_size//1024}KB)"
        )

        # ── Subir archivos a fal.ai ──────────────────────────────────────────
        self.logger.info("Subiendo imagen a fal.ai...")
        image_url = fal_client.upload_file(str(avatar_path))
        self.logger.info(f"Imagen subida: {image_url}")

        self.logger.info("Subiendo audio a fal.ai...")
        audio_url = fal_client.upload_file(str(audio_path))
        self.logger.info(f"Audio subido: {audio_url}")

        # ── Llamar Kling Avatar v2 Pro ───────────────────────────────────────
        self.logger.info("Llamando Kling Avatar v2 Pro en fal.ai...")
        t0 = time.time()

        result = fal_client.subscribe(
            "fal-ai/kling-video/v2/pro",
            arguments={
                "image_url": image_url,
                "audio_url": audio_url,
            },
            with_logs=True,
            on_queue_update=lambda u: self.logger.info(f"fal.ai: {u}"),
        )

        elapsed = time.time() - t0
        video_url = result["video"]["url"]
        self.logger.info(f"Kling OK en {elapsed:.0f}s — url: {video_url}")

        # ── Descargar video ──────────────────────────────────────────────────
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUTPUT_DIR / "helios_talking.mp4"
        urllib.request.urlretrieve(video_url, out_path)
        size_mb = out_path.stat().st_size / 1024 / 1024
        self.logger.info(f"Video guardado: {out_path} ({size_mb:.1f} MB)")

        # ── Actualizar contexto ──────────────────────────────────────────────
        ctx.avatar_path = str(out_path)
        ctx.metadata["helios_v3"]    = True
        ctx.metadata["helios_motor"] = "fal-ai/kling-video/v2/pro"
        ctx.metadata["helios_time"]  = round(elapsed)

        console.print(Panel(
            f"[bold green]HELIOS v3 OK[/] — Kling Avatar v2 Pro\n"
            f"Video: {out_path.name} ({size_mb:.1f} MB) en {elapsed:.0f}s",
            border_style="green",
            title="HELIOS",
        ))

        self.logger.info("HELIOS completado")
        return ctx
