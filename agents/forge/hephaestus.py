# -*- coding: utf-8 -*-
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/forge/hephaestus.py
HEPHAESTUS v3 — Motor de Video Autonomo NEXUS CryptoVerdad

Genera video 1920x1080 con 6 capas composicionadas:
  CAPA 1 — Fondo estudio (assets/studio_background.png; fallback programatico)
  CAPA 2 — Avatar LatentSync -> Ken Burns (lip-sync real; SadTalker ELIMINADO)
  CAPA 3 — Pantalla dinamica content-aware (grafico/noticia segun keywords del guion)
  CAPA 4 — Ticker de precios animado (derecha -> izquierda, fondo #111111, texto #F7931A)
  CAPA 5 — Subtitulos sincronizados (SRT -> Whisper -> distribucion uniforme)
  CAPA 6 — Compositor final MoviePy v1.0.3 con CompositeVideoClip

Layout 1920x1080:
  ┌──────────────────────────────────────────────────────────┐
  │  TICKER (40px) fondo #111111 texto #F7931A scroll        │
  │  AVATAR (45%W) zona izquierda  |  PANTALLA (55%W) dcha   │
  │  Subtitulos barra inferior semitransparente              │
  └──────────────────────────────────────────────────────────┘

Short 1080x1920:
  Avatar centrado + ticker + subtitulos. Sin pantalla dinamica. Max 60s.

Cadena de fallback:
  Avatar: LatentSync (latsync/) -> Ken Burns sobre avatar_face.png
  Fondo:  assets/studio_background.png -> fondo programatico Pillow
  Subs:   SRT -> Whisper -> distribucion uniforme del guion

Nota LatentSync:
  El codigo esta implementado y listo. Para activarlo hay que descargar
  los checkpoints del modelo en latsync/checkpoints/:
    - checkpoints/latentsync_unet.pt  (de ByteDance HuggingFace)
    - checkpoints/whisper/tiny.pt     (o small.pt)
  Mientras no existan los checkpoints, el sistema usa Ken Burns como fallback.
  Comando para descargar:
    huggingface-cli download ByteDance/LatentSync --local-dir latsync/checkpoints

SadTalker:
  Eliminado definitivamente. El campo ctx.sadtalker_used se mantiene por
  compatibilidad pero siempre sera False.
"""

import math
import os
import re
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from core.context import Context
from utils.logger import get_logger

console = Console()

# ── Directorios de salida ──────────────────────────────────────────────────────
_ROOT             = Path(__file__).resolve().parents[2]
OUTPUT_VIDEO_DIR  = _ROOT / "output" / "video"
OUTPUT_LATSYNC_DIR = _ROOT / "output" / "latsync"
OUTPUT_AUDIO_DIR  = _ROOT / "output" / "audio"
ASSETS_DIR        = _ROOT / "assets"

# ── Paleta CryptoVerdad ────────────────────────────────────────────────────────
C_BG     = (10,  10,  10)   # #0A0A0A
C_ACCENT = (247, 147, 26)   # #F7931A
C_TEXT   = (255, 255, 255)  # #FFFFFF
C_DARK   = (26,  26,  26)   # #1A1A1A
C_GREY   = (136, 136, 136)  # #888888
C_TICKER = (17,  17,  17)   # #111111

# ── Resoluciones ───────────────────────────────────────────────────────────────
RES_MAIN  = (1920, 1080)
RES_SHORT = (1080, 1920)

FPS = 30

# ── Layout constants — única fuente de verdad para posiciones ─────────────────
# YouTube largo 1920×1080
_YT_TICKER_Y, _YT_TICKER_H = 0,   40   # Ticker SUPERIOR (y=0 a y=40)
_YT_LOGO_Y                 = 10         # Logo overlay en y=10
_YT_CONTENT_Y              = 40         # Gráfico desde y=40
_YT_CONTENT_H              = 950        # Gráfico hasta y=990
_YT_SUB_Y,    _YT_SUB_H   = 990, 90    # Subs y=990 a y=1080
# Short / TikTok / Reels 1080×1920
_SH_LOGO_Y,    _SH_LOGO_H    = 10,   68   # Logo en y=10, h=68 → bottom=78
_SH_CONTENT_Y, _SH_CONTENT_H = 80, 1700   # Gráfico y=80 a y=1780
_SH_SUB_Y,     _SH_SUB_H     = 1780, 80   # Subs y=1780 a y=1860
_SH_TICKER_Y,  _SH_TICKER_H  = 1860, 60   # Ticker y=1860 a y=1920

# ── Formatos de video ──────────────────────────────────────────────────────────
FORMAT_NOTICIARIO = "NOTICIARIO"
FORMAT_ANALISIS   = "ANALISIS"
FORMAT_EDUCATIVO  = "EDUCATIVO"
FORMAT_SHORT      = "SHORT"
FORMAT_FULLSCREEN = "FULLSCREEN"   # Gráfico a pantalla completa, sin avatar

MODE_FORMAT_MAP = {
    "urgente":  FORMAT_FULLSCREEN,
    "standard": FORMAT_FULLSCREEN,
    "analisis": FORMAT_FULLSCREEN,   # analisis usa FULLSCREEN con zoom engine y graficos complementarios
    "tutorial": FORMAT_EDUCATIVO,
    "opinion":  FORMAT_FULLSCREEN,
    "short":    FORMAT_SHORT,
    "thread":   FORMAT_SHORT,
}

# Número mínimo de escenas garantizadas por modo
MIN_SCENES = {
    "analisis":  12,
    "standard":  12,
    "educativo": 10,
    "tutorial":  10,
    "noticia":    8,
    "urgente":    6,
}

# Secuencias template por modo: (content_type, dur_sugerida_s)
_SCENE_TEMPLATES = {
    "analisis": [
        # Alternancia: gráfico BTC nunca más de 2 veces seguidas.
        # Resultado: máximo 2 escenas de BTC chart consecutivas.
        ("precio",         10.0),   # 1  gancho + precio en tiempo real
        ("analisis",        8.0),   # 2  zoom resistencia/soporte
        ("fear_greed",      8.0),   # 3  medidor Fear & Greed (siempre distinto)
        ("dominancia",      8.0),   # 4  pie chart dominancia BTC
        ("analisis",        8.0),   # 5  zoom segunda zona técnica
        ("heatmap",         8.0),   # 6  heatmap altcoins 24h
        ("volumen",         8.0),   # 7  barras de volumen en exchanges
        ("dominancia_area", 8.0),   # 8  área dominancia histórica
        ("correlacion",     8.0),   # 9  correlación BTC vs SP500
        ("adopcion",        8.0),   # 10 adopción institucional (ETF / on-chain)
        ("analisis",        8.0),   # 11 zoom zona de acumulación
        ("prediccion",     10.0),   # 12 escenario alcista / bajista
    ],
    "standard": [
        ("precio",         10.0),   # 1  precio actual
        ("analisis",        8.0),   # 2  análisis técnico principal
        ("fear_greed",      8.0),   # 3  sentimiento mercado
        ("dominancia",      8.0),   # 4  dominancia BTC
        ("analisis",        8.0),   # 5  segundo nivel técnico
        ("heatmap",         8.0),   # 6  heatmap altcoins
        ("volumen",         8.0),   # 7  volumen
        ("dominancia_area", 8.0),   # 8  área dominancia
        ("correlacion",     8.0),   # 9  correlación
        ("precio",          8.0),   # 10 recapitulación precio
        ("analisis",        8.0),   # 11 conclusión técnica
        ("prediccion",     10.0),   # 12 predicción / cierre
    ],
    "educativo": [
        ("titulo_edu",      8.0),
        ("definicion_edu",  8.0),
        ("halving",        10.0),
        ("comparativa_edu", 8.0),
        ("definicion_edu",  8.0),   # segunda vez = analogia
        ("datos_edu",       8.0),
        ("correlacion",     8.0),
        ("dominancia_area", 8.0),
        ("halving",         8.0),   # segunda aparicion = resumen
        ("datos_edu",      10.0),   # cierre con datos finales
    ],
    "tutorial": [  # igual que educativo
        ("titulo_edu",      8.0),
        ("definicion_edu",  8.0),
        ("halving",        10.0),
        ("comparativa_edu", 8.0),
        ("definicion_edu",  8.0),
        ("datos_edu",       8.0),
        ("correlacion",     8.0),
        ("dominancia_area", 8.0),
        ("halving",         8.0),
        ("datos_edu",      10.0),
    ],
    "noticia": [
        ("urgente_alert",   6.0),
        ("noticia",         8.0),
        ("precio",          6.0),
        ("analisis",        8.0),
        ("heatmap",         8.0),
        ("volumen",         8.0),
        ("dominancia",      8.0),
        ("prediccion",      8.0),
    ],
    "urgente": [
        ("urgente_alert",   6.0),
        ("precio",          6.0),
        ("fear_greed",      6.0),
        ("heatmap",         8.0),
        ("analisis",        8.0),
        ("prediccion",      8.0),
    ],
}

# ── Paths clave ────────────────────────────────────────────────────────────────
_LATSYNC_DIR      = str(_ROOT / "latsync")
_LATSYNC_SCRIPT   = str(_ROOT / "latsync" / "scripts" / "inference.py")
_LATSYNC_CKPT     = str(_ROOT / "latsync" / "checkpoints" / "latentsync_unet.pt")
_LATSYNC_CFG      = str(_ROOT / "latsync" / "configs" / "unet" / "stage2.yaml")
_LATSYNC_PYTHON   = "C:/Python311/python.exe"
_AVATAR_IMG       = str(ASSETS_DIR / "avatar_face.png")
_STUDIO_BG        = str(ASSETS_DIR / "studio_background.png")

# Keywords para deteccion de segmentos dinamicos
_KW_PRECIO   = re.compile(r'\b(precio|cotiza|d[oó]lares|mercado|sube|baja|alza|caida|cotizaci[oó]n)\b', re.I)
_KW_NOTICIA  = re.compile(r'\b(noticia|anunci[ao]|seg[uú]n|public[oó]|report[ao]|declar[oó]|inform[oó])\b', re.I)
_KW_TECNICO  = re.compile(r'\b(soporte|resistencia|t[eé]cnico|an[aá]lisis|RSI|MACD|EMA|media|tendencia|canal)\b', re.I)


class HEPHAESTUS:
    """
    Motor de video autonomo de NEXUS.

    Interfaz estandar de agente NEXUS:
      __init__(self, config: dict, db)
      run(self, ctx: Context) -> Context

    Cadena de degradacion elegante:
      Fondo:  studio_background.png -> fallback programatico Pillow
      Avatar: LatentSync -> Ken Burns (SadTalker eliminado)
      Subs:   SRT -> Whisper -> distribucion uniforme
    """

    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self.logger = get_logger("HEPHAESTUS")
        OUTPUT_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_LATSYNC_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)

        # Cache de precios para el ticker (se rellena en run())
        self._prices: dict = {}

        # Intentar cargar DAEDALUS para graficos
        self._daedalus_cls = None
        try:
            from agents.forge.daedalus import DAEDALUS
            self._daedalus_cls = DAEDALUS
        except Exception as e:
            self.logger.warning(f"DAEDALUS no disponible: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # run() — Punto de entrada del agente
    # ══════════════════════════════════════════════════════════════════════════

    def run(self, ctx: Context) -> Context:
        self.logger.info("HEPHAESTUS v3 iniciado — Motor de Video Autonomo")
        video_format = self._detect_format(ctx)

        console.print(
            Panel(
                f"[bold #F7931A]HEPHAESTUS v3[/] — Motor de Video Autonomo\n"
                f"Formato: [bold]{video_format}[/] · Modo: {ctx.mode} · "
                f"Pipeline: {ctx.pipeline_id[:8]}",
                border_style="#F7931A",
            )
        )

        try:
            output_path = str(OUTPUT_VIDEO_DIR / f"{ctx.pipeline_id}.mp4")
            short_path  = str(OUTPUT_VIDEO_DIR / f"{ctx.pipeline_id}_short.mp4")

            # ── Poblar precios para ticker ─────────────────────────────────────
            # Fuente primaria: ctx.btc_price / ctx.eth_price / ctx.sol_price (ARGOS tiempo real).
            # Fallback secundario: ctx.prices dict (estructura CoinGecko).
            # Fallback terciario: DAEDALUS._fetch_current_prices (cache 5min, sin hardcode).
            # NUNCA usar valores hardcodeados.
            self._prices = {}
            try:
                # Prioridad 1: campos directos del ctx (puestos por ARGOS)
                _ctx_btc = getattr(ctx, "btc_price", 0) or 0
                _ctx_eth = getattr(ctx, "eth_price", 0) or 0
                _ctx_sol = getattr(ctx, "sol_price", 0) or 0
                if _ctx_btc > 0:
                    self._prices["bitcoin"] = _ctx_btc
                if _ctx_eth > 0:
                    self._prices["ethereum"] = _ctx_eth
                if _ctx_sol > 0:
                    self._prices["solana"] = _ctx_sol

                # Prioridad 2: ctx.prices dict para monedas que aun falten
                raw = getattr(ctx, "prices", {}) or {}
                for cg_id, symbol in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
                    if cg_id in self._prices:
                        continue  # ya tenemos valor de ctx directo
                    found = raw.get(cg_id) or raw.get(symbol)
                    if isinstance(found, (int, float)) and found > 0:
                        self._prices[cg_id] = found
                    elif isinstance(found, dict):
                        val = found.get("usd") or found.get("price")
                        if val is not None and float(val) > 0:
                            self._prices[cg_id] = float(val)

                # Prioridad 3: DAEDALUS como último recurso (cache 5min, sin hardcode)
                missing = [cg for cg in ["bitcoin", "ethereum", "solana"] if cg not in self._prices]
                if missing and self._daedalus_cls is not None:
                    try:
                        _dae = self._daedalus_cls(self.config)
                        fetched = _dae._fetch_current_prices(missing)
                        for cg_id, val in fetched.items():
                            if val and float(val) > 0:
                                self._prices[cg_id] = float(val)
                    except Exception as _ep:
                        self.logger.warning(f"Precios via DAEDALUS: {_ep}")

                # Log de advertencia si algun precio sigue en 0
                for cg_id, label in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
                    if cg_id not in self._prices or self._prices[cg_id] <= 0:
                        self.logger.warning(
                            f"{label} price = 0 en ticker — ARGOS no cargó precio en tiempo real"
                        )
            except Exception as _epr:
                self.logger.warning(f"Error poblando precios: {_epr}")

            with Progress(
                SpinnerColumn(spinner_name="line"),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:

                task = progress.add_task("HEPHAESTUS: iniciando...", total=None)

                # ── Paso 1: Fondo ──────────────────────────────────────────────
                progress.update(task, description="Cargando fondo de estudio...")
                studio_bg_img = None
                try:
                    studio_bg_img = self._load_studio_background(*RES_MAIN)
                    progress.update(task, description="Fondo de estudio listo")
                except Exception as e:
                    self.logger.warning(f"Fondo fallo (no critico): {e}")
                    ctx.add_warning("HEPHAESTUS", f"Fondo: {e}")

                # ── Paso 2: Subtitulos ─────────────────────────────────────────
                progress.update(task, description="Preparando subtitulos...")
                subtitle_entries: List[Tuple[float, float, str]] = []
                try:
                    subtitle_entries = self._get_subtitle_entries(ctx)
                    console.print(f"[dim]Subtitulos: {len(subtitle_entries)} entradas[/]")
                except Exception as e:
                    self.logger.warning(f"Subtitulos fallo (no critico): {e}")
                    ctx.add_warning("HEPHAESTUS", f"Subtitulos: {e}")

                # ── Paso 3: Avatar — DESACTIVADO ──────────────────────────────
                # El vídeo no tiene presentador hasta activación manual.
                # Activar: asignar ctx.avatar_path desde PROMETHEUS/HELIOS antes de HEPHAESTUS.
                avatar_clip_path: Optional[str] = None
                progress.update(task, description="Avatar desactivado — modo FULLSCREEN")

                # ── Paso 4: Grafico ────────────────────────────────────────────
                progress.update(task, description="Obteniendo grafico (DAEDALUS)...")
                chart_path: Optional[str] = None
                try:
                    chart_path = self._get_chart_path(ctx)
                    if chart_path:
                        progress.update(task, description=f"Grafico: {Path(chart_path).name}")
                except Exception as e:
                    self.logger.warning(f"Grafico fallo (no critico): {e}")
                    ctx.add_warning("HEPHAESTUS", f"Grafico: {e}")

                # ── Paso 5: Precios para ticker ────────────────────────────────
                progress.update(task, description="Extrayendo precios para ticker...")
                prices: dict = {}
                try:
                    prices = self._extract_ticker_prices(ctx)
                except Exception as e:
                    self.logger.warning(f"Ticker precios fallo (no critico): {e}")

                # ── Paso 6: Segmentos dinamicos del guion ──────────────────────
                progress.update(task, description="Parseando segmentos del guion...")
                script_segments: List[dict] = []
                try:
                    script_segments = self._parse_script_segments(ctx.script or "")
                    console.print(f"[dim]Segmentos del guion: {len(script_segments)} detectados[/]")
                except Exception as e:
                    self.logger.warning(f"Segmentos fallo (no critico): {e}")

                # ── Paso 7: Composicion video principal (1920x1080) ────────────
                progress.update(task, description=f"Componiendo video {video_format} 1920x1080...")
                w, h = RES_MAIN
                try:
                    if video_format == FORMAT_FULLSCREEN:
                        self._compose_fullscreen(
                            ctx=ctx,
                            chart_path=chart_path,
                            prices=prices,
                            subtitle_entries=subtitle_entries,
                            output_path=output_path,
                            w=w, h=h,
                        )
                    elif video_format == FORMAT_ANALISIS:
                        self._compose_analisis(
                            w, h, ctx.audio_path, chart_path,
                            prices, subtitle_entries, output_path
                        )
                    elif video_format == FORMAT_EDUCATIVO:
                        self._compose_educativo(
                            w, h, ctx.audio_path, prices,
                            subtitle_entries, output_path, topic=ctx.topic
                        )
                    else:
                        # NOTICIARIO (default)
                        self._compose_noticiario(
                            ctx=ctx,
                            studio_bg_img=studio_bg_img,
                            chart_path=chart_path,
                            prices=prices,
                            subtitle_entries=subtitle_entries,
                            script_segments=script_segments,
                            avatar_clip_path=avatar_clip_path,
                            output_path=output_path,
                            w=w, h=h,
                        )

                    ctx.video_path = output_path
                    ctx.sadtalker_used = False  # SadTalker eliminado
                    progress.update(task, description="Video 1920x1080 exportado")
                    console.print(
                        f"[green]Video horizontal:[/] output/video/{ctx.pipeline_id[:8]}....mp4"
                    )
                    self.logger.info(f"Video guardado: {output_path}")

                except Exception as e:
                    self.logger.error(f"Composicion de video fallo: {e}")
                    ctx.add_error("HEPHAESTUS", f"Composicion: {e}")

                # ── Paso 8: Version SHORT (1080x1920) ─────────────────────────
                progress.update(task, description="Generando version SHORT 1080x1920...")
                try:
                    ws, hs = RES_SHORT
                    short_audio = getattr(ctx, "short_audio_path", "") or ""
                    short_subs = []

                    # Si hay guion + audio Short propios, generar Short nativo vertical
                    if short_audio and Path(short_audio).exists() and getattr(ctx, "short_script", ""):
                        # Parsear subtítulos del short_script
                        try:
                            from agents.forge.echo import preprocess_script as _pp
                            _short_clean = _pp(ctx.short_script)
                            _short_words = _short_clean.split()
                            # Distribuir palabras uniformemente a lo largo del audio
                            from moviepy.editor import AudioFileClip as _AC
                            _ac = _AC(short_audio)
                            _dur = _ac.duration
                            _ac.close()
                            _words_per_seg = max(3, len(_short_words) // max(len(_short_words) // 8, 1))
                            _t = 0.0
                            for _i in range(0, len(_short_words), _words_per_seg):
                                _seg = " ".join(_short_words[_i:_i + _words_per_seg])
                                _seg_dur = max(1.0, _dur * _words_per_seg / max(len(_short_words), 1))
                                short_subs.append((_t, min(_t + _seg_dur, _dur), _seg))
                                _t += _seg_dur
                        except Exception as _se:
                            self.logger.warning(f"Short subs parsing: {_se}")

                        self._compose_short_vertical(
                            ws, hs, short_audio, chart_path,
                            prices, short_subs, short_path
                        )
                        self.logger.info("Short nativo generado con guion propio")
                    elif video_format == FORMAT_SHORT:
                        self._compose_short_vertical(
                            ws, hs, ctx.audio_path, chart_path,
                            prices, subtitle_entries, short_path
                        )
                    elif ctx.video_path and Path(ctx.video_path).exists():
                        self._crop_to_short(ctx.video_path, short_path)

                    if Path(short_path).exists():
                        ctx.short_video_path = short_path
                        progress.update(task, description="Short 1080x1920 exportado")
                        console.print(
                            f"[green]Video SHORT:[/] output/video/{ctx.pipeline_id[:8]}...._short.mp4"
                        )
                        self.logger.info(f"Short guardado: {short_path}")
                    else:
                        ctx.add_warning("HEPHAESTUS", "Short no pudo generarse")

                except Exception as e:
                    self.logger.warning(f"SHORT fallo (no critico): {e}")
                    ctx.add_warning("HEPHAESTUS", f"SHORT: {e}")

            try:
                console.print(
                    Panel(
                        f"[bold green]HEPHAESTUS v3 completado[/]\n"
                        f"Modo: FULLSCREEN sin avatar\n"
                        f"Video principal: {ctx.video_path or 'N/A'}\n"
                        f"Short:           {ctx.short_video_path or 'N/A'}",
                        border_style="green",
                    )
                )
            except Exception:
                pass  # error de encoding en consola no es critico

        except Exception as e:
            self.logger.error(f"Error critico en HEPHAESTUS: {e}")
            ctx.add_error("HEPHAESTUS", str(e))

        return ctx

    # ══════════════════════════════════════════════════════════════════════════
    # Helper write_videofile con temp_audiofile en directorio de salida
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _write_clip(clip, output_path: str, fps: int = FPS,
                    audio_codec: str = "aac", threads: int = 2,
                    audio: bool = True) -> None:
        """
        Wrapper sobre write_videofile que:
          - garantiza que el directorio de salida existe
          - coloca temp_audiofile en el mismo directorio (evita huerfanos en CWD)
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        temp_audio = str(out.parent / (out.stem + "_TEMP_AUDIO_.mp4"))
        clip.write_videofile(
            output_path,
            fps=fps,
            codec="libx264",
            audio_codec=audio_codec if audio else None,
            audio=audio,
            temp_audiofile=temp_audio,
            threads=threads,
            logger=None,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # CAPA 1 — Fondo de estudio
    # ══════════════════════════════════════════════════════════════════════════

    def _load_studio_background(self, w: int, h: int):
        """
        Carga el fondo de estudio en orden de prioridad:
          1. assets/studio_background.png (generado por SD o Pillow)
          2. Fallback programatico Pillow
        Devuelve imagen PIL.
        """
        from PIL import Image

        bg_path = Path(_STUDIO_BG)
        if bg_path.exists() and bg_path.stat().st_size > 100 * 1024:
            try:
                bg = Image.open(str(bg_path)).convert("RGB")
                if bg.size != (w, h):
                    bg = bg.resize((w, h), Image.LANCZOS)
                self.logger.info("Fondo cargado desde studio_background.png")
                return bg
            except Exception as e:
                self.logger.warning(f"studio_background.png fallo: {e}")

        return self._build_studio_background_fallback(w, h)

    def _build_studio_background_fallback(self, w: int, h: int):
        """Genera fondo programatico de estudio con gradiente y detalles decorativos."""
        from PIL import Image, ImageDraw, ImageFont
        import math

        img = Image.new("RGB", (w, h), C_BG)
        draw = ImageDraw.Draw(img)

        # Gradiente vertical sutil
        for y in range(h):
            ratio = y / h
            gray = int(10 + 8 * (1 - ratio * 0.6))
            blue = int(10 + 14 * (1 - ratio))
            draw.line([(0, y), (w, y)], fill=(gray, gray, blue))

        # Marcos de pantallas de fondo (zona derecha)
        screen_rects = [
            (int(w * 0.55), 60, w - 20, int(h * 0.50)),
            (int(w * 0.55), int(h * 0.55), int(w * 0.77), int(h * 0.85)),
            (int(w * 0.78), int(h * 0.55), w - 20, int(h * 0.85)),
        ]
        for rect in screen_rects:
            x0, y0, x1, y1 = rect
            draw.rectangle(rect, fill=(8, 12, 20))
            draw.rectangle(rect, outline=C_ACCENT, width=2)
            # Cabecera de pantalla
            draw.rectangle([(x0, y0), (x1, y0 + 30)], fill=(20, 14, 4))
            draw.rectangle([(x0, y0), (x0 + 3, y1)], fill=C_ACCENT)
            # Barras decorativas
            for bar_y in range(y0 + 40, y1 - 10, 16):
                bw = int((x1 - x0 - 20) * 0.55)
                draw.rectangle([(x0 + 8, bar_y), (x0 + 8 + bw, bar_y + 8)], fill=(22, 22, 28))

        # Linea acento superior
        draw.rectangle([(0, 0), (w, 5)], fill=C_ACCENT)

        # Barra lateral izquierda de estudio
        draw.rectangle([(0, 0), (40, h)], fill=(8, 8, 12))
        draw.rectangle([(40, 0), (43, h)], fill=C_ACCENT)

        # Logo CryptoVerdad
        try:
            font_logo = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 24)
            font_sm   = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 14)
        except Exception:
            font_logo = ImageFont.load_default()
            font_sm   = ImageFont.load_default()

        draw.text((55, 15), "CryptoVerdad", fill=C_ACCENT, font=font_logo)
        draw.text((55, 43), "@CryptoVerdad", fill=C_GREY, font=font_sm)

        return img

    # ══════════════════════════════════════════════════════════════════════════
    # CAPA 2 — Avatar (LatentSync -> Ken Burns; SadTalker ELIMINADO)
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_avatar_clip(self, ctx: Context) -> Tuple[Optional[str], str]:
        """
        Orquestador de avatar. Devuelve (ruta_mp4_o_None, metodo).
        metodo: "latsync" | "ken_burns" | "ninguno"
        Nunca lanza excepcion.
        """
        audio_path = getattr(ctx, "audio_path", "") or ""
        pipeline_id = ctx.pipeline_id

        if not audio_path or not Path(audio_path).exists():
            self.logger.warning("_generate_avatar_clip: sin audio — saltando avatar")
            return None, "ninguno"

        # ── Intento 1: LatentSync ─────────────────────────────────────────
        latsync_out = str(OUTPUT_LATSYNC_DIR / f"{pipeline_id}_latsync.mp4")
        latsync_result = None
        try:
            latsync_result = self._run_latsync(audio_path, latsync_out)
        except Exception as e:
            self.logger.warning(f"LatentSync excepcion: {e}")

        if latsync_result and Path(latsync_result).exists():
            self.logger.info(f"LatentSync OK: {latsync_result}")
            return latsync_result, "latsync"

        # ── Intento 2: Ken Burns (fallback) ───────────────────────────────
        console.print("[yellow]Avatar fallback:[/] Ken Burns (imagen estatica animada)...")
        kb_out = str(OUTPUT_VIDEO_DIR / f"{pipeline_id}_avatar_kb.mp4")
        kb_result = ""
        try:
            kb_result = self._avatar_ken_burns(audio_path, kb_out)
        except Exception as e:
            self.logger.error(f"Ken Burns excepcion: {e}")

        if kb_result and Path(kb_result).exists():
            self.logger.info(f"Ken Burns OK: {kb_result}")
            return kb_result, "ken_burns"

        self.logger.warning("Todos los metodos de avatar fallaron")
        return None, "ninguno"

    def _run_latsync(self, audio_path: str, output_path: str) -> Optional[str]:
        """
        Invoca LatentSync para lip-sync real del avatar.

        LatentSync requiere:
          - latsync/checkpoints/latentsync_unet.pt
          - latsync/checkpoints/whisper/tiny.pt (o small.pt)
          - Un video source del avatar (se genera desde avatar_face.png)

        Si los checkpoints no existen, devuelve None inmediatamente sin error.
        Devuelve ruta al MP4 o None si no disponible o falla.
        """
        import subprocess

        latsync_dir  = Path(_LATSYNC_DIR)
        latsync_script = Path(_LATSYNC_SCRIPT)
        latsync_ckpt = Path(_LATSYNC_CKPT)
        avatar_img   = Path(_AVATAR_IMG)

        # Verificar precondiciones
        if not latsync_dir.exists():
            self.logger.info("LatentSync: directorio latsync/ no encontrado")
            return None

        if not latsync_script.exists():
            self.logger.info("LatentSync: scripts/inference.py no encontrado")
            return None

        if not latsync_ckpt.exists():
            self.logger.info(
                "LatentSync: checkpoints no encontrados. "
                "Para activar LatentSync descarga el modelo en latsync/checkpoints/. "
                "Comando: huggingface-cli download ByteDance/LatentSync --local-dir latsync/checkpoints"
            )
            return None

        if not avatar_img.exists():
            self.logger.warning("LatentSync: avatar_face.png no encontrado")
            return None

        # LatentSync necesita un VIDEO de entrada (no imagen). Generar video corto desde avatar_face.png
        avatar_video = str(OUTPUT_LATSYNC_DIR / "avatar_source.mp4")
        if not Path(avatar_video).exists():
            try:
                self._make_avatar_source_video(str(avatar_img), avatar_video)
            except Exception as e:
                self.logger.warning(f"LatentSync: no pudo generar video source: {e}")
                return None

        audio_abs = str(Path(audio_path).resolve())
        if not Path(audio_abs).exists():
            return None

        OUTPUT_LATSYNC_DIR.mkdir(parents=True, exist_ok=True)

        cmd = [
            _LATSYNC_PYTHON,
            str(latsync_script),
            "--unet_config_path",   _LATSYNC_CFG,
            "--inference_ckpt_path", str(latsync_ckpt),
            "--video_path",         avatar_video,
            "--audio_path",         audio_abs,
            "--video_out_path",     output_path,
            "--inference_steps",    "20",
            "--guidance_scale",     "1.0",
            "--seed",               "1247",
        ]

        self.logger.info("LatentSync: lanzando inferencia...")
        console.print("[dim]LatentSync: ejecutando — puede tardar varios minutos...[/]")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                cwd=str(latsync_dir),
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("LatentSync: timeout (900s)")
            return None
        except FileNotFoundError:
            self.logger.warning(f"LatentSync: Python no encontrado ({_LATSYNC_PYTHON})")
            return None
        except Exception as e:
            self.logger.warning(f"LatentSync: error subprocess: {e}")
            return None

        if result.returncode != 0:
            self.logger.warning(
                f"LatentSync: proceso termino con codigo {result.returncode}.\n"
                f"stderr: {result.stderr[-400:] if result.stderr else '(vacio)'}"
            )
            return None

        if Path(output_path).exists():
            self.logger.info(f"LatentSync: clip generado -> {output_path}")
            return output_path

        # Buscar MP4 mas reciente como alternativa
        videos = sorted(OUTPUT_LATSYNC_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if videos:
            return str(videos[0])

        self.logger.warning("LatentSync: proceso OK pero no se genero MP4")
        return None

    def _make_avatar_source_video(self, img_path: str, output_path: str, duration: float = 5.0):
        """Genera un video loopeable corto desde la imagen del avatar para usar como source en LatentSync."""
        from moviepy.editor import ImageClip

        clip = ImageClip(img_path).set_duration(duration)
        self._write_clip(clip, output_path, fps=25, audio=False)
        clip.close()

    def _avatar_ken_burns(self, audio_path: str, output_path: str) -> str:
        """
        Ken Burns: zoom suave sobre avatar_face.png con duracion = duracion del audio.
        Genera MP4 usando MoviePy.
        Devuelve output_path si tiene exito, "" si falla.
        Nunca lanza excepcion.
        """
        from moviepy.editor import AudioFileClip, VideoClip
        import numpy as np
        from PIL import Image as PILImage

        avatar_img = Path(_AVATAR_IMG)

        try:
            audio_clip = AudioFileClip(audio_path)
            duration   = audio_clip.duration

            if avatar_img.exists():
                base = PILImage.open(str(avatar_img)).convert("RGB")
            else:
                base = PILImage.new("RGB", (640, 640), C_BG)

            base_arr  = np.array(base)
            base_w, base_h = base.size
            target_w, target_h = RES_MAIN

            def make_frame(t):
                progress_ratio = t / max(duration, 1.0)
                scale = 1.0 + 0.12 * progress_ratio  # zoom 1.00 -> 1.12

                new_w = int(base_w * scale)
                new_h = int(base_h * scale)

                scaled = PILImage.fromarray(base_arr).resize(
                    (new_w, new_h), PILImage.LANCZOS
                )

                crop_x = max((new_w - target_w) // 2, 0)
                crop_y = max((new_h - target_h) // 2, 0)
                cropped = scaled.crop(
                    (crop_x, crop_y, crop_x + target_w, crop_y + target_h)
                )

                if cropped.size != (target_w, target_h):
                    canvas = PILImage.new("RGB", (target_w, target_h), C_BG)
                    canvas.paste(cropped, (0, 0))
                    cropped = canvas

                return np.array(cropped)

            video = VideoClip(make_frame, duration=duration)
            video = video.set_audio(audio_clip)

            self._write_clip(video, output_path)

            for clip in [video, audio_clip]:
                try:
                    clip.close()
                except Exception:
                    pass

            self.logger.info(f"Ken Burns generado: {output_path}")
            return output_path

        except Exception as e:
            self.logger.error(f"Ken Burns fallo: {e}")
            return ""

    # ══════════════════════════════════════════════════════════════════════════
    # CAPA 3 — Pantalla dinamica (content-aware)
    # ══════════════════════════════════════════════════════════════════════════

    # Regex para etiquetas de escena explícitas de CALÍOPE
    _TAG_PRECIO      = re.compile(r'^\[PRECIO\]',   re.I)
    _TAG_NOTICIA     = re.compile(r'^\[NOTICIA\]',  re.I)
    _TAG_DATO        = re.compile(r'^\[DATO:([^\]]+)\]', re.I)
    _TAG_ANALISIS    = re.compile(r'^\[AN[AÁ]LISIS\]', re.I)
    _TAG_GENERAL     = re.compile(r'^\[GENERAL(?::([^\]]*))?\]', re.I)
    _TAG_SENTIMIENTO = re.compile(r'^\[SENTIMIENTO\]', re.I)
    _TAG_DOMINANCIA  = re.compile(r'^\[DOMINANCIA\]',  re.I)
    _TAG_VOLUMEN     = re.compile(r'^\[VOLUMEN\]',      re.I)
    _TAG_ADOPCION    = re.compile(r'^\[ADOPCI[OÓ]N\]', re.I)
    _TAG_PREDICCION  = re.compile(r'^\[PREDICCI[OÓ]N\]', re.I)

    # Etiquetas modo EDUCATIVO
    _TAG_TITULO_EDU     = re.compile(r'^\[TITULO\]',      re.I)
    _TAG_DEFINICION_EDU = re.compile(r'^\[DEFINICION\]',  re.I)
    _TAG_TIMELINE       = re.compile(r'^\[TIMELINE\]',    re.I)
    _TAG_COMPARATIVA    = re.compile(r'^\[COMPARATIVA\]', re.I)
    _TAG_DATOS_EDU      = re.compile(r'^\[DATOS\]',       re.I)
    _TAG_RESUMEN        = re.compile(r'^\[RESUMEN\]',     re.I)

    # Etiquetas modo URGENTE
    _TAG_ALERTA  = re.compile(r'^\[ALERTA\]',  re.I)
    _TAG_IMPACTO = re.compile(r'^\[IMPACTO\]', re.I)
    _TAG_REACCION = re.compile(r'^\[REACCION\]', re.I)

    def _parse_script_segments(self, script: str) -> List[dict]:
        """
        Parsea el guion para asignar escenas visuales a cada momento del video.

        Prioridad:
          1. Etiquetas explícitas de CALÍOPE: [PRECIO], [NOTICIA], [DATO:X],
             [ANÁLISIS], [GENERAL] al inicio de cada párrafo.
          2. Fallback: detección por keywords (compatibilidad con guiones sin etiquetas).

        Devuelve lista de dicts:
          start_ratio, end_ratio  — fracción del tiempo total (0–1)
          content_type            — precio | noticia | dato | analisis | general
          dato_value              — número extraído de [DATO:X] (str)
          query                   — query Pexels para tipo 'noticia'/'general'
        """
        if not script:
            return [{"start_ratio": 0.0, "end_ratio": 1.0,
                     "content_type": "precio", "dato_value": "", "query": ""}]

        # ── Agrupar líneas por bloque de escena ───────────────────────────
        # Cada vez que aparece una etiqueta de escena explícita al inicio de
        # una línea, abre un nuevo segmento. Las líneas sin etiqueta se añaden
        # al segmento actual.
        lines = [l.rstrip() for l in script.split("\n")]

        def _classify_line(line: str):
            """Devuelve (ctype, dato_value, query) si la línea tiene etiqueta explícita, o None."""
            stripped = line.strip()
            m = self._TAG_DATO.match(stripped)
            if m:
                # DATO se convierte en ANALISIS — nunca pantalla de número solo
                return ("analisis", "", "")
            if self._TAG_PRECIO.match(stripped):
                return ("precio", "", "")
            if self._TAG_ANALISIS.match(stripped):
                return ("analisis", "", "")
            if self._TAG_NOTICIA.match(stripped):
                return ("noticia", "", "cryptocurrency bitcoin news")
            m2 = self._TAG_GENERAL.match(stripped)
            if m2:
                return ("general", "", (m2.group(1) or "").strip() or "bitcoin crypto market")
            if self._TAG_SENTIMIENTO.match(stripped):
                return ("fear_greed", "", "")
            if self._TAG_DOMINANCIA.match(stripped):
                return ("dominancia", "", "")
            if self._TAG_VOLUMEN.match(stripped):
                return ("volumen", "", "")
            if self._TAG_ADOPCION.match(stripped):
                return ("adopcion", "", "bitcoin adoption")
            if self._TAG_PREDICCION.match(stripped):
                return ("prediccion", "", "")

            # ── Etiquetas modo EDUCATIVO ──────────────────────────────────
            if self._TAG_TITULO_EDU.match(stripped):
                return ("titulo_edu", "", "")
            if self._TAG_DEFINICION_EDU.match(stripped):
                return ("definicion_edu", "", "")
            if self._TAG_TIMELINE.match(stripped):
                return ("halving", "", "")
            if self._TAG_COMPARATIVA.match(stripped):
                return ("comparativa_edu", "", "")
            if self._TAG_DATOS_EDU.match(stripped):
                return ("datos_edu", "", "")
            if self._TAG_RESUMEN.match(stripped):
                return ("prediccion", "", "")

            # ── Etiquetas modo URGENTE ────────────────────────────────────
            if self._TAG_ALERTA.match(stripped):
                return ("urgente_alert", "", "")
            if self._TAG_IMPACTO.match(stripped):
                return ("analisis", "", "")
            if self._TAG_REACCION.match(stripped):
                return ("precio", "", "")

            return None

        # Construir bloques: list of (ctype, dato_value, query, text_chars)
        blocks = []
        current_ctype, current_dato, current_query, current_chars = "precio", "", "", 0

        for line in lines:
            classified = _classify_line(line)
            if classified:
                # Nueva escena — guardar bloque anterior si tiene contenido
                if current_chars > 0:
                    blocks.append((current_ctype, current_dato, current_query, current_chars))
                current_ctype, current_dato, current_query = classified
                current_chars = max(len(line) - len(line.lstrip('[').split(']')[0]) - 2, 5)
            else:
                current_chars += max(len(line), 1)

        # Añadir el último bloque
        if current_chars > 0 or not blocks:
            blocks.append((current_ctype, current_dato, current_query, max(current_chars, 10)))

        if len(blocks) <= 1:
            # Fallback keyword-based si no hay etiquetas (o solo 1 bloque generico)
            paras = [p.strip() for p in re.split(r'\n\s*\n', script.strip()) if p.strip()]
            if not paras:
                paras = [script.strip()]
            if len(paras) > 1:
                # Reconstruir bloques desde párrafos con clasificación por keywords
                blocks = []
                for p in paras:
                    p_lower = p.lower()
                    if re.search(r'\bmiedo\b.*\bcodicia\b|\bfear.*greed\b|sentimiento', p_lower):
                        ctype = "fear_greed"
                    elif re.search(r'\bdominancia\b|\bdomin.*btc\b|\bmarket.*cap.*total\b', p_lower):
                        ctype = "dominancia"
                    elif re.search(r'\bvolumen\b|\bexchange\b|\bprofundidad\b', p_lower):
                        ctype = "volumen"
                    elif re.search(r'\badopcion\b|\bwallet\b|\binstituc\b|\betf\b|\bon.chain\b', p_lower):
                        ctype = "adopcion"
                    elif re.search(r'\ben mi opinion\b|\bprediccion\b|\bcierre\b|\bsuscri\b', p_lower):
                        ctype = "prediccion"
                    elif _KW_TECNICO.search(p):
                        ctype = "analisis"
                    elif _KW_PRECIO.search(p) or re.search(r'\b(Bitcoin|BTC)\b', p, re.I):
                        ctype = "precio"
                    elif _KW_NOTICIA.search(p):
                        ctype = "noticia"
                    else:
                        ctype = "precio"
                    blocks.append((ctype, "", "", max(len(p), 10)))

        total_w = sum(b[3] for b in blocks)
        segments = []
        cumulative = 0

        for ctype, dato_value, query, w in blocks:
            start_r = cumulative / total_w
            end_r   = (cumulative + w) / total_w
            cumulative += w
            segments.append({
                "start_ratio":  start_r,
                "end_ratio":    end_r,
                "content_type": ctype,
                "dato_value":   dato_value,
                "query":        query,
            })

        # Normalizar: "dato" -> "analisis", "general" -> "precio" (zoom engine siempre)
        # Los tipos educativos y urgentes se preservan tal cual.
        _PRESERVE_TYPES = {
            "titulo_edu", "definicion_edu", "halving", "comparativa_edu",
            "datos_edu", "urgente_alert",
        }
        for seg in segments:
            if seg["content_type"] in _PRESERVE_TYPES:
                continue
            if seg["content_type"] == "dato":
                seg["content_type"] = "analisis"
            if seg["content_type"] == "general":
                seg["content_type"] = "precio"

        # Fusionar adyacentes del mismo tipo (evita micro-escenas)
        merged = [segments[0]]
        for seg in segments[1:]:
            prev = merged[-1]
            if seg["content_type"] == prev["content_type"]:
                prev["end_ratio"] = seg["end_ratio"]
            else:
                merged.append(seg)

        return merged

    def _get_dynamic_content_image(
        self,
        t: float,
        duration: float,
        chart_path: Optional[str],
        script_segments: List[dict],
    ):
        """
        Devuelve imagen PIL para la pantalla dinamica en el instante t.
        Fallback: chart_path global -> None.
        """
        from PIL import Image

        if not script_segments or duration <= 0:
            if chart_path and Path(chart_path).exists():
                try:
                    return Image.open(chart_path).convert("RGB")
                except Exception:
                    pass
            return None

        ratio = t / duration
        active = script_segments[-1]
        for seg in script_segments:
            if seg["start_ratio"] <= ratio < seg["end_ratio"]:
                active = seg
                break

        ctype = active.get("content_type", "pexels_stock")

        # chart_technical, chart_btc, pexels_stock -> chart_path
        if ctype in ("chart_btc", "chart_technical", "pexels_stock"):
            if chart_path and Path(chart_path).exists():
                try:
                    return Image.open(chart_path).convert("RGB")
                except Exception:
                    pass
            return None

        # chart_eth -> intenta chart_eth.png, luego chart_path
        if ctype == "chart_eth":
            for candidate in [ASSETS_DIR / "chart_eth.png", Path(chart_path or "")]:
                if candidate and Path(candidate).exists():
                    try:
                        return Image.open(str(candidate)).convert("RGB")
                    except Exception:
                        pass
            return None

        # news_image -> Pexels
        if ctype == "news_image":
            query = active.get("query", "cryptocurrency news")
            pexels_img = self._fetch_pexels_image(query)
            if pexels_img and Path(pexels_img).exists():
                try:
                    return Image.open(pexels_img).convert("RGB")
                except Exception:
                    pass
            if chart_path and Path(chart_path).exists():
                try:
                    return Image.open(chart_path).convert("RGB")
                except Exception:
                    pass
            return None

        return None

    # Términos en inglés que generan clips con texto visible en inglés
    _PEXELS_QUERY_BLACKLIST: Dict[str, str] = {
        "cryptocurrency":   "graficos financieros",
        "crypto":           "tecnologia futurista",
        "blockchain":       "datos digitales",
        "bitcoin whiteboard": "pantallas financieras",
        "crypto whiteboard":  "ciudad nocturna",
        "whiteboard":       "pantallas financieras",
    }

    def _sanitize_pexels_query(self, query: str) -> str:
        """Reemplaza términos que generan contenido con texto en inglés visible."""
        q = query.lower().strip()
        # Primero revisar frases compuestas
        for bad, replacement in self._PEXELS_QUERY_BLACKLIST.items():
            if bad in q:
                return replacement
        return query

    def _fetch_pexels_image(self, query: str) -> Optional[str]:
        """Descarga imagen de Pexels para el query. Cachea en assets/pexels_HASH.jpg."""
        import hashlib

        pexels_key = os.environ.get("PEXELS_API_KEY", "")
        if not pexels_key:
            return None

        # Sanitizar query: evitar términos que traen imágenes con texto en inglés
        query = self._sanitize_pexels_query(query)

        query_hash = hashlib.md5(query.encode()).hexdigest()[:8]
        cache_file = ASSETS_DIR / f"pexels_{query_hash}.jpg"
        if cache_file.exists():
            return str(cache_file)

        try:
            import requests
            resp = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": pexels_key},
                params={
                    "query":       query,
                    "per_page":    5,
                    "orientation": "landscape",
                    "locale":      "es-ES",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                photos = resp.json().get("photos", [])
                if photos:
                    img_resp = requests.get(photos[0]["src"]["large"], timeout=15)
                    if img_resp.status_code == 200:
                        cache_file.write_bytes(img_resp.content)
                        self.logger.info(f"Pexels: imagen cacheada -> {cache_file.name}")
                        return str(cache_file)
        except Exception as e:
            self.logger.warning(f"Pexels: {e}")

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # CAPA 4 — Ticker de precios animado
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ticker_frame(self, prices: dict, w: int, tick_offset: int = 0):
        """
        Genera barra de ticker (40px altura):
          - Fondo #111111
          - Texto #F7931A
          - Animacion: scroll de derecha a izquierda
        """
        from PIL import Image, ImageDraw, ImageFont

        bar = Image.new("RGB", (w, 40), C_TICKER)
        draw = ImageDraw.Draw(bar)

        font_ticker = None
        for fp in ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/verdanab.ttf"]:
            try:
                font_ticker = ImageFont.truetype(fp, 22)
                break
            except Exception:
                continue
        if font_ticker is None:
            font_ticker = ImageFont.load_default()

        _p = self._prices
        def _fmt(keys: list, fallback: str) -> str:
            for k in keys:
                v = _p.get(k)
                if v is not None:
                    try:
                        return f"${float(v):,.0f}"
                    except Exception:
                        pass
            # Intentar desde prices dict (string fallback)
            fv = prices.get(fallback.replace("$", ""), "")
            if fv and fv not in ("???,???", "?,???", "???"):
                try:
                    return f"${float(fv.replace(',', '')):,.0f}"
                except Exception:
                    return fv
            return "N/D"

        btc = _fmt(["bitcoin", "BTC"], "BTC")
        eth = _fmt(["ethereum", "ETH"], "ETH")
        sol = _fmt(["solana", "SOL"], "SOL")

        segment = (
            f"  BTC {btc}  |  ETH {eth}  |  SOL {sol}  |  "
            f"CryptoVerdad — Crypto sin humo  |  "
        )
        ticker_text = segment * 6

        # Calcular ancho de segmento para el loop
        try:
            bbox = draw.textbbox((0, 0), segment, font=font_ticker)
            seg_w = bbox[2] - bbox[0]
        except Exception:
            seg_w = max(w, 800)

        x_pos = -(tick_offset % max(seg_w, 1))
        draw.text((x_pos, 9), ticker_text, fill=C_ACCENT, font=font_ticker)

        return bar

    # ══════════════════════════════════════════════════════════════════════════
    # CAPA 5 — Subtitulos sincronizados
    # ══════════════════════════════════════════════════════════════════════════

    def _get_subtitle_entries(self, ctx: Context) -> List[Tuple[float, float, str]]:
        """
        Subtitulos en orden de prioridad:
          1. SRT de ctx.srt_path
          2. Whisper sobre ctx.audio_path
          3. Distribucion uniforme del guion
        """
        if ctx.srt_path and Path(ctx.srt_path).exists():
            entries = self._parse_srt(ctx.srt_path)
            if entries:
                self.logger.info(f"Subtitulos desde SRT: {len(entries)} entradas")
                return entries

        if ctx.audio_path and Path(ctx.audio_path).exists():
            whisper_entries = self._transcribe_with_whisper(ctx.audio_path)
            if whisper_entries:
                self.logger.info(f"Subtitulos desde Whisper: {len(whisper_entries)} entradas")
                return whisper_entries

        if ctx.script and ctx.audio_path and Path(ctx.audio_path).exists():
            try:
                from moviepy.editor import AudioFileClip
                ac = AudioFileClip(ctx.audio_path)
                dur = ac.duration
                ac.close()
            except Exception:
                dur = 60.0
            entries = self._build_subtitle_entries_from_script(ctx.script, dur)
            self.logger.info(f"Subtitulos distribuidos: {len(entries)} fragmentos")
            return entries

        return []

    def _parse_srt(self, srt_path: str) -> List[Tuple[float, float, str]]:
        """Parsea archivo .srt -> lista de (start_s, end_s, texto). Nunca lanza excepcion."""
        entries = []
        try:
            content = Path(srt_path).read_text(encoding="utf-8", errors="replace")
            for block in re.split(r'\n\s*\n', content.strip()):
                lines = block.strip().splitlines()
                if len(lines) < 3:
                    continue
                m = re.match(
                    r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)',
                    lines[1]
                )
                if not m:
                    continue
                h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
                start = h1*3600 + m1*60 + s1 + ms1/1000
                end   = h2*3600 + m2*60 + s2 + ms2/1000
                text  = " ".join(lines[2:]).strip()
                entries.append((start, end, text))
        except Exception as e:
            self.logger.warning(f"Parse SRT fallo: {e}")
        return entries

    def _transcribe_with_whisper(self, audio_path: str) -> List[Tuple[float, float, str]]:
        """Transcribe con Whisper (si disponible). Devuelve [] si falla."""
        try:
            import whisper
            self.logger.info("Whisper: transcribiendo audio (model=base)...")
            model = whisper.load_model("base")
            result = model.transcribe(audio_path, language="es", task="transcribe", verbose=False)
            entries = []
            for seg in result.get("segments", []):
                text = seg.get("text", "").strip()
                if text:
                    entries.append((float(seg.get("start", 0)), float(seg.get("end", 2)), text))
            return entries
        except ImportError:
            self.logger.info("Whisper no disponible")
            return []
        except Exception as e:
            self.logger.warning(f"Whisper fallo: {e}")
            return []

    def _build_subtitle_entries_from_script(
        self, script: str, audio_duration: float
    ) -> List[Tuple[float, float, str]]:
        """Distribuye el guion en chunks de ~8 palabras uniformemente en el tiempo."""
        if not script or audio_duration <= 0:
            return []

        clean = re.sub(r'\[.*?\]', '', script).strip()
        words = clean.split()
        if not words:
            return []

        chunk_size = 8
        chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]
        per_chunk = audio_duration / len(chunks)

        return [
            (idx * per_chunk, (idx + 1) * per_chunk, chunk)
            for idx, chunk in enumerate(chunks)
        ]

    # ══════════════════════════════════════════════════════════════════════════
    # CAPA 6 — Compositor principal NOTICIARIO (1920x1080)
    # ══════════════════════════════════════════════════════════════════════════

    def _compose_noticiario(
        self,
        ctx: Context,
        studio_bg_img,
        chart_path: Optional[str],
        prices: dict,
        subtitle_entries: List[Tuple[float, float, str]],
        script_segments: List[dict],
        avatar_clip_path: Optional[str],
        output_path: str,
        w: int,
        h: int,
    ) -> str:
        """
        Compositor NOTICIARIO con 6 capas usando MoviePy v1.0.3.

        Layout:
          - Fondo SD/Pillow (full frame)
          - Avatar en zona izquierda (45% W), bottom-aligned
          - Pantalla dinamica en zona derecha (55% W)
          - Ticker en barra superior (40px)
          - Subtitulos en barra inferior
        """
        from moviepy.editor import (
            AudioFileClip, ColorClip, CompositeVideoClip,
            ImageClip, VideoClip, VideoFileClip,
        )
        import numpy as np
        from PIL import Image

        # Duracion desde audio
        audio_clip = None
        duration = 60.0
        if ctx.audio_path and Path(ctx.audio_path).exists():
            try:
                audio_clip = AudioFileClip(ctx.audio_path)
                duration = audio_clip.duration
            except Exception as e:
                self.logger.warning(f"No se pudo cargar audio: {e}")

        # ── Mezcla de audio: voz + musica de fondo ────────────────────────
        try:
            from utils.music_generator import generate_music
            _not_mode = (ctx.mode or "").lower().strip() or "analisis"
            _not_music_path = str(OUTPUT_AUDIO_DIR / f"{ctx.pipeline_id}_music_not.wav")
            _not_music_path = generate_music(_not_mode, duration, _not_music_path)
            if _not_music_path and Path(_not_music_path).exists() and audio_clip is not None:
                from moviepy.editor import AudioFileClip as _AFC2, CompositeAudioClip, concatenate_audioclips
                _m2 = _AFC2(_not_music_path).volumex(0.08)   # 8% musica
                _v2 = audio_clip.volumex(0.92)               # 92% voz
                if _m2.duration < duration:
                    _loops2 = int(duration / _m2.duration) + 1
                    _m2 = concatenate_audioclips([_m2] * _loops2)
                _m2 = _m2.subclip(0, duration)
                audio_clip = CompositeAudioClip([_v2, _m2])
                self.logger.info(f"Audio mezclado noticiario: voz 92% + musica 8% (modo={_not_mode})")
        except Exception as _me2:
            self.logger.warning(f"Mezcla musica noticiario: {_me2}")
            # Continuar sin musica — no es critico

        # Pre-calcular datos constantes
        _panel_title = ""
        try:
            seo_title = getattr(ctx, "seo_title", None) or ctx.topic or ""
            _panel_title = (seo_title[:40] + "...") if len(seo_title) > 40 else seo_title
        except Exception:
            pass

        # Pre-cargar fondo como array numpy
        _bg_arr = None
        if studio_bg_img is not None:
            try:
                _bg_arr = np.array(studio_bg_img.resize((w, h), Image.LANCZOS))
            except Exception:
                pass

        # ── Layout TELEDIARIO (coordenadas fijas 1920x1080) ──────────────────
        # TICKER:          y=0, h=40 — barra superior scroll naranja
        # AVATAR:          x=0, y=80, w=660, h=880 — zona izquierda
        # PANTALLA:        x=680, y=80, w=1200, h=780 — zona derecha
        # LOWER THIRD:     y=820, h=60 — primeros 5 segundos
        # SUBTITULOS:      y=1005 (o h-35), h=30 — barra inferior
        _TICKER_H   = 40
        _AV_X       = 0
        _AV_Y       = 45     # era 80 — sube para no recortar cabeza del avatar
        _AV_W       = 660
        _AV_H       = 930    # era 880 — más alto
        _DYN_X      = 680
        _DYN_Y      = 45     # era 80 — alineado con avatar
        _DYN_W      = 1220   # era 1200 — ligeramente más ancho
        _DYN_H      = 880    # era 780 — mucho más alto
        _LT_Y       = 895    # era 820 — ajustado al nuevo slot del avatar
        _LT_H       = 75     # era 60 — más visible
        _SUB_H      = 30
        _SUB_Y      = h - _SUB_H - 5

        def make_base_frame(t: float):
            from PIL import ImageDraw, ImageFont

            # ── CAPA 1: Fondo de estudio ──────────────────────────────────
            if _bg_arr is not None:
                frame = Image.fromarray(_bg_arr.copy())
            else:
                frame = self._build_studio_background_fallback(w, h)

            draw = ImageDraw.Draw(frame)

            # ── CAPA 3: Pantalla dinamica (zona derecha) ──────────────────
            try:
                dyn_img = self._get_dynamic_content_image(
                    t, duration, chart_path, script_segments
                )
                if dyn_img is not None:
                    # Aumentar brillo del gráfico (+30%)
                    try:
                        from PIL import ImageEnhance
                        dyn_img = ImageEnhance.Brightness(dyn_img).enhance(1.3)
                        dyn_img = ImageEnhance.Contrast(dyn_img).enhance(1.1)
                    except Exception:
                        pass
                    dyn_resized = dyn_img.resize((_DYN_W, _DYN_H), Image.LANCZOS)
                    frame.paste(dyn_resized, (_DYN_X, _DYN_Y))
                    # Marco naranja 5px (era 3px)
                    draw.rectangle(
                        [(_DYN_X - 5, _DYN_Y - 5),
                         (_DYN_X + _DYN_W + 5, _DYN_Y + _DYN_H + 5)],
                        outline=C_ACCENT, width=5
                    )
            except Exception:
                pass

            # ── CAPA 4: Ticker animado (barra superior 40px, y=960 simulado) ─
            # En el layout el ticker va en y=960 segun spec, pero para visibilidad
            # lo mantenemos en la barra superior (y=0) como en la version anterior.
            try:
                tick_offset = int(t * 80)
                ticker_bar = self._build_ticker_frame(prices, w, tick_offset)
                frame.paste(ticker_bar, (0, 0))
            except Exception:
                pass

            # ── Lower Third (primeros 5 segundos) ────────────────────────
            try:
                if t < 5.5:
                    # fade-in rápido (0→0.5s) + fade-out suave (4.5→5.5s)
                    if t < 0.5:
                        fade = t / 0.5
                    elif t < 4.5:
                        fade = 1.0
                    else:
                        fade = (5.5 - t) / 1.0
                    fade = max(0.0, min(1.0, fade))
                    alpha = int(240 * fade)
                    from PIL import Image as PILImg
                    lt_w = int(w * 0.45)  # 45% del ancho, cubre zona avatar
                    lt_bar = PILImg.new("RGBA", (lt_w, _LT_H), (*C_ACCENT, alpha))
                    lt_draw = ImageDraw.Draw(lt_bar)
                    try:
                        font_lt = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
                        font_lt_sm = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 20)
                    except Exception:
                        font_lt = font_lt_sm = ImageFont.load_default()
                    lt_draw.text((20, 8),  "CARLOS",              fill=C_BG, font=font_lt)
                    lt_draw.text((20, 42), "Analista CryptoVerdad", fill=C_BG, font=font_lt_sm)
                    lt_rgb = PILImg.new("RGB", lt_bar.size, (*C_ACCENT,))
                    lt_rgb.paste(lt_bar, mask=lt_bar.split()[3])
                    frame.paste(lt_rgb, (_AV_X, _LT_Y))
            except Exception:
                pass

            # ── CAPA 6: Subtitulos (barra inferior 30px) ──────────────────
            try:
                current_sub = ""
                for start_s, end_s, text in subtitle_entries:
                    if start_s <= t < end_s:
                        current_sub = text
                        break

                if current_sub:
                    clean_sub = self._clean_text_for_display(current_sub)
                    lines_sub = textwrap.wrap(clean_sub, width=100)[:2]

                    from PIL import Image as PILImg
                    sub_bar = PILImg.new("RGBA", (w, _SUB_H * len(lines_sub)),
                                        (0, 0, 0, 178))
                    sub_draw = ImageDraw.Draw(sub_bar)
                    font_sub = None
                    for _fp in ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
                                "C:/Windows/Fonts/verdana.ttf", "C:/Windows/Fonts/calibri.ttf"]:
                        try:
                            font_sub = ImageFont.truetype(_fp, 24)
                            break
                        except Exception:
                            continue
                    if font_sub is None:
                        font_sub = ImageFont.load_default()

                    for i, line in enumerate(lines_sub):
                        sub_draw.text(
                            (w // 2, _SUB_H // 2 + i * _SUB_H),
                            line,
                            fill=C_TEXT, font=font_sub, anchor="mm"
                        )

                    sub_rgb = PILImg.new("RGB", sub_bar.size, C_BG)
                    sub_rgb.paste(sub_bar, mask=sub_bar.split()[3])
                    frame.paste(sub_rgb, (0, _SUB_Y - (_SUB_H * (len(lines_sub) - 1))))
            except Exception:
                pass

            return np.array(frame)

        # Video base (capas 1, 3, 4, lower-third, subtitulos)
        base_video = VideoClip(make_base_frame, duration=duration)
        if audio_clip:
            base_video = base_video.set_audio(audio_clip)

        # ── CAPA 2: Avatar (zona izquierda x=0, y=80, w=660, h=880) ──────
        if avatar_clip_path and Path(avatar_clip_path).exists():
            try:
                avatar_raw = VideoFileClip(avatar_clip_path)

                # Ajustar al slot del layout: w=660, h=880
                av_src_w, av_src_h = avatar_raw.size
                # Escalar para cubrir el alto manteniendo aspecto
                scale_av = _AV_H / max(av_src_h, 1)
                target_w_av = int(av_src_w * scale_av)
                target_h_av = _AV_H

                # Limitar ancho al slot
                if target_w_av > _AV_W:
                    target_w_av = _AV_W
                    target_h_av = int(av_src_h * (_AV_W / max(av_src_w, 1)))

                # Centrar horizontalmente dentro del slot de 660px
                av_offset_x = _AV_X + max(0, (_AV_W - target_w_av) // 2)
                av_offset_y = _AV_Y + max(0, _AV_H - target_h_av)

                avatar_sized = avatar_raw.resize((target_w_av, target_h_av))

                # Loop si el clip es mas corto que el audio
                if avatar_raw.duration < duration - 0.1:
                    n_loops = int(duration / max(avatar_raw.duration, 0.1)) + 2
                    avatar_timed = avatar_sized.loop(n=n_loops).subclip(0, duration)
                else:
                    avatar_timed = avatar_sized.subclip(0, min(avatar_raw.duration, duration))

                avatar_placed = avatar_timed.set_position((av_offset_x, av_offset_y))
                final_video = CompositeVideoClip([base_video, avatar_placed], size=(w, h))
                if audio_clip:
                    final_video = final_video.set_audio(audio_clip)

                self._write_clip(final_video, output_path)

                for clip in [avatar_placed, avatar_timed, avatar_sized,
                             avatar_raw, final_video, base_video]:
                    try:
                        clip.close()
                    except Exception:
                        pass
                if audio_clip:
                    try:
                        audio_clip.close()
                    except Exception:
                        pass

                self.logger.info(f"Composite telediario base+avatar -> {output_path}")
                return output_path

            except Exception as e:
                self.logger.warning(
                    f"Composite con avatar fallo: {e} — exportando sin avatar"
                )

        # Sin avatar: exportar solo el base
        self._write_clip(base_video, output_path)
        try:
            base_video.close()
            if audio_clip:
                audio_clip.close()
        except Exception:
            pass

        return output_path

    # ══════════════════════════════════════════════════════════════════════════
    # Secuencias mínimas garantizadas por formato
    # ══════════════════════════════════════════════════════════════════════════

    def _build_minimum_scene_sequence(
        self, mode: str, audio_dur: float
    ) -> List[dict]:
        """
        Devuelve una lista de segmentos con content_type y start_s/end_s
        distribuidos proporcionalmente sobre audio_dur, usando la secuencia
        template óptima para el modo indicado.

        Si audio_dur < suma de duraciones mínimas del template, las duraciones
        se escalan proporcionalmente para que el total sea exactamente audio_dur.

        Parámetros:
          mode      — modo del pipeline (analisis, standard, educativo, etc.)
          audio_dur — duración total del audio en segundos

        Devuelve lista de dicts con:
          start_s, end_s, content_type, dato_value, query, start_ratio, end_ratio
        """
        try:
            template = _SCENE_TEMPLATES.get(mode) or _SCENE_TEMPLATES.get("standard")
            raw_durs = [d for _, d in template]
            total_raw = sum(raw_durs)

            # Escalar si audio_dur es menor que la suma de duraciones sugeridas
            if audio_dur > 0 and total_raw > 0:
                scale = audio_dur / total_raw
            else:
                scale = 1.0

            result = []
            cursor = 0.0
            for ctype, raw_d in template:
                dur = raw_d * scale
                end = min(cursor + dur, audio_dur)
                result.append({
                    "start_s":      cursor,
                    "end_s":        end,
                    "start_ratio":  cursor / audio_dur if audio_dur > 0 else 0.0,
                    "end_ratio":    end   / audio_dur if audio_dur > 0 else 1.0,
                    "content_type": ctype,
                    "dato_value":   "",
                    "query":        "cryptocurrency bitcoin news" if ctype == "noticia" else "",
                })
                cursor = end

            # Ajuste fino: el último segmento debe terminar exactamente en audio_dur
            if result and abs(result[-1]["end_s"] - audio_dur) > 0.01:
                result[-1]["end_s"]     = audio_dur
                result[-1]["end_ratio"] = 1.0

            self.logger.info(
                f"Template '{mode}': {len(result)} escenas base — "
                + " | ".join(
                    f"{s['content_type']}({s['start_s']:.1f}s-{s['end_s']:.1f}s)"
                    for s in result
                )
            )
            return result

        except Exception as e:
            self.logger.error(f"_build_minimum_scene_sequence error: {e}")
            # Fallback seguro: una sola escena precio que cubre todo
            return [{
                "start_s": 0.0, "end_s": audio_dur,
                "start_ratio": 0.0, "end_ratio": 1.0,
                "content_type": "precio", "dato_value": "", "query": "",
            }]

    def _merge_with_minimum_scenes(
        self,
        timed: List[dict],
        mode: str,
        audio_dur: float,
    ) -> List[dict]:
        """
        Merge inteligente entre los segmentos reales del guión y el template mínimo.

        Reglas:
          1. Los segmentos del guión conservan su content_type y posición temporal.
          2. Los huecos entre segmentos del guión se rellenan con escenas del template.
          3. El total de duraciones siempre suma exactamente audio_dur.
          4. Si ya hay suficientes escenas (>= MIN_SCENES[mode]), devuelve timed sin cambios.

        Parámetros:
          timed     — lista de segmentos ya timed (con start_s/end_s)
          mode      — modo del pipeline
          audio_dur — duración total del audio en segundos

        Devuelve lista combinada ordenada por start_s.
        """
        try:
            min_n = MIN_SCENES.get(mode, 6)

            # Escenas visuales clave que el template garantiza para analisis/standard
            # pero que _parse_script_segments raramente produce por keywords.
            # Aunque haya suficientes segmentos numéricos, si faltan estas escenas
            # hay que aplicar el merge para inyectarlas.
            _KEY_TYPES = {"fear_greed", "dominancia", "heatmap"}
            _TEMPLATE_MODES = {"analisis", "standard"}
            existing_types = {s["content_type"] for s in timed}
            missing_key = (
                mode in _TEMPLATE_MODES
                and not _KEY_TYPES.issubset(existing_types)
            )

            if len(timed) >= min_n and not missing_key:
                # Ya hay suficientes escenas y todas las visuales clave presentes
                return timed

            template_segs = self._build_minimum_scene_sequence(mode, audio_dur)

            # Cuando faltan escenas clave visuales, el guión cubre todo el tiempo sin
            # huecos: el relleno hueco-a-hueco no inyecta nada. En su lugar, se usa
            # directamente el template completo como secuencia visual base. Los
            # subtítulos siguen siendo los del guión porque dependen del audio, no
            # de content_type.
            if missing_key:
                self.logger.info(
                    f"Escenas clave ausentes en guion para modo '{mode}': "
                    f"{_KEY_TYPES - existing_types}. "
                    f"Usando template completo ({len(template_segs)} escenas)."
                )
                return template_segs

            self.logger.info(
                f"Escenas del guion ({len(timed)}) < minimo requerido ({min_n}) "
                f"para modo '{mode}'. Aplicando merge con template."
            )

            template_iter = iter(template_segs)
            _next_tmpl = next(template_iter, None)

            merged: List[dict] = []
            script_segs = sorted(timed, key=lambda s: s["start_s"])
            cursor = 0.0

            for script_seg in script_segs:
                seg_start = script_seg["start_s"]
                seg_end   = script_seg["end_s"]

                # Rellenar el hueco [cursor, seg_start] con escenas del template
                if seg_start > cursor + 0.05:
                    gap_start = cursor
                    gap_end   = seg_start
                    while _next_tmpl is not None and gap_start < gap_end - 0.05:
                        fill_dur = min(
                            _next_tmpl["end_s"] - _next_tmpl["start_s"],
                            gap_end - gap_start,
                        )
                        if fill_dur > 0.1:
                            fill_seg = dict(_next_tmpl)
                            fill_seg["start_s"]     = gap_start
                            fill_seg["end_s"]       = gap_start + fill_dur
                            fill_seg["start_ratio"] = (
                                gap_start / audio_dur if audio_dur > 0 else 0.0
                            )
                            fill_seg["end_ratio"] = (
                                fill_seg["end_s"] / audio_dur if audio_dur > 0 else 1.0
                            )
                            merged.append(fill_seg)
                            gap_start += fill_dur
                        _next_tmpl = next(template_iter, None)

                # Añadir el segmento real del guión (preserva su content_type)
                merged.append(dict(script_seg))
                cursor = seg_end

            # Rellenar cola [cursor, audio_dur]
            if cursor < audio_dur - 0.05:
                gap_start = cursor
                gap_end   = audio_dur
                while _next_tmpl is not None and gap_start < gap_end - 0.05:
                    fill_dur = min(
                        _next_tmpl["end_s"] - _next_tmpl["start_s"],
                        gap_end - gap_start,
                    )
                    if fill_dur > 0.1:
                        fill_seg = dict(_next_tmpl)
                        fill_seg["start_s"]     = gap_start
                        fill_seg["end_s"]       = gap_start + fill_dur
                        fill_seg["start_ratio"] = (
                            gap_start / audio_dur if audio_dur > 0 else 0.0
                        )
                        fill_seg["end_ratio"] = (
                            fill_seg["end_s"] / audio_dur if audio_dur > 0 else 1.0
                        )
                        merged.append(fill_seg)
                        gap_start += fill_dur
                    _next_tmpl = next(template_iter, None)

            # Garantizar cobertura total si quedó un pequeño hueco al final
            if merged and abs(merged[-1]["end_s"] - audio_dur) > 0.05:
                merged[-1]["end_s"]     = audio_dur
                merged[-1]["end_ratio"] = 1.0

            # Si merged quedó vacío (edge case), usar el template completo
            if not merged:
                merged = template_segs

            merged.sort(key=lambda s: s["start_s"])

            self.logger.info(
                f"Merge completado: {len(merged)} escenas finales — "
                + " | ".join(
                    f"{s['content_type']}({s['start_s']:.1f}s-{s['end_s']:.1f}s)"
                    for s in merged
                )
            )
            return merged

        except Exception as e:
            self.logger.error(f"_merge_with_minimum_scenes error: {e}")
            return timed  # devolver original en caso de error

    # ══════════════════════════════════════════════════════════════════════════
    # FULLSCREEN — Gráfico a pantalla completa sin avatar
    # ══════════════════════════════════════════════════════════════════════════

    def _compose_fullscreen(
        self,
        ctx: Context,
        chart_path: Optional[str],
        prices: dict,
        subtitle_entries: List[Tuple[float, float, str]],
        output_path: str,
        w: int,
        h: int,
    ) -> str:
        """
        Layout FULLSCREEN con escenas dinamicas por segmento de guion.
        Identidad visual distinta segun formato/modo:
          urgente/noticia/breaking -> paleta roja, banner ULTIMA HORA, pulso
          educativo/tutorial       -> paleta azul oscura, fondo solido en escenas de texto
          analisis/standard/otros  -> paleta naranja CryptoVerdad (identidad base)

        Escenas (cambian cada 8s segun etiquetas de CALIOPE):
          precio       -> grafico BTC/USD a pantalla completa
          analisis     -> mismo grafico con badge ANALISIS TECNICO
          noticia      -> imagen Pexels del tema (fallback: grafico)
          fear_greed   -> medidor semicircular animado
          dominancia   -> grafico de dominancia
          volumen      -> grafico de volumen con reveal
          titulo_edu   -> fondo azul (educativo)
          definicion_edu -> fondo azul (educativo)
          halving      -> grafico halving si disponible
          comparativa_edu -> grafico correlacion si disponible
          datos_edu    -> fondo azul (educativo)
          urgente_alert -> grafico con tinte rojo

        Capas permanentes (todas las escenas):
          - Logo CryptoVerdad -- esquina superior izquierda
          - Subtitulos sincronizados -- franja inferior
          - Ticker BTC/ETH/SOL -- barra muy inferior
          - Transicion suave 0.3s entre escenas (cross-fade)
          - Banner ULTIMA HORA (solo modo urgente)
        """
        from moviepy.editor import AudioFileClip, VideoClip
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        # ── Paso 1: Detectar formato activo ───────────────────────────────
        _mode = (ctx.mode or "").lower().strip()
        _is_urgente   = _mode in ("urgente",)
        _is_noticia   = _mode in ("noticia", "breaking")
        _is_educativo = _mode in ("educativo", "tutorial")
        # standard/analisis/opinion/etc
        _is_analisis  = not _is_urgente and not _is_noticia and not _is_educativo

        # ── Paso 2: Paleta de colores segun formato ───────────────────────
        if _is_urgente:
            # URGENTE: rojo agresivo, pulsante — crisis critica
            _accent_color = (220, 50, 50)
            _bg_color     = (30, 5, 5)
            _ticker_color = (200, 30, 30)
            _badge_bg     = (180, 20, 20)
        elif _is_noticia:
            # NOTICIA/BREAKING: naranja CryptoVerdad — distinto visualmente de urgente
            _accent_color = (247, 147, 26)   # naranja identidad
            _bg_color     = (10, 10, 10)     # negro puro, sin rojo
            _ticker_color = (180, 100, 10)
            _badge_bg     = (180, 80, 0)
        elif _is_educativo:
            _accent_color = (52, 152, 219)
            _bg_color     = (10, 15, 30)
            _ticker_color = (52, 100, 180)
            _badge_bg     = (30, 80, 160)
        else:
            _accent_color = C_ACCENT
            _bg_color     = C_BG
            _ticker_color = (180, 100, 10)
            _badge_bg     = (180, 80, 0)

        # ── Audio ──────────────────────────────────────────────────────────
        audio_clip = None
        duration = 60.0
        if ctx.audio_path and Path(ctx.audio_path).exists():
            try:
                audio_clip = AudioFileClip(ctx.audio_path)
                duration = audio_clip.duration
            except Exception as e:
                self.logger.warning(f"Audio fullscreen: {e}")

        # ── Mezcla de audio: voz + musica de fondo ────────────────────────
        try:
            from utils.music_generator import generate_music
            _music_path = str(OUTPUT_AUDIO_DIR / f"{ctx.pipeline_id}_music.wav")
            _music_path = generate_music(_mode, duration, _music_path)
            if _music_path and Path(_music_path).exists() and audio_clip is not None:
                from moviepy.editor import AudioFileClip as _AFC, CompositeAudioClip, concatenate_audioclips
                _music_clip = _AFC(_music_path).volumex(0.08)   # 8% musica
                _voice_clip = audio_clip.volumex(0.92)          # 92% voz
                if _music_clip.duration < duration:
                    _loops = int(duration / _music_clip.duration) + 1
                    _music_clip = concatenate_audioclips([_music_clip] * _loops)
                _music_clip = _music_clip.subclip(0, duration)
                audio_clip = CompositeAudioClip([_voice_clip, _music_clip])
                self.logger.info(f"Audio mezclado: voz 92% + musica 8% (modo={_mode})")
        except Exception as _me:
            self.logger.warning(f"Mezcla musica fullscreen: {_me}")
            # Continuar sin musica — no es critico

        # ── Layout fijo 1920×1080 — usa constantes globales _YT_* ─────────
        _LOGO_X   = 24
        _LOGO_Y   = _YT_LOGO_Y             # 10
        _TICKER_H = _YT_TICKER_H           # 40 — SUPERIOR
        _TICKER_Y = _YT_TICKER_Y           # 0
        _SUB_H    = _YT_SUB_H              # 90
        _SUB_Y    = _YT_SUB_Y              # 990
        _GRAD_H   = 280
        _GRAD_Y   = h - _GRAD_H
        _FADE_DUR = 0.3                    # segundos de cross-fade entre escenas

        # ── Fuentes ────────────────────────────────────────────────────────
        def _load_fonts():
            fonts = {}
            specs = {
                "logo":     32,   # CryptoVerdad title
                "logo_sm":  15,   # tagline
                "title":    52,   # titulos principales (naranja)
                "subtitle": 36,   # subtitulos (blanco)
                "data":     44,   # datos importantes
                "sub":      32,   # subtitulos sincronizados
                "info":     24,   # texto informativo (gris claro)
                "tag":      18,   # tags y labels pequeños
                "badge":    22,   # badges de tipo de escena
                "dato_big": 110,  # numeros muy grandes (precio, %)
                "dato_lbl": 36,   # labels de datos grandes
                "ticker":   22,   # barra ticker inferior
            }
            # Fuentes con soporte Unicode completo para ñ/tildes (orden de preferencia)
            candidates = [
                "C:/Windows/Fonts/arialbd.ttf",    # Arial Bold — soporte Latin completo
                "C:/Windows/Fonts/segoeui.ttf",    # Segoe UI — excelente Unicode
                "C:/Windows/Fonts/verdanab.ttf",   # Verdana Bold
                "C:/Windows/Fonts/calibrib.ttf",   # Calibri Bold
                "C:/Windows/Fonts/tahoma.ttf",     # Tahoma
            ]
            for fp in candidates:
                try:
                    for k, sz in specs.items():
                        if k not in fonts:
                            fonts[k] = ImageFont.truetype(fp, sz)
                    if len(fonts) == len(specs):
                        break
                except Exception:
                    continue
            # Para subtitulos: garantizar fuente con soporte de tildes/ñ
            # load_default() NO soporta caracteres no-ASCII — intentar siempre TrueType
            if "sub" not in fonts or fonts.get("sub") is None:
                for fp in ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
                           "C:/Windows/Fonts/verdana.ttf", "C:/Windows/Fonts/calibri.ttf"]:
                    try:
                        fonts["sub"] = ImageFont.truetype(fp, 32)
                        break
                    except Exception:
                        continue
            default = ImageFont.load_default()
            for k in specs:
                fonts.setdefault(k, default)
            return fonts

        F = _load_fonts()

        # ── ChartZoomEngine — zoom dinámico en gráfico BTC ─────────────────
        _zoom_engine = None
        try:
            from agents.forge.chart_zoom_engine import ChartZoomEngine
            # Obtener OHLCV desde DAEDALUS (usa su caché de 1h)
            _ohlcv = []
            if self._daedalus_cls is not None:
                try:
                    _dae_tmp = self._daedalus_cls(self.config)
                    _ohlcv = _dae_tmp._fetch_ohlcv("bitcoin", 30)
                except Exception as _e_ohlcv:
                    self.logger.warning(f"OHLCV para zoom: {_e_ohlcv}")

            _levels = {
                "supports":    getattr(ctx, "support_levels", []) or [],
                "resistances": getattr(ctx, "resistance_levels", []) or [],
            }
            _zoom_engine = ChartZoomEngine(
                base_chart_path=chart_path or "",
                ohlcv=_ohlcv,
                subtitle_entries=subtitle_entries,
                duration=duration,
                levels=_levels,
            )
            console.print(
                f"[dim]ChartZoomEngine activo — "
                f"{len(_zoom_engine._zoom_events)} eventos de zoom "
                f"· tendencia {'alcista' if _zoom_engine._trend_px and _zoom_engine._trend_px['bullish'] else 'bajista o N/A'}[/]"
            )
        except Exception as _e_zoom:
            self.logger.warning(f"ChartZoomEngine no disponible: {_e_zoom}")

        # ── Segmentos con tiempo absoluto ──────────────────────────────────
        raw_segments = self._parse_script_segments(ctx.script or "")
        timed = []
        for seg in raw_segments:
            timed.append({
                **seg,
                "start_s": seg["start_ratio"] * duration,
                "end_s":   seg["end_ratio"]   * duration,
            })
        if not timed:
            timed = [{"start_s": 0, "end_s": duration, "content_type": "precio",
                      "dato_value": "", "query": ""}]

        # ── Subdividir segmentos mayores de 8s ────────────────────────────
        def _split_long_segments(segs, max_dur=8.0):
            result = []
            for seg in segs:
                dur = seg["end_s"] - seg["start_s"]
                if dur <= max_dur:
                    result.append(seg)
                else:
                    n = int(math.ceil(dur / max_dur))
                    chunk = dur / n
                    for i in range(n):
                        s = seg.copy()
                        s["start_s"] = seg["start_s"] + i * chunk
                        s["end_s"]   = seg["start_s"] + (i + 1) * chunk
                        result.append(s)
            return result

        timed = _split_long_segments(timed, max_dur=8.0)

        # ── Paso 5b: Garantizar escenas mínimas por modo ──────────────────
        # Si el guión generó menos segmentos que el mínimo requerido para el
        # modo activo, se insertan escenas del template en los huecos
        # temporales sin alterar los segmentos reales del guión.
        try:
            timed = self._merge_with_minimum_scenes(timed, _mode, duration)
            console.print(
                f"[dim]Escenas tras merge minimo: {len(timed)} "
                f"(min={MIN_SCENES.get(_mode, 6)} para modo '{_mode}')[/]"
            )
        except Exception as _em:
            self.logger.warning(f"Merge minimo escenas fallo (no critico): {_em}")

        # ── Paso 5c: Modo educativo — convertir escenas "precio"/"analisis" ──
        # Los segmentos del guion que CALÍOPE etiquetó como "precio" o "analisis"
        # no tienen sentido en un video educativo. Los reemplazamos por tipos
        # conceptuales antes de la dedup, para que _get_base_frame no caiga
        # nunca en ChartZoomEngine cuando el modo es educativo/tutorial.
        if _mode in ("educativo", "tutorial"):
            _edu_replacements = [
                "halving", "correlacion", "dominancia_area",
                "datos_edu", "comparativa_edu",
            ]
            _repl_idx = 0
            for _seg in timed:
                if _seg.get("content_type") in ("precio", "analisis", "prediccion"):
                    _seg["content_type"] = _edu_replacements[_repl_idx % len(_edu_replacements)]
                    _repl_idx += 1
            self.logger.info(
                f"Post-proceso educativo: {_repl_idx} escenas precio/analisis/prediccion "
                f"convertidas a tipos conceptuales."
            )

        # ── Paso 6: Evitar dos escenas del mismo tipo consecutivas ─────────
        def _dedup_consecutive(segs):
            """Evita que dos escenas del mismo tipo visual sean consecutivas."""
            result = []
            prev_type = None
            REPEATABLE = {"precio", "analisis"}
            for seg in segs:
                ctype = seg["content_type"]
                if ctype == prev_type and ctype not in REPEATABLE:
                    seg = dict(seg)
                    seg["content_type"] = "precio"
                result.append(seg)
                prev_type = seg["content_type"]
            return result

        timed = _dedup_consecutive(timed)

        self.logger.info(
            f"FULLSCREEN: {len(timed)} escenas — "
            + " | ".join(f"{s['content_type']}({s['start_s']:.0f}s-{s['end_s']:.0f}s)"
                         for s in timed)
        )

        # ── Gradiente inferior (constante) ─────────────────────────────────
        _grad_layer = Image.new("RGBA", (w, _GRAD_H), (0, 0, 0, 0))
        _gd = ImageDraw.Draw(_grad_layer)
        for i in range(_GRAD_H):
            alpha = int(210 * (i / _GRAD_H))
            _gd.line([(0, i), (w, i)], fill=(0, 0, 0, alpha))

        # ── Gráfico estático fallback (cuando zoom engine no disponible) ──
        _chart_static: Optional[Image.Image] = None
        if chart_path and Path(chart_path).exists():
            try:
                from PIL import ImageEnhance
                _chart_static = (
                    ImageEnhance.Brightness(
                        Image.open(chart_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    ).enhance(1.15)
                )
            except Exception:
                pass

        # ── Pre-fetch imágenes para segmentos NOTICIA (máx 2, sin duplicados) ─
        # POLÍTICA DE IMÁGENES NOTICIAS:
        # NUNCA usar imágenes descargadas de medios externos (CoinTelegraph, CoinDesk, etc.)
        # — viola copyright. Solo Pexels (CC0) o fondo oscuro con título.
        # Prioridad: 1) Pexels con query del título/tema
        #            2) Fondo sólido #0A0A0A con título de la noticia en texto grande
        MAX_PEXELS = 2
        _noticia_imgs: dict = {}      # seg_idx → PIL Image
        _pexels_queries_used: list = []

        def _load_and_crop_image(img_or_path) -> Optional[Image.Image]:
            """Carga una PIL Image o ruta, recorta a 16:9, oscurece para legibilidad."""
            try:
                from PIL import ImageEnhance
                if isinstance(img_or_path, str):
                    img = Image.open(img_or_path).convert("RGB")
                else:
                    img = img_or_path.convert("RGB")
                pw, ph = img.size
                tr = w / h
                if pw / ph > tr:
                    nw = int(ph * tr)
                    img = img.crop(((pw - nw) // 2, 0, (pw - nw) // 2 + nw, ph))
                else:
                    nh = int(pw / tr)
                    img = img.crop((0, (ph - nh) // 2, pw, (ph - nh) // 2 + nh))
                img = img.resize((w, h), Image.LANCZOS)
                img = ImageEnhance.Brightness(img).enhance(0.70)
                return img
            except Exception:
                return None

        def _make_news_title_frame(title: str) -> Image.Image:
            """Fondo sólido #0A0A0A con el título de la noticia en texto grande.
            Alternativa copyright-safe cuando Pexels no está disponible."""
            frame = Image.new("RGB", (w, h), (10, 10, 10))
            draw = ImageDraw.Draw(frame)
            # Banda naranja superior
            draw.rectangle([(0, 0), (w, 8)], fill=_accent_color)
            # Banda naranja inferior
            draw.rectangle([(0, h - 8), (w, h)], fill=_accent_color)
            # Etiqueta NOTICIA en naranja
            try:
                _lbl_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 36)
            except Exception:
                _lbl_font = ImageFont.load_default()
            draw.text((w // 2, h // 2 - 120), "NOTICIA", fill=_accent_color,
                      font=_lbl_font, anchor="mm")
            # Título en blanco, múltiples líneas
            _title_clean = title[:160] if title else "CryptoVerdad"
            try:
                _ttl_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 52)
            except Exception:
                _ttl_font = ImageFont.load_default()
            # Ajuste de líneas: máx ~36 caracteres por línea
            import textwrap
            _lines = textwrap.wrap(_title_clean, width=36)[:4]
            _y_start = h // 2 - (len(_lines) - 1) * 35
            for _line in _lines:
                draw.text((w // 2, _y_start), _line, fill=(255, 255, 255),
                          font=_ttl_font, anchor="mm")
                _y_start += 70
            return frame

        for _si, _seg in enumerate(timed):
            if _seg["content_type"] not in ("noticia", "adopcion"):
                continue

            if len(_pexels_queries_used) >= MAX_PEXELS:
                # Cuota agotada — fondo oscuro con título
                _news_title = ""
                if ctx.news:
                    _news_title = ctx.news[0].get("title", "")
                _noticia_imgs[_si] = _make_news_title_frame(_news_title or ctx.topic or "")
                continue

            # Prioridad 1: Pexels con query del título/tema (no hardcodeado a "bitcoin")
            _title_query = ""
            if ctx.news:
                _title_query = ctx.news[0].get("title", "")
            _raw_query = _title_query or ctx.topic or "bitcoin cryptocurrency news"
            # Limpiar: máx 4 palabras significativas, sin puntuación
            _words = re.sub(r"[^\w\s]", " ", _raw_query).split()
            _stop = {"de", "la", "el", "los", "las", "en", "y", "a", "que", "por", "con", "del"}
            _words = [_w for _w in _words if _w.lower() not in _stop and len(_w) > 2][:4]
            _q = " ".join(_words) if _words else "bitcoin cryptocurrency"

            if _q in _pexels_queries_used:
                # Query ya usada — fondo oscuro con título
                _news_title = _title_query if _title_query else ctx.topic or ""
                _noticia_imgs[_si] = _make_news_title_frame(_news_title)
                continue
            try:
                _pp = self._fetch_pexels_image(_q)
                _pimg = _load_and_crop_image(_pp) if (_pp and Path(_pp).exists()) else None
                if _pimg:
                    _noticia_imgs[_si] = _pimg
                    _pexels_queries_used.append(_q)
                else:
                    # Pexels sin clave o sin resultados — fondo oscuro con título
                    _news_title = _title_query if _title_query else ctx.topic or ""
                    _noticia_imgs[_si] = _make_news_title_frame(_news_title)
            except Exception:
                _news_title = _title_query if _title_query else ctx.topic or ""
                _noticia_imgs[_si] = _make_news_title_frame(_news_title)

        console.print(
            f"[dim]Imágenes noticia cargadas: {len(_noticia_imgs)}/{MAX_PEXELS} "
            f"(fuente: Pexels/fondo-titulo, queries: {_pexels_queries_used})[/]"
        )

        # ── Helper: frame de fondo diferenciado cuando path no disponible ────────
        # Cada tipo visual tiene su propio color y etiqueta para que las escenas
        # roten visualmente incluso sin los gráficos de DAEDALUS (dry-run, APIs caídas).
        _SCENE_FALLBACK_STYLE = {
            "fear_greed":      ((10, 30, 10),   (50, 200, 50),   "FEAR & GREED"),
            "dominancia":      ((10, 10, 35),   (80, 80, 220),   "DOMINANCIA BTC"),
            "heatmap":         ((30, 10, 10),   (220, 80, 40),   "HEATMAP ALTCOINS"),
            "volumen":         ((10, 25, 35),   (40, 160, 200),  "VOLUMEN"),
            "dominancia_area": ((10, 10, 30),   (100, 100, 200), "DOMINANCIA AREA"),
            "correlacion":     ((25, 10, 30),   (180, 80, 200),  "CORRELACION"),
            "comparativa_edu": ((25, 10, 30),   (180, 80, 200),  "COMPARATIVA"),
            "halving":         ((30, 20, 5),    (200, 140, 30),  "HALVING"),
            "prediccion":      ((5, 20, 35),    (30, 150, 220),  "PREDICCION"),
            "adopcion":        ((10, 30, 15),   (40, 200, 100),  "ADOPCION"),
            "noticia":         ((5, 5, 30),     (247, 147, 26),  "NOTICIA"),
            "urgente_alert":   ((35, 5, 5),     (220, 30, 30),   "!! ALERTA !!"),
        }

        def _make_fallback_frame(ctype: str) -> Image.Image:
            """Genera fondo plano diferenciado por tipo de escena cuando path no existe."""
            bg_col, accent_col, label = _SCENE_FALLBACK_STYLE.get(
                ctype, ((10, 10, 10), _accent_color, ctype.upper())
            )
            fb = Image.new("RGB", (w, h), bg_col)
            fb_draw = ImageDraw.Draw(fb)
            # Barra superior de acento
            fb_draw.rectangle([(0, 0), (w, 8)], fill=accent_col)
            # Barra inferior de acento
            fb_draw.rectangle([(0, h - 8), (w, h)], fill=accent_col)
            # Etiqueta centrada grande
            try:
                _fb_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 72)
            except Exception:
                _fb_font = F.get("dato_lbl", ImageFont.load_default())
            fb_draw.text((w // 2, h // 2), label, fill=accent_col,
                         font=_fb_font, anchor="mm")
            # Subtexto "Cargando datos..." debajo
            try:
                _fb_sm = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 28)
            except Exception:
                _fb_sm = F.get("info", ImageFont.load_default())
            fb_draw.text((w // 2, h // 2 + 80), "datos no disponibles",
                         fill=(120, 120, 120), font=_fb_sm, anchor="mm")
            return fb

        def _get_base_frame(t: float, active_idx: int) -> Image.Image:
            """
            Jerarquia de visual principal:
              1. Noticia/adopcion — Pexels (CC0) o fondo #0A0A0A con título (nunca medios externos)
              2. Graficos complementarios: Fear&Greed, Dominancia, Volumen
              3. ChartZoomEngine (zoom dinamico)
              4. Grafico estatico
              5. Fondo negro
            Fallback: cada tipo tiene frame diferenciado (no gráfico BTC genérico).
            """
            ctype = timed[active_idx].get("content_type", "precio")

            # Noticia: imagen real del artículo o Pexels pre-cargada
            if ctype in ("noticia", "adopcion") and active_idx in _noticia_imgs:
                return _noticia_imgs[active_idx]

            # Grafico Fear & Greed — animado con aguja que crece en 1s
            if ctype == "fear_greed":
                fg_value = getattr(ctx, "fear_greed_value", 50) or 50
                t_scene = t - timed[active_idx]["start_s"]
                anim_dur = 1.0
                progress = min(t_scene / anim_dur, 1.0)
                eased = 1.0 - (1.0 - progress) ** 2
                try:
                    fg_frame = self._render_fear_greed_frame(fg_value, eased, w, h)
                    return fg_frame
                except Exception as _fge:
                    self.logger.warning(f"Fear&Greed frame render: {_fge}")
                fg_path = getattr(ctx, "fear_greed_chart_path", "") or ""
                if fg_path and Path(fg_path).exists():
                    try:
                        return Image.open(fg_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                # Fallback diferenciado — no caer al grafico BTC
                return _make_fallback_frame(ctype)

            # Grafico Dominancia
            if ctype == "dominancia":
                dom_path = getattr(ctx, "dominance_chart_path", "") or ""
                if dom_path and Path(dom_path).exists():
                    try:
                        return Image.open(dom_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                return _make_fallback_frame(ctype)

            # Grafico Volumen — reveal de barras de abajo hacia arriba (1.5s)
            if ctype == "volumen":
                vol_path = getattr(ctx, "volume_chart_path", "") or ""
                if vol_path and Path(vol_path).exists():
                    try:
                        vol_base = Image.open(vol_path).convert("RGB").resize((w, h), Image.LANCZOS)
                        t_scene = t - timed[active_idx]["start_s"]
                        anim_dur = 1.5
                        progress = min(t_scene / anim_dur, 1.0)
                        eased = 1.0 - (1.0 - progress) ** 3
                        if progress < 1.0:
                            # Oscurecer zona no revelada de arriba (y=80 a y=920 escalados)
                            chart_top = int(80 * h / 1080)
                            chart_bot = int(920 * h / 1080)
                            reveal_top = int(chart_bot - eased * (chart_bot - chart_top))
                            vol_img = vol_base.convert("RGBA")
                            if reveal_top > chart_top:
                                left_px  = int(80 * w / 1920)
                                right_px = int(1840 * w / 1920)
                                overlay_region = Image.new(
                                    "RGBA",
                                    (right_px - left_px, reveal_top - chart_top),
                                    (13, 17, 23, 230),
                                )
                                vol_img.paste(overlay_region, (left_px, chart_top))
                            return vol_img.convert("RGB")
                        return vol_base
                    except Exception as _ve:
                        self.logger.warning(f"Volume reveal: {_ve}")
                        try:
                            return Image.open(vol_path).convert("RGB").resize((w, h), Image.LANCZOS)
                        except Exception:
                            pass
                return _make_fallback_frame(ctype)

            # Adopcion: fallback al grafico de dominancia si Pexels no disponible
            if ctype == "adopcion":
                dom_path = getattr(ctx, "dominance_chart_path", "") or ""
                if dom_path and Path(dom_path).exists():
                    try:
                        return Image.open(dom_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                return _make_fallback_frame(ctype)

            # Prediccion: grafico estatico a pantalla completa
            if ctype == "prediccion":
                if _chart_static is not None:
                    return _chart_static.copy()
                return _make_fallback_frame(ctype)

            # ── Paso 7: Graficos nuevos de DAEDALUS ───────────────────────
            if ctype == "heatmap":
                hm_path = getattr(ctx, "heatmap_chart_path", "") or ""
                if hm_path and Path(hm_path).exists():
                    try:
                        return Image.open(hm_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                return _make_fallback_frame(ctype)

            if ctype == "halving":
                hv_path = getattr(ctx, "halving_chart_path", "") or ""
                if hv_path and Path(hv_path).exists():
                    try:
                        return Image.open(hv_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                return _make_fallback_frame(ctype)

            if ctype == "correlacion" or ctype == "comparativa_edu":
                co_path = getattr(ctx, "correlation_chart_path", "") or ""
                if co_path and Path(co_path).exists():
                    try:
                        return Image.open(co_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                return _make_fallback_frame(ctype)

            if ctype == "dominancia_area":
                da_path = getattr(ctx, "dominance_area_chart_path", "") or ""
                if da_path and Path(da_path).exists():
                    try:
                        return Image.open(da_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                return _make_fallback_frame(ctype)

            # ── Paso 5: Fondo azul para escenas educativas sin grafico ────
            if _is_educativo and ctype in (
                "titulo_edu", "definicion_edu", "datos_edu", "comparativa_edu"
            ):
                edu_img = Image.new("RGB", (w, h), _bg_color)
                edu_draw = ImageDraw.Draw(edu_img)
                # Linea decorativa superior
                edu_draw.rectangle([(0, 0), (w, 6)], fill=_accent_color)
                # Linea inferior sutil
                edu_draw.rectangle([(0, h - 4), (w, h)], fill=_accent_color)

                import textwrap as _twrap

                if ctype == "titulo_edu":
                    # Header en acento
                    edu_draw.text(
                        (w // 2, h // 2 - 120), "APRENDE:",
                        fill=_accent_color, font=F["dato_lbl"], anchor="mm"
                    )
                    # Titulo del video en blanco, hasta 3 lineas
                    topic_upper = (ctx.topic or "BITCOIN").upper()
                    lines = _twrap.wrap(topic_upper, width=32)[:3]
                    for li, ln in enumerate(lines):
                        ypos = h // 2 - 40 + li * 68
                        edu_draw.text(
                            (w // 2, ypos), ln,
                            fill=(255, 255, 255), font=F["dato_lbl"], anchor="mm"
                        )
                    # Linea separadora
                    sep_y = h // 2 + 80 + max(0, len(lines) - 1) * 68
                    edu_draw.rectangle(
                        [(w // 2 - 220, sep_y), (w // 2 + 220, sep_y + 4)],
                        fill=_accent_color
                    )
                    edu_draw.text(
                        (w // 2, sep_y + 28), "CryptoVerdad",
                        fill=(120, 150, 190), font=F["tag"], anchor="mm"
                    )

                elif ctype == "definicion_edu":
                    edu_draw.text(
                        (w // 2, h // 2 - 120), "DEFINICION SIMPLE",
                        fill=_accent_color, font=F["dato_lbl"], anchor="mm"
                    )
                    edu_draw.rectangle(
                        [(w // 2 - 220, h // 2 - 78), (w // 2 + 220, h // 2 - 74)],
                        fill=_accent_color
                    )
                    edu_draw.text(
                        (w // 2, h // 2 - 30), "Como si tuvieras 12 anos",
                        fill=(180, 210, 240), font=F["sub"], anchor="mm"
                    )
                    # Recuadro azul con icono BTC
                    box_x1, box_y1 = w // 2 - 80, h // 2 + 40
                    box_x2, box_y2 = w // 2 + 80, h // 2 + 160
                    edu_draw.rounded_rectangle(
                        [(box_x1, box_y1), (box_x2, box_y2)],
                        radius=16, fill=_accent_color
                    )
                    edu_draw.text(
                        (w // 2, h // 2 + 100), "BTC",
                        fill=(10, 10, 20), font=F["dato_lbl"], anchor="mm"
                    )

                elif ctype == "comparativa_edu":
                    edu_draw.text(
                        (w // 2, 120), "ANTES VS DESPUES",
                        fill=_accent_color, font=F["dato_lbl"], anchor="mm"
                    )
                    mid_x = w // 2
                    box_top, box_bot = 190, 650
                    # Columna izquierda — rojo
                    edu_draw.rounded_rectangle(
                        [(mid_x - 560, box_top), (mid_x - 30, box_bot)],
                        radius=12, fill=(40, 8, 8), outline=(180, 40, 40), width=2
                    )
                    edu_draw.text(
                        (mid_x - 295, box_top + 55), "SIN ENTENDERLO",
                        fill=(220, 60, 60), font=F["tag"], anchor="mm"
                    )
                    edu_draw.text(
                        (mid_x - 295, box_top + 100), "Vendes en el peor",
                        fill=(200, 160, 160), font=F["tag"], anchor="mm"
                    )
                    edu_draw.text(
                        (mid_x - 295, box_top + 130), "momento",
                        fill=(200, 160, 160), font=F["tag"], anchor="mm"
                    )
                    # Columna derecha — verde
                    edu_draw.rounded_rectangle(
                        [(mid_x + 30, box_top), (mid_x + 560, box_bot)],
                        radius=12, fill=(8, 40, 8), outline=(40, 180, 40), width=2
                    )
                    edu_draw.text(
                        (mid_x + 295, box_top + 55), "ENTENDIENDOLO",
                        fill=(60, 200, 60), font=F["tag"], anchor="mm"
                    )
                    edu_draw.text(
                        (mid_x + 295, box_top + 100), "Sabes cuando actuar",
                        fill=(160, 200, 160), font=F["tag"], anchor="mm"
                    )
                    # VS en el centro
                    edu_draw.text(
                        (mid_x, (box_top + box_bot) // 2), "VS",
                        fill=_accent_color, font=F["dato_lbl"], anchor="mm"
                    )

                elif ctype == "datos_edu":
                    edu_draw.text(
                        (w // 2, 110), "DATOS CLAVE",
                        fill=_accent_color, font=F["dato_lbl"], anchor="mm"
                    )
                    edu_draw.rectangle(
                        [(w // 2 - 220, 150), (w // 2 + 220, 154)],
                        fill=_accent_color
                    )
                    for idx in range(3):
                        y_row = 220 + idx * 160
                        # Circulo numerado
                        cx, cy = 130, y_row + 55
                        edu_draw.ellipse(
                            [(cx - 45, cy - 45), (cx + 45, cy + 45)],
                            fill=_accent_color
                        )
                        edu_draw.text(
                            (cx, cy), str(idx + 1),
                            fill=(10, 10, 20), font=F["dato_lbl"], anchor="mm"
                        )
                        # Barra de dato
                        edu_draw.rounded_rectangle(
                            [(200, y_row + 20), (w - 100, y_row + 90)],
                            radius=8, fill=(20, 40, 70)
                        )

                return edu_img

            # ── Urgente_alert: fondo rojo pulsante (solo modo URGENTE) ─────
            if ctype == "urgente_alert":
                try:
                    pulse = 0.5 + 0.5 * math.sin(t * 2.5)
                    r_base = 35 + int(15 * pulse)
                    urgent_bg = Image.new("RGB", (w, h), (r_base, 5, 5))
                    urgent_draw = ImageDraw.Draw(urgent_bg)
                    # Lineas decorativas rojas
                    urgent_draw.rectangle([(0, 0), (w, 6)], fill=(220, 30, 30))
                    urgent_draw.rectangle([(0, h - 4), (w, h)], fill=(220, 30, 30))
                    # Precio BTC grande centrado si disponible
                    btc_val = getattr(ctx, "btc_price", 0) or 0
                    if btc_val > 0:
                        price_str = f"${btc_val:,.0f}"
                        urgent_draw.text(
                            (w // 2, h // 2 - 60), "BTC",
                            fill=(255, 100, 100), font=F["dato_lbl"], anchor="mm"
                        )
                        urgent_draw.text(
                            (w // 2, h // 2 + 40), price_str,
                            fill=(255, 255, 255), font=F["dato_big"], anchor="mm"
                        )
                    else:
                        urgent_draw.text(
                            (w // 2, h // 2), "ALERTA",
                            fill=(255, 60, 60), font=F["dato_big"], anchor="mm"
                        )
                    return urgent_bg
                except Exception:
                    return Image.new("RGB", (w, h), (30, 5, 5))

            # En modo EDUCATIVO, nunca mostrar gráfico de precio como fallback.
            # Usar gráfico de halvings o correlación como visual neutro.
            if _is_educativo:
                hv_path = getattr(ctx, "halving_chart_path", "") or ""
                if hv_path and Path(hv_path).exists():
                    try:
                        return Image.open(hv_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                co_path = getattr(ctx, "correlation_chart_path", "") or ""
                if co_path and Path(co_path).exists():
                    try:
                        return Image.open(co_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                da_path = getattr(ctx, "dominance_area_chart_path", "") or ""
                if da_path and Path(da_path).exists():
                    try:
                        return Image.open(da_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    except Exception:
                        pass
                # Último recurso: fondo azul con etiqueta "EDUCATIVO"
                edu_fallback = Image.new("RGB", (w, h), _bg_color)
                edu_fb_draw = ImageDraw.Draw(edu_fallback)
                edu_fb_draw.rectangle([(0, 0), (w, 6)], fill=_accent_color)
                edu_fb_draw.text(
                    (w // 2, h // 2), "EDUCATIVO",
                    fill=_accent_color, font=F["dato_big"], anchor="mm"
                )
                return edu_fallback

            # Default: ChartZoomEngine (precio, analisis, y cualquier tipo sin visual especifico)
            # URGENTE sin escena especifica -> fondo rojo oscuro simple
            # NOTICIA -> grafico normal (sin rojo)
            if _is_urgente and ctype not in ("precio", "analisis", "heatmap"):
                if _zoom_engine is not None and _zoom_engine._base_img is not None:
                    return _zoom_engine.get_frame(t, w, h)
                if _chart_static is not None:
                    return _chart_static.copy()
                return Image.new("RGB", (w, h), (20, 5, 5))
            if _zoom_engine is not None and _zoom_engine._base_img is not None:
                return _zoom_engine.get_frame(t, w, h)
            if _chart_static is not None:
                return _chart_static.copy()
            return Image.new("RGB", (w, h), C_BG)

        # ── Función de frame ───────────────────────────────────────────────
        def make_frame(t: float):
            from PIL import Image as PILImg

            # Encontrar segmento activo
            active_idx = len(timed) - 1
            for i, seg in enumerate(timed):
                if seg["start_s"] <= t < seg["end_s"]:
                    active_idx = i
                    break

            # Visual principal: zoom engine / Pexels / fallback estatico
            scene = _get_base_frame(t, active_idx)

            # Cross-fade suave con el visual del segmento anterior
            t_in_seg = t - timed[active_idx]["start_s"]
            if t_in_seg < _FADE_DUR and active_idx > 0:
                prev_scene = _get_base_frame(timed[active_idx]["start_s"], active_idx - 1)
                alpha = t_in_seg / _FADE_DUR
                alpha = alpha * alpha * (3 - 2 * alpha)  # smoothstep
                frame = PILImg.blend(prev_scene, scene, alpha)
            else:
                frame = scene

            # ── Paso 3: Pulsacion roja para modo urgente ───────────────────
            if _is_urgente:
                try:
                    pulse = 0.85 + 0.15 * abs(math.sin(t * 1.5))
                    frame_arr = np.array(frame)
                    frame_arr[:, :, 0] = np.clip(
                        frame_arr[:, :, 0].astype(float) * pulse + 20 * pulse, 0, 255
                    ).astype(np.uint8)
                    frame = PILImg.fromarray(frame_arr.astype(np.uint8))
                except Exception:
                    pass

            # ── Gradiente inferior ─────────────────────────────────────────
            try:
                fr_rgba = frame.convert("RGBA")
                fr_rgba.paste(_grad_layer, (0, _GRAD_Y), mask=_grad_layer.split()[3])
                frame = fr_rgba.convert("RGB")
            except Exception:
                pass

            draw = ImageDraw.Draw(frame)

            # ── Logo CryptoVerdad (acento segun formato) ───────────────────
            try:
                _LOGO_W, _LOGO_H = 260, 68
                logo_bg = PILImg.new("RGBA", (_LOGO_W, _LOGO_H), (0, 0, 0, 160))
                ld = ImageDraw.Draw(logo_bg)
                ld.rectangle([(0, 0), (5, _LOGO_H)], fill=(*_accent_color, 255))
                try:
                    _font_logo_big = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 32)
                    _font_logo_sm  = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 15)
                except Exception:
                    _font_logo_big = F["logo"]
                    _font_logo_sm  = F["tag"]
                ld.text((18, 8),  "CryptoVerdad",
                        fill=(*_accent_color, 255), font=_font_logo_big)
                ld.text((18, 46), "Crypto sin humo · @CryptoVerdad",
                        fill=(180, 180, 180, 255), font=_font_logo_sm)
                fr_rgba2 = frame.convert("RGBA")
                fr_rgba2.paste(logo_bg, (_LOGO_X, _LOGO_Y), mask=logo_bg.split()[3])
                frame = fr_rgba2.convert("RGB")
                draw = ImageDraw.Draw(frame)
            except Exception:
                pass

            # ── Paso 4: Banner superior (urgente=rojo pulsante / noticia=naranja fijo)
            if _is_urgente or _is_noticia:
                try:
                    banner_h = 60
                    try:
                        banner_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
                    except Exception:
                        banner_font = ImageFont.load_default()

                    if _is_urgente:
                        # Rojo pulsante: oscila entre 180 y 220 en el canal rojo
                        pulse = 0.5 + 0.5 * math.sin(t * 3)
                        r_val = int(180 + 40 * pulse)
                        banner_img = PILImg.new("RGBA", (w, banner_h), (r_val, 20, 20, 220))
                        banner_draw = ImageDraw.Draw(banner_img)
                        banner_text = "!! ALERTA CRITICA !!"
                        # El texto parpadea para reforzar urgencia
                        txt_fill = (255, 255, 255, 255) if int(t * 2) % 2 == 0 else (255, 180, 180, 200)
                        banner_draw.text((20, 15), banner_text, fill=txt_fill, font=banner_font)
                    else:
                        # Naranja fijo: identidad CryptoVerdad, sin pulso
                        banner_img = PILImg.new("RGBA", (w, banner_h), (200, 110, 0, 210))
                        banner_draw = ImageDraw.Draw(banner_img)
                        banner_text = "!! ULTIMA HORA !!"
                        banner_draw.text((20, 15), banner_text,
                                         fill=(255, 255, 255, 255), font=banner_font)

                    # Precio BTC a la derecha — ctx.btc_price es la fuente primaria (ARGOS tiempo real)
                    _ctx_btc = getattr(ctx, 'btc_price', 0) or 0
                    _prices_btc = prices.get('BTC') or prices.get('bitcoin')
                    _btc_raw = (
                        (f"{_ctx_btc:,.0f}" if _ctx_btc > 0 else None) or
                        (_prices_btc if _prices_btc and _prices_btc not in ("???,???", "---", "") else None) or
                        "---"
                    )
                    btc_price_txt = f"BTC ${_btc_raw}"
                    btc_fill = (255, 255, 100, 255) if _is_urgente else (255, 220, 150, 255)
                    bbox = banner_draw.textbbox((0, 0), btc_price_txt, font=banner_font)
                    txt_w = bbox[2] - bbox[0]
                    banner_draw.text((w - txt_w - 20, 15), btc_price_txt,
                                     fill=btc_fill, font=banner_font)
                    fr_banner = frame.convert("RGBA")
                    fr_banner.paste(banner_img, (0, 0), banner_img)
                    frame = fr_banner.convert("RGB")
                    draw = ImageDraw.Draw(frame)
                except Exception:
                    pass

            # ── Paso 9+10: Badge de tipo de escena (esquina superior derecha)
            try:
                ctype = timed[active_idx]["content_type"]

                # Paso 9: labels completos incluyendo tipos nuevos
                badge_labels = {
                    "precio":          "PRECIO EN VIVO",
                    "analisis":        "ANALISIS TECNICO",
                    "noticia":         "ULTIMA HORA",
                    "fear_greed":      "SENTIMIENTO",
                    "dominancia":      "DOMINANCIA BTC",
                    "volumen":         "VOLUMEN",
                    "adopcion":        "ADOPCION",
                    "prediccion":      "PREDICCION",
                    "heatmap":         "MAPA DE CALOR",
                    "halving":         "HALVING TIMELINE",
                    "correlacion":     "CORRELACION",
                    "dominancia_area": "DOMINANCIA",
                    "titulo_edu":      "EDUCATIVO",
                    "definicion_edu":  "DEFINICION",
                    "comparativa_edu": "COMPARATIVA",
                    "datos_edu":       "DATOS CLAVE",
                    "urgente_alert":   "ALERTA",
                }

                # Paso 10: color del badge segun formato y tipo
                if ctype == "urgente_alert" or _is_urgente:
                    badge_color = (200, 30, 30)   # rojo — solo URGENTE y urgente_alert
                elif _is_noticia or ctype == "noticia":
                    badge_color = (200, 100, 0)   # naranja oscuro — NOTICIA/BREAKING
                elif ctype in ("titulo_edu", "definicion_edu", "halving",
                               "comparativa_edu", "datos_edu") or _is_educativo:
                    badge_color = (52, 120, 210)
                else:
                    # Colores por defecto segun tipo especifico
                    _badge_defaults = {
                        "precio":     _accent_color,
                        "analisis":   (33, 150, 243),
                        "fear_greed": (156, 39, 176),
                        "dominancia": _accent_color,
                        "volumen":    (33, 150, 243),
                        "adopcion":   (76, 175, 80),
                        "prediccion": (244, 67, 54),
                        "heatmap":    (255, 152, 0),
                        "correlacion": (0, 188, 212),
                        "dominancia_area": _accent_color,
                    }
                    badge_color = _badge_defaults.get(ctype, _badge_bg)

                label = badge_labels.get(ctype, "")
                if label:
                    # En modo urgente el badge se posiciona debajo del banner
                    badge_y = 70 if (_is_urgente or _is_noticia) else 20
                    bw = len(label) * 14 + 32
                    badge = PILImg.new("RGBA", (bw, 38), (*badge_color, 220))
                    bd = ImageDraw.Draw(badge)
                    bd.text((bw // 2, 19), label,
                            fill=(10, 10, 10), font=F["badge"], anchor="mm")
                    fr_rgba3 = frame.convert("RGBA")
                    fr_rgba3.paste(badge, (w - bw - 20, badge_y), mask=badge.split()[3])
                    frame = fr_rgba3.convert("RGB")
                    draw = ImageDraw.Draw(frame)
            except Exception:
                pass

            # ── Subtítulos sincronizados ────────────────────────────────────
            try:
                current_sub = ""
                for start_s, end_s, text in subtitle_entries:
                    if start_s <= t < end_s:
                        current_sub = text
                        break
                if current_sub:
                    clean = self._clean_text_for_display(current_sub)
                    lines = textwrap.wrap(clean, width=88)[:2]
                    bar_h = _SUB_H * len(lines)
                    sub_bar = PILImg.new("RGBA", (w, bar_h), (0, 0, 0, 190))
                    sd = ImageDraw.Draw(sub_bar)
                    for i, line in enumerate(lines):
                        cy = _SUB_H // 2 + i * _SUB_H
                        # Sombra
                        sd.text((w // 2 + 2, cy + 2), line,
                                fill=(0, 0, 0, 220), font=F["sub"], anchor="mm")
                        sd.text((w // 2, cy), line,
                                fill=(*C_TEXT, 255), font=F["sub"], anchor="mm")
                    sub_rgb = PILImg.new("RGB", sub_bar.size, C_BG)
                    sub_rgb.paste(sub_bar, mask=sub_bar.split()[3])
                    frame.paste(sub_rgb, (0, _SUB_Y - (bar_h - _SUB_H)))
            except Exception:
                pass

            # ── Ticker BTC/ETH/SOL ─────────────────────────────────────────
            try:
                ticker_bar = self._build_ticker_frame(prices, w, int(t * 90))
                ticker_bar = ticker_bar.resize((w, _TICKER_H), Image.LANCZOS)
                frame.paste(ticker_bar, (0, _TICKER_Y))
            except Exception:
                pass

            return np.array(frame)

        video = VideoClip(make_frame, duration=duration)
        if audio_clip:
            video = video.set_audio(audio_clip)

        self._write_clip(video, output_path)
        try:
            video.close()
            if audio_clip:
                audio_clip.close()
        except Exception:
            pass

        return output_path

    # ── Helpers de animacion FULLSCREEN ──────────────────────────────────────

    def _render_fear_greed_frame(
        self, value: int, progress: float, w: int, h: int
    ):
        """
        Dibuja un medidor semicircular Fear & Greed en PIL puro (sin matplotlib).
        progress: 0.0 (aguja en 0) -> 1.0 (aguja en valor real).
        Retorna PIL Image RGB de tamano (w, h).
        """
        import math as _math
        from PIL import Image as PILImg, ImageDraw, ImageFont

        img = PILImg.new("RGB", (w, h), (13, 17, 23))
        draw = ImageDraw.Draw(img)

        # Centro y radios en coordenadas absolutas (medidor centrado en pantalla completa)
        cx = w // 2
        cy = int(h * 0.55)
        r_outer = int(min(w, h) * 0.35)
        r_inner = int(r_outer * 0.55)

        # Zonas del semicirculo (izquierda=0, derecha=100)
        zones = [
            (0,  25, (244, 67,  54)),   # rojo miedo extremo
            (25, 45, (255, 152, 0)),    # naranja miedo
            (45, 55, (255, 235, 59)),   # amarillo neutral
            (55, 75, (139, 195, 74)),   # verde claro codicia
            (75, 100, (76, 175, 80)),   # verde codicia extrema
        ]

        n_pts = 80
        for (v0, v1, color) in zones:
            # Semicirculo: angulo 180deg (izq) a 0deg (dcha) = PI a 0
            theta0 = _math.pi - (v0 / 100.0) * _math.pi
            theta1 = _math.pi - (v1 / 100.0) * _math.pi
            thetas = [theta0 + (theta1 - theta0) * k / n_pts for k in range(n_pts + 1)]

            outer_pts = [(cx + int(r_outer * _math.cos(a)),
                          cy - int(r_outer * _math.sin(a))) for a in thetas]
            inner_pts = [(cx + int(r_inner * _math.cos(a)),
                          cy - int(r_inner * _math.sin(a))) for a in reversed(thetas)]
            poly = outer_pts + inner_pts
            draw.polygon(poly, fill=color)

        # Aguja animada
        display_value = value * progress
        needle_angle = _math.pi - (display_value / 100.0) * _math.pi
        needle_len = int(r_outer * 0.80)
        nx = cx + int(needle_len * _math.cos(needle_angle))
        ny = cy - int(needle_len * _math.sin(needle_angle))
        draw.line([(cx, cy), (nx, ny)], fill=(255, 255, 255), width=max(4, r_outer // 30))
        draw.ellipse(
            [(cx - 10, cy - 10), (cx + 10, cy + 10)],
            fill=(255, 255, 255),
        )

        # Texto valor animado
        try:
            font_big = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", max(48, r_outer // 3))
            font_lbl = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", max(22, r_outer // 7))
        except Exception:
            font_big = ImageFont.load_default()
            font_lbl = ImageFont.load_default()

        def _zone_color_rgb(v):
            if v < 25:
                return (244, 67, 54)
            if v < 45:
                return (255, 152, 0)
            if v < 55:
                return (255, 235, 59)
            if v < 75:
                return (139, 195, 74)
            return (76, 175, 80)

        zc = _zone_color_rgb(int(display_value))
        draw.text((cx, cy + int(r_inner * 0.3)), str(int(display_value)),
                  fill=zc, font=font_big, anchor="mm")

        lbl_map = [(75, "CODICIA EXTREMA"), (55, "CODICIA"), (45, "NEUTRAL"),
                   (25, "MIEDO"), (0, "MIEDO EXTREMO")]
        lbl = "NEUTRAL"
        for threshold, name in lbl_map:
            if int(display_value) >= threshold:
                lbl = name
                break
        draw.text((cx, cy + int(r_inner * 0.65)), lbl,
                  fill=zc, font=font_lbl, anchor="mm")

        # Titulo
        try:
            font_title = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
        except Exception:
            font_title = ImageFont.load_default()
        draw.text((cx, int(h * 0.10)), "FEAR & GREED INDEX",
                  fill=(255, 255, 255), font=font_title, anchor="mm")

        return img

    # ══════════════════════════════════════════════════════════════════════════
    # Modos alternativos: ANALISIS, EDUCATIVO
    # ══════════════════════════════════════════════════════════════════════════

    def _compose_analisis(
        self,
        w: int, h: int,
        audio_path: str,
        chart_path: Optional[str],
        prices: dict,
        subtitle_entries: List[Tuple[float, float, str]],
        output_path: str,
    ) -> str:
        """Modo ANALISIS: grafico cubre todo el fondo, avatar en esquina inferior izquierda."""
        from moviepy.editor import AudioFileClip, VideoClip
        import numpy as np
        from PIL import Image

        audio_clip = None
        duration = 60.0
        if audio_path and Path(audio_path).exists():
            try:
                audio_clip = AudioFileClip(audio_path)
                duration = audio_clip.duration
            except Exception as e:
                self.logger.warning(f"Audio analisis: {e}")

        def make_frame(t):
            frame = Image.new("RGB", (w, h), C_BG)

            if chart_path and Path(chart_path).exists():
                try:
                    chart_img = Image.open(chart_path).convert("RGB").resize((w, h), Image.LANCZOS)
                    overlay = Image.new("RGB", (w, h), (0, 0, 0))
                    frame = Image.blend(chart_img, overlay, alpha=0.30)
                except Exception:
                    pass

            frame = self._draw_avatar_placeholder(frame, w, h, small=True)

            try:
                ticker_bar = self._build_ticker_frame(prices, w, int(t * 80))
                frame.paste(ticker_bar, (0, 0))
            except Exception:
                pass

            current_sub = ""
            for start_s, end_s, text in subtitle_entries:
                if start_s <= t < end_s:
                    current_sub = text
                    break
            if current_sub:
                sub_h = 70
                sub_bar = self._build_subtitle_overlay(current_sub, w, sub_h)
                sub_rgb = Image.new("RGB", sub_bar.size, C_BG)
                sub_rgb.paste(sub_bar, mask=sub_bar.split()[3])
                frame.paste(sub_rgb, (0, h - sub_h - 5))

            return np.array(frame)

        video = VideoClip(make_frame, duration=duration)
        if audio_clip:
            video = video.set_audio(audio_clip)

        self._write_clip(video, output_path)
        try:
            video.close()
            if audio_clip:
                audio_clip.close()
        except Exception:
            pass

        return output_path

    def _compose_educativo(
        self,
        w: int, h: int,
        audio_path: str,
        prices: dict,
        subtitle_entries: List[Tuple[float, float, str]],
        output_path: str,
        topic: str = "",
    ) -> str:
        """Modo EDUCATIVO: infografia Pillow + avatar + subtitulos."""
        from moviepy.editor import AudioFileClip, VideoClip
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        audio_clip = None
        duration = 60.0
        if audio_path and Path(audio_path).exists():
            try:
                audio_clip = AudioFileClip(audio_path)
                duration = audio_clip.duration
            except Exception as e:
                self.logger.warning(f"Audio educativo: {e}")

        def make_frame(t):
            frame = self._build_studio_background_fallback(w, h)
            draw = ImageDraw.Draw(frame)

            px0, py0 = int(w * 0.57), 50
            px1, py1 = w - 10, h - 90

            draw.rectangle([(px0, py0), (px1, py1)], fill=C_DARK, outline=C_ACCENT, width=2)

            try:
                font_title = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 20)
            except Exception:
                font_title = ImageFont.load_default()

            topic_short = textwrap.shorten(topic or "CryptoVerdad", width=35, placeholder="...")
            draw.text((px0 + 15, py0 + 15), topic_short, fill=C_ACCENT, font=font_title)

            bar_labels = ["Volumen", "Market Cap", "Dominancia", "RSI", "Tendencia"]
            bar_y = py0 + 55
            for i, label in enumerate(bar_labels):
                fill_pct = (60 + (i * 15 + t * 10) % 140) / 200
                bw = int((px1 - px0 - 80) * fill_pct)
                lx, ly = px0 + 15, bar_y + i * 45
                draw.text((lx, ly), label, fill=C_GREY, font=ImageFont.load_default())
                draw.rectangle([(lx, ly + 16), (lx + bw, ly + 30)], fill=C_ACCENT)

            frame = self._draw_avatar_placeholder(frame, w, h, small=False)

            try:
                ticker_bar = self._build_ticker_frame(prices, w, int(t * 80))
                frame.paste(ticker_bar, (0, 0))
            except Exception:
                pass

            current_sub = ""
            for start_s, end_s, text in subtitle_entries:
                if start_s <= t < end_s:
                    current_sub = text
                    break
            if current_sub:
                sub_h = 70
                sub_bar = self._build_subtitle_overlay(current_sub, w, sub_h)
                sub_rgb = Image.new("RGB", sub_bar.size, C_BG)
                sub_rgb.paste(sub_bar, mask=sub_bar.split()[3])
                frame.paste(sub_rgb, (0, h - sub_h - 5))

            return np.array(frame)

        video = VideoClip(make_frame, duration=duration)
        if audio_clip:
            video = video.set_audio(audio_clip)

        self._write_clip(video, output_path)
        try:
            video.close()
            if audio_clip:
                audio_clip.close()
        except Exception:
            pass

        return output_path

    # ══════════════════════════════════════════════════════════════════════════
    # SHORT vertical (1080x1920)
    # ══════════════════════════════════════════════════════════════════════════

    def _compose_short_vertical(
        self,
        w: int, h: int,
        audio_path: str,
        chart_path: Optional[str],
        prices: dict,
        subtitle_entries: List[Tuple[float, float, str]],
        output_path: str,
    ) -> str:
        """
        Short vertical 1080x1920:
          - Avatar centrado (sin pantalla dinamica)
          - Ticker superior
          - Subtitulos grandes
          - Duracion maxima 60s
        """
        from moviepy.editor import AudioFileClip, VideoClip
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        audio_clip = None
        duration = 60.0
        if audio_path and Path(audio_path).exists():
            try:
                audio_clip = AudioFileClip(audio_path)
                duration = min(audio_clip.duration, 60.0)
                if audio_clip.duration > 60.0:
                    audio_clip = audio_clip.subclip(0, 60.0)
            except Exception as e:
                self.logger.warning(f"Audio SHORT: {e}")

        def make_frame(t):
            frame = Image.new("RGB", (w, h), C_BG)
            draw = ImageDraw.Draw(frame)

            # Gradiente de fondo
            for y in range(0, h, 3):
                g = int(10 + (y / h) * 6)
                draw.line([(0, y), (w, y)], fill=(g, g, g + 4))
                draw.line([(0, y + 1), (w, y + 1)], fill=(g, g, g + 4))
                draw.line([(0, y + 2), (w, y + 2)], fill=(g, g, g + 4))

            # ── Layout Short 1080x1920 — usa constantes globales _SH_* ──────────
            # Logo: y=10  h=68  → bottom=78
            # Gráfico: y=80  h=1700 → bottom=1780
            # Subs:   y=1780 h=80  → bottom=1860
            # Ticker: y=1860 h=60  → bottom=1920  (total: 1920px exactos, 0 negro)
            _LOGO_Y       = _SH_LOGO_Y            # 10
            _LOGO_H       = _SH_LOGO_H            # 68
            _CHART_Y      = _SH_CONTENT_Y         # 80
            _CHART_AVAIL  = _SH_CONTENT_H         # 1700
            _SUB_Y        = _SH_SUB_Y             # 1780
            _SUB_H_S      = _SH_SUB_H             # 80
            _TICKER_Y     = _SH_TICKER_Y          # 1860
            _TICKER_H_S   = _SH_TICKER_H          # 60

            # Gráfico BTC — escala para llenar toda la zona disponible
            # Estrategia: escalar por altura, crop derecha (muestra datos más recientes)
            if chart_path and Path(chart_path).exists():
                try:
                    chart_img = Image.open(chart_path).convert("RGB")
                    ch_ratio = chart_img.width / max(chart_img.height, 1)
                    _ch_h = _CHART_AVAIL
                    _ch_w = int(_ch_h * ch_ratio)
                    if _ch_w < w:
                        # Chart portrait o muy estrecho — escalar por ancho
                        _ch_w = w
                        _ch_h = min(int(_ch_w / max(ch_ratio, 0.1)), _CHART_AVAIL)
                    chart_img = chart_img.resize((_ch_w, _ch_h), Image.LANCZOS)
                    # Crop: mostrar parte derecha (datos más recientes) y ajustar alto
                    x_off = max(0, _ch_w - w)
                    crop_h = min(_ch_h, _CHART_AVAIL)
                    chart_img = chart_img.crop((x_off, 0, x_off + w, crop_h))
                    frame.paste(chart_img, (0, _CHART_Y))
                    draw = ImageDraw.Draw(frame)
                    draw.rectangle(
                        [(0, _CHART_Y), (w - 1, _CHART_Y + crop_h - 1)],
                        outline=C_ACCENT, width=2
                    )
                except Exception:
                    pass

            # Logo CryptoVerdad — overlay arriba-izquierda con fondo semi-opaco
            try:
                from PIL import Image as PILImg, ImageDraw as PILDraw
                _sv_logo_w, _sv_logo_h = 280, _LOGO_H
                _sv_logo_bg = PILImg.new("RGBA", (_sv_logo_w, _sv_logo_h), (0, 0, 0, 190))
                _sv_ld = PILDraw.Draw(_sv_logo_bg)
                _sv_ld.rectangle([(0, 0), (5, _sv_logo_h)], fill=(*C_ACCENT, 255))
                try:
                    _sv_font_big = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 30)
                    _sv_font_sm  = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 14)
                except Exception:
                    _sv_font_big = ImageFont.load_default()
                    _sv_font_sm  = ImageFont.load_default()
                _sv_ld.text((16, 8),  "CryptoVerdad",
                            fill=(*C_ACCENT, 255), font=_sv_font_big)
                _sv_ld.text((16, 44), "Crypto sin humo · @CryptoVerdad",
                            fill=(180, 180, 180, 255), font=_sv_font_sm)
                _sv_frame_rgba = frame.convert("RGBA")
                _sv_frame_rgba.paste(_sv_logo_bg, (8, _LOGO_Y), mask=_sv_logo_bg.split()[3])
                frame = _sv_frame_rgba.convert("RGB")
                draw = ImageDraw.Draw(frame)
            except Exception:
                pass

            # Subtítulos — overlay sobre el gráfico, encima del ticker
            current_sub = ""
            for start_s, end_s, text in subtitle_entries:
                if start_s <= t < end_s:
                    current_sub = text
                    break

            if current_sub:
                from PIL import Image as PILImg
                sub_bar = PILImg.new("RGBA", (w, _SUB_H_S), (0, 0, 0, 220))
                sub_draw = ImageDraw.Draw(sub_bar)
                font_sub = None
                for _fp in ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/verdanab.ttf",
                            "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/segoeui.ttf"]:
                    try:
                        font_sub = ImageFont.truetype(_fp, 44)
                        break
                    except Exception:
                        continue
                if font_sub is None:
                    font_sub = ImageFont.load_default()
                wrapped = textwrap.fill(self._clean_text_for_display(current_sub), width=22)
                sub_draw.text((w // 2, _SUB_H_S // 2), wrapped, fill=C_TEXT, font=font_sub,
                              anchor="mm", align="center")
                sub_rgb = PILImg.new("RGB", sub_bar.size, C_BG)
                sub_rgb.paste(sub_bar, mask=sub_bar.split()[3])
                frame.paste(sub_rgb, (0, _SUB_Y))

            # Ticker — INFERIOR (zona segura para TikTok/Shorts, no tapado por UI)
            try:
                ticker_bar = self._build_ticker_frame(prices, w, int(t * 80))
                _ticker_img = ticker_bar.resize((w, _TICKER_H_S), Image.LANCZOS)
                frame.paste(_ticker_img, (0, _TICKER_Y))
                draw = ImageDraw.Draw(frame)
            except Exception:
                draw = ImageDraw.Draw(frame)
                draw.rectangle([(0, _TICKER_Y), (w, h)], fill=(17, 17, 17))

            return np.array(frame)

        video = VideoClip(make_frame, duration=duration)
        if audio_clip:
            video = video.set_audio(audio_clip)

        self._write_clip(video, output_path)
        try:
            video.close()
            if audio_clip:
                audio_clip.close()
        except Exception:
            pass

        return output_path

    def _crop_to_short(self, horizontal_path: str, output_path: str) -> str:
        """Genera version SHORT recortando el video horizontal 1920x1080 a 1080x1920.

        Estrategia crop+zoom para llenar 100% la pantalla vertical sin barras negras:
          1. Escalar el video horizontal para que su ALTO llene los 1920px del target.
             Un 1920x1080 escalado por altura 1920/1080 = 1.777x queda 3413x1920.
          2. Recortar el centro horizontal para quedarse con 1080px de ancho.
        Resultado: 1080x1920 con contenido al 100%, sin barras negras.
        """
        from moviepy.editor import VideoFileClip
        from moviepy.video.fx.all import crop as fx_crop

        try:
            src = VideoFileClip(horizontal_path)
            tw, th = RES_SHORT  # 1080, 1920
            src_w, src_h = src.size  # tipicamente 1920, 1080

            src_ratio   = src_w / src_h      # 1.777 para 16:9
            target_ratio = tw / th           # 0.5625 para 9:16

            if src_ratio > target_ratio:
                # Source es mas ancho que el target (caso tipico 16:9 → 9:16):
                # Escalar por altura para que src_h → th (1920px)
                scale_factor = th / src_h    # 1920/1080 = 1.777
                scaled = src.resize(scale_factor)  # → ~3413x1920
                # Recortar el centro horizontal para obtener tw (1080px)
                x_center = scaled.w / 2
                result = fx_crop(scaled, x_center=x_center, width=tw)
            else:
                # Source es mas alto que el target (poco frecuente):
                # Escalar por ancho para que src_w → tw (1080px)
                scale_factor = tw / src_w
                scaled = src.resize(scale_factor)  # → 1080x?
                y_center = scaled.h / 2
                result = fx_crop(scaled, y_center=y_center, height=th)

            if src.audio:
                result = result.set_audio(src.audio)

            self._write_clip(result, output_path)
            src.close()
            result.close()
            return output_path

        except Exception as e:
            self.logger.warning(f"Recorte a SHORT fallo: {e}")
            return ""

    # ══════════════════════════════════════════════════════════════════════════
    # Helpers Pillow (avatar placeholder y overlays)
    # ══════════════════════════════════════════════════════════════════════════

    def _draw_avatar_placeholder(self, img, w: int, h: int, small: bool = False):
        """
        Pega avatar_face.png (con transparencia) sobre el frame.
        Fallback: silueta Pillow si no existe la imagen.
        """
        from PIL import Image

        avatar_path = ASSETS_DIR / "avatar_base.png"
        if not avatar_path.exists():
            avatar_path = ASSETS_DIR / "avatar_face.png"

        if avatar_path.exists():
            try:
                avatar = Image.open(str(avatar_path)).convert("RGBA")
                target_h = int(h * 0.40) if small else int(h * 0.90)
                scale = target_h / max(avatar.height, 1)
                target_w = int(avatar.width * scale)
                avatar_resized = avatar.resize((target_w, target_h), Image.LANCZOS)
                x = 10 if small else int(w * 0.02)
                y = h - target_h - (10 if small else 0)
                frame_rgba = img.convert("RGBA")
                frame_rgba.paste(avatar_resized, (x, y), mask=avatar_resized.split()[3])
                return frame_rgba.convert("RGB")
            except Exception as e:
                self.logger.warning(f"Avatar placeholder fallo: {e}")

        return self._draw_avatar_fallback(img, w, h, small)

    def _draw_avatar_fallback(self, img, w: int, h: int, small: bool = False):
        """Silueta de presentador dibujada con Pillow como ultimo fallback."""
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)

        if small:
            ax0, ay0 = 10, h - int(h * 0.30) - 10
            ax1, ay1 = int(w * 0.22), h - 90
        else:
            ax0, ay0 = 3, 43
            ax1, ay1 = int(w * 0.55), h - 80

        aw = ax1 - ax0
        ah = ay1 - ay0

        for row in range(ah):
            ratio = row / max(ah - 1, 1)
            r = int(13 + 13 * ratio)
            g = int(13 + 13 * ratio)
            b = int(26 + 20 * ratio)
            draw.line([(ax0, ay0 + row), (ax1, ay0 + row)], fill=(r, g, b))

        draw.rectangle([(ax0, ay0), (ax1, ay1)], outline=C_ACCENT, width=2)

        cx = (ax0 + ax1) // 2
        head_r = max(35, min(75, int(aw * 0.12))) if not small else max(12, int(aw * 0.18))
        cy_head = ay0 + int(ah * 0.28)

        # Cuello
        nw = max(4, head_r // 3)
        nh = max(6, head_r // 2)
        draw.rectangle([(cx - nw, cy_head + head_r - 2), (cx + nw, cy_head + head_r + nh)], fill=(200, 168, 130))

        # Traje
        suit_top_y = cy_head + head_r + nh
        suit_bot_y = ay1 - 5
        stw = int(head_r * 1.6)
        sbw = int(head_r * 2.8)
        draw.polygon([(cx - stw, suit_top_y), (cx + stw, suit_top_y), (cx + sbw, suit_bot_y), (cx - sbw, suit_bot_y)], fill=(26, 26, 46))

        # Corbata naranja
        tw2 = max(3, head_r // 5)
        draw.polygon([
            (cx - tw2, suit_top_y + nh // 2), (cx + tw2, suit_top_y + nh // 2),
            (cx + tw2 + 2, suit_top_y + int((suit_bot_y - suit_top_y) * 0.55)),
            (cx, suit_top_y + int((suit_bot_y - suit_top_y) * 0.55) + tw2 * 2),
            (cx - tw2 - 2, suit_top_y + int((suit_bot_y - suit_top_y) * 0.55)),
        ], fill=C_ACCENT)

        # Cabeza
        draw.ellipse([(cx - head_r, cy_head - head_r), (cx + head_r, cy_head + head_r)], fill=(200, 168, 130))
        hh = max(3, head_r // 3)
        draw.ellipse([(cx - head_r, cy_head - head_r), (cx + head_r, cy_head - head_r + hh * 2)], fill=(30, 20, 10))

        return img

    def _build_subtitle_overlay(self, text: str, w: int, h_bar: int = 60):
        """Genera imagen PIL con subtitulo sobre fondo semitransparente."""
        from PIL import Image, ImageDraw, ImageFont

        bar = Image.new("RGBA", (w, h_bar), (0, 0, 0, 185))
        draw = ImageDraw.Draw(bar)

        font_sub = None
        for fp in ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
                   "C:/Windows/Fonts/verdana.ttf", "C:/Windows/Fonts/calibri.ttf"]:
            try:
                font_sub = ImageFont.truetype(fp, 26)
                break
            except Exception:
                continue
        if font_sub is None:
            font_sub = ImageFont.load_default()

        clean = self._clean_text_for_display(text)
        short_text = textwrap.shorten(clean, width=90, placeholder="...")
        draw.text((w // 2, h_bar // 2), short_text, fill=C_TEXT, font=font_sub, anchor="mm")

        return bar

    def _clean_text_for_display(self, text: str) -> str:
        """Elimina marcadores, markdown y emoticonos antes de renderizar."""
        text = re.sub(r'\[[A-Z_:]+[^\]]*\]', '', text)
        text = re.sub(r'##?\s+[A-ZÁÉÍÓÚ\s\(\)0-9\-]+\n?', '', text)
        text = re.sub(
            r'[^\x00-\x7FáéíóúñüàèìòùâêîôûäëïöüÁÉÍÓÚÑÜÀÈÌÒÙ.,;:!?¡¿\s\'\"\-0-9$%]',
            '', text
        )
        text = re.sub(r'\s{2,}', ' ', text)
        return text.strip()

    # ══════════════════════════════════════════════════════════════════════════
    # Helpers internos de run()
    # ══════════════════════════════════════════════════════════════════════════

    def _detect_format(self, ctx: Context) -> str:
        """Determina el formato de video segun ctx.video_format o ctx.mode."""
        vf = getattr(ctx, "video_format", None)
        if vf in (FORMAT_NOTICIARIO, FORMAT_ANALISIS, FORMAT_EDUCATIVO,
                  FORMAT_SHORT, FORMAT_FULLSCREEN):
            return vf
        mode_lower = (ctx.mode or "").lower().strip()
        return MODE_FORMAT_MAP.get(mode_lower, FORMAT_FULLSCREEN)

    def _extract_ticker_prices(self, ctx: Context) -> dict:
        """Extrae precios formateados de ctx (tiempo real via ARGOS) o ctx.prices dict."""
        defaults = {"BTC": "???,???", "ETH": "?,???", "SOL": "???"}
        # Mapa símbolo → campo directo en ctx y clave CoinGecko
        ctx_fields = {"BTC": "btc_price", "ETH": "eth_price", "SOL": "sol_price"}
        cg_keys    = {"BTC": "bitcoin",   "ETH": "ethereum",  "SOL": "solana"}

        result = {}
        for symbol, fallback in defaults.items():
            found = False

            # Prioridad 1: campo directo en ctx (escrito por ARGOS en tiempo real)
            _ctx_val = getattr(ctx, ctx_fields[symbol], 0) or 0
            if _ctx_val > 0:
                try:
                    result[symbol] = f"{float(_ctx_val):,.0f}"
                    found = True
                except (ValueError, TypeError):
                    pass

            # Prioridad 2: ctx.prices dict
            if not found and ctx.prices:
                data = ctx.prices.get(symbol) or ctx.prices.get(cg_keys[symbol], {})
                if isinstance(data, dict):
                    price = data.get("price") or data.get("usd")
                    if price is not None:
                        try:
                            result[symbol] = f"{float(price):,.0f}"
                            found = True
                        except (ValueError, TypeError):
                            pass
                elif isinstance(data, (int, float)) and data > 0:
                    try:
                        result[symbol] = f"{float(data):,.0f}"
                        found = True
                    except (ValueError, TypeError):
                        pass

            # Prioridad 3: self._prices (ya populado con DAEDALUS cache 5min)
            if not found:
                _cached_val = self._prices.get(cg_keys[symbol], 0) or 0
                if _cached_val > 0:
                    try:
                        result[symbol] = f"{float(_cached_val):,.0f}"
                        found = True
                    except (ValueError, TypeError):
                        pass

            if not found:
                result[symbol] = fallback
        return result

    def _get_chart_path(self, ctx: Context) -> Optional[str]:
        """Devuelve la ruta al grafico. Usa ctx.chart_path si existe, si no llama a DAEDALUS."""
        if ctx.chart_path and Path(ctx.chart_path).exists():
            return ctx.chart_path

        if self._daedalus_cls is None:
            self.logger.warning("DAEDALUS no disponible — sin grafico de precio")
            return None

        try:
            daedalus = self._daedalus_cls(self.config)
            ctx = daedalus.run(ctx)
            if ctx.chart_path and Path(ctx.chart_path).exists():
                return ctx.chart_path
        except Exception as e:
            self.logger.warning(f"DAEDALUS fallo: {e}")

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # API publica de compatibilidad (para tests y llamadas externas)
    # ══════════════════════════════════════════════════════════════════════════

    def generate_avatar_clip(self, audio_path: str, avatar_img: str = None):
        """
        Devuelve MoviePy VideoClip del avatar.
        Intenta LatentSync, luego Ken Burns sobre la imagen.
        """
        # Intento LatentSync
        latsync_result = None
        try:
            out = str(OUTPUT_LATSYNC_DIR / "api_compat_latsync.mp4")
            latsync_result = self._run_latsync(audio_path, out)
        except Exception as e:
            self.logger.warning(f"generate_avatar_clip/LatentSync: {e}")

        if latsync_result and Path(latsync_result).exists():
            try:
                from moviepy.editor import VideoFileClip
                return VideoFileClip(latsync_result)
            except Exception as e:
                self.logger.warning(f"generate_avatar_clip: no pudo abrir MP4 LatentSync: {e}")

        # Ken Burns fallback
        _img = avatar_img or _AVATAR_IMG
        try:
            from moviepy.editor import AudioFileClip, ImageClip
            audio = AudioFileClip(audio_path)
            clip = ImageClip(_img).set_duration(audio.duration)
            audio.close()
            return clip
        except Exception as e:
            self.logger.error(f"generate_avatar_clip fallback fallo: {e}")
            return None
