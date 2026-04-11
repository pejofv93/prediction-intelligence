"""
Generador de avatar_base.png para CryptoVerdad NEXUS
800x900px RGBA con fondo transparente
"""
from PIL import Image, ImageDraw, ImageFilter
import math

W, H = 800, 900
img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# ─── PALETA ───────────────────────────────────────────────────────────
SKIN        = (212, 165, 116, 255)
SKIN_DARK   = (176, 128,  80, 255)
SKIN_SHADOW = (192, 144,  96, 255)
HAIR        = ( 44,  44,  44, 255)
HAIR_GRAY   = (168, 168, 168, 255)
BROW        = ( 26,  26,  26, 255)
EYE_IRIS    = ( 74,  74, 138, 255)
EYE_PUPIL   = (  8,   8,   8, 255)
EYE_WHITE   = (245, 245, 245, 255)
SHIRT       = ( 26,  26,  46, 255)
SHIRT_LIGHT = ( 38,  38,  68, 255)
BEARD_DOT   = ( 74,  58,  42, 200)
CV_ORANGE   = (247, 147,  26, 255)
WHITE       = (255, 255, 255, 255)
LIPS        = (185, 120,  85, 255)
LIPS_DARK   = (160, 100,  70, 255)

# ─── HELPERS ──────────────────────────────────────────────────────────
def ellipse(draw, cx, cy, rx, ry, fill, outline=None, width=1):
    draw.ellipse([cx-rx, cy-ry, cx+rx, cy+ry], fill=fill,
                 outline=outline, width=width)

def poly(draw, points, fill, outline=None):
    draw.polygon(points, fill=fill, outline=outline)

# ═══════════════════════════════════════════════════════════════════════
# CUERPO / TORSO — comienza en y=580, ancho hombros 380px
# ═══════════════════════════════════════════════════════════════════════
TORSO_TOP   = 600
TORSO_CX    = 400   # ligeramente hacia la izquierda respecto al centro de 800 → usar 385
SHOULDER_W  = 200   # mitad del ancho hombros

# Torso principal (trapezoide)
torso_pts = [
    (TORSO_CX - SHOULDER_W,      TORSO_TOP),
    (TORSO_CX + SHOULDER_W + 20, TORSO_TOP),
    (TORSO_CX + 180,             H + 10),
    (TORSO_CX - 180,             H + 10),
]
poly(draw, torso_pts, SHIRT)

# Sombra lateral izquierda del torso
shadow_torso = [
    (TORSO_CX - SHOULDER_W,      TORSO_TOP),
    (TORSO_CX - SHOULDER_W + 60, TORSO_TOP),
    (TORSO_CX - 120,             H + 10),
    (TORSO_CX - 180,             H + 10),
]
poly(draw, shadow_torso, (20, 20, 40, 180))

# Línea central camisa (pliegue)
draw.line([(TORSO_CX, TORSO_TOP + 30), (TORSO_CX, H)],
          fill=(15, 15, 35, 200), width=3)

# ─── CUELLO DE CAMISA (solapa abierta V) ──────────────────────────────
collar_pts_left = [
    (TORSO_CX - 30, TORSO_TOP),
    (TORSO_CX,      TORSO_TOP + 60),
    (TORSO_CX - 80, TORSO_TOP),
]
poly(draw, collar_pts_left, (20, 20, 40, 255))

collar_pts_right = [
    (TORSO_CX + 30, TORSO_TOP),
    (TORSO_CX,      TORSO_TOP + 60),
    (TORSO_CX + 80, TORSO_TOP),
]
poly(draw, collar_pts_right, (18, 18, 38, 255))

# Interior cuello (triángulo piel visible)
inner_collar = [
    (TORSO_CX - 25, TORSO_TOP + 5),
    (TORSO_CX + 25, TORSO_TOP + 5),
    (TORSO_CX,      TORSO_TOP + 55),
]
poly(draw, inner_collar, SKIN)

