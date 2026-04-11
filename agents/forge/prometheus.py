"""
agents/forge/prometheus.py
PROMETHEUS — Motor de Avatar Fotorrealista NEXUS CryptoVerdad

Pipeline:
  1. Cargar o generar assets/avatar_carlos_base.png
       - Prioridad 1: avatar_carlos_base.png (generado por SD via gen_sd_assets.py)
       - Prioridad 2: avatar_base.png (generado por Pillow)
       - Prioridad 3: Unsplash API
       - Prioridad 4: Silueta Pillow generica
  2. LivePortrait -> avatar_moving.mp4 (movimiento natural)
       - Fallback: Ken Burns mejorado (zoom + pan suave)
  3. LatentSync -> avatar_talking.mp4 (lip-sync con audio ECHO)
       - Fallback: devolver clip de movimiento sin lip-sync
  4. ctx.avatar_path = ruta del clip generado
  5. Registrar timings en ctx.metadata

Interfaz estandar de agente NEXUS:
  class PROMETHEUS:
      def __init__(self, config: dict, db=None): ...
      def run(self, ctx: Context) -> Context: ...

Activacion de tecnologias:
  - LivePortrait: clonar en liveportrait/ y descargar checkpoints
      git clone https://github.com/KwaiVision/LivePortrait liveportrait
      cd liveportrait && pip install -r requirements.txt
  - LatentSync: descargar checkpoints en latsync/checkpoints/
      huggingface-cli download ByteDance/LatentSync --local-dir latsync/checkpoints
  - SD: python assets/gen_sd_assets.py (requiere CUDA y conexion a internet)
"""

import os
import subprocess
import time
import urllib.request
import urllib.error
import json
from pathlib import Path
from typing import Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from core.context import Context
from utils.logger import get_logger

console = Console()

# ── Directorios ───────────────────────────────────────────────────────────────
_ROOT             = Path(__file__).resolve().parents[2]
_ASSETS_DIR       = _ROOT / "assets"
_OUTPUT_VIDEO_DIR = _ROOT / "output" / "video"
_OUTPUT_PROM_DIR  = _ROOT / "output" / "prometheus"

# ── Paths de avatar ───────────────────────────────────────────────────────────
_AVATAR_SD_PATH     = _ASSETS_DIR / "avatar_carlos_base.png"  # SD fotorrealista
_AVATAR_PILLOW_PATH = _ASSETS_DIR / "avatar_base.png"         # Pillow ilustrado
_AVATAR_FACE_PATH   = _ASSETS_DIR / "avatar_face.png"         # legacy (no SD)

# ── Paths LivePortrait ────────────────────────────────────────────────────────
_LIVEPORTRAIT_DIR    = _ROOT / "liveportrait"
_LIVEPORTRAIT_SCRIPT = _ROOT / "liveportrait" / "inference.py"

# ── Paths LatentSync ──────────────────────────────────────────────────────────
_LATSYNC_DIR    = _ROOT / "latsync"
_LATSYNC_SCRIPT = _ROOT / "latsync" / "scripts" / "inference.py"
_LATSYNC_CKPT   = _ROOT / "latsync" / "checkpoints" / "latentsync_unet.pt"
_LATSYNC_CFG    = _ROOT / "latsync" / "configs" / "unet" / "stage2.yaml"
_LATSYNC_PYTHON = "C:/Python311/python.exe"

# ── Paleta CryptoVerdad ───────────────────────────────────────────────────────
C_BG     = (10,  10,  10)
C_ACCENT = (247, 147, 26)
C_TEXT   = (255, 255, 255)
C_GREY   = (136, 136, 136)

FPS = 30


