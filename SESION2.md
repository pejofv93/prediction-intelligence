# NEXUS — PROMPT MAESTRO SESIÓN 2
# Pega esto directamente en Claude Code dentro de C:\Users\Usuario\nexus\

Lee el CLAUDE.md de este proyecto antes de empezar.
NEXUS Lite está construido y el pipeline básico funciona.
Esta sesión completa el sistema para que publique vídeos reales automáticamente.

════════════════════════════════════════════
ESTADO ACTUAL (lo que ya funciona — NO tocar)
════════════════════════════════════════════

✅ Pipeline completo: ORÁCULO → FORGE → HERALD → MIND
✅ CALÍOPE genera guiones (SEO Score 85/100)
✅ HERMES genera título, descripción, tags SEO
✅ ECHO genera voz con edge-tts AlvaroNeural
✅ IRIS genera thumbnails A/B con Pillow
✅ YouTube OAuth2 configurado (token.json en raíz)
✅ .env con todas las keys (GROQ, PEXELS, TELEGRAM, YOUTUBE)
✅ Auto-aprobación activada en nexus_core.py
✅ OLYMPUS busca token.json y YOUTUBE_CLIENT_SECRET_PATH

════════════════════════════════════════════
ERRORES CONOCIDOS (corregir primero, en paralelo)
════════════════════════════════════════════

ERROR 1 — HEPHAESTUS fondo negro:
  Causa: PEXELS_API_KEY no llega a hephaestus.py
  Solución DESCARTADA: clips de Pexels (no queremos stock video)
  Solución CORRECTA: ver TAREA 1 abajo (formato noticiario)

ERROR 2 — MoviePy v2 API cambios:
  Reemplazar en todos los archivos que usen MoviePy:
  .set_audio() → .with_audio()
  .set_duration() → .with_duration()
  .set_position() → .with_position()
  .set_fps() → .with_fps()
  .set_start() → .with_start()
  from moviepy.editor import → from moviepy import

ERROR 3 — load_dotenv no cargado en agentes:
  Añadir al inicio de CADA agente que use os.getenv():
  from pathlib import Path
  from dotenv import load_dotenv
  load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')

ERROR 4 — OLYMPUS YouTube URL vacía:
  Verificar que sube correctamente con privacyStatus="private"
  para la prueba inicial. Luego cambiar a lógica automática:
  - Noticias urgentes → público inmediato
  - Contenido estándar → programado en hora óptima de KAIROS
  - Siempre incluir: título SEO, descripción completa, tags,
    thumbnail A, categoría "Ciencia y Tecnología", aviso legal

════════════════════════════════════════════
TAREA 1 — HEPHAESTUS FORMATO NOTICIARIO (prioridad máxima)
════════════════════════════════════════════

Rediseñar completamente hephaestus.py para el formato NOTICIARIO.
Eliminar dependencia de Pexels. Todo se genera localmente.