# ─── LOGO "CV" en el pecho ────────────────────────────────────────────
logo_cx, logo_cy = TORSO_CX + 80, TORSO_TOP + 100
ellipse(draw, logo_cx, logo_cy, 22, 22, CV_ORANGE)
ellipse(draw, logo_cx, logo_cy, 19, 19, SHIRT)
# Texto CV — dibujado a mano con rectángulos pequeños
# Letra C
draw.arc([logo_cx-12, logo_cy-8, logo_cx-2, logo_cy+8],
         start=40, end=320, fill=CV_ORANGE, width=2)
# Letra V (dos líneas)
draw.line([(logo_cx+2, logo_cy-8), (logo_cx+7, logo_cy+7)],
          fill=CV_ORANGE, width=2)
draw.line([(logo_cx+12, logo_cy-8), (logo_cx+7, logo_cy+7)],
          fill=CV_ORANGE, width=2)

# ═══════════════════════════════════════════════════════════════════════
# BRAZO IZQUIERDO — relajado al costado, codo doblado, mano en cadera
# ═══════════════════════════════════════════════════════════════════════
# Hombro izquierdo → codo
arm_l_shoulder = (TORSO_CX - SHOULDER_W + 20, TORSO_TOP + 20)
arm_l_elbow    = (TORSO_CX - SHOULDER_W - 40, TORSO_TOP + 180)
arm_l_hand     = (TORSO_CX - SHOULDER_W + 10, TORSO_TOP + 200)

# Brazo superior izquierdo (rectángulo inclinado)
arm_l_upper = [
    (arm_l_shoulder[0] - 32, arm_l_shoulder[1]),
    (arm_l_shoulder[0] + 10, arm_l_shoulder[1] + 5),
    (arm_l_elbow[0] + 25,    arm_l_elbow[1]),
    (arm_l_elbow[0] - 15,    arm_l_elbow[1]),
]
poly(draw, arm_l_upper, SHIRT)

# Antebrazo izquierdo (mano en cadera)
arm_l_fore = [
    (arm_l_elbow[0] - 10, arm_l_elbow[1]),
    (arm_l_elbow[0] + 25, arm_l_elbow[1]),
    (arm_l_hand[0] + 25,  arm_l_hand[1]),
    (arm_l_hand[0] - 10,  arm_l_hand[1]),
]
poly(draw, arm_l_fore, SHIRT)

# Mano izquierda
ellipse(draw, arm_l_hand[0]+8, arm_l_hand[1]+10, 20, 15, SKIN)

# ═══════════════════════════════════════════════════════════════════════
# BRAZO DERECHO — extendido señalando a la derecha ~30° abajo
# ═══════════════════════════════════════════════════════════════════════
arm_r_shoulder = (TORSO_CX + SHOULDER_W + 10, TORSO_TOP + 20)

# Ángulo 30° hacia abajo-derecha
angle_rad = math.radians(25)
arm_length_upper = 160
arm_length_fore  = 140

arm_r_elbow = (
    int(arm_r_shoulder[0] + arm_length_upper * math.cos(angle_rad)),
    int(arm_r_shoulder[1] + arm_length_upper * math.sin(angle_rad)),
)
arm_r_wrist = (
    int(arm_r_elbow[0] + arm_length_fore * math.cos(math.radians(20))),
    int(arm_r_elbow[1] + arm_length_fore * math.sin(math.radians(20))),
)

# Brazo superior derecho
perp_angle = angle_rad + math.pi / 2
hw = 22  # half-width
p1 = (int(arm_r_shoulder[0] - hw*math.cos(perp_angle)),
      int(arm_r_shoulder[1] - hw*math.sin(perp_angle)))
p2 = (int(arm_r_shoulder[0] + hw*math.cos(perp_angle)),
      int(arm_r_shoulder[1] + hw*math.sin(perp_angle)))
p3 = (int(arm_r_elbow[0] + 18*math.cos(perp_angle)),
      int(arm_r_elbow[1] + 18*math.sin(perp_angle)))
p4 = (int(arm_r_elbow[0] - 18*math.cos(perp_angle)),
      int(arm_r_elbow[1] - 18*math.sin(perp_angle)))
