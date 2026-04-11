"""
Genera assets/studio_background.png — Fondo estudio CryptoVerdad
1920x1080px RGB
"""
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import math
import os

W, H = 1920, 1080
out_path = os.path.join(os.path.dirname(__file__), "studio_background.png")

# ─────────────────────────────────────────────
# HELPER: color con alpha sobre imagen base
# ─────────────────────────────────────────────
def blend_rect(base: Image.Image, x0, y0, x1, y1, color_rgb, alpha: float):
    """Pinta un rectángulo semitransparente sobre base (RGBA interno)."""
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    a = int(alpha * 255)
    d.rectangle([x0, y0, x1, y1], fill=(*color_rgb, a))
    base_rgba = base.convert("RGBA")
    merged = Image.alpha_composite(base_rgba, overlay)
    return merged.convert("RGB")


def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


# ─────────────────────────────────────────────
# 1. FONDO BASE — gradiente vertical
# ─────────────────────────────────────────────
img = Image.new("RGB", (W, H))
draw = ImageDraw.Draw(img)

top_color    = (10, 10, 10)       # #0A0A0A
bottom_color = (26, 26, 46)       # #1A1A2E
center_lift  = 18                 # cuánto aclarar en el centro (efecto vignette inv.)

pixels = img.load()
for y in range(H):
    t = y / (H - 1)
    base = lerp_color(top_color, bottom_color, t)
    # Vignette invertido: más claro en el centro horizontal
    for x in range(W):
        cx = abs(x - W / 2) / (W / 2)          # 0 en centro, 1 en bordes
        brightness = int(center_lift * (1 - cx * cx))
        r = min(255, base[0] + brightness)
        g = min(255, base[1] + brightness)
        b = min(255, base[2] + brightness)
        pixels[x, y] = (r, g, b)

draw = ImageDraw.Draw(img)

# ─────────────────────────────────────────────
# 2. REJILLA DE PISO en perspectiva (zona baja)
# ─────────────────────────────────────────────
floor_y = int(H * 0.78)
vp_x, vp_y = W // 2, floor_y          # punto de fuga

grid_color = (30, 30, 45)

# Líneas horizontales
for i in range(8):
    t = i / 7
    y_line = int(floor_y + (H - floor_y) * t)
    draw.line([(0, y_line), (W, y_line)], fill=grid_color, width=1)

# Líneas radiales desde el punto de fuga
num_radial = 18
for i in range(num_radial + 1):
    angle = math.radians(-70 + 140 * i / num_radial)
    length = 900
    ex = int(vp_x + length * math.cos(angle))
    ey = int(vp_y + length * math.sin(angle))
    draw.line([(vp_x, vp_y), (ex, ey)], fill=grid_color, width=1)

# ─────────────────────────────────────────────
# 3. ILUMINACIÓN AMBIENTAL lateral (antes de monitores)
# ─────────────────────────────────────────────
# Gradiente naranja derecha
for x in range(320):
    alpha = 0.38 * (1 - x / 320) ** 2
    a = int(alpha * 255)
    overlay = Image.new("RGBA", (1, H), (247, 147, 26, a))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(overlay, (W - 1 - x, 0), overlay)
    img = img_rgba.convert("RGB")

# Gradiente azul izquierda
for x in range(280):
    alpha = 0.28 * (1 - x / 280) ** 2
    a = int(alpha * 255)
    overlay = Image.new("RGBA", (1, H), (68, 68, 255, a))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(overlay, (x, 0), overlay)
    img = img_rgba.convert("RGB")

draw = ImageDraw.Draw(img)

# ─────────────────────────────────────────────
# 4. MONITORES EN LA PARED
# ─────────────────────────────────────────────

