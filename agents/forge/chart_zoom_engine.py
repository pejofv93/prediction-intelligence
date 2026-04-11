"""
agents/forge/chart_zoom_engine.py
ChartZoomEngine — Motor de zoom dinámico para gráficos BTC en NEXUS.

Features:
  - Zoom animado hacia zonas de precio mencionadas en los subtítulos
  - Línea de tendencia lineal dibujada progresivamente durante el vídeo
  - Flechas de dirección alcista/bajista animadas (pulso sinusoidal)
  - Transiciones ease-in-out suaves entre estados de zoom

Diseñado para ser importado por HEPHAESTUS y consumido en make_frame(t).

Uso:
    engine = ChartZoomEngine(
        base_chart_path=ctx.chart_path,
        ohlcv=ohlcv_data,          # [[ts_ms, o, h, l, c], ...]
        subtitle_entries=subs,     # [(start_s, end_s, text), ...]
        duration=audio_dur,
        levels=ctx_levels,         # {"supports": [...], "resistances": [...]}
    )
    frame_pil = engine.get_frame(t, w=1920, h=1080)
"""

import math
import re
from pathlib import Path
from typing import List, Optional, Tuple

from utils.logger import get_logger

# Paleta CryptoVerdad
C_BG     = (10,  10,  10)
C_ACCENT = (247, 147, 26)
C_TEXT   = (255, 255, 255)
C_UP     = (76,  175, 80)    # verde alcista
C_DOWN   = (244, 67,  54)    # rojo bajista
C_GREY   = (136, 136, 136)

# ── Márgenes del plot area dentro del PNG 1920×1080 de DAEDALUS ──────────────
# matplotlib tight_layout(pad=1.5), DPI=100, fontsize 9-14
# Ajustados empiricamente para el estilo TradingView de DAEDALUS
PLOT_LEFT   = 155   # px desde borde izquierdo (deja espacio al eje Y $XX,XXX)
PLOT_RIGHT  = 1875  # px desde borde izquierdo
PLOT_TOP    = 90    # px desde borde superior (título)
PLOT_BOTTOM = 1020  # px desde borde superior (eje X)