poly(draw, [p1, p2, p3, p4], SHIRT)

# Antebrazo derecho (manga ligeramente subida → piel visible)
fore_angle = math.radians(20)
perp2 = fore_angle + math.pi / 2
hw2 = 18
f1 = (int(arm_r_elbow[0] - hw2*math.cos(perp2)),
      int(arm_r_elbow[1] - hw2*math.sin(perp2)))
f2 = (int(arm_r_elbow[0] + hw2*math.cos(perp2)),
      int(arm_r_elbow[1] + hw2*math.sin(perp2)))
f3 = (int(arm_r_wrist[0] + 14*math.cos(perp2)),
      int(arm_r_wrist[1] + 14*math.sin(perp2)))
f4 = (int(arm_r_wrist[0] - 14*math.cos(perp2)),
      int(arm_r_wrist[1] - 14*math.sin(perp2)))
poly(draw, [f1, f2, f3, f4], SKIN)
# manga sobre antebrazo
poly(draw, [f1, f2,
            (f2[0]-10, f2[1]-30),
            (f1[0]-10, f1[1]-30)], SHIRT)

# Mano derecha — puño con dedo índice extendido
hand_cx = arm_r_wrist[0] + 20
hand_cy = arm_r_wrist[1] + 5

# Puño
ellipse(draw, hand_cx, hand_cy, 20, 16, SKIN)
# Dedo índice extendido (apuntando a la derecha)
finger_pts = [
    (hand_cx + 10, hand_cy - 6),
    (hand_cx + 55, hand_cy - 4),
    (hand_cx + 60, hand_cy + 2),   # punta redondeada
    (hand_cx + 55, hand_cy + 8),
    (hand_cx + 10, hand_cy + 8),
]
poly(draw, finger_pts, SKIN)
# Uña
ellipse(draw, hand_cx + 54, hand_cy + 2, 5, 5, (230, 200, 170, 255))
# Nudillos leves
for i in range(3):
    kx = hand_cx - 6 + i * 8
    ellipse(draw, kx, hand_cy - 12, 4, 3, SKIN_SHADOW)