DISEÑO DEL VÍDEO (1920x1080, paleta #0A0A0A/#F7931A/#FFFFFF):

CAPA 1 — Fondo estudio:
  - Imagen fija generada con Pillow: estudio de noticias crypto oscuro
  - Pantallas de fondo con gráficos (rectángulos con datos animados)
  - Logo CryptoVerdad en esquina superior izquierda
  - Paleta: fondo #0A0A0A, acentos #F7931A

CAPA 2 — Avatar presentador (placeholder hasta SadTalker):
  - Por ahora: rectángulo #1A1A1A con icono de persona en naranja
  - Posición: lado izquierdo, 60% del ancho de pantalla
  - Cuando llegue SadTalker: sustituir por video del avatar

CAPA 3 — Ventana de gráfico (generada por DAEDALUS):
  - Gráfico de precio BTC animado (Matplotlib → MP4)
  - Posición normal: esquina derecha, 35% pantalla
  - MOMENTOS CLAVE (marcador [GRAFICO_GRANDE] en guión):
    → Gráfico pasa a 70% pantalla lado derecho
    → Avatar se reduce a esquina inferior izquierda (25%)
    → Al terminar el momento: vuelven a posición original
  - Borde naranja #F7931A alrededor de la ventana activa

CAPA 4 — Ticker de precios (siempre visible):
  - Barra horizontal en la parte superior (altura 40px)
  - Fondo #F7931A, texto #0A0A0A bold
  - Muestra: BTC $XX,XXX | ETH $X,XXX | SOL $XXX (precios reales CoinGecko)
  - Animación: scroll horizontal continuo

CAPA 5 — Subtítulos:
  - Texto sincronizado con el audio (.srt)
  - Posición: centro inferior
  - Fuente blanca, fondo semitransparente negro

IMPLEMENTACIÓN:
  - Usar SOLO MoviePy v2 + Matplotlib + Pillow (sin Pexels, sin SadTalker aún)
  - DAEDALUS.generate_price_line("bitcoin", 24) → clip fondo gráfico
  - DAEDALUS.generate_ticker({"BTC": precio, "ETH": precio}) → clip ticker
  - Exportar: 1920x1080 YouTube + 1080x1920 Short/TikTok (recorte automático)

FORMATOS (recetas visuales — implementar todas):
  NOTICIARIO: descrito arriba (para NOTICIA, URGENTE)
  ANALISIS:   gráfico fondo completo + avatar esquina
  EDUCATIVO:  infografías Pillow animadas + avatar
  SHORT:      vertical 1080x1920, avatar centrado, subtítulos grandes

════════════════════════════════════════════
TAREA 2 — VOZ MÁS HUMANA (echo.py + caliope.py)
════════════════════════════════════════════

ECHO — Pre-procesado de texto antes de TTS:
  Crear función preprocess_script(text) que:
  1. Redondea precios a cifras limpias:
     - $83,241.67 → "más de 83 mil dólares"
     - $1,234.56 → "alrededor de mil 200 dólares"
     - Nunca leer decimales
  2. Convierte símbolos a palabras:
     - % → "por ciento"
     - & → "y"
     - # → "" (eliminar)
  3. Añade pausas naturales:
     - Después de punto → [PAUSA]
     - Antes de dato importante → [PAUSA]
     - Entre secciones → [PAUSA_LARGA]
  4. Parámetros edge-tts:
     - rate: -5% (más lento, más natural)
     - pitch: +0%
     - volume: +0%

CALÍOPE — Prompt de experto crypto:
  Modificar el system prompt en prompts/caliope_noticia.txt y
  todos los prompts/*.txt para incluir:

  "Eres Carlos, analista senior de criptomonedas con 8 años
  de experiencia en mercados financieros. Hablas con autoridad
  y criterio propio. Tienes opiniones claras y las defiendes.
  
  REGLAS DE ESCRITURA:
  - Habla en primera persona con criterio propio
  - Usa frases cortas (máximo 15 palabras)
  - Nunca escribas precios con decimales — redondea siempre
  - Añade [PAUSA] entre ideas importantes
  - Usa analogías del mundo real para explicar conceptos
  - Incluye tu opinión personal: 'En mi opinión...', 
    'Lo que me preocupa es...', 'El mercado está ignorando...'
  - Termina siempre con una pregunta al espectador"

════════════════════════════════════════════
TAREA 3 — OLYMPUS PUBLICACIÓN COMPLETA
════════════════════════════════════════════

Completar olympus.py para publicación 100% automática:

def run(self, ctx: Context) -> Context:
  1. Determinar privacyStatus:
     - ctx.is_urgent → "public" inmediato
     - ctx.mode == "standard" → "private" primero, luego
       programar para hora óptima de KAIROS
  
  2. Subir vídeo con TODOS los metadatos:
     - title: ctx.seo_title (de HERMES)
     - description: ctx.seo_description (incluye timestamps,
       links redes sociales, aviso legal, hashtags)
     - tags: ctx.seo_tags (lista de HERMES)
     - categoryId: "24" (Entretenimiento) o "28" (Ciencia/Tech)
     - defaultLanguage: "es"
     - thumbnail: ctx.thumbnail_a_path
  
  3. Después de subir:
     - Guardar youtube_url en ctx.youtube_url
     - Notificar a MERCURY para que avise por Telegram
     - Registrar en SQLite tabla memoria_videos

  4. Notificación Telegram (via MERCURY):
     Formato: "✅ Publicado en YouTube\n
     📺 [título]\n🔗 [url]\n📊 SEO: [score]/100"

════════════════════════════════════════════
TAREA 4 — SADTALKER (avatar IA)
════════════════════════════════════════════

Instalar y configurar SadTalker para el avatar presentador:

1. Clonar SadTalker en C:\Users\Usuario\nexus\sadtalker\
   git clone https://github.com/OpenTalker/SadTalker.git sadtalker

2. Instalar dependencias (usa RTX 3050 con CUDA):
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
   pip install -r sadtalker/requirements.txt

3. Descargar modelos preentrenados en sadtalker/checkpoints/

4. Crear avatar base:
   - Generar imagen de presentador con Stable Diffusion
     o usar foto placeholder hasta que el usuario elija
   - Guardar en assets/avatar_base.png

5. Integrar en HEPHAESTUS:
   def generate_avatar_clip(self, audio_path, avatar_img):
     → Llama a SadTalker para generar video sincronizado
     → Retorna clip MoviePy del avatar hablando
     → Duración = duración del audio

6. Si SadTalker falla o no hay GPU suficiente:
   → Fallback: imagen estática del avatar sin movimiento labial

════════════════════════════════════════════
TAREA 5 — DEPLOY RAILWAY
════════════════════════════════════════════

Preparar y desplegar NEXUS en Railway para funcionamiento 24/7:

1. Verificar Dockerfile:
   FROM python:3.11-slim
   - Instalar ffmpeg
   - Instalar todas las dependencias
   - Copiar proyecto
   - CMD: python main.py --auto

2. Crear railway.toml con variables de entorno

3. Verificar que .env tiene todas las variables:
   GROQ_API_KEY, PEXELS_API_KEY, TELEGRAM_BOT_TOKEN,
   TELEGRAM_CHAT_ID, WEB_PIN, TIKTOK_SESSION_ID,
   YOUTUBE_CLIENT_SECRET_PATH

4. Deploy:
   railway login
   railway init
   railway up

5. Verificar:
   - Panel web accesible desde móvil
   - Bot Telegram responde
   - Pipeline automático arranca a las 9:00

════════════════════════════════════════════
TAREA 6 — PANEL WEB COMPLETO
════════════════════════════════════════════

Completar el panel web (FastAPI + Tailwind + Alpine.js):
Puerto 8080, PIN: WEB_PIN del .env, paleta #0A0A0A/#F7931A

8 secciones:
  /dashboard    — precio BTC/ETH en tiempo real SSE, métricas canal
  /pipeline     — estado pipeline en vivo, logs por agente
  /calendar     — calendario drag&drop de vídeos programados
  /history      — historial vídeos con filtros y métricas
  /ideas        — sugerencias SEO Engine + botón "Crear vídeo"
  /agents       — estado 24 agentes, editar prompts en vivo
  /learning     — lo que MNEME ha aprendido, ajustes aplicados
  /settings     — configuración general, API keys, horarios

Responsive: funciona en móvil (para ver desde el teléfono)

════════════════════════════════════════════
ORDEN DE EJECUCIÓN (Agent Teams en paralelo)
════════════════════════════════════════════

AGENTE A (empieza primero):
  → Corregir errores (MoviePy v2, load_dotenv en todos los agentes)
  → Cuando termine: escribe "FIXES LISTOS"

AGENTE B (espera FIXES LISTOS):
  → HEPHAESTUS formato noticiario completo
  → DAEDALUS integrado para gráficos
  → Test: python -c "from agents.forge.hephaestus import HEPHAESTUS"

AGENTE C (espera FIXES LISTOS):
  → Voz mejorada en echo.py
  → Prompts CALÍOPE como experto crypto
  → Test: generar audio de prueba

AGENTE D (espera FIXES LISTOS):
  → OLYMPUS publicación completa con todos los metadatos
  → MERCURY notificaciones Telegram
  → Test: subida privada a YouTube

AGENTE E (espera B + C + D listos):
  → SadTalker instalación
  → Deploy Railway
  → Panel web completo

════════════════════════════════════════════
CUANDO TODO ESTÉ LISTO:
════════════════════════════════════════════

1. Ejecutar python main.py → opción 1 → "Bitcoin $80K: ¿Qué Sigue?"
2. Verificar que el vídeo tiene visual real (no fondo negro)
3. Verificar que sube a YouTube con todos los metadatos
4. Verificar que llega notificación a Telegram
5. Si todo OK → cambiar a publicación automática
6. Activar modo automático (opción 2 del menú)
7. Actualizar CLAUDE.md con estado final

Objetivo: NEXUS publicando solo, tú solo recibes Telegram.
