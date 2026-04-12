from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/forge/iris.py
IRIS — Diseñadora de Thumbnails A/B de NEXUS.

Crea siempre dos versiones del thumbnail con Pillow.
Version A: dato numerico impactante, grafico 40% derecho, avatar inferior izq.
Version B: pregunta + flecha apuntando al grafico.
Dimensiones: 1280x720 (YouTube).
"""

import os
import re
import traceback
from pathlib import Path
from typing import Optional, Tuple

from rich.console import Console
from rich.panel import Panel

from core.base_agent import BaseAgent
from core.context import Context

console = Console()

OUTPUT_THUMBNAILS_DIR = Path(__file__).resolve().parents[2] / "output" / "thumbnails"

# Paleta de marca
COLOR_BG       = (10, 10, 10)
COLOR_ACCENT   = (247, 147, 26)
COLOR_WHITE    = (255, 255, 255)
COLOR_GRAY     = (136, 136, 136)
COLOR_GREEN    = (76, 175, 80)
COLOR_RED      = (244, 67, 54)

THUMB_W = 1280
THUMB_H = 720

# Fuentes prioritarias (Windows + Linux)
_FONTS_IMPACT = [
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/Impact.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
]
_FONTS_BOLD = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONTS_REGULAR = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans.ttf",
]


# ── Helpers de modulo ────────────────────────────────────────────────────────

def _load_font(candidates: list, size: int):
    """Intenta cargar fuente de una lista de rutas candidatas."""
    from PIL import ImageFont
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _extract_number(ctx: Context) -> str:
    """Extrae el numero mas impactante del contexto (cambio 24h o mencion en titulo/script)."""
    # 1. Cambio de precio BTC (más relevante)
    prices = getattr(ctx, 'prices', {}) or {}
    btc_data = prices.get('BTC', prices.get('bitcoin', {}))
    if isinstance(btc_data, dict):
        chg = btc_data.get('change_24h', 0)
        if chg:
            return f"{chg:+.0f}%"

    # 2. Cualquier otra moneda con cambio
    for coin_data in prices.values():
        if isinstance(coin_data, dict):
            chg = coin_data.get('change_24h', 0)
            if chg:
                return f"{chg:+.0f}%"

    # 3. Número en el título del video actual
    title = getattr(ctx, 'seo_title', '') or getattr(ctx, 'topic', '') or ''
    match = re.search(r'(\d[\d,.]*\s*[kK%]?)', title)
    if match:
        return match.group(1).strip().replace(',', '.')

    # 4. Número en el script (primera mención de precio o porcentaje)
    script = getattr(ctx, 'script', '') or ''
    if script:
        pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', script)
        if pct_match:
            return f"{float(pct_match.group(1)):+.1f}%".replace('+', '')
        price_match = re.search(r'\$(\d[\d,.]+)', script)
        if price_match:
            return '$' + price_match.group(1)[:8]

    return "HOY"


def _generate_question(ctx_or_title) -> str:
    """Genera una pregunta corta en dos o tres lineas para la version B.
    Acepta un Context o un string (titulo).
    Usa el titulo Y el topic para generar una pregunta mas relevante.
    """
    if hasattr(ctx_or_title, 'seo_title'):
        ctx = ctx_or_title
        title = (getattr(ctx, 'seo_title', '') or getattr(ctx, 'topic', '') or '').lower()
    else:
        title = (ctx_or_title or '').lower()

    if any(w in title for w in ('cae', 'baja', 'crash', 'desplome', 'colapso', 'caída')):
        return "HASTA\nDONDE\nCAE?"
    if any(w in title for w in ('sube', 'ath', 'bull', 'rompe', 'rally', 'máximo', 'record')):
        return "HASTA\nDONDE\nSUBE?"
    if any(w in title for w in ('halving', 'halvening')):
        return "QUE\nCAMBIA\nCON EL HALVING?"
    if any(w in title for w in ('etf', 'institucional', 'blackrock', 'fidelity')):
        return "QUE SIGNIFICA\nPARA EL\nMERCADO?"
    if any(w in title for w in ('regulación', 'sec', 'gobierno', 'ley', 'ban', 'prohib')):
        return "COMO NOS\nAFECTA\nA NOSOTROS?"
    if any(w in title for w in ('predicción', 'precio', 'objetivo', 'análisis')):
        return "A DONDE\nVA EL\nPRECIO?"
    if 'bitcoin' in title or 'btc' in title:
        return "QUE PASA\nCON\nBITCOIN?"
    if 'ethereum' in title or 'eth' in title:
        return "QUE PASA\nCON\nETHEREUM?"
    if 'solana' in title or 'sol' in title:
        return "QUE PASA\nCON\nSOLANA?"
    return "QUE\nPASA\nAHORA?"


def _draw_impact_text(draw, text: str, x: int, y: int, max_width: int,
                      color_main=(255, 255, 255), shadow_color=(247, 147, 26),
                      size_range=(90, 75, 60, 48, 36)):
    """
    Dibuja texto con sombra naranja y autoajuste de tamano.
    Acepta '\\n' para forzar saltos de linea.
    """
    from PIL import ImageFont

    lines = text.split('\n') if '\n' in text else [text]

    font = None
    chosen_size = size_range[-1]
    for size in size_range:
        candidate = _load_font(_FONTS_IMPACT, size)
        if candidate is None:
            candidate = _load_font(_FONTS_BOLD, size)
        if candidate is None:
            continue
        try:
            max_line_w = max(
                candidate.getlength(l) if hasattr(candidate, 'getlength')
                else len(l) * size // 2
                for l in lines
            )
        except Exception:
            max_line_w = max_width + 1
        if max_line_w <= max_width:
            font = candidate
            chosen_size = size
            break

    if font is None:
        font = _load_font(_FONTS_BOLD, size_range[-1])

    line_h = int(chosen_size * 1.15)
    for i, line in enumerate(lines):
        ly = y + i * line_h
        # Sombra (offset 3px)
        draw.text((x + 3, ly + 3), line, fill=shadow_color, font=font)
        # Texto principal
        draw.text((x, ly), line, fill=color_main, font=font)


def _fit_text(text: str, max_chars_per_line: int = 14) -> str:
    """Divide texto en lineas respetando max_chars_per_line."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 <= max_chars_per_line:
            current = (current + " " + word).strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines[:3])