# ═══════════════════════════════════════════════════════════════════════
# CUELLO
# ═══════════════════════════════════════════════════════════════════════
NECK_CX = 395
NECK_TOP = 530
NECK_BOT = TORSO_TOP + 10
ellipse(draw, NECK_CX, (NECK_TOP + NECK_BOT)//2,
        38, (NECK_BOT - NECK_TOP)//2 + 5, SKIN)
# Sombra lateral cuello
neck_shadow = [
    (NECK_CX - 38, NECK_TOP + 15),
    (NECK_CX - 20, NECK_TOP + 15),
    (NECK_CX - 18, NECK_BOT),
    (NECK_CX - 38, NECK_BOT),
]
poly(draw, neck_shadow, SKIN_SHADOW)

# ═══════════════════════════════════════════════════════════════════════
# CABEZA
# ═══════════════════════════════════════════════════════════════════════
HEAD_CX  = 393
HEAD_CY  = 310
HEAD_RX  = 155   # radio horizontal (cara ancha)
HEAD_RY  = 175   # radio vertical

# Sombra izquierda cara (iluminación desde arriba-derecha)
shadow_face = img.copy()
sd = ImageDraw.Draw(shadow_face)
sd.ellipse([HEAD_CX - HEAD_RX, HEAD_CY - HEAD_RY,
            HEAD_CX + HEAD_RX, HEAD_CY + HEAD_RY],
           fill=SKIN_SHADOW)
# aplicar solo lado izquierdo
face_shadow_mask = Image.new("L", (W, H), 0)
msk = ImageDraw.Draw(face_shadow_mask)
msk.rectangle([0, 0, HEAD_CX - 20, H], fill=180)
img.paste(shadow_face, (0, 0), face_shadow_mask)

# Cara principal
draw.ellipse([HEAD_CX - HEAD_RX, HEAD_CY - HEAD_RY,
              HEAD_CX + HEAD_RX, HEAD_CY + HEAD_RY],
             fill=SKIN)

# Mandíbula cuadrada — extender parte inferior
jaw_pts = [
    (HEAD_CX - HEAD_RX + 30, HEAD_CY + 80),
    (HEAD_CX + HEAD_RX - 30, HEAD_CY + 80),
    (HEAD_CX + HEAD_RX - 50, HEAD_CY + HEAD_RY + 15),
    (HEAD_CX - HEAD_RX + 50, HEAD_CY + HEAD_RY + 15),
]
poly(draw, jaw_pts, SKIN)

# Sombra mandíbula (definición)
jaw_shadow = [
    (HEAD_CX - HEAD_RX + 30, HEAD_CY + 100),
    (HEAD_CX - HEAD_RX + 55, HEAD_CY + HEAD_RY + 18),
    (HEAD_CX - HEAD_RX + 80, HEAD_CY + HEAD_RY + 18),
    (HEAD_CX - HEAD_RX + 50, HEAD_CY + 100),
]
poly(draw, jaw_shadow, SKIN_DARK)

# Mejilla derecha sombra sutil
ellipse(draw, HEAD_CX + 80, HEAD_CY + 60, 35, 20,
        (200, 155, 105, 60))

# ─── OREJAS ───────────────────────────────────────────────────────────
# Oreja izquierda
ear_l_cx = HEAD_CX - HEAD_RX + 12
ear_l_cy = HEAD_CY + 20
ellipse(draw, ear_l_cx, ear_l_cy, 22, 28, SKIN)
ellipse(draw, ear_l_cx + 5, ear_l_cy, 14, 20, SKIN_SHADOW)
# Hélix oreja izquierda
draw.arc([ear_l_cx-18, ear_l_cy-24, ear_l_cx+4, ear_l_cy+24],
         start=200, end=340, fill=SKIN_DARK, width=2)

# Oreja derecha
ear_r_cx = HEAD_CX + HEAD_RX - 12
ear_r_cy = HEAD_CY + 20
ellipse(draw, ear_r_cx, ear_r_cy, 22, 28, SKIN)
ellipse(draw, ear_r_cx - 5, ear_r_cy, 14, 20, SKIN_SHADOW)
draw.arc([ear_r_cx-4, ear_r_cy-24, ear_r_cx+18, ear_r_cy+24],
         start=200, end=340, fill=SKIN_DARK, width=2)

# ─── PELO ─────────────────────────────────────────────────────────────
# Base pelo oscuro — cubre parte superior y lados
hair_top = [
    (HEAD_CX - HEAD_RX + 5,  HEAD_CY - HEAD_RY + 10),
    (HEAD_CX - HEAD_RX + 20, HEAD_CY - HEAD_RY - 30),
    (HEAD_CX - 60,            HEAD_CY - HEAD_RY - 60),
    (HEAD_CX,                 HEAD_CY - HEAD_RY - 70),
    (HEAD_CX + 60,            HEAD_CY - HEAD_RY - 60),
    (HEAD_CX + HEAD_RX - 20, HEAD_CY - HEAD_RY - 30),
    (HEAD_CX + HEAD_RX - 5,  HEAD_CY - HEAD_RY + 10),
    (HEAD_CX + HEAD_RX - 30, HEAD_CY - HEAD_RY + 35),
    (HEAD_CX - HEAD_RX + 30, HEAD_CY - HEAD_RY + 35),
]
poly(draw, hair_top, HAIR)

# Volumen pelo arriba (ligero relieve)
hair_vol = [
    (HEAD_CX - 80, HEAD_CY - HEAD_RY - 55),
    (HEAD_CX - 20, HEAD_CY - HEAD_RY - 80),
    (HEAD_CX + 40, HEAD_CY - HEAD_RY - 75),
    (HEAD_CX + 80, HEAD_CY - HEAD_RY - 50),
    (HEAD_CX + 40, HEAD_CY - HEAD_RY - 40),
    (HEAD_CX - 40, HEAD_CY - HEAD_RY - 40),
]
poly(draw, hair_vol, (55, 55, 55, 255))

# Rasurado lados (más oscuro, muy corto)
# Lado izquierdo
side_l = [
    (HEAD_CX - HEAD_RX + 5,  HEAD_CY - HEAD_RY + 10),
    (HEAD_CX - HEAD_RX + 30, HEAD_CY - HEAD_RY + 35),
    (HEAD_CX - HEAD_RX + 35, HEAD_CY + 20),
    (HEAD_CX - HEAD_RX + 10, HEAD_CY + 20),
]
poly(draw, side_l, (30, 30, 30, 255))

# Lado derecho rasurado
side_r = [
    (HEAD_CX + HEAD_RX - 5,  HEAD_CY - HEAD_RY + 10),
    (HEAD_CX + HEAD_RX - 30, HEAD_CY - HEAD_RY + 35),
    (HEAD_CX + HEAD_RX - 35, HEAD_CY + 20),
    (HEAD_CX + HEAD_RX - 10, HEAD_CY + 20),
]
poly(draw, side_r, (30, 30, 30, 255))

# Canas en las sienes (mechones grises)
# Sien izquierda
for i in range(6):
    sx = HEAD_CX - HEAD_RX + 28 + i * 4
    sy = HEAD_CY - 50 + i * 6
    draw.line([(sx, sy), (sx - 8, sy + 12)], fill=HAIR_GRAY, width=2)

# Sien derecha
for i in range(6):
    sx = HEAD_CX + HEAD_RX - 28 - i * 4
    sy = HEAD_CY - 50 + i * 6
    draw.line([(sx, sy), (sx + 8, sy + 12)], fill=HAIR_GRAY, width=2)

# Línea de nacimiento del pelo
draw.arc([HEAD_CX - HEAD_RX + 5, HEAD_CY - HEAD_RY + 30,
          HEAD_CX + HEAD_RX - 5, HEAD_CY - HEAD_RY + 60],
         start=200, end=340, fill=(22, 22, 22, 255), width=2)

# ─── CEJAS GRUESAS SERIAS ─────────────────────────────────────────────
# Ceja izquierda (más alta en exterior → ceño fruncido leve)
brow_l = [
    (HEAD_CX - 130, HEAD_CY - 75),
    (HEAD_CX - 60,  HEAD_CY - 85),
    (HEAD_CX - 58,  HEAD_CY - 72),
    (HEAD_CX - 130, HEAD_CY - 62),
]
poly(draw, brow_l, BROW)

# Ceja derecha (simétrica pero espejada)
brow_r = [
    (HEAD_CX + 58,  HEAD_CY - 85),
    (HEAD_CX + 130, HEAD_CY - 75),
    (HEAD_CX + 130, HEAD_CY - 62),
    (HEAD_CX + 58,  HEAD_CY - 72),
]
poly(draw, brow_r, BROW)

# Sombra bajo cejas (profundidad)
for bx, by in [(HEAD_CX - 100, HEAD_CY - 62), (HEAD_CX + 70, HEAD_CY - 62)]:
    ellipse(draw, bx, by, 35, 8, (160, 100, 60, 80))

# ─── OJOS ─────────────────────────────────────────────────────────────
# Ojo izquierdo (para el espectador)
eye_l_cx = HEAD_CX - 68
eye_l_cy = HEAD_CY - 30

# Párpado superior (forma almendrada)
eye_l_pts = [
    (eye_l_cx - 38, eye_l_cy + 2),
    (eye_l_cx - 20, eye_l_cy - 18),
    (eye_l_cx + 10, eye_l_cy - 22),
    (eye_l_cx + 38, eye_l_cy - 5),
    (eye_l_cx + 38, eye_l_cy + 12),
    (eye_l_cx - 38, eye_l_cy + 12),
]
poly(draw, eye_l_pts, EYE_WHITE)
# Iris izquierdo (mirada ligeramente hacia la derecha → desplazado +5)
ellipse(draw, eye_l_cx + 5, eye_l_cy + 2, 16, 15, EYE_IRIS)
ellipse(draw, eye_l_cx + 5, eye_l_cy + 2,  9,  9, EYE_PUPIL)
# Brillo ojo izquierdo
ellipse(draw, eye_l_cx + 9, eye_l_cy - 3,  4,  4, (255, 255, 255, 200))
# Contorno ojo
draw.arc([eye_l_cx - 38, eye_l_cy - 18,
          eye_l_cx + 38, eye_l_cy + 12],
         start=190, end=360, fill=(20, 20, 20, 255), width=2)
# Párpado inferior
draw.arc([eye_l_cx - 38, eye_l_cy - 5,
          eye_l_cx + 38, eye_l_cy + 18],
         start=0, end=180, fill=(180, 130, 90, 180), width=1)

# Ojo derecho
eye_r_cx = HEAD_CX + 68
eye_r_cy = HEAD_CY - 30
eye_r_pts = [
    (eye_r_cx - 38, eye_r_cy - 5),
    (eye_r_cx - 10, eye_r_cy - 22),
    (eye_r_cx + 20, eye_r_cy - 18),
    (eye_r_cx + 38, eye_r_cy + 2),
    (eye_r_cx + 38, eye_r_cy + 12),
    (eye_r_cx - 38, eye_r_cy + 12),
]
poly(draw, eye_r_pts, EYE_WHITE)
# Iris derecho (también desplazado +5 → mirando a la derecha)
ellipse(draw, eye_r_cx + 5, eye_r_cy + 2, 16, 15, EYE_IRIS)
ellipse(draw, eye_r_cx + 5, eye_r_cy + 2,  9,  9, EYE_PUPIL)
ellipse(draw, eye_r_cx + 9, eye_r_cy - 3,  4,  4, (255, 255, 255, 200))
draw.arc([eye_r_cx - 38, eye_r_cy - 18,
          eye_r_cx + 38, eye_r_cy + 12],
         start=190, end=360, fill=(20, 20, 20, 255), width=2)
draw.arc([eye_r_cx - 38, eye_r_cy - 5,
          eye_r_cx + 38, eye_r_cy + 18],
         start=0, end=180, fill=(180, 130, 90, 180), width=1)

# Bolsas/líneas bajo ojos (expresión seria, adulto)
draw.arc([eye_l_cx - 30, eye_l_cy + 10,
          eye_l_cx + 30, eye_l_cy + 26],
         start=0, end=180, fill=(180, 130, 90, 100), width=2)
draw.arc([eye_r_cx - 30, eye_r_cy + 10,
          eye_r_cx + 30, eye_r_cy + 26],
         start=0, end=180, fill=(180, 130, 90, 100), width=2)

# ─── NARIZ ────────────────────────────────────────────────────────────
nose_cx = HEAD_CX - 2  # ligeramente hacia la izquierda (perspectiva)
nose_top_y = HEAD_CY - 10
nose_bot_y = HEAD_CY + 40

# Puente de la nariz (líneas laterales)
draw.line([(nose_cx - 12, nose_top_y), (nose_cx - 16, nose_bot_y - 10)],
          fill=SKIN_DARK, width=2)
draw.line([(nose_cx + 12, nose_top_y), (nose_cx + 16, nose_bot_y - 10)],
          fill=SKIN_DARK, width=2)

# Punta nariz
ellipse(draw, nose_cx, nose_bot_y, 22, 14, SKIN)
# Fosas nasales
ellipse(draw, nose_cx - 18, nose_bot_y + 2, 9, 7, SKIN_DARK)
ellipse(draw, nose_cx + 16, nose_bot_y + 2, 9, 7, SKIN_DARK)
# Sombra bajo nariz
ellipse(draw, nose_cx, nose_bot_y + 10, 28, 6, (160, 110, 70, 120))

# ─── BOCA SERIA ───────────────────────────────────────────────────────
mouth_cx = HEAD_CX - 2
mouth_y  = HEAD_CY + 80

# Labio superior
lip_up = [
    (mouth_cx - 35, mouth_y),
    (mouth_cx - 15, mouth_y - 8),
    (mouth_cx,      mouth_y - 5),
    (mouth_cx + 15, mouth_y - 8),
    (mouth_cx + 35, mouth_y),
    (mouth_cx,      mouth_y + 4),
]
poly(draw, lip_up, LIPS)

# Labio inferior (más grueso)
lip_dn = [
    (mouth_cx - 35, mouth_y + 2),
    (mouth_cx,      mouth_y + 4),
    (mouth_cx + 35, mouth_y + 2),
    (mouth_cx + 28, mouth_y + 16),
    (mouth_cx,      mouth_y + 20),
    (mouth_cx - 28, mouth_y + 16),
]
poly(draw, lip_dn, LIPS)

# Línea firme de boca (expresión seria)
draw.line([(mouth_cx - 35, mouth_y), (mouth_cx + 35, mouth_y)],
          fill=LIPS_DARK, width=2)
# Comisuras leves
draw.point((mouth_cx - 35, mouth_y), fill=(140, 90, 60, 255))
draw.point((mouth_cx + 35, mouth_y), fill=(140, 90, 60, 255))

# Surco naso-labial
draw.arc([mouth_cx - 50, mouth_y - 30,
          mouth_cx - 20, mouth_y + 10],
         start=30, end=150, fill=SKIN_DARK, width=1)
draw.arc([mouth_cx + 20, mouth_y - 30,
          mouth_cx + 50, mouth_y + 10],
         start=30, end=150, fill=SKIN_DARK, width=1)

# ─── BARBA DE 3 DÍAS ──────────────────────────────────────────────────
import random
random.seed(42)

# Zona de barba: mejillas inferiores, barbilla, labio superior
beard_zones = [
    # (cx_range, cy_range, cantidad)
    (HEAD_CX - 100, HEAD_CX + 98,  HEAD_CY + 55,  HEAD_CY + 100, 80),   # mejillas
    (HEAD_CX - 70,  HEAD_CX + 68,  HEAD_CY + 100, HEAD_CY + 145, 60),   # barbilla
    (HEAD_CX - 30,  HEAD_CX + 28,  HEAD_CY + 55,  HEAD_CY + 72,  25),   # bigote
]
for x0, x1, y0, y1, n in beard_zones:
    for _ in range(n):
        bx = random.randint(x0, x1)
        by = random.randint(y0, y1)
        # Solo dentro de la cara (verificación aproximada)
        dx = (bx - HEAD_CX) / HEAD_RX
        dy = (by - HEAD_CY) / (HEAD_RY + 20)
        if dx*dx + dy*dy < 0.95:
            draw.ellipse([bx-1, by-1, bx+1, by+1],
                         fill=BEARD_DOT)

# ─── ILUMINACIÓN FINAL: highlight derecho ────────────────────────────
# Brillo suave en lado derecho de la cara
highlight = Image.new("RGBA", (W, H), (0, 0, 0, 0))
hd = ImageDraw.Draw(highlight)
hd.ellipse([HEAD_CX + 30, HEAD_CY - 100,
            HEAD_CX + HEAD_RX + 20, HEAD_CY + 80],
           fill=(255, 230, 180, 30))
img = Image.alpha_composite(img, highlight)
draw = ImageDraw.Draw(img)

# ─── SUAVIZADO LIGERO ────────────────────────────────────────────────
# Aplicar un suavizado muy leve para que las formas sean menos pixeladas
img_smooth = img.filter(ImageFilter.SMOOTH_MORE)
# Preservar transparencia
img = Image.composite(img_smooth, img, img_smooth.split()[3])

# ─── GUARDAR ─────────────────────────────────────────────────────────
out_path = r"C:\Users\Usuario\nexus\assets\avatar_base.png"
img.save(out_path, "PNG")
print(f"Avatar guardado en: {out_path}")
print(f"Tamaño: {img.size}, Modo: {img.mode}")