class PROMETHEUS:
    """
    Motor de avatar fotorrealista para NEXUS CryptoVerdad.

    Genera un clip de video del presentador Carlos hablando
    y lo deposita en ctx.avatar_path para que HEPHAESTUS lo composite.

    Cadena de degradacion:
      Avatar imagen: SD -> Pillow avatar_base.png -> Unsplash -> silueta Pillow
      Movimiento:    LivePortrait -> Ken Burns mejorado
      Lip-sync:      HeyGem (Docker) -> LatentSync -> clip de movimiento sin sync
    """

    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self.logger = get_logger("PROMETHEUS")
        _OUTPUT_PROM_DIR.mkdir(parents=True, exist_ok=True)
        _OUTPUT_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        _ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    # ══════════════════════════════════════════════════════════════════════════
    # run() — Punto de entrada del agente
    # ══════════════════════════════════════════════════════════════════════════

    def run(self, ctx: Context) -> Context:
        self.logger.info("PROMETHEUS iniciado — Motor de Avatar Fotorrealista")

        console.print(
            Panel(
                "[bold #F7931A]PROMETHEUS[/] — Motor de Avatar Fotorrealista\n"
                f"Pipeline: {ctx.pipeline_id[:8]} · Audio: "
                f"{Path(ctx.audio_path).name if ctx.audio_path else 'pendiente'}",
                border_style="#F7931A",
            )
        )

        timings: dict = {}

        try:
            output_path = str(
                _OUTPUT_VIDEO_DIR / f"{ctx.pipeline_id}_avatar_prometheus.mp4"
            )

            with Progress(
                SpinnerColumn(spinner_name="line"),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("PROMETHEUS: iniciando...", total=None)

                # ── PASO 1: Obtener imagen base del avatar ─────────────────────
                progress.update(task, description="Obteniendo imagen base del avatar...")
                t0 = time.time()
                avatar_img_path = self._get_avatar_image(ctx)
                timings["avatar_image_s"] = round(time.time() - t0, 2)

                if avatar_img_path:
                    console.print(
                        f"[dim]Avatar imagen: {Path(avatar_img_path).name}[/]"
                    )
                else:
                    self.logger.warning(
                        "No se pudo obtener imagen de avatar — PROMETHEUS saltado"
                    )
                    ctx.add_warning("PROMETHEUS", "Sin imagen de avatar disponible")
                    ctx.metadata = {**getattr(ctx, "metadata", {}), "prometheus_timings": timings}
                    return ctx

                # ── PASO 2: Generar clip de movimiento con LivePortrait ─────────
                progress.update(task, description="Generando movimiento (LivePortrait)...")
                t0 = time.time()
                audio_path = getattr(ctx, "audio_path", "") or ""
                motion_clip, motion_method = self._generate_motion_clip(
                    avatar_img_path, audio_path, ctx.pipeline_id
                )
                timings["motion_clip_s"] = round(time.time() - t0, 2)
                timings["motion_method"] = motion_method

                if motion_clip:
                    progress.update(
                        task,
                        description=f"Movimiento listo ({motion_method})"
                    )
                    console.print(
                        f"[dim]Movimiento: {motion_method} -> {Path(motion_clip).name}[/]"
                    )
                else:
                    self.logger.warning("No se pudo generar clip de movimiento")
                    ctx.add_warning("PROMETHEUS", "Sin clip de movimiento")
                    ctx.metadata = {**getattr(ctx, "metadata", {}), "prometheus_timings": timings}
                    return ctx

                # ── PASO 3: Lip-sync con LatentSync ───────────────────────────
                progress.update(task, description="Aplicando lip-sync (LatentSync)...")
                t0 = time.time()
                final_clip, sync_method = self._apply_lipsync(
                    motion_clip, audio_path, output_path, ctx.pipeline_id
                )
                timings["lipsync_s"] = round(time.time() - t0, 2)
                timings["lipsync_method"] = sync_method

                if final_clip and Path(final_clip).exists():
                    ctx.avatar_path = final_clip
                    timings["avatar_path"] = final_clip
                    progress.update(task, description="PROMETHEUS completado")
                    console.print(
                        f"[bold green]PROMETHEUS:[/] Avatar listo -> "
                        f"{Path(final_clip).name} [{sync_method}]"
                    )
                    self.logger.info(
                        f"Avatar generado: {final_clip} "
                        f"(motion={motion_method}, sync={sync_method})"
                    )
                else:
                    ctx.add_warning(
                        "PROMETHEUS",
                        "Lip-sync fallo — usando clip de movimiento sin sync"
                    )
                    ctx.avatar_path = motion_clip
                    timings["avatar_path"] = motion_clip
                    console.print(
                        "[yellow]PROMETHEUS:[/] Usando clip sin lip-sync "
                        f"({motion_method})"
                    )

        except Exception as e:
            self.logger.error(f"Error critico en PROMETHEUS: {e}")
            ctx.add_error("PROMETHEUS", str(e))

        # Guardar timings en ctx.metadata
        try:
            existing_meta = getattr(ctx, "metadata", {}) or {}
            existing_meta["prometheus_timings"] = timings
            ctx.metadata = existing_meta
        except Exception:
            pass

        console.print(
            Panel(
                f"[bold green]PROMETHEUS completado[/]\n"
                f"Avatar: {getattr(ctx, 'avatar_path', 'N/A') or 'N/A'}\n"
                f"Metodo: {timings.get('motion_method', '?')} + "
                f"{timings.get('lipsync_method', '?')}\n"
                f"Tiempo total: "
                f"{sum(v for k, v in timings.items() if k.endswith('_s')):.1f}s",
                border_style="green",
            )
        )

        return ctx

    # ══════════════════════════════════════════════════════════════════════════
    # PASO 1 — Imagen base del avatar
    # ══════════════════════════════════════════════════════════════════════════

    def _get_avatar_image(self, ctx: Context) -> Optional[str]:
        """
        Obtiene la imagen base del avatar en orden de prioridad:
          1. assets/avatar_carlos_base.png (SD fotorrealista)
          2. assets/avatar_base.png (Pillow ilustrado)
          3. assets/avatar_face.png (legacy)
          4. Unsplash API
          5. Silueta Pillow generica

        Devuelve ruta a la imagen o None si todo falla.
        """
        # Prioridad 1: SD fotorrealista
        if _AVATAR_SD_PATH.exists() and _AVATAR_SD_PATH.stat().st_size > 50_000:
            self.logger.info(f"Avatar: usando SD fotorrealista -> {_AVATAR_SD_PATH.name}")
            return str(_AVATAR_SD_PATH)

        # Prioridad 2: Pillow avatar_base.png
        if _AVATAR_PILLOW_PATH.exists() and _AVATAR_PILLOW_PATH.stat().st_size > 10_000:
            self.logger.info(f"Avatar: usando Pillow avatar_base -> {_AVATAR_PILLOW_PATH.name}")
            return str(_AVATAR_PILLOW_PATH)

        # Prioridad 3: avatar_face.png legacy (solo si es razonablemente grande)
        if _AVATAR_FACE_PATH.exists() and _AVATAR_FACE_PATH.stat().st_size > 10_000:
            self.logger.info(f"Avatar: usando legacy avatar_face -> {_AVATAR_FACE_PATH.name}")
            return str(_AVATAR_FACE_PATH)

        # Prioridad 4: Unsplash
        self.logger.info("Avatar: intentando Unsplash...")
        unsplash_path = self._fetch_unsplash_avatar()
        if unsplash_path and Path(unsplash_path).exists():
            return unsplash_path

        # Prioridad 5: Silueta Pillow generica
        self.logger.info("Avatar: generando silueta Pillow como ultimo recurso...")
        silhouette_path = self._generate_pillow_silhouette()
        if silhouette_path and Path(silhouette_path).exists():
            return silhouette_path

        return None

    def _fetch_unsplash_avatar(self) -> Optional[str]:
        """
        Descarga foto de presentador profesional de Unsplash.
        Requiere UNSPLASH_API_KEY en la config o variable de entorno.
        """
        api_key = (
            self.config.get("unsplash_api_key", "")
            or os.environ.get("UNSPLASH_API_KEY", "")
        )
        if not api_key:
            self.logger.info("Unsplash: sin API key configurada")
            return None

        cache_path = _ASSETS_DIR / "avatar_unsplash.jpg"
        if cache_path.exists() and cache_path.stat().st_size > 50_000:
            self.logger.info("Unsplash: usando cache existente")
            return str(cache_path)

        try:
            url = (
                "https://api.unsplash.com/search/photos"
                "?query=professional+male+broadcaster+suit&per_page=3"
                "&orientation=portrait"
            )
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Client-ID {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            photos = data.get("results", [])
            if not photos:
                self.logger.warning("Unsplash: sin resultados")
                return None

            # Tomar el primer resultado con buena resolucion
            img_url = photos[0]["urls"].get("regular") or photos[0]["urls"].get("small")
            if not img_url:
                return None

            self.logger.info(f"Unsplash: descargando imagen...")
            urllib.request.urlretrieve(img_url, str(cache_path))

            if cache_path.exists() and cache_path.stat().st_size > 50_000:
                self.logger.info(f"Unsplash: imagen guardada -> {cache_path.name}")
                return str(cache_path)

        except urllib.error.URLError as e:
            self.logger.warning(f"Unsplash: error de red: {e}")
        except Exception as e:
            self.logger.warning(f"Unsplash: error: {e}")

        return None

    def _generate_pillow_silhouette(self) -> Optional[str]:
        """
        Genera silueta profesional con Pillow como fallback final.
        Produce una imagen de 512x768 con presentador estilizado.
        """
        out_path = _ASSETS_DIR / "avatar_prometheus_fallback.png"

        try:
            from PIL import Image, ImageDraw, ImageFilter
            import math

            W, H = 512, 768
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            cx = W // 2

            # ── Traje ──────────────────────────────────────────────────────
            torso_y = H // 2
            suit_pts = [
                (cx - 120, torso_y),
                (cx + 130, torso_y),
                (cx + 160, H),
                (cx - 150, H),
            ]
            draw.polygon(suit_pts, fill=(26, 26, 66, 255))

            # Solapas
            draw.polygon(
                [(cx - 30, torso_y), (cx, torso_y + 80), (cx - 90, torso_y)],
                fill=(20, 20, 54, 255)
            )
            draw.polygon(
                [(cx + 30, torso_y), (cx, torso_y + 80), (cx + 90, torso_y)],
                fill=(18, 18, 50, 255)
            )
            # Camisa blanca interior
            draw.polygon(
                [(cx - 25, torso_y + 5), (cx + 25, torso_y + 5), (cx, torso_y + 75)],
                fill=(240, 240, 250, 255)
            )
            # Corbata naranja
            draw.polygon(
                [(cx - 12, torso_y + 10), (cx + 12, torso_y + 10),
                 (cx + 8, torso_y + 120), (cx, torso_y + 135), (cx - 8, torso_y + 120)],
                fill=(247, 147, 26, 255)
            )

            # ── Cuello ────────────────────────────────────────────────────
            neck_top = torso_y - 80
            draw.ellipse(
                [(cx - 28, neck_top), (cx + 28, torso_y + 10)],
                fill=(200, 160, 110, 255)
            )

            # ── Cabeza ────────────────────────────────────────────────────
            head_cy = neck_top - 130
            head_rx, head_ry = 110, 130

            # Sombra lateral
            draw.ellipse(
                [(cx - head_rx - 10, head_cy - head_ry),
                 (cx + 10, head_cy + head_ry)],
                fill=(170, 130, 85, 180)
            )
            # Cara principal
            draw.ellipse(
                [(cx - head_rx, head_cy - head_ry),
                 (cx + head_rx, head_cy + head_ry)],
                fill=(200, 160, 110, 255)
            )

            # ── Pelo oscuro corto ──────────────────────────────────────────
            hair_pts = [
                (cx - head_rx + 5, head_cy - head_ry + 20),
                (cx - head_rx + 25, head_cy - head_ry - 20),
                (cx - 50, head_cy - head_ry - 50),
                (cx, head_cy - head_ry - 60),
                (cx + 50, head_cy - head_ry - 50),
                (cx + head_rx - 25, head_cy - head_ry - 20),
                (cx + head_rx - 5, head_cy - head_ry + 20),
                (cx + head_rx - 30, head_cy - head_ry + 40),
                (cx - head_rx + 30, head_cy - head_ry + 40),
            ]
            draw.polygon(hair_pts, fill=(40, 35, 30, 255))

            # ── Cejas ─────────────────────────────────────────────────────
            brow_y = head_cy - 45
            draw.rectangle([(cx - 80, brow_y - 8), (cx - 30, brow_y + 2)], fill=(35, 28, 20, 255))
            draw.rectangle([(cx + 30, brow_y - 8), (cx + 80, brow_y + 2)], fill=(35, 28, 20, 255))

            # ── Ojos ──────────────────────────────────────────────────────
            for ex in [cx - 50, cx + 50]:
                ey = head_cy - 28
                draw.ellipse([(ex - 20, ey - 12), (ex + 20, ey + 12)], fill=(245, 245, 245, 255))
                draw.ellipse([(ex - 9, ey - 9), (ex + 9, ey + 9)], fill=(70, 70, 130, 255))
                draw.ellipse([(ex - 5, ey - 5), (ex + 5, ey + 5)], fill=(10, 10, 10, 255))
                draw.ellipse([(ex + 3, ey - 4), (ex + 7, ey)], fill=(255, 255, 255, 200))

            # ── Nariz ─────────────────────────────────────────────────────
            draw.ellipse([(cx - 14, head_cy + 15), (cx + 14, head_cy + 35)],
                        fill=(185, 145, 95, 255))

            # ── Boca seria ────────────────────────────────────────────────
            mouth_y = head_cy + 65
            draw.line([(cx - 28, mouth_y), (cx + 28, mouth_y)], fill=(155, 105, 70, 255), width=4)

            # ── Logo CryptoVerdad en solapa ────────────────────────────────
            logo_x, logo_y = cx + 55, torso_y + 80
            draw.ellipse([(logo_x - 16, logo_y - 16), (logo_x + 16, logo_y + 16)],
                        fill=(247, 147, 26, 255))
            draw.ellipse([(logo_x - 12, logo_y - 12), (logo_x + 12, logo_y + 12)],
                        fill=(26, 26, 66, 255))

            # Suavizado leve
            img_smooth = img.filter(ImageFilter.SMOOTH)
            img = Image.composite(img_smooth, img, img_smooth.split()[3])

            img.save(str(out_path), "PNG")
            self.logger.info(f"Silueta Pillow generada -> {out_path.name}")
            return str(out_path)

        except Exception as e:
            self.logger.error(f"Silueta Pillow fallo: {e}")
            return None

    # ══════════════════════════════════════════════════════════════════════════
    # PASO 2 — Clip de movimiento (LivePortrait o Ken Burns)
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_motion_clip(
        self,
        avatar_img_path: str,
        audio_path: str,
        pipeline_id: str,
    ) -> Tuple[Optional[str], str]:
        """
        Genera clip de movimiento natural del avatar.
        Devuelve (ruta_mp4_o_None, metodo).
        metodo: "liveportrait" | "ken_burns" | "ninguno"
        """
        # ── Intento 1: LivePortrait ───────────────────────────────────────
        if self._liveportrait_available():
            lp_out = str(_OUTPUT_PROM_DIR / f"{pipeline_id}_liveportrait.mp4")
            lp_result = self._run_liveportrait(avatar_img_path, audio_path, lp_out)
            if lp_result and Path(lp_result).exists():
                self.logger.info(f"LivePortrait OK: {lp_result}")
                return lp_result, "liveportrait"

        # ── Intento 2: Ken Burns mejorado ─────────────────────────────────
        if not audio_path or not Path(audio_path).exists():
            self.logger.warning("Sin audio — no se puede generar Ken Burns")
            return None, "ninguno"

        console.print(
            "[yellow]PROMETHEUS:[/] LivePortrait no disponible — "
            "usando Ken Burns mejorado..."
        )
        kb_out = str(_OUTPUT_PROM_DIR / f"{pipeline_id}_ken_burns.mp4")
        kb_result = self._ken_burns_enhanced(avatar_img_path, audio_path, kb_out)
        if kb_result and Path(kb_result).exists():
            self.logger.info(f"Ken Burns mejorado OK: {kb_result}")
            return kb_result, "ken_burns"

        self.logger.warning("Todos los metodos de movimiento fallaron")
        return None, "ninguno"

    def _liveportrait_available(self) -> bool:
        """Comprueba si LivePortrait esta instalado y tiene checkpoints."""
        if not _LIVEPORTRAIT_DIR.exists():
            return False
        if not _LIVEPORTRAIT_SCRIPT.exists():
            return False
        # Buscar checkpoints (directorio pretrained_weights o similar)
        ckpt_dirs = [
            _LIVEPORTRAIT_DIR / "pretrained_weights",
            _LIVEPORTRAIT_DIR / "checkpoints",
        ]
        for d in ckpt_dirs:
            if d.exists() and any(d.iterdir()):
                return True
        self.logger.info(
            "LivePortrait: directorio existe pero sin checkpoints. "
            "Para activar: git clone https://github.com/KwaiVision/LivePortrait liveportrait "
            "&& cd liveportrait && pip install -r requirements.txt "
            "&& descargar pretrained_weights/"
        )
        return False

    def _run_liveportrait(
        self,
        avatar_img: str,
        audio_path: str,
        output_path: str,
    ) -> Optional[str]:
        """
        Invoca LivePortrait para generar movimiento natural.
        Devuelve ruta al MP4 o None si falla.
        El driving video se repite en bucle hasta cubrir la duracion del audio.
        """
        try:
            import os
            from moviepy.editor import AudioFileClip, VideoFileClip, concatenate_videoclips

            # ── Paso 1: obtener duracion del audio ───────────────────────────
            audio_dur = AudioFileClip(audio_path).duration  # segundos

            # ── Paso 2: generar clip LivePortrait con driving template ────────
            _driving = str(_LIVEPORTRAIT_DIR / "assets" / "examples" / "driving" / "d0.mp4")
            lp_env = os.environ.copy()
            _FFMPEG_DIR = r"C:\ffmpeg\bin"
            lp_env["PATH"] = _FFMPEG_DIR + os.pathsep + lp_env.get("PATH", "")
            lp_env["PYTHONIOENCODING"] = "utf-8"
            cmd = [
                _LATSYNC_PYTHON,   # C:/Python311/python.exe
                str(_LIVEPORTRAIT_SCRIPT),
                "--source",    avatar_img,
                "--driving",   _driving,
                "--output_dir", str(_OUTPUT_PROM_DIR),
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                cwd=str(_LIVEPORTRAIT_DIR),
                env=lp_env,
            )
            if result.returncode != 0:
                self.logger.warning(
                    f"LivePortrait: codigo {result.returncode} — "
                    f"{result.stderr[-300:] if result.stderr else '(sin stderr)'}"
                )
                return None

            # ── Paso 3: encontrar el MP4 generado ────────────────────────────
            candidates = sorted(
                _OUTPUT_PROM_DIR.glob("avatar_carlos_base--*.mp4"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                # fallback: cualquier MP4 reciente
                candidates = sorted(
                    _OUTPUT_PROM_DIR.glob("*.mp4"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            if not candidates:
                self.logger.warning("LivePortrait: no se encontró MP4 generado")
                return None

            lp_clip_path = str(candidates[0])
            lp_clip = VideoFileClip(lp_clip_path)
            clip_dur = lp_clip.duration

            # ── Paso 4: loopear hasta cubrir la duracion del audio ────────────
            if clip_dur < audio_dur:
                repeats = int(audio_dur / clip_dur) + 2
                looped = concatenate_videoclips([lp_clip] * repeats)
                looped_trimmed = looped.subclip(0, audio_dur)
                looped_trimmed.write_videofile(
                    output_path,
                    codec="libx264",
                    audio=False,
                    logger=None,
                )
                lp_clip.close()
                looped.close()
                looped_trimmed.close()
                self.logger.info(f"LivePortrait looped {repeats}x -> {audio_dur:.1f}s")
            else:
                # Clip ya suficientemente largo, recortar
                trimmed = lp_clip.subclip(0, audio_dur)
                trimmed.write_videofile(output_path, codec="libx264", audio=False, logger=None)
                lp_clip.close()
                trimmed.close()

            if Path(output_path).exists():
                return output_path

        except subprocess.TimeoutExpired:
            self.logger.warning("LivePortrait: timeout (600s)")
        except FileNotFoundError:
            self.logger.warning("LivePortrait: Python no encontrado")
        except Exception as e:
            self.logger.warning(f"LivePortrait: {e}")

        return None

    def _ken_burns_enhanced(
        self,
        avatar_img_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """
        Ken Burns mejorado: zoom lento + pan suave + parpadeo sutil.
        Produce un clip mas natural que el Ken Burns basico de HEPHAESTUS.
        Devuelve output_path si tiene exito, "" si falla.
        """
        try:
            from moviepy.editor import AudioFileClip, VideoClip
            import numpy as np
            from PIL import Image as PILImage, ImageFilter, ImageEnhance
            import math

            audio_clip = AudioFileClip(audio_path)
            duration = audio_clip.duration

            # Cargar imagen base
            base_img = PILImage.open(avatar_img_path).convert("RGBA")
            W, H = 660, 880  # zona avatar en el layout telediario

            # Redimensionar preservando aspecto
            bw, bh = base_img.size
            scale = min(W / bw, H / bh) * 1.15  # un poco mas grande para el Ken Burns
            new_w = int(bw * scale)
            new_h = int(bh * scale)
            base_img = base_img.resize((new_w, new_h), PILImage.LANCZOS)

            # Convertir a array numpy RGBA
            base_arr = np.array(base_img)

            def make_frame(t: float) -> np.ndarray:
                progress_ratio = t / max(duration, 1.0)

                # Zoom 1.00 -> 1.06 (suave, solo 6%)
                zoom = 1.0 + 0.06 * math.sin(progress_ratio * math.pi)

                # Pan horizontal: -2% -> +2% -> -2% (oscilacion suave)
                pan_x = 0.02 * math.sin(progress_ratio * math.pi * 2)

                # Parpadeo sutil: ligero cambio de brillo cada ~4s
                blink_factor = 0.97 + 0.03 * math.sin(t * 0.8)

                scaled_w = int(new_w * zoom)
                scaled_h = int(new_h * zoom)

                # Redimensionar frame
                scaled = PILImage.fromarray(base_arr).resize(
                    (scaled_w, scaled_h), PILImage.LANCZOS
                )

                # Aplicar brillo del parpadeo
                if abs(blink_factor - 1.0) > 0.005:
                    enhancer = ImageEnhance.Brightness(scaled)
                    scaled = enhancer.enhance(blink_factor)

                # Calcular crop centrado con pan
                crop_x = max(0, int((scaled_w - W) // 2 + pan_x * scaled_w))
                crop_y = max(0, (scaled_h - H) // 2)
                crop_x = min(crop_x, max(0, scaled_w - W))
                crop_y = min(crop_y, max(0, scaled_h - H))

                cropped = scaled.crop((crop_x, crop_y, crop_x + W, crop_y + H))

                if cropped.size != (W, H):
                    canvas = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
                    canvas.paste(cropped, (0, 0))
                    cropped = canvas

                # Convertir RGBA -> RGB con fondo negro
                canvas_rgb = PILImage.new("RGB", (W, H), C_BG)
                if cropped.mode == "RGBA":
                    canvas_rgb.paste(cropped, mask=cropped.split()[3])
                else:
                    canvas_rgb.paste(cropped)

                return np.array(canvas_rgb)

            video = VideoClip(make_frame, duration=duration)
            video = video.set_audio(audio_clip)

            from agents.forge.hephaestus import HEPHAESTUS
            HEPHAESTUS._write_clip(video, output_path)

            for clip in [video, audio_clip]:
                try:
                    clip.close()
                except Exception:
                    pass

            self.logger.info(f"Ken Burns mejorado generado: {output_path}")
            return output_path

        except Exception as e:
            self.logger.error(f"Ken Burns mejorado fallo: {e}")
            return ""

    # ══════════════════════════════════════════════════════════════════════════
    # PASO 3 — Lip-sync (LatentSync)
    # ══════════════════════════════════════════════════════════════════════════

    def _apply_lipsync(
        self,
        motion_clip_path: str,
        audio_path: str,
        output_path: str,
        pipeline_id: str,
    ) -> Tuple[Optional[str], str]:
        """
        Aplica lip-sync al clip de movimiento.
        Orden de prioridad:
          1. HeyGem (Duix.Avatar) — mejor calidad, requiere Docker
          2. LatentSync — requiere Python 3.11 + checkpoints
          3. sin_sync  — fallback: clip de movimiento sin sincronización

        Devuelve (ruta_clip_final, metodo).
        """
        if not audio_path or not Path(audio_path).exists():
            return motion_clip_path, "sin_sync"

        # ── Intento 1: HeyGem ──────────────────────────────────────────────
        if self._heygem_available():
            self.logger.info("HeyGem disponible — iniciando lip-sync...")
            result = self._run_heygem(motion_clip_path, audio_path, output_path)
            if result and Path(result).exists():
                return result, "heygem"
            self.logger.warning("HeyGem fallo — probando LatentSync...")

        # ── Intento 2: LatentSync ─────────────────────────────────────────
        if self._latsync_available():
            result = self._run_latsync(motion_clip_path, audio_path, output_path)
            if result and Path(result).exists():
                return result, "latsync"

        # ── Fallback: devolver clip de movimiento sin sync ────────────────
        self.logger.info("Sin lip-sync disponible — usando clip de movimiento")
        return motion_clip_path, "sin_sync"

    # ── HeyGem ────────────────────────────────────────────────────────────

    def _heygem_available(self) -> bool:
        """Devuelve True si el contenedor Docker de HeyGem está corriendo."""
        try:
            from agents.forge.heygem_client import _is_available
            ok = _is_available()
            if not ok:
                self.logger.debug(
                    "HeyGem no responde en localhost:8383. "
                    "Para activar: cd heygem/deploy && docker-compose -f docker-compose-nexus.yml up -d"
                )
            return ok
        except ImportError:
            return False

    def _run_heygem(
        self,
        motion_clip_path: str,
        audio_path: str,
        output_path: str,
    ) -> Optional[str]:
        """
        Invoca HeyGem (Duix.Avatar) para lip-sync de alta calidad.
        motion_clip_path: vídeo del avatar con movimiento (LivePortrait o Ken Burns)
        audio_path:       audio de ECHO (.mp3)
        Devuelve ruta al MP4 final o None si falla.
        """
        try:
            from agents.forge.heygem_client import submit_lipsync
            from moviepy.editor import AudioFileClip
            # Timeout dinamico
            try:
                dur = AudioFileClip(audio_path).duration
            except Exception:
                dur = 60.0
            dyn_timeout = int(120 + dur * 4)  # HeyGem es mas rapido que LatentSync

            self.logger.info(f"HeyGem: enviando job (timeout={dyn_timeout}s)...")
            result = submit_lipsync(
                video_path=motion_clip_path,
                audio_path=audio_path,
                timeout=dyn_timeout,
            )
            if result:
                # Copiar resultado al output_path esperado
                import shutil
                shutil.copy2(result, output_path)
                return output_path
            return None
        except Exception as e:
            self.logger.warning(f"HeyGem excepcion: {e}")
            return None

    def _latsync_available(self) -> bool:
        """Comprueba si LatentSync tiene checkpoints."""
        if not _LATSYNC_DIR.exists():
            return False
        if not _LATSYNC_SCRIPT.exists():
            return False
        if not _LATSYNC_CKPT.exists():
            self.logger.info(
                "LatentSync: checkpoints no encontrados en latsync/checkpoints/. "
                "Comando para descargar: "
                "huggingface-cli download ByteDance/LatentSync "
                "--local-dir latsync/checkpoints"
            )
            return False
        return True

    def _run_latsync(
        self,
        video_path: str,
        audio_path: str,
        output_path: str,
    ) -> Optional[str]:
        """
        Invoca LatentSync para lip-sync.
        Devuelve ruta al MP4 o None si falla.
        Timeout dinamico: 60s base + 10x duracion del audio.
        """
        try:
            # Timeout dinamico segun duracion del audio
            from moviepy.editor import AudioFileClip
            try:
                audio_dur = AudioFileClip(audio_path).duration
            except Exception:
                audio_dur = 60.0
            dyn_timeout = int(60 + audio_dur * 10)

            cmd = [
                _LATSYNC_PYTHON,
                str(_LATSYNC_SCRIPT),
                "--unet_config_path",    str(_LATSYNC_CFG),
                "--inference_ckpt_path", str(_LATSYNC_CKPT),
                "--video_path",          video_path,
                "--audio_path",          audio_path,
                "--video_out_path",      output_path,
                "--inference_steps",     "20",
                "--guidance_scale",      "1.0",
                "--seed",                "1247",
            ]
            self.logger.info(f"LatentSync: lanzando inferencia (timeout={dyn_timeout}s)...")
            console.print("[dim]LatentSync: ejecutando (puede tardar varios minutos)...[/]")

            import os
            latsync_env = os.environ.copy()
            # Inyectar latsync/ en PYTHONPATH para que `import latentsync` funcione
            existing_pp = latsync_env.get("PYTHONPATH", "")
            latsync_env["PYTHONPATH"] = (
                str(_LATSYNC_DIR) + os.pathsep + existing_pp
                if existing_pp else str(_LATSYNC_DIR)
            )
            # Inyectar ffmpeg en PATH (LatentSync llama check_ffmpeg_installed())
            _FFMPEG_DIR = r"C:\ffmpeg\bin"
            existing_path = latsync_env.get("PATH", "")
            latsync_env["PATH"] = _FFMPEG_DIR + os.pathsep + existing_path
            latsync_env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=dyn_timeout,
                cwd=str(_LATSYNC_DIR),
                env=latsync_env,
            )
        except subprocess.TimeoutExpired:
            self.logger.warning(f"LatentSync: timeout ({dyn_timeout}s)")
            return None
        except FileNotFoundError:
            self.logger.warning(f"LatentSync: Python no encontrado ({_LATSYNC_PYTHON})")
            return None
        except Exception as e:
            self.logger.warning(f"LatentSync subprocess: {e}")
            return None

        if result.returncode != 0:
            self.logger.warning(
                f"LatentSync: codigo {result.returncode}. "
                f"stderr: {result.stderr[-400:] if result.stderr else '(vacio)'}"
            )
            return None

        if Path(output_path).exists():
            self.logger.info(f"LatentSync: clip generado -> {output_path}")
            return output_path

        # Buscar MP4 mas reciente como alternativa
        videos = sorted(
            _OUTPUT_PROM_DIR.glob("*.mp4"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if videos:
            return str(videos[0])

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # API publica: generar SD bajo demanda
    # ══════════════════════════════════════════════════════════════════════════

    def generate_sd_assets(self) -> dict:
        """
        Genera studio_background.png y avatar_carlos_base.png con Stable Diffusion.
        Requiere CUDA disponible y conexion a internet (primera vez descarga ~4GB).

        Uso:
            prometheus = PROMETHEUS(config)
            result = prometheus.generate_sd_assets()
            # result = {"studio": True, "avatar": True}
        """
        result = {"studio": False, "avatar": False}
        try:
            import torch
            if not torch.cuda.is_available():
                self.logger.warning("SD: CUDA no disponible — omitiendo generacion SD")
                return result

            from diffusers import StableDiffusionPipeline

            console.print("[dim]SD: Cargando modelo (puede tardar si es la primera vez)...[/]")
            pipe = StableDiffusionPipeline.from_pretrained(
                "runwayml/stable-diffusion-v1-5",
                torch_dtype=torch.float16,
                safety_checker=None,
            )
            pipe = pipe.to("cuda")
            pipe.enable_attention_slicing()

            # Studio background
            try:
                from PIL import Image
                prompt_studio = (
                    "professional TV news studio, crypto financial news set, "
                    "large LED screens on wall showing orange bitcoin price charts, "
                    "dark background with blue and orange accent lighting, "
                    "anchor desk in foreground, cinematic, photorealistic, 8k, "
                    "broadcast quality, CNN Bloomberg style"
                )
                neg = "cartoon, anime, ugly, blurry, low quality, text, watermark, people"
                console.print("[dim]SD: Generando studio_background.png...[/]")
                res = pipe(prompt_studio, negative_prompt=neg, width=768, height=512,
                          num_inference_steps=30, guidance_scale=7.5)
                img = res.images[0].resize((1920, 1080), Image.LANCZOS)
                img.save(str(_ASSETS_DIR / "studio_background.png"))
                result["studio"] = True
                self.logger.info("SD: studio_background.png generado")
            except Exception as e:
                self.logger.warning(f"SD studio fallo: {e}")

            # Avatar Carlos
            try:
                prompt_avatar = (
                    "photorealistic male TV news anchor, 38 years old, short dark hair, "
                    "dark navy suit, white shirt, serious professional expression, "
                    "looking directly at camera, broadcasting, studio lighting, "
                    "sharp focus, 8k portrait, professional broadcaster, "
                    "crypto financial analyst, half body shot"
                )
                neg_av = (
                    "cartoon, anime, ugly, blurry, deformed, extra limbs, bad anatomy, "
                    "drawing, illustration, painting, watermark, text"
                )
                console.print("[dim]SD: Generando avatar_carlos_base.png...[/]")
                res_av = pipe(prompt_avatar, negative_prompt=neg_av, width=512, height=768,
                             num_inference_steps=35, guidance_scale=8.0)
                res_av.images[0].save(str(_AVATAR_SD_PATH))
                result["avatar"] = True
                self.logger.info("SD: avatar_carlos_base.png generado")
            except Exception as e:
                self.logger.warning(f"SD avatar fallo: {e}")

        except ImportError:
            self.logger.warning("SD: diffusers no instalado. pip install diffusers transformers accelerate")
        except Exception as e:
            self.logger.error(f"SD: error general: {e}")

        return result