class IRIS(BaseAgent):
    """
    Diseñadora de thumbnails A/B para CryptoVerdad.

    Version A: dato numerico impactante, grafico 40% lado derecho, avatar inferior izq.
    Version B: pregunta corta + flecha hacia el grafico.

    Guarda en:
        ctx.thumbnail_a_path = output/thumbnails/{pipeline_id}_A.png
        ctx.thumbnail_b_path = output/thumbnails/{pipeline_id}_B.png
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config)
        self.db = db
        OUTPUT_THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Font helper de instancia (compatibilidad con generate_channel_assets) ─

    def _get_font(self, size: int, bold: bool = False):
        """Devuelve una fuente del sistema con fallback a Pillow default."""
        candidates = _FONTS_BOLD if bold else _FONTS_REGULAR
        return _load_font(candidates, size)

    # ── Helpers de datos ──────────────────────────────────────────────────────

    def _get_btc_price(self, ctx: Context) -> Optional[str]:
        """Extrae precio de BTC formateado o None."""
        btc = ctx.prices.get("BTC", {}) if ctx.prices else {}
        if isinstance(btc, dict) and btc.get("price"):
            price = btc["price"]
            change = btc.get("change_24h", 0)
            sign = "+" if change >= 0 else ""
            return f"${price:,.0f}  {sign}{change:.1f}%"
        return None

    def _change_color(self, ctx: Context) -> Tuple[int, int, int]:
        """Devuelve verde o rojo segun variacion de BTC."""
        btc = (ctx.prices or {}).get("BTC", {})
        if isinstance(btc, dict):
            return COLOR_GREEN if btc.get("change_24h", 0) >= 0 else COLOR_RED
        return COLOR_ACCENT

    # ── Render del grafico en el lienzo ───────────────────────────────────────

    def _paste_chart(self, img, draw, ctx: Context,
                     chart_x: int, chart_y: int,
                     chart_w: int, chart_h: int):
        """
        Incrusta ctx.chart_path en el area indicada.
        Solo usa el chart si pertenece al pipeline actual (pipeline_id en el nombre).
        Si no existe o es de otro pipeline, dibuja un placeholder oscuro con borde naranja.
        Devuelve (img, draw) actualizados (necesario si se uso RGBA).
        """
        from PIL import Image

        chart_path = getattr(ctx, 'chart_path', None)
        pipeline_id = getattr(ctx, 'pipeline_id', '')

        # Rechazar charts de pipelines anteriores
        if chart_path and pipeline_id:
            chart_name = Path(chart_path).stem
            if pipeline_id[:8] not in chart_name and pipeline_id not in chart_name:
                self.logger.warning(
                    f"IRIS: chart_path '{Path(chart_path).name}' no pertenece al pipeline "
                    f"'{pipeline_id[:8]}' — ignorando para evitar frame antiguo"
                )
                chart_path = None

        if chart_path and Path(chart_path).exists():
            try:
                chart_img = Image.open(chart_path).convert("RGB")
                chart_img = chart_img.resize((chart_w, chart_h), Image.LANCZOS)
                img.paste(chart_img, (chart_x, chart_y))
            except Exception as err:
                self.logger.warning(f"No se pudo incrustar grafico: {err}")
                draw.rectangle(
                    [(chart_x, chart_y), (chart_x + chart_w, chart_y + chart_h)],
                    fill=(13, 17, 23)
                )
        else:
            draw.rectangle(
                [(chart_x, chart_y), (chart_x + chart_w, chart_y + chart_h)],
                fill=(13, 17, 23)
            )

        # Borde naranja
        draw.rectangle(
            [(chart_x - 2, chart_y - 2),
             (chart_x + chart_w + 2, chart_y + chart_h + 2)],
            outline=COLOR_ACCENT, width=3
        )
        return img, draw

    # ── Frame del vídeo ──────────────────────────────────────────────────────

    def _extract_video_frame(self, ctx: Context, second: float = 5.0):
        """Extrae un frame del segundo indicado del vídeo generado.
        Devuelve PIL Image 1280x720 o None si no hay vídeo disponible.
        """
        from PIL import Image

        video_path = getattr(ctx, "video_path", None)
        if not video_path or not Path(video_path).exists():
            return None
        try:
            from moviepy.editor import VideoFileClip
            clip = VideoFileClip(video_path)
            t = min(second, clip.duration - 0.1)
            frame = clip.get_frame(t)  # numpy RGB array
            clip.close()
            img = Image.fromarray(frame).resize((THUMB_W, THUMB_H), Image.LANCZOS)
            return img
        except Exception as err:
            self.logger.warning(f"No se pudo extraer frame del vídeo: {err}")
            return None

    # ── Overlay oscuro ────────────────────────────────────────────────────────

    @staticmethod
    def _apply_dark_overlay(img, alpha: int = 140):
        """Aplica overlay negro semitransparente sobre la imagen.
        Usa paste con máscara en lugar de alpha_composite para
        compatibilidad con entornos Railway sin soporte RGBA completo.
        """
        from PIL import Image
        # Trabajar siempre en RGB — evita problemas RGBA en Railway/Debian
        base = img.convert("RGB")
        overlay = Image.new("RGB", base.size, (0, 0, 0))
        mask = Image.new("L", base.size, alpha)
        base.paste(overlay, mask=mask)
        return base

    # ── Logo CryptoVerdad (arriba izquierda) ──────────────────────────────────

    def _draw_logo_topleft(self, draw):
        """Dibuja 'CryptoVerdad' en esquina superior izquierda."""
        font_logo = _load_font(_FONTS_BOLD, 24)
        # Sombra
        draw.text((22, 22), "CryptoVerdad", fill=(0, 0, 0), font=font_logo)
        draw.text((20, 20), "CryptoVerdad", fill=COLOR_ACCENT, font=font_logo)

    # ── Precio BTC en naranja ─────────────────────────────────────────────────

    def _draw_btc_price(self, draw, ctx: Context, x: int, y: int):
        """Dibuja precio BTC en naranja."""
        btc_str = self._get_btc_price(ctx)
        if not btc_str:
            return
        font_price = _load_font(_FONTS_BOLD, 36)
        draw.text((x + 2, y + 2), f"BTC {btc_str}", fill=(0, 0, 0), font=font_price)
        draw.text((x, y), f"BTC {btc_str}", fill=COLOR_ACCENT, font=font_price)

    # ── Fondo con degradado ───────────────────────────────────────────────────

    @staticmethod
    def _draw_gradient_bg(draw, w: int, h: int):
        """Gradiente diagonal #0A0A0A -> #1A1A2E."""
        for y in range(h):
            r = int(10 + (26 - 10) * y / h)
            g = int(10 + (26 - 10) * y / h)
            b = int(10 + (46 - 10) * y / h)
            draw.line([(0, y), (w, y)], fill=(r, g, b))

    # ── Logo CryptoVerdad ─────────────────────────────────────────────────────

    def _draw_logo(self, draw, w: int):
        """Dibuja 'CryptoVerdad' en esquina superior derecha."""
        font_logo = _load_font(_FONTS_BOLD, 22)
        draw.text((w - 250, 20), "CryptoVerdad", fill=COLOR_ACCENT, font=font_logo)

    # ── Version A ─────────────────────────────────────────────────────────────

    def _generate_version_a(self, ctx: Context) -> "Image":
        """
        Version A — Frame del vídeo + overlay oscuro + texto ABAJO IZQUIERDA.
        Logo CryptoVerdad arriba izquierda. Precio BTC naranja arriba derecha.
        SIN avatar cartoon.
        """
        from PIL import Image, ImageDraw

        # Base: frame del vídeo o fondo degradado como fallback
        img = self._extract_video_frame(ctx, second=5.0)
        if img is None:
            img = Image.new("RGB", (THUMB_W, THUMB_H), COLOR_BG)
            draw = ImageDraw.Draw(img)
            self._draw_gradient_bg(draw, THUMB_W, THUMB_H)

        # Overlay oscuro semitransparente
        img = self._apply_dark_overlay(img, alpha=150)
        draw = ImageDraw.Draw(img)

        # Logo CryptoVerdad arriba izquierda
        self._draw_logo_topleft(draw)

        # Precio BTC arriba derecha
        self._draw_btc_price(draw, ctx, x=THUMB_W - 420, y=18)

        # Título en blanco bold grande — ABAJO IZQUIERDA
        title = (getattr(ctx, 'seo_title', None)
                 or getattr(ctx, 'topic', None)
                 or 'Bitcoin')
        title_text = _fit_text(title.upper(), max_chars_per_line=18)

        _draw_impact_text(
            draw, title_text,
            x=40, y=THUMB_H - 240,
            max_width=int(THUMB_W * 0.90),
            color_main=COLOR_WHITE,
            shadow_color=COLOR_ACCENT,
            size_range=(72, 60, 50, 42, 34),
        )

        # Barra naranja en borde inferior
        draw.rectangle([(0, THUMB_H - 8), (THUMB_W, THUMB_H)], fill=COLOR_ACCENT)

        return img

    # ── Version B ─────────────────────────────────────────────────────────────

    def _generate_version_b(self, ctx: Context) -> "Image":
        """
        Version B — Frame del vídeo + overlay oscuro + texto CENTRADO.
        Logo CryptoVerdad arriba izquierda. Precio BTC naranja arriba derecha.
        SIN avatar cartoon.
        """
        from PIL import Image, ImageDraw

        # Base: frame del vídeo o fondo degradado como fallback
        img = self._extract_video_frame(ctx, second=5.0)
        if img is None:
            img = Image.new("RGB", (THUMB_W, THUMB_H), COLOR_BG)
            draw = ImageDraw.Draw(img)
            self._draw_gradient_bg(draw, THUMB_W, THUMB_H)

        # Overlay más oscuro en el centro para que el texto destaque
        img = self._apply_dark_overlay(img, alpha=165)
        draw = ImageDraw.Draw(img)

        # Logo CryptoVerdad arriba izquierda
        self._draw_logo_topleft(draw)

        # Precio BTC arriba derecha
        self._draw_btc_price(draw, ctx, x=THUMB_W - 420, y=18)

        # Pregunta centrada en grandes
        question = _generate_question(ctx)
        lines = question.split("\n")
        font_q = (_load_font(_FONTS_IMPACT, 110) or _load_font(_FONTS_BOLD, 90))
        line_h = 120
        total_h = len(lines) * line_h
        start_y = (THUMB_H - total_h) // 2 - 20

        for i, line in enumerate(lines):
            y = start_y + i * line_h
            # Sombra
            try:
                lw = font_q.getlength(line) if hasattr(font_q, "getlength") else len(line) * 55
            except Exception:
                lw = len(line) * 55
            x = (THUMB_W - lw) // 2
            draw.text((x + 3, y + 3), line, fill=COLOR_ACCENT, font=font_q)
            draw.text((x, y), line, fill=COLOR_WHITE, font=font_q)

        # Barra naranja en borde inferior
        draw.rectangle([(0, THUMB_H - 8), (THUMB_W, THUMB_H)], fill=COLOR_ACCENT)

        return img

    # ── run() ─────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("IRIS iniciada")
        console.print(
            Panel(
                "[bold #F7931A]IRIS[/] — Diseñadora de Thumbnails A/B\n"
                f"Tema: [italic]{ctx.topic}[/] · 1280x720",
                border_style="#F7931A",
            )
        )

        try:
            path_a = str(OUTPUT_THUMBNAILS_DIR / f"{ctx.pipeline_id}_A.png")
            path_b = str(OUTPUT_THUMBNAILS_DIR / f"{ctx.pipeline_id}_B.png")

            console.print("[dim]Generando thumbnail Version A (dato numerico)...[/]")
            ctx.thumbnail_a_path = self._save_thumbnail(ctx, "A", path_a)

            console.print("[dim]Generando thumbnail Version B (pregunta + flecha)...[/]")
            ctx.thumbnail_b_path = self._save_thumbnail(ctx, "B", path_b)

            console.print(
                f"[green]Thumbnails generados:[/]\n"
                f"  A: {path_a}\n"
                f"  B: {path_b}"
            )
            self.logger.info(
                f"Thumbnails A/B guardados para pipeline {ctx.pipeline_id[:8]}"
            )

        except Exception as e:
            self.logger.error(f"Error en IRIS: {e}")
            ctx.add_error("IRIS", str(e))

        return ctx

    def _save_thumbnail(self, ctx: Context, variant: str, output_path: str) -> str:
        """Genera y guarda un thumbnail. Devuelve la ruta o '' en caso de error."""
        try:
            if variant == "A":
                img = self._generate_version_a(ctx)
            else:
                img = self._generate_version_b(ctx)
            img.save(output_path, "PNG")
            return output_path
        except Exception as e:
            self.logger.error(f"Error creando thumbnail {variant}: {e}")
            self.logger.error(f"IRIS traceback:\n{traceback.format_exc()}")
            ctx.add_error("IRIS", f"Thumbnail {variant}: {e}")
            return ""

    # ── Channel assets ────────────────────────────────────────────────────────

    def generate_channel_assets(self, output_dir: str = "output/thumbnails") -> dict:
        """
        Genera los assets visuales del canal CryptoVerdad:
          - banner_youtube.png  (2560x1440) — banner del canal
          - profile_icon.png    (800x800)   — icono de perfil circular
          - watermark.png       (300x80)    — marca de agua transparente

        No requiere Context ni API keys. Usa solo Pillow.
        Devuelve dict con las rutas generadas.
        """
        from PIL import Image, ImageDraw

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = {}

        # ── 1. Banner YouTube 2560x1440 ───────────────────────────────────────
        try:
            W, H = 2560, 1440
            banner = Image.new("RGB", (W, H), COLOR_BG)
            draw = ImageDraw.Draw(banner)

            # Degradado lateral sutil
            for x in range(W // 2):
                alpha = int(30 * (x / (W // 2)))
                draw.line([(x, 0), (x, H)], fill=(alpha, alpha, alpha))

            draw.rectangle([(0, 0), (18, H)], fill=COLOR_ACCENT)
            draw.rectangle([(0, H - 12), (W, H)], fill=COLOR_ACCENT)

            font_title = self._get_font(180, bold=True)
            draw.text((120, 480), "CryptoVerdad", font=font_title, fill=COLOR_WHITE)

            font_tag = self._get_font(72)
            draw.text(
                (124, 700),
                "Crypto sin humo. Analisis real, opinion directa.",
                font=font_tag, fill=COLOR_GRAY,
            )

            font_handle = self._get_font(56)
            draw.text((124, 820), "@CryptoVerdad", font=font_handle, fill=COLOR_ACCENT)

            # Circulos decorativos
            for cx, cy, r, col in [
                (2100, 500, 300, (*COLOR_ACCENT, 30)),
                (2300, 300, 180, (*COLOR_ACCENT, 18)),
                (1900, 800, 200, (255, 255, 255, 12)),
            ]:
                overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                od = ImageDraw.Draw(overlay)
                od.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=col)
                banner = Image.alpha_composite(banner.convert("RGBA"), overlay).convert("RGB")
                draw = ImageDraw.Draw(banner)

            path_banner = str(out / "banner_youtube.png")
            banner.save(path_banner, "PNG", optimize=True)
            paths["banner"] = path_banner
            console.print(f"  [green]Banner[/]   -> {path_banner}")
        except Exception as e:
            self.logger.error(f"Error generando banner: {e}")
            paths["banner"] = f"ERROR: {e}"

        # ── 2. Profile icon 800x800 ───────────────────────────────────────────
        try:
            S = 800
            icon = Image.new("RGB", (S, S), COLOR_BG)
            draw = ImageDraw.Draw(icon)

            margin = 40
            draw.ellipse([(margin, margin), (S - margin, S - margin)], fill=COLOR_ACCENT)

            font_icon = self._get_font(420, bold=True)
            draw.text((S // 2, S // 2), "C", font=font_icon, fill=COLOR_BG, anchor="mm")

            draw.ellipse(
                [(margin, margin), (S - margin, S - margin)],
                outline=COLOR_WHITE, width=6,
            )

            path_icon = str(out / "profile_icon.png")
            icon.save(path_icon, "PNG", optimize=True)
            paths["icon"] = path_icon
            console.print(f"  [green]Icono[/]    -> {path_icon}")
        except Exception as e:
            self.logger.error(f"Error generando icono: {e}")
            paths["icon"] = f"ERROR: {e}"

        # ── 3. Watermark 300x80 (fondo transparente) ──────────────────────────
        try:
            wm = Image.new("RGBA", (300, 80), (0, 0, 0, 0))
            draw = ImageDraw.Draw(wm)
            font_wm = self._get_font(32, bold=True)
            draw.text((3, 23), "CryptoVerdad", font=font_wm, fill=(0, 0, 0, 140))
            draw.text((2, 22), "CryptoVerdad", font=font_wm, fill=(*COLOR_ACCENT, 210))

            path_wm = str(out / "watermark.png")
            wm.save(path_wm, "PNG")
            paths["watermark"] = path_wm
            console.print(f"  [green]Marca[/]    -> {path_wm}")
        except Exception as e:
            self.logger.error(f"Error generando watermark: {e}")
            paths["watermark"] = f"ERROR: {e}"

        # ── Resumen ───────────────────────────────────────────────────────────
        ok = sum(1 for v in paths.values() if not str(v).startswith("ERROR"))
        console.print(Panel(
            f"[bold #F7931A]Canal CryptoVerdad - {ok}/{len(paths)} assets generados[/]\n"
            + "\n".join(f"  {k}: {v}" for k, v in paths.items()),
            title="[bold]IRIS | Channel Assets[/bold]",
            border_style="#F7931A",
        ))
        return paths


# Alias para compatibilidad con llamadas lowercase
Iris = IRIS
