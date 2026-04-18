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
    def _draw_gradient_bg(draw, w: int, h: int, accent: tuple = None):
        """
        Gradiente vertical adaptado al sentimiento del mercado.
        Bajista → rojo oscuro a negro. Alcista → naranja oscuro a negro. Neutro → azul oscuro.
        """
        if accent is None:
            accent = COLOR_ACCENT  # naranja por defecto
        r0, g0, b0 = accent
        for y in range(h):
            t = y / h  # 0 arriba, 1 abajo
            # Degradar hacia negro conforme bajamos
            r = int(r0 * (1 - t) * 0.30)
            g = int(g0 * (1 - t) * 0.15)
            b = int(b0 * (1 - t) * 0.10 + 10)
            draw.line([(0, y), (w, y)], fill=(max(5, r), max(5, g), max(5, b)))

    # ── Logo CryptoVerdad ─────────────────────────────────────────────────────

    def _draw_logo(self, draw, w: int):
        """Dibuja 'CryptoVerdad' en esquina superior derecha."""
        font_logo = _load_font(_FONTS_BOLD, 22)
        draw.text((w - 250, 20), "CryptoVerdad", fill=COLOR_ACCENT, font=font_logo)

    # ── Helpers de sentimiento ────────────────────────────────────────────────

    @staticmethod
    def _detect_sentiment(ctx: Context) -> tuple:
        """
        Detecta el sentimiento de mercado a partir del contexto.
        Retorna (change_24h: float, fear_greed: int, accent_color: tuple, show_alert: bool)
        """
        change_24h = (
            getattr(ctx, "btc_24h_change", 0)
            or (ctx.prices or {}).get("BTC", {}).get("change_24h", 0)
            or 0
        )
        fear_greed = getattr(ctx, "fear_greed_value", 50) or 50

        # Color de acento: rojo en mercado bajista fuerte, naranja en el resto
        accent_rgb = COLOR_RED if change_24h < -3 else COLOR_ACCENT
        # Alerta de mercado: miedo extremo o caída muy fuerte
        show_alert = fear_greed < 25 or change_24h < -7

        return float(change_24h), int(fear_greed), accent_rgb, bool(show_alert)

    @staticmethod
    def _draw_alert_band(draw, w: int) -> None:
        """Dibuja banda roja de 40px en la parte superior con texto de alerta."""
        draw.rectangle([(0, 0), (w, 40)], fill=COLOR_RED)
        font_alert = _load_font(_FONTS_BOLD, 22)
        draw.text(
            (w // 2, 20),
            "ALERTA DE MERCADO",
            fill=COLOR_WHITE,
            font=font_alert,
            anchor="mm",
        )

    # ── Helpers de diseño profesional ────────────────────────────────────────

    @staticmethod
    def _draw_centered_text(draw, text: str, y: int, font, color, shadow_color=None,
                             shadow_offset: int = 3):
        """Dibuja texto centrado horizontalmente con sombra opcional."""
        try:
            lw = font.getlength(text) if hasattr(font, "getlength") else len(text) * (font.size if hasattr(font, 'size') else 40)
        except Exception:
            lw = len(text) * 40
        x = max(0, (THUMB_W - int(lw)) // 2)
        if shadow_color:
            draw.text((x + shadow_offset, y + shadow_offset), text, fill=shadow_color, font=font)
        draw.text((x, y), text, fill=color, font=font)

    def _draw_stat_panel(self, draw, ctx: Context, x: int, y: int, w: int, h: int,
                          accent_rgb: tuple, change_24h: float) -> None:
        """
        Dibuja el panel izquierdo de la versión A: flecha + % + precio + marca.
        Es el elemento dominante del thumbnail — jerarquía visual clara.
        """
        # Fondo del panel levemente diferenciado
        draw.rectangle([(x, y), (x + w, y + h)], fill=(0, 0, 0))

        cx = x + w // 2
        panel_top = y + 30

        # ── Flecha direccional (el elemento más grande) ───────────────────
        arrow = "▼" if change_24h < 0 else "▲"
        arrow_color = COLOR_RED if change_24h < 0 else COLOR_GREEN
        font_arrow = _load_font(_FONTS_IMPACT, 130) or _load_font(_FONTS_BOLD, 110)
        if font_arrow:
            try:
                aw = font_arrow.getlength(arrow) if hasattr(font_arrow, "getlength") else 110
            except Exception:
                aw = 110
            draw.text((cx - aw // 2 + 3, panel_top + 3), arrow, fill=(0, 0, 0), font=font_arrow)
            draw.text((cx - aw // 2, panel_top), arrow, fill=arrow_color, font=font_arrow)

        # ── Porcentaje de cambio ──────────────────────────────────────────
        pct_str = f"{change_24h:+.1f}%" if change_24h != 0 else "AHORA"
        font_pct = _load_font(_FONTS_IMPACT, 90) or _load_font(_FONTS_BOLD, 75)
        if font_pct:
            try:
                pw = font_pct.getlength(pct_str) if hasattr(font_pct, "getlength") else len(pct_str) * 45
            except Exception:
                pw = len(pct_str) * 45
            px = cx - int(pw) // 2
            py = panel_top + 145
            draw.text((px + 3, py + 3), pct_str, fill=(0, 0, 0), font=font_pct)
            draw.text((px, py), pct_str, fill=arrow_color, font=font_pct)

        # ── Precio BTC ────────────────────────────────────────────────────
        btc = (ctx.prices or {}).get("BTC", {})
        if isinstance(btc, dict) and btc.get("price"):
            price_str = f"${btc['price']:,.0f}"
            font_price = _load_font(_FONTS_BOLD, 46) or _load_font(_FONTS_REGULAR, 40)
            if font_price:
                try:
                    prw = font_price.getlength(price_str) if hasattr(font_price, "getlength") else len(price_str) * 22
                except Exception:
                    prw = len(price_str) * 22
                prx = cx - int(prw) // 2
                pry = panel_top + 255
                draw.text((prx + 2, pry + 2), price_str, fill=(0, 0, 0), font=font_price)
                draw.text((prx, pry), price_str, fill=accent_rgb, font=font_price)

        # ── Separador horizontal ──────────────────────────────────────────
        sep_y = panel_top + 315
        draw.rectangle([(x + 20, sep_y), (x + w - 20, sep_y + 3)], fill=accent_rgb)

        # ── Branding: CryptoVerdad ────────────────────────────────────────
        font_brand = _load_font(_FONTS_BOLD, 28)
        if font_brand:
            brand_y = sep_y + 15
            try:
                brw = font_brand.getlength("CryptoVerdad") if hasattr(font_brand, "getlength") else 220
            except Exception:
                brw = 220
            draw.text((cx - int(brw) // 2 + 2, brand_y + 2), "CryptoVerdad",
                      fill=(0, 0, 0), font=font_brand)
            draw.text((cx - int(brw) // 2, brand_y), "CryptoVerdad",
                      fill=accent_rgb, font=font_brand)

    def _draw_fear_greed_badge(self, draw, fear_greed: int, x: int, y: int) -> None:
        """Mini badge circular con el valor de Fear & Greed."""
        # Color según zona
        if fear_greed <= 25:
            fg_color = COLOR_RED
        elif fear_greed <= 45:
            fg_color = (255, 140, 0)   # naranja oscuro
        elif fear_greed <= 55:
            fg_color = COLOR_GRAY
        elif fear_greed <= 75:
            fg_color = (100, 200, 100)  # verde claro
        else:
            fg_color = COLOR_GREEN

        # Fondo del badge
        r = 38
        draw.ellipse([(x - r, y - r), (x + r, y + r)], fill=(20, 20, 20))
        draw.ellipse([(x - r, y - r), (x + r, y + r)], outline=fg_color, width=3)

        # Número
        font_fg = _load_font(_FONTS_BOLD, 26)
        if font_fg:
            s = str(fear_greed)
            try:
                sw = font_fg.getlength(s) if hasattr(font_fg, "getlength") else len(s) * 14
            except Exception:
                sw = len(s) * 14
            draw.text((x - int(sw) // 2, y - 18), s, fill=fg_color, font=font_fg)

        # Label "F&G"
        font_lbl = _load_font(_FONTS_REGULAR, 16)
        if font_lbl:
            draw.text((x - 14, y + 10), "F&G", fill=COLOR_GRAY, font=font_lbl)

    # ── Version A — Layout dividido: STAT + CHART ─────────────────────────────

    def _generate_version_a(self, ctx: Context) -> "Image":
        """
        Version A — Layout dividido 50/50.
        Lado izquierdo: panel negro con flecha + % + precio (jerarquía visual clara).
        Lado derecho: gráfico BTC o fondo degradado con Fear & Greed badge.

        Principio: el primer golpe de vista del espectador debe ser el %.
        El segundo, el precio. El tercero, el gráfico.
        """
        from PIL import Image, ImageDraw

        change_24h, fear_greed, accent_rgb, show_alert = self._detect_sentiment(ctx)

        # ── Base: fondo degradado adaptado al sentimiento ─────────────────
        img = Image.new("RGB", (THUMB_W, THUMB_H), (5, 5, 5))
        draw = ImageDraw.Draw(img)
        self._draw_gradient_bg(draw, THUMB_W, THUMB_H, accent=accent_rgb)

        # ── Panel izquierdo (50%): stat dominante ─────────────────────────
        panel_w = THUMB_W // 2
        # Fondo negro semisólido para contraste máximo
        panel_bg = Image.new("RGB", (panel_w, THUMB_H), (4, 4, 4))
        img.paste(panel_bg, (0, 0))
        draw = ImageDraw.Draw(img)

        # Borde vertical derecho del panel (accent color)
        draw.rectangle([(panel_w - 4, 0), (panel_w, THUMB_H)], fill=accent_rgb)

        # Dibujar stat (flecha + % + precio + marca)
        self._draw_stat_panel(draw, ctx, x=0, y=0, w=panel_w, h=THUMB_H,
                               accent_rgb=accent_rgb, change_24h=change_24h)

        # ── Panel derecho (50%): gráfico o degradado ──────────────────────
        chart_placed = False
        chart_path = getattr(ctx, 'chart_path', None)
        pipeline_id = getattr(ctx, 'pipeline_id', '')
        if chart_path and pipeline_id and Path(chart_path).exists():
            try:
                chart_img = Image.open(chart_path).convert("RGB")
                chart_img = chart_img.resize((panel_w, THUMB_H), Image.LANCZOS)
                # Overlay oscuro sobre el gráfico para que el texto sea legible
                overlay = Image.new("RGB", (panel_w, THUMB_H), (0, 0, 0))
                mask = Image.new("L", (panel_w, THUMB_H), 70)
                chart_img.paste(overlay, mask=mask)
                img.paste(chart_img, (panel_w, 0))
                chart_placed = True
            except Exception as e:
                self.logger.debug(f"Chart panel derecho falló: {e}")

        if not chart_placed:
            # Fondo degradado más oscuro en el panel derecho
            draw.rectangle([(panel_w, 0), (THUMB_W, THUMB_H)], fill=(8, 8, 10))

        draw = ImageDraw.Draw(img)

        # ── Fear & Greed badge (esquina superior derecha) ─────────────────
        if fear_greed and fear_greed != 50:
            try:
                self._draw_fear_greed_badge(draw, fear_greed,
                                            x=THUMB_W - 55, y=55)
            except Exception:
                pass

        # ── Título del vídeo (panel derecho, parte inferior) ─────────────
        title = (getattr(ctx, 'seo_title', None)
                 or getattr(ctx, 'topic', None) or 'Bitcoin Análisis')
        title_text = _fit_text(title.upper(), max_chars_per_line=16)
        _draw_impact_text(
            draw, title_text,
            x=panel_w + 20, y=THUMB_H - 200,
            max_width=panel_w - 30,
            color_main=COLOR_WHITE,
            shadow_color=accent_rgb,
            size_range=(52, 44, 36, 28),
        )

        # ── Banda de alerta roja arriba (solo si mercado extremo) ─────────
        if show_alert:
            draw.rectangle([(0, 0), (THUMB_W, 36)], fill=COLOR_RED)
            font_alert = _load_font(_FONTS_BOLD, 20)
            if font_alert:
                draw.text((THUMB_W // 2, 18), "⚠ ALERTA DE MERCADO ⚠",
                          fill=COLOR_WHITE, font=font_alert, anchor="mm")

        # ── Barra inferior de marca (acento) ──────────────────────────────
        draw.rectangle([(0, THUMB_H - 7), (THUMB_W, THUMB_H)], fill=accent_rgb)

        return img

    # ── Version B — Pregunta dramática full-width ─────────────────────────────

    def _generate_version_b(self, ctx: Context) -> "Image":
        """
        Version B — Pregunta dramática a pantalla completa.
        Fondo: gráfico desvanecido o degradado de sentimiento.
        Texto: pregunta en dos líneas, letra masiva, alta legibilidad.
        Precio y marca en banda inferior.

        Principio: el espectador lee la pregunta → quiere saber la respuesta → clica.
        """
        from PIL import Image, ImageDraw

        change_24h, fear_greed, accent_rgb, show_alert = self._detect_sentiment(ctx)

        # ── Base: frame del vídeo muy oscurecido, o degradado ────────────
        base_img = self._extract_video_frame(ctx, second=8.0)
        if base_img:
            img = self._apply_dark_overlay(base_img, alpha=190)
        else:
            img = Image.new("RGB", (THUMB_W, THUMB_H), (5, 5, 5))
            draw_bg = ImageDraw.Draw(img)
            self._draw_gradient_bg(draw_bg, THUMB_W, THUMB_H, accent=accent_rgb)

        draw = ImageDraw.Draw(img)

        # ── Vignette (oscurecer bordes) ───────────────────────────────────
        for i in range(60):
            alpha = int(i * 2.5)
            draw.rectangle([(i, i), (THUMB_W - i, THUMB_H - i)],
                           outline=(0, 0, 0), width=1)

        # ── Línea de acento superior ──────────────────────────────────────
        draw.rectangle([(0, 0), (THUMB_W, 6)], fill=accent_rgb)

        # ── Pregunta: el elemento dominante ──────────────────────────────
        question = _generate_question(ctx)
        q_lines = question.split("\n")

        # Elegir tamaño de fuente que quepa en el ancho disponible
        font_q = None
        for size in (130, 110, 90, 72):
            candidate = _load_font(_FONTS_IMPACT, size) or _load_font(_FONTS_BOLD, size - 10)
            if candidate is None:
                continue
            try:
                max_w = max(
                    candidate.getlength(l) if hasattr(candidate, "getlength")
                    else len(l) * size // 2
                    for l in q_lines
                )
            except Exception:
                max_w = THUMB_W + 1
            if max_w <= THUMB_W - 60:
                font_q = candidate
                break
        if font_q is None:
            font_q = _load_font(_FONTS_BOLD, 60)

        # Centrar verticalmente dejando espacio para la banda inferior
        line_h = 130
        total_h = len(q_lines) * line_h
        start_y = max(60, (THUMB_H - 120 - total_h) // 2)

        for i, line in enumerate(q_lines):
            ly = start_y + i * line_h
            # Sombra de color acento
            try:
                lw = font_q.getlength(line) if hasattr(font_q, "getlength") else len(line) * 60
            except Exception:
                lw = len(line) * 60
            lx = max(30, (THUMB_W - int(lw)) // 2)
            # Sombra gruesa para legibilidad sobre cualquier fondo
            for dx, dy in [(-4, -4), (4, -4), (-4, 4), (4, 4), (0, 5), (5, 0)]:
                draw.text((lx + dx, ly + dy), line, fill=(0, 0, 0), font=font_q)
            # Texto principal blanco
            draw.text((lx, ly), line, fill=COLOR_WHITE, font=font_q)
            # Subrayado de la última línea con acento
            if i == len(q_lines) - 1:
                ul_y = ly + line_h - 20
                draw.rectangle([(lx, ul_y), (lx + int(lw), ul_y + 5)], fill=accent_rgb)

        # ── Banda inferior: precio + CryptoVerdad ────────────────────────
        band_y = THUMB_H - 80
        draw.rectangle([(0, band_y), (THUMB_W, THUMB_H)], fill=(10, 10, 10))

        # Precio BTC izquierda
        btc = (ctx.prices or {}).get("BTC", {})
        if isinstance(btc, dict) and btc.get("price"):
            price_str = f"BTC ${btc['price']:,.0f}  ({change_24h:+.1f}%)"
            font_price = _load_font(_FONTS_BOLD, 34)
            if font_price:
                price_color = COLOR_GREEN if change_24h >= 0 else COLOR_RED
                draw.text((24, band_y + 12), price_str, fill=price_color, font=font_price)

        # CryptoVerdad derecha
        font_brand = _load_font(_FONTS_BOLD, 32)
        if font_brand:
            try:
                brw = font_brand.getlength("CryptoVerdad") if hasattr(font_brand, "getlength") else 230
            except Exception:
                brw = 230
            draw.text((THUMB_W - int(brw) - 24, band_y + 14),
                      "CryptoVerdad", fill=accent_rgb, font=font_brand)

        # Línea separadora de la banda
        draw.rectangle([(0, band_y), (THUMB_W, band_y + 4)], fill=accent_rgb)

        # ── Banda de alerta (si mercado extremo) ──────────────────────────
        if show_alert:
            draw.rectangle([(0, 6), (THUMB_W, 44)], fill=COLOR_RED)
            font_alert = _load_font(_FONTS_BOLD, 20)
            if font_alert:
                draw.text((THUMB_W // 2, 25), "ALERTA MERCADO — VOLATILIDAD EXTREMA",
                          fill=COLOR_WHITE, font=font_alert, anchor="mm")

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

            # Registrar sentimiento en ctx para diagnóstico y ALETHEIA A/B
            try:
                _chg, _, _, _ = self._detect_sentiment(ctx)
                if _chg < -3:
                    ctx.thumbnail_sentiment = "bearish"
                elif _chg < 2:
                    ctx.thumbnail_sentiment = "neutral"
                else:
                    ctx.thumbnail_sentiment = "bullish"
                console.print(
                    f"[dim]Sentimiento thumbnail: {ctx.thumbnail_sentiment} "
                    f"(BTC 24h: {_chg:+.1f}%)[/]"
                )
            except Exception as _se:
                self.logger.warning(f"Sentimiento thumbnail: {_se}")
                ctx.thumbnail_sentiment = "neutral"

            console.print(
                f"[green]Thumbnails generados:[/]\n"
                f"  A: {path_a}\n"
                f"  B: {path_b}"
            )
            self.logger.info(
                f"Thumbnails A/B guardados para pipeline {ctx.pipeline_id[:8]} "
                f"· sentimiento: {getattr(ctx, 'thumbnail_sentiment', 'neutral')}"
            )

        except Exception as e:
            self.logger.error(f"Error en IRIS: {e}\n{traceback.format_exc()}")
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