def _ease_inout(x: float) -> float:
    """Smoothstep ease-in-out: x ∈ [0,1] → suavizado [0,1]."""
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class ChartZoomEngine:
    """
    Motor de zoom dinámico para gráficos de precio BTC en vídeos NEXUS.

    Produce frames PIL Image con:
      1. Zoom suave (PIL crop+resize) a zonas de precio mencionadas en subs
      2. Línea de tendencia lineal animada (crece de izq→dcha en TREND_DRAW_DUR s)
      3. Flechas de dirección alcista/bajista con pulso sinusoidal
      4. Transiciones ease-in-out de 0.7 s entre estados de zoom

    Todos los renderizados usan PIL (no matplotlib en tiempo de vídeo) → rápido.
    """

    ZOOM_ENTER_DUR  = 0.7    # s de transición al entrar en zoom
    ZOOM_EXIT_DUR   = 0.5    # s de transición al salir de zoom
    TREND_DRAW_DUR  = 5.0    # s para dibujar la línea de tendencia completa
    ARROW_VISIBLE   = 3.5    # s que permanece visible la flecha de dirección
    ZOOM_Y_MARGIN   = 0.22   # ±22% del rango de precio en el eje Y al hacer zoom
    ZOOM_X_START    = 0.35   # mostrar desde el 35% del gráfico al hacer zoom (últimos datos)
    MIN_ZOOM_START  = 3.0    # s mínimo antes de que empiece cualquier zoom (evita líneas en frame 0)

    def __init__(
        self,
        base_chart_path: str,
        ohlcv: list,
        subtitle_entries: List[Tuple[float, float, str]],
        duration: float,
        levels: dict = None,
    ):
        self.logger = get_logger("CHART_ZOOM")
        self.base_path = base_chart_path
        self.ohlcv = ohlcv or []
        self.subtitle_entries = subtitle_entries or []
        self.duration = max(duration, 1.0)
        self.levels = levels or {"supports": [], "resistances": []}

        # Parsear OHLCV
        try:
            self._closes = [float(d[4]) for d in self.ohlcv]
            self._highs  = [float(d[2]) for d in self.ohlcv]
            self._lows   = [float(d[3]) for d in self.ohlcv]
        except (IndexError, TypeError, ValueError):
            self._closes, self._highs, self._lows = [], [], []

        if self._closes:
            self._p_min = min(self._lows)   * 0.993
            self._p_max = max(self._highs)  * 1.007
        else:
            # Fallback cuando no hay OHLCV: usar support/resistance para estimar
            all_lvls = (levels or {}).get("supports", []) + (levels or {}).get("resistances", [])
            if all_lvls:
                self._p_min = min(all_lvls) * 0.93
                self._p_max = max(all_lvls) * 1.07
            else:
                self._p_min = 60_000.0
                self._p_max = 75_000.0

        # Cargar imagen base UNA sola vez
        self._base_img = None
        try:
            if base_chart_path and Path(base_chart_path).exists():
                from PIL import Image
                self._base_img = Image.open(base_chart_path).convert("RGB")
                self.logger.info(
                    f"ChartZoomEngine: imagen base cargada "
                    f"{self._base_img.width}x{self._base_img.height}"
                )
        except Exception as e:
            self.logger.warning(f"ChartZoomEngine: no pudo cargar imagen base: {e}")

        # Calcular línea de tendencia en coordenadas píxel del PNG original
        self._trend_px = self._calc_trend_pixels()

        # Coordenada Y del precio objetivo del zoom activo (para circulo parpadeante)
        self._last_zoom_target_px: int = 0

        # Timer de aparición de líneas S/R: mapea "S_69000" / "R_73000" → t_start
        self._line_draw_start: dict = {}

        # Detectar eventos de zoom desde subtítulos
        self._zoom_events = self._parse_zoom_events()

        # Complementar/garantizar zoom en R1 y S1 desde los niveles del ctx
        # aunque no aparezcan mencionados exactamente en los subtítulos.
        self._inject_level_zoom_events()

        self.logger.info(
            f"ChartZoomEngine: {len(self._zoom_events)} eventos de zoom — "
            + (", ".join(f"${e['price']:,.0f}@{e['t_start']:.1f}s"
                         for e in self._zoom_events) or "ninguno")
        )

    # ── Helpers de coordenadas ─────────────────────────────────────────────

    def _price_to_y_pixel(self, price: float) -> int:
        """
        Mapea un precio USD a coordenada Y en el PNG original (1920×1080).
        Convención matplotlib: precio alto → y bajo (arriba).
        """
        p_range = self._p_max - self._p_min
        if p_range < 1.0:
            return (PLOT_TOP + PLOT_BOTTOM) // 2
        norm = (price - self._p_min) / p_range          # 0 (mínimo) → 1 (máximo)
        y = PLOT_BOTTOM - norm * (PLOT_BOTTOM - PLOT_TOP)
        return int(max(PLOT_TOP, min(PLOT_BOTTOM, y)))

    def _idx_to_x_pixel(self, idx: int, n_total: int) -> int:
        """Mapea índice de candlestick (0 → n-1) a coordenada X en el PNG."""
        if n_total <= 1:
            return (PLOT_LEFT + PLOT_RIGHT) // 2
        return int(PLOT_LEFT + (idx / max(n_total - 1, 1)) * (PLOT_RIGHT - PLOT_LEFT))

    def _get_price_y_pixel(self, price: float, img_h: int) -> int:
        """
        Mapea un precio puntual a su coordenada Y en la imagen original.
        Usa el mismo espacio de precio que _price_to_y_pixel pero acepta
        img_h explícito (para compatibilidad con la llamada desde S/R lines).
        """
        if not self._closes:
            return img_h // 2
        closes = self._closes
        price_max = max(closes) * 1.02
        price_min = min(closes) * 0.98
        p_range = price_max - price_min
        if p_range < 1.0:
            return (PLOT_TOP + PLOT_BOTTOM) // 2
        ratio = (price_max - price) / p_range
        y = int(PLOT_TOP + ratio * (PLOT_BOTTOM - PLOT_TOP))
        return max(PLOT_TOP, min(PLOT_BOTTOM, y))

    # ── Línea de tendencia ─────────────────────────────────────────────────

    def _calc_trend_pixels(self) -> Optional[dict]:
        """
        Calcula la línea de tendencia por regresión lineal sobre los cierres.
        Retorna: {x0, y0, x1, y1, bullish} en coordenadas del PNG original.
        """
        if len(self._closes) < 4:
            return None
        try:
            import numpy as np
            x_arr = np.arange(len(self._closes), dtype=float)
            y_arr = np.array(self._closes, dtype=float)
            m, b = np.polyfit(x_arr, y_arr, 1)
            n = len(self._closes)
            price_y0 = float(b)
            price_y1 = float(m * (n - 1) + b)
            return {
                "x0": self._idx_to_x_pixel(0, n),
                "y0": self._price_to_y_pixel(price_y0),
                "x1": self._idx_to_x_pixel(n - 1, n),
                "y1": self._price_to_y_pixel(price_y1),
                "bullish": m > 0,
                "slope": float(m),
            }
        except Exception as e:
            self.logger.warning(f"_calc_trend_pixels: {e}")
            return None

    # ── Parser de zoom events ──────────────────────────────────────────────

    def _parse_zoom_events(self) -> list:
        """
        Detecta menciones de precio en los subtítulos y construye una
        línea de tiempo de eventos de zoom.

        Soporta formatos: $84,000 · $84.000 · 84000 · 84 mil · 84k
        Retorna: [{"t_start", "t_end", "price"}, ...]  ordenados por t_start
        """
        patterns = [
            # $84,000 o $84.000 con decimales opcionales
            re.compile(r'\$\s*([\d]{2,3}[,.][\d]{3}(?:[,.][\d]{1,2})?)', re.I),
            # 84,000 USD / dólares
            re.compile(
                r'\b([\d]{2,3}[,.][\d]{3}(?:[,.][\d]{1,2})?)'
                r'\s*(?:d[oó]lares|usd|usdt)?\b', re.I
            ),
            # "84 mil" → 84_000
            re.compile(r'\b([\d]{2,3})\s+mil\b', re.I),
            # "84k" → 84_000
            re.compile(r'\b([\d]{2,3})k\b', re.I),
        ]

        events = []
        seen_t = set()

        for start_s, end_s, text in self.subtitle_entries:
            # No arrancar zoom antes de MIN_ZOOM_START — evita líneas horizontales
            # en los primeros segundos cuando el gráfico aún no se ha estabilizado.
            if start_s < self.MIN_ZOOM_START:
                continue

            t_key = round(start_s, 1)
            if t_key in seen_t:
                continue

            for pat in patterns:
                m = pat.search(text)
                if not m:
                    continue
                raw = m.group(1).strip()
                try:
                    # Normalizar: quitar puntos y comas de millares
                    clean = re.sub(r'[.,](?=\d{3}\b)', '', raw)
                    clean = clean.replace(",", ".").replace(".", "")
                    price = float(clean)
                    # Escalar "84 mil" / "84k"
                    if price < 1000:
                        price *= 1000
                    # Validar rango BTC razonable
                    if not (5_000 <= price <= 500_000):
                        continue
                    # Solo hacer zoom si el precio está cerca del rango del gráfico
                    margin = (self._p_max - self._p_min) * 0.25
                    if not (self._p_min - margin <= price <= self._p_max + margin):
                        continue
                    events.append({
                        "t_start": start_s,
                        "t_end": min(end_s + 2.5, self.duration),
                        "price": price,
                    })
                    seen_t.add(t_key)
                    break
                except (ValueError, AttributeError):
                    continue

        return sorted(events, key=lambda e: e["t_start"])

    def _inject_level_zoom_events(self) -> None:
        """
        Garantiza que R1 y S1 de los niveles del ctx tengan su propio
        evento de zoom, sincronizados con los momentos del vídeo donde
        CALÍOPE los menciona.

        - R1 (primera resistencia): zoom al 35% de la duración del vídeo
        - S1 (primer soporte):      zoom al 65% de la duración del vídeo

        Solo añade el evento si no hay ya un evento de subtítulo que caiga
        dentro de ±5 s del tiempo objetivo (evita duplicados).
        """
        resistances = self.levels.get("resistances") or []
        supports    = self.levels.get("supports")    or []

        def _has_nearby_event(t_target: float, tolerance: float = 5.0) -> bool:
            return any(
                abs(ev["t_start"] - t_target) <= tolerance
                for ev in self._zoom_events
            )

        injected = []

        if resistances:
            r1_price = float(resistances[0])
            t_r1 = max(self.MIN_ZOOM_START, self.duration * 0.35)
            if not _has_nearby_event(t_r1):
                zoom_dur = min(4.0, self.duration * 0.12)
                injected.append({
                    "price":   r1_price,
                    "t_start": t_r1,
                    "t_end":   min(t_r1 + zoom_dur, self.duration),
                    "label":   f"R1: ${r1_price:,.0f}",
                })
                self.logger.info(
                    f"Zoom inyectado desde niveles — R1: ${r1_price:,.0f} @ {t_r1:.1f}s"
                )

        if supports:
            s1_price = float(supports[0])
            t_s1 = max(self.MIN_ZOOM_START, self.duration * 0.65)
            if not _has_nearby_event(t_s1):
                zoom_dur = min(4.0, self.duration * 0.12)
                injected.append({
                    "price":   s1_price,
                    "t_start": t_s1,
                    "t_end":   min(t_s1 + zoom_dur, self.duration),
                    "label":   f"S1: ${s1_price:,.0f}",
                })
                self.logger.info(
                    f"Zoom inyectado desde niveles — S1: ${s1_price:,.0f} @ {t_s1:.1f}s"
                )

        if injected:
            self._zoom_events = sorted(
                self._zoom_events + injected,
                key=lambda e: e["t_start"],
            )

    # ── Estado de zoom ─────────────────────────────────────────────────────

    def _get_zoom_state(
        self, t: float, img_w: int, img_h: int
    ) -> Tuple[Optional[tuple], float]:
        """
        Calcula crop box y alpha de zoom para el tiempo t.

        Returns:
            crop_box: (x1, y1, x2, y2) en píxeles del PNG base, o None (sin zoom)
            zoom_alpha: 0.0 (sin zoom) → 1.0 (zoom completo)
        """
        active = None
        for ev in self._zoom_events:
            if ev["t_start"] <= t <= ev["t_end"]:
                active = ev
                break

        if active is None:
            self._last_zoom_target_px = None
            return None, 0.0

        price = active["price"]
        # Guardar pixel Y del precio objetivo para el circulo parpadeante
        self._last_zoom_target_px = self._price_to_y_pixel(price)
        t_start = active["t_start"]
        t_end   = active["t_end"]
        event_dur = max(t_end - t_start, 0.01)
        t_in = t - t_start

        # Calcular alpha (ease-in-out entrada/salida)
        if t_in < self.ZOOM_ENTER_DUR:
            alpha = _ease_inout(t_in / self.ZOOM_ENTER_DUR)
        elif t_in > event_dur - self.ZOOM_EXIT_DUR:
            alpha = _ease_inout((t_end - t) / max(self.ZOOM_EXIT_DUR, 0.01))
        else:
            alpha = 1.0

        # ── Calcular crop box del zoom ─────────────────────────────────────
        p_range  = self._p_max - self._p_min
        half_rng = p_range * self.ZOOM_Y_MARGIN

        price_lo = max(self._p_min, price - half_rng)
        price_hi = min(self._p_max, price + half_rng)

        # Asegurar rango mínimo (±3% del precio para no crashear)
        mid = (price_lo + price_hi) / 2
        min_half = price * 0.03
        if (price_hi - price_lo) / 2 < min_half:
            price_lo = mid - min_half
            price_hi = mid + min_half

        # Padding generoso para que las mechas altas no salgan del frame
        # (especialmente crítico en Short 1080x1920 donde el chart es estirado 4x)
        y_top = self._price_to_y_pixel(price_hi) - 55
        y_bot = self._price_to_y_pixel(price_lo) + 25
        # Respetar PLOT_TOP: no subir por encima del área de plot del matplotlib
        y_top = max(PLOT_TOP, y_top)
        y_bot = min(img_h, y_bot)

        x_left  = int(PLOT_LEFT + (PLOT_RIGHT - PLOT_LEFT) * self.ZOOM_X_START)
        x_right = min(img_w, PLOT_RIGHT + 30)

        # Interpolar desde vista completa hasta crop objetivo
        full = (0, 0, img_w, img_h)
        target = (x_left, y_top, x_right, y_bot)
        crop = tuple(
            int(_lerp(full[i], target[i], alpha))
            for i in range(4)
        )
        # Garantizar crop válido (w > 0, h > 0)
        if crop[2] - crop[0] < 32 or crop[3] - crop[1] < 32:
            return None, 0.0

        return crop, alpha

    # ── Línea de tendencia overlay ─────────────────────────────────────────

    def _draw_trend_line(
        self,
        frame,       # PIL Image (se modifica in-place)
        t: float,
        crop: Optional[tuple],
        out_w: int,
        out_h: int,
        img_w: int,
        img_h: int,
    ):
        """
        Dibuja la línea de tendencia animada sobre el frame PIL.
        La línea crece progresivamente de izquierda → derecha en TREND_DRAW_DUR s.
        Si hay zoom activo, las coordenadas se transforman al espacio del crop.
        """
        from PIL import ImageDraw, ImageFont

        if not self._trend_px:
            return frame

        draw_pct = min(1.0, t / max(self.TREND_DRAW_DUR, 0.1))
        if draw_pct <= 0.01:
            return frame

        td = self._trend_px

        # ── Función de transformación de coordenadas ───────────────────────
        if crop:
            cx1, cy1, cx2, cy2 = crop
            cw = max(cx2 - cx1, 1)
            ch = max(cy2 - cy1, 1)

            def tx(px: int) -> int:
                return int((px - cx1) / cw * out_w)

            def ty(py: int) -> int:
                return int((py - cy1) / ch * out_h)
        else:
            sx = out_w / max(img_w, 1)
            sy = out_h / max(img_h, 1)

            def tx(px: int) -> int:
                return int(px * sx)

            def ty(py: int) -> int:
                return int(py * sy)

        x0_out = tx(td["x0"])
        y0_out = ty(td["y0"])
        x1_full = tx(td["x1"])
        y1_full = ty(td["y1"])

        # Punto final animado proporcional a draw_pct
        x1_anim = int(x0_out + draw_pct * (x1_full - x0_out))
        y1_anim = int(y0_out + draw_pct * (y1_full - y0_out))

        color = C_UP if td["bullish"] else C_DOWN

        draw = ImageDraw.Draw(frame)

        # Sombra difusa (±2px)
        for dx, dy in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
            draw.line(
                [(x0_out + dx, y0_out + dy), (x1_anim + dx, y1_anim + dy)],
                fill=(0, 0, 0),
                width=5,
            )
        # Línea principal
        draw.line(
            [(x0_out, y0_out), (x1_anim, y1_anim)],
            fill=color,
            width=3,
        )

        # Etiqueta de tendencia cuando está completa (>90%)
        if draw_pct > 0.88:
            fade = min(1.0, (draw_pct - 0.88) / 0.12)
            label = "↑ TENDENCIA ALCISTA" if td["bullish"] else "↓ TENDENCIA BAJISTA"
            try:
                font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 20)
            except Exception:
                font = ImageFont.load_default()
            lx = x0_out + 10
            ly = min(y0_out, y1_anim) - 30
            ly = max(4, min(out_h - 30, ly))
            # Fondo semitransparente
            try:
                from PIL import Image as PILImg
                label_w = len(label) * 12 + 16
                lbl_bg = PILImg.new("RGBA", (label_w, 28), (0, 0, 0, int(170 * fade)))
                lbl_d = ImageDraw.Draw(lbl_bg)
                lbl_d.text((8, 4), label, fill=(*color, int(220 * fade)), font=font)
                frame_rgba = frame.convert("RGBA")
                frame_rgba.paste(lbl_bg, (lx, ly), mask=lbl_bg.split()[3])
                frame = frame_rgba.convert("RGB")
            except Exception:
                draw.text((lx, ly), label, fill=color, font=font)

        return frame

    # ── Flecha de dirección ────────────────────────────────────────────────

    def _draw_direction_arrow(
        self,
        frame,
        t: float,
        crop: Optional[tuple],
        out_w: int,
        out_h: int,
        img_w: int,
        img_h: int,
    ):
        """
        Dibuja una flecha de dirección alcista (↑verde) o bajista (↓rojo)
        junto al nivel de precio del evento activo, con pulso sinusoidal.
        """
        from PIL import Image as PILImg, ImageDraw, ImageFont

        if not self._zoom_events:
            return frame

        for ev in self._zoom_events:
            t_arrow_start = ev["t_start"] + self.ZOOM_ENTER_DUR
            t_arrow_end   = t_arrow_start + self.ARROW_VISIBLE
            if not (t_arrow_start <= t <= t_arrow_end):
                continue

            price = ev["price"]
            t_in  = t - t_arrow_start
            event_vis = t_arrow_end - t_arrow_start

            # Alpha de aparición/desaparición suave (0.3s fade)
            fade_dur = 0.35
            if t_in < fade_dur:
                arrow_alpha = _ease_inout(t_in / fade_dur)
            elif t_in > event_vis - fade_dur:
                arrow_alpha = _ease_inout((t_arrow_end - t) / fade_dur)
            else:
                arrow_alpha = 1.0

            if arrow_alpha < 0.02:
                continue

            # Dirección: comparar precio mencionado con precio actual
            current = self._closes[-1] if self._closes else price
            going_up = price > current * 0.97  # target > actual → sube

            color = C_UP if going_up else C_DOWN

            # ── Posición de la flecha (en coords del PNG original) ─────────
            y_px_orig = self._price_to_y_pixel(price)
            x_px_orig = self._idx_to_x_pixel(
                max(0, len(self._closes) - 3), len(self._closes)
            )

            # Transformar al espacio de salida
            if crop:
                cx1, cy1, cx2, cy2 = crop
                cw = max(cx2 - cx1, 1)
                ch = max(cy2 - cy1, 1)
                ax = int((x_px_orig - cx1) / cw * out_w)
                ay = int((y_px_orig - cy1) / ch * out_h)
            else:
                ax = int(x_px_orig * out_w / max(img_w, 1))
                ay = int(y_px_orig * out_h / max(img_h, 1))

            ax = max(80, min(out_w - 80, ax)) - 40
            ay = max(80, min(out_h - 140, ay))

            # Pulso suave: escala entre 0.9 y 1.1 (no agresivo)
            pulse_scale = 0.9 + 0.1 * math.sin(t * 3)

            # Dimensiones base de la flecha escaladas por pulso
            half_head = int(28 * pulse_scale)   # mitad del ancho de la cabeza
            shaft_hx  = int(11 * pulse_scale)   # semiancho del tronco
            head_h    = int(42 * pulse_scale)   # altura de la cabeza
            shaft_h   = int(36 * pulse_scale)   # altura del tronco
            tip_off   = int(48 * pulse_scale)   # offset del pico desde ay

            # ── Construir vértices de la flecha ────────────────────────────
            if going_up:
                tip_y = ay - tip_off
                base_y = tip_y + head_h
                pts = [
                    (ax,              tip_y),
                    (ax - half_head,  base_y),
                    (ax - shaft_hx,   base_y),
                    (ax - shaft_hx,   base_y + shaft_h),
                    (ax + shaft_hx,   base_y + shaft_h),
                    (ax + shaft_hx,   base_y),
                    (ax + half_head,  base_y),
                ]
            else:
                tip_y = ay + tip_off
                base_y = tip_y - head_h
                pts = [
                    (ax,              tip_y),
                    (ax - half_head,  base_y),
                    (ax - shaft_hx,   base_y),
                    (ax - shaft_hx,   base_y - shaft_h),
                    (ax + shaft_hx,   base_y - shaft_h),
                    (ax + shaft_hx,   base_y),
                    (ax + half_head,  base_y),
                ]

            # ── Dibujar flecha en capa RGBA ────────────────────────────────
            try:
                alpha_val = int(225 * arrow_alpha)
                arrow_layer = PILImg.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
                ad = ImageDraw.Draw(arrow_layer)
                ad.polygon(pts, fill=(*color, alpha_val))
                ad.line(pts + [pts[0]], fill=(*color, min(255, alpha_val + 25)), width=2)

                frame_rgba = frame.convert("RGBA")
                frame_rgba.paste(arrow_layer, (0, 0), mask=arrow_layer.split()[3])
                frame = frame_rgba.convert("RGB")
            except Exception:
                pass

            # ── Etiqueta de precio ─────────────────────────────────────────
            try:
                from PIL import ImageFont
                try:
                    font_lbl = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
                except Exception:
                    font_lbl = ImageFont.load_default()
                label = f"${price:,.0f}"
                lx = ax + 38
                if going_up:
                    ly = ay - 52
                else:
                    ly = ay + 16
                ly = max(4, min(out_h - 40, ly))
                draw2 = ImageDraw.Draw(frame)
                # Fondo semitransparente detrás del precio
                lw = len(label) * 17 + 12
                draw2.rectangle(
                    [(lx - 4, ly - 4), (lx + lw, ly + 34)],
                    fill=(0, 0, 0),
                )
                draw2.text((lx, ly), label, fill=color, font=font_lbl)
            except Exception:
                pass

            break  # solo una flecha activa a la vez

        return frame

    # ── Líneas de soporte/resistencia animadas ────────────────────────────

    def _draw_support_resistance_lines(
        self,
        img,                # PIL Image (tamaño final w x h)
        t: float,
        crop_box: tuple,    # (x0, y0, x1, y1) en coords del PNG original
    ):
        """
        Dibuja líneas horizontales S/R animadas sobre el frame final ya
        redimensionado. Cada línea crece de izquierda a derecha en 0.5 s.
        La etiqueta aparece con fade cuando la línea supera el 85 %.
        """
        from PIL import ImageDraw, ImageFont, Image as PILImg

        if self._base_img is None:
            return img

        w, h = img.size
        x0_crop, y0_crop, x1_crop, y1_crop = crop_box
        orig_w, orig_h = self._base_img.size

        # Altura del rango del crop (evitar division por cero)
        crop_h = max(y1_crop - y0_crop, 1)
        crop_w = max(x1_crop - x0_crop, 1)

        draw = ImageDraw.Draw(img)

        all_levels = []
        for price in (self.levels.get("supports") or []):
            all_levels.append((float(price), "S", (255, 100, 100)))   # rojo claro
        for price in (self.levels.get("resistances") or []):
            all_levels.append((float(price), "R", (247, 147, 26)))    # naranja

        for price, label_prefix, color in all_levels:
            key = f"{label_prefix}_{int(price)}"

            # Iniciar timer de aparición la primera vez que se procesa
            if key not in self._line_draw_start:
                self._line_draw_start[key] = t

            t_line = t - self._line_draw_start[key]
            DRAW_DUR = 0.5  # segundos para que la línea crezca izq → dcha
            progress = min(t_line / max(DRAW_DUR, 0.01), 1.0)

            # Coordenada Y del precio en el PNG original
            y_orig = self._get_price_y_pixel(price, orig_h)

            # Mapear Y al espacio del crop y luego al frame redimensionado
            y_in_crop = (y_orig - y0_crop) / crop_h
            y_final = int(y_in_crop * h)

            # Solo dibujar si está dentro del frame visible
            if y_final < 0 or y_final > h:
                continue

            # Línea que crece de izquierda a derecha
            x_end = int(progress * w)
            if x_end < 2:
                continue

            # Sombra fina
            draw.line([(0, y_final + 1), (x_end, y_final + 1)],
                      fill=(0, 0, 0), width=2)
            # Línea principal
            draw.line([(0, y_final), (x_end, y_final)],
                      fill=color, width=3)

            # Etiqueta con fade cuando la línea supera el 85 %
            if progress > 0.85:
                label_alpha = int(255 * min((progress - 0.85) / 0.15, 1.0))
                price_k = price / 1000
                label_text = (
                    f"Soporte ${price_k:.0f}k"
                    if label_prefix == "S"
                    else f"Resist. ${price_k:.0f}k"
                )
                try:
                    font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 18)
                except Exception:
                    font = ImageFont.load_default()

                lx = max(0, x_end - 170)
                ly = y_final - 24

                # Fondo semitransparente usando capa RGBA
                try:
                    bbox = draw.textbbox((lx, ly), label_text, font=font)
                    pad = 4
                    bbox_padded = (
                        bbox[0] - pad, bbox[1] - pad,
                        bbox[2] + pad, bbox[3] + pad,
                    )
                    bg = PILImg.new("RGBA", img.size, (0, 0, 0, 0))
                    bg_draw = ImageDraw.Draw(bg)
                    bg_draw.rectangle(
                        bbox_padded,
                        fill=(*color, label_alpha // 2),
                    )
                    img = PILImg.alpha_composite(
                        img.convert("RGBA"), bg
                    ).convert("RGB")
                    draw = ImageDraw.Draw(img)
                except Exception:
                    pass

                draw.text((lx, ly), label_text, fill=color, font=font)

        return img

    # ── Frame público ──────────────────────────────────────────────────────

    def get_frame(self, t: float, w: int = 1920, h: int = 1080):
        """
        Retorna PIL Image de (w×h) para el tiempo t con:
          - Zoom hacia zona de precio (si hay mención en subtítulo activo)
          - Línea de tendencia progresiva
          - Flecha de dirección con pulso

        Si no hay imagen base, devuelve fondo #0A0A0A.
        """
        from PIL import Image

        if self._base_img is None:
            return Image.new("RGB", (w, h), C_BG)

        try:
            img_w, img_h = self._base_img.size

            # 1. Calcular estado de zoom
            crop, zoom_alpha = self._get_zoom_state(t, img_w, img_h)

            # 2. Obtener imagen base con zoom aplicado
            if crop and zoom_alpha > 0.01:
                full_resized = self._base_img.resize((w, h), Image.LANCZOS)
                zoom_img = self._base_img.crop(crop).resize((w, h), Image.LANCZOS)
                frame = Image.blend(full_resized, zoom_img, zoom_alpha)
                effective_crop = crop
            else:
                frame = self._base_img.resize((w, h), Image.LANCZOS)
                effective_crop = None

            # 3. Dibujar línea de tendencia animada
            frame = self._draw_trend_line(
                frame, t, effective_crop, w, h, img_w, img_h
            )

            # 4. Dibujar flecha de dirección
            frame = self._draw_direction_arrow(
                frame, t, effective_crop, w, h, img_w, img_h
            )

            # 4b. Dibujar líneas S/R animadas (solo cuando hay crop activo)
            if effective_crop is not None:
                frame = self._draw_support_resistance_lines(frame, t, effective_crop)

            # 5. Circulo parpadeante naranja cuando hay zoom activo
            if effective_crop is not None and self._last_zoom_target_px:
                try:
                    sin_val = math.sin(t * 8)
                    if sin_val > 0:
                        cx1, cy1, cx2, cy2 = effective_crop
                        ch = max(cy2 - cy1, 1)
                        # Posicion Y del circulo en el frame final
                        circle_y = int((self._last_zoom_target_px - cy1) / ch * h)
                        circle_x = int(w * 0.85)
                        # Mantener dentro de los limites
                        circle_y = max(12, min(h - 12, circle_y))
                        circle_x = max(12, min(w - 12, circle_x))
                        radius = 12
                        alpha_val = int(180 + 75 * abs(sin_val))
                        alpha_val = min(255, alpha_val)
                        from PIL import Image as _PILImg, ImageDraw as _IDraw
                        circle_layer = _PILImg.new("RGBA", (w, h), (0, 0, 0, 0))
                        cd = _IDraw.Draw(circle_layer)
                        cd.ellipse(
                            [(circle_x - radius, circle_y - radius),
                             (circle_x + radius, circle_y + radius)],
                            fill=(247, 147, 26, alpha_val),
                            outline=(255, 255, 255, min(255, alpha_val + 30)),
                            width=2,
                        )
                        frame_rgba = frame.convert("RGBA")
                        frame_rgba.paste(circle_layer, (0, 0), mask=circle_layer.split()[3])
                        frame = frame_rgba.convert("RGB")
                except Exception:
                    pass

            return frame

        except Exception as e:
            self.logger.warning(f"ChartZoomEngine.get_frame(t={t:.2f}): {e}")
            try:
                return self._base_img.resize((w, h), Image.LANCZOS)
            except Exception:
                from PIL import Image as PILImg
                return PILImg.new("RGB", (w, h), C_BG)

    # ── Utilidad: captura frames de preview ───────────────────────────────

    def capture_preview_frames(
        self, output_dir: str, times: List[float] = None, w: int = 1920, h: int = 1080
    ) -> List[str]:
        """
        Guarda frames PNG de preview en output_dir para los tiempos indicados.
        Retorna lista de rutas guardadas.
        Usado por test_chart_zoom.py para verificar visualmente el resultado.
        """
        if times is None:
            # Distribuir en puntos de interés: inicio, zoom activo, final
            times = [2.0, self.duration * 0.4, self.duration * 0.8]
            # Preferir tiempos donde hay zoom activo
            if self._zoom_events:
                times = []
                # Frame antes del primer zoom
                t0 = max(1.0, self._zoom_events[0]["t_start"] - 1.0)
                # Frame en el pico del primer zoom
                ev0 = self._zoom_events[0]
                t1 = ev0["t_start"] + self.ZOOM_ENTER_DUR + 0.5
                # Frame en el pico del segundo zoom (si hay) o al final
                if len(self._zoom_events) > 1:
                    ev1 = self._zoom_events[1]
                    t2 = ev1["t_start"] + self.ZOOM_ENTER_DUR + 0.5
                else:
                    t2 = self.duration * 0.75
                times = [t0, t1, t2]

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []

        for i, t in enumerate(times):
            t_clamped = max(0.0, min(self.duration - 0.1, t))
            try:
                frame = self.get_frame(t_clamped, w, h)
                out_path = str(out_dir / f"zoom_preview_frame{i+1}_{t_clamped:.1f}s.png")
                frame.save(out_path)
                self.logger.info(f"Preview frame {i+1} guardado: {out_path}")
                paths.append(out_path)
            except Exception as e:
                self.logger.warning(f"capture_preview_frames t={t}: {e}")

        return paths