def draw_monitor(draw_obj, img_obj, mx, my, mw, mh,
                 content="price", label=""):
    """Dibuja un monitor con marco, contenido y soporte."""
    bord = (51, 51, 85)
    inner_bg = (13, 17, 23)
    bezel = 6

    # Marco exterior
    draw_obj.rectangle([mx, my, mx + mw, my + mh],
                       outline=bord, width=2, fill=(20, 22, 32))
    # Pantalla interior
    draw_obj.rectangle([mx + bezel, my + bezel,
                         mx + mw - bezel, my + mh - bezel],
                        fill=inner_bg)

    sx = mx + bezel + 2
    sy = my + bezel + 2
    sw = mw - bezel * 2 - 4
    sh = mh - bezel * 2 - 4

    if content == "price":
        # BTC/USD label
        draw_obj.text((sx + 4, sy + 4), label or "BTC/USD",
                      fill=(200, 200, 200))
        # Línea de precio simulada (zigzag que sube)
        points = []
        n = 30
        base_price_y = sy + sh - 20
        for i in range(n):
            px_ = sx + 4 + int(i * (sw - 8) / (n - 1))
            wave = math.sin(i * 0.6) * 14 + math.sin(i * 1.3) * 8
            trend = -i * (sh * 0.45) / n
            py_ = int(base_price_y + wave + trend)
            points.append((px_, py_))
        # Área bajo la curva
        area = [(sx + 4, base_price_y)] + points + [(points[-1][0], base_price_y)]
        draw_obj.polygon(area, fill=(247, 147, 26, 40) if False else (20, 12, 0))
        # Línea naranja
        for i in range(len(points) - 1):
            draw_obj.line([points[i], points[i + 1]],
                          fill=(247, 147, 26), width=2)
        # Punto final
        draw_obj.ellipse([points[-1][0] - 4, points[-1][1] - 4,
                           points[-1][0] + 4, points[-1][1] + 4],
                          fill=(247, 147, 26))

    elif content == "green_lines":
        # Líneas de datos verdes estilo terminal
        for row in range(6):
            y_r = sy + 8 + row * (sh // 7)
            w_r = int(sw * (0.4 + 0.5 * abs(math.sin(row * 1.7))))
            draw_obj.rectangle([sx + 4, y_r, sx + 4 + w_r, y_r + 3],
                                fill=(76, 175, 80))
        draw_obj.text((sx + 4, sy + 4), label or "ETH/USD",
                      fill=(150, 220, 150))

    elif content == "small_chart":
        # Gráfico pequeño
        pts = []
        for i in range(12):
            px_ = sx + 4 + int(i * (sw - 8) / 11)
            py_ = sy + sh // 2 + int(math.sin(i * 0.9) * sh * 0.28)
            pts.append((px_, py_))
        for i in range(len(pts) - 1):
            draw_obj.line([pts[i], pts[i + 1]],
                          fill=(100, 149, 237), width=2)
        draw_obj.text((sx + 4, sy + 4), label or "SOL/USD",
                      fill=(150, 180, 255))

    elif content == "partial":
        draw_obj.text((sx + 4, sy + 4), label or "ADA",
                      fill=(150, 150, 180))
        for row in range(4):
            y_r = sy + 18 + row * 14
            draw_obj.rectangle([sx + 2, y_r, sx + sw - 2, y_r + 5],
                                fill=(30, 35, 55))

    # Soporte del monitor
    mid_x = mx + mw // 2
    bot_y = my + mh
    draw_obj.rectangle([mid_x - 10, bot_y, mid_x + 10, bot_y + 30],
                        fill=(50, 50, 60))
    draw_obj.rectangle([mid_x - 28, bot_y + 28, mid_x + 28, bot_y + 36],
                        fill=(60, 60, 70))

    # Reflejo inferior (línea clara semitransparente)
    draw_obj.line([(mx + 4, my + mh - bezel - 2),
                   (mx + mw - 4, my + mh - bezel - 2)],
                  fill=(80, 80, 100), width=1)

    # Glow detrás del monitor (halo naranja suave)
    return img_obj


# Monitor 1 — grande, centro-derecha
m1_x, m1_y, m1_w, m1_h = 880, 130, 380, 220
draw_monitor(draw, img, m1_x, m1_y, m1_w, m1_h, content="price", label="BTC/USD")

# Monitor 2 — mediano, derecha
m2_x, m2_y, m2_w, m2_h = 1310, 180, 260, 160
draw_monitor(draw, img, m2_x, m2_y, m2_w, m2_h, content="green_lines", label="ETH/USD")

# Monitor 3 — pequeño, arriba derecha
m3_x, m3_y, m3_w, m3_h = 1620, 140, 180, 120
draw_monitor(draw, img, m3_x, m3_y, m3_w, m3_h, content="small_chart", label="SOL/USD")

# Monitor 4 — izquierda, parcialmente visible (solo mitad derecha)
m4_w, m4_h = 200, 150
m4_x, m4_y = -m4_w // 2, 200       # la mitad fuera del canvas
draw_monitor(draw, img, m4_x, m4_y, m4_w, m4_h, content="partial", label="ADA")

# Glow naranja suave detrás del monitor 1
img = blend_rect(img, m1_x - 30, m1_y - 20, m1_x + m1_w + 30, m1_y + m1_h + 40,
                 (247, 147, 26), 0.08)
# Glow azul suave detrás del monitor 2
img = blend_rect(img, m2_x - 20, m2_y - 15, m2_x + m2_w + 20, m2_y + m2_h + 30,
                 (68, 100, 220), 0.07)

draw = ImageDraw.Draw(img)

# ─────────────────────────────────────────────
# 5. ESCRITORIO (franja inferior ~20%)
# ─────────────────────────────────────────────
desk_y = int(H * 0.80)
desk_color = (30, 30, 42)

# Superficie del escritorio
draw.rectangle([0, desk_y, W, H], fill=desk_color)
# Borde naranja sutil en el borde superior del escritorio
draw.line([(0, desk_y), (W, desk_y)], fill=(247, 147, 26), width=1)

# Profundidad/bisel del borde frontal del escritorio
draw.rectangle([0, desk_y, W, desk_y + 6], fill=(40, 40, 55))

# ── Teclado mecánico ──
kb_x, kb_y = 680, desk_y + 55
kb_w, kb_h = 520, 100
draw.rectangle([kb_x, kb_y, kb_x + kb_w, kb_y + kb_h],
               fill=(42, 42, 58), outline=(60, 60, 80), width=1)

# Teclas individuales
key_cols, key_rows = 18, 5
key_w = (kb_w - 16) // key_cols
key_h = (kb_h - 12) // key_rows
led_colors = [(247, 147, 26), (68, 68, 255), (247, 147, 26),
              (100, 200, 255), (247, 147, 26)]
for row in range(key_rows):
    for col in range(key_cols):
        kx = kb_x + 8 + col * (key_w + 1)
        ky = kb_y + 6 + row * (key_h + 1)
        draw.rectangle([kx, ky, kx + key_w - 1, ky + key_h - 1],
                        fill=(51, 51, 64), outline=(70, 70, 85), width=1)
        # LED glow sutil en tecla
        led_c = led_colors[col % len(led_colors)]
        draw.rectangle([kx + 1, ky + key_h - 3, kx + key_w - 2, ky + key_h - 2],
                        fill=(led_c[0] // 3, led_c[1] // 3, led_c[2] // 3))

# ── Monitor pequeño frente al teclado ──
sm_x, sm_y, sm_w, sm_h = 820, desk_y - 95, 200, 110
draw.rectangle([sm_x, sm_y, sm_x + sm_w, sm_y + sm_h],
               fill=(20, 22, 32), outline=(51, 51, 85), width=2)
draw.rectangle([sm_x + 5, sm_y + 5, sm_x + sm_w - 5, sm_y + sm_h - 5],
               fill=(13, 17, 23))
# Contenido: precio en verde
draw.text((sm_x + 10, sm_y + 10), "BTC $87,450", fill=(76, 175, 80))
draw.text((sm_x + 10, sm_y + 30), "+2.34%", fill=(76, 175, 80))
draw.rectangle([sm_x + sm_w // 2 - 5, sm_y + sm_h, sm_x + sm_w // 2 + 5, sm_y + sm_h + 20],
               fill=(50, 50, 60))

# ── Taza de café (esquina izquierda) ──
cup_cx, cup_cy = 200, desk_y + 75
r_cup = 32
# Cuerpo de la taza
draw.ellipse([cup_cx - r_cup, cup_cy - r_cup // 2,
               cup_cx + r_cup, cup_cy + r_cup // 2],
              fill=(80, 50, 30), outline=(100, 65, 40), width=2)
draw.ellipse([cup_cx - r_cup + 4, cup_cy - r_cup // 2 + 4,
               cup_cx + r_cup - 4, cup_cy - r_cup // 2 + 14],
              fill=(50, 30, 15))
# Asa
draw.arc([cup_cx + r_cup - 4, cup_cy - 14,
           cup_cx + r_cup + 18, cup_cy + 14],
          start=320, end=220, fill=(100, 65, 40), width=3)
# Vapor
for i in range(3):
    vx = cup_cx - 10 + i * 10
    draw.line([(vx, cup_cy - r_cup // 2 - 5),
               (vx + 3, cup_cy - r_cup // 2 - 18)],
              fill=(100, 100, 110), width=1)

# ── Cables ──
cable_color = (25, 25, 35)
for cx_start, cy_start, cx_end, cy_end in [
    (kb_x + 260, kb_y + kb_h, 900, H),
    (sm_x + 100, sm_y + sm_h + 20, 920, H),
    (1310 + 130, 180 + 160 + 36, 1380, H),
]:
    mid_x_c = (cx_start + cx_end) // 2 + 30
    mid_y_c = (cy_start + cy_end) // 2
    draw.line([(cx_start, cy_start), (mid_x_c, mid_y_c), (cx_end, cy_end)],
              fill=cable_color, width=2)

# ─────────────────────────────────────────────
# 6. LOGO CRYPTOVERDAD (esquina superior izquierda)
# ─────────────────────────────────────────────
logo_x, logo_y = 30, 30

# Bitcoin icon (círculo con B)
btc_cx, btc_cy, btc_r = logo_x + 22, logo_y + 22, 20
draw.ellipse([btc_cx - btc_r, btc_cy - btc_r,
               btc_cx + btc_r, btc_cy + btc_r],
              fill=(247, 147, 26))
draw.text((btc_cx - 7, btc_cy - 10), "B", fill=(10, 10, 10))

# Texto "CryptoVerdad"
try:
    from PIL import ImageFont
    # Intenta cargar fuente del sistema
    font_bold = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 36)
    font_sub  = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 18)
except Exception:
    font_bold = ImageFont.load_default()
    font_sub  = font_bold

draw.text((logo_x + 50, logo_y + 5), "CryptoVerdad",
          fill=(247, 147, 26), font=font_bold)
draw.text((logo_x + 50, logo_y + 46), "Crypto sin humo.",
          fill=(170, 170, 170), font=font_sub)

# ─────────────────────────────────────────────
# 7. BLUR SUAVE en elementos de fondo lejano
#    (monitores 3 y 4 reciben un leve blur simulado
#     con un overlay ligeramente más bajo en contraste)
# ─────────────────────────────────────────────
# Región fondo lejano (zona superior izquierda y derecha extrema)
far_region_left  = img.crop((0, 0, 200, 500))
far_region_right = img.crop((1700, 0, W, 450))
far_region_left  = far_region_left.filter(ImageFilter.GaussianBlur(radius=2))
far_region_right = far_region_right.filter(ImageFilter.GaussianBlur(radius=1))
img.paste(far_region_left,  (0, 0))
img.paste(far_region_right, (1700, 0))

draw = ImageDraw.Draw(img)

# ─────────────────────────────────────────────
# 8. OVERLAY FINAL de ambiente (oscurece bordes)
# ─────────────────────────────────────────────
img = blend_rect(img, 0, 0, W, H, (0, 0, 0), 0.0)   # sin velo extra

# Vignette exterior real (bordes muy oscuros)
vig_steps = 60
for i in range(vig_steps):
    t = i / vig_steps
    alpha = 0.004 * (vig_steps - i)
    # Top
    img = blend_rect(img, 0, i, W, i + 1, (0, 0, 0), alpha)
    # Bottom
    img = blend_rect(img, 0, H - i - 1, W, H - i, (0, 0, 0), alpha)
    # Left
    img = blend_rect(img, i, 0, i + 1, H, (0, 0, 0), alpha)
    # Right
    img = blend_rect(img, W - i - 1, 0, W - i, H, (0, 0, 0), alpha)

# ─────────────────────────────────────────────
# GUARDAR
# ─────────────────────────────────────────────
img.save(out_path, "PNG", optimize=False)
print(f"Guardado: {out_path}")
print(f"Tamano: {img.size}")
