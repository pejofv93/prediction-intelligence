# NEXUS v1.0 — Sistema Autónomo CryptoVerdad

## Identidad
Canal: CryptoVerdad · @CryptoVerdad
Tagline: "Crypto sin humo. Análisis real, opinión directa."
Paleta: #0A0A0A · #F7931A · #FFFFFF
Voz: es-ES-AlvaroNeural (edge-tts)

## Stack (100% gratis)
LLM: Groq (llama-3.3-70b-versatile) + Ollama fallback
Noticias: feedparser + Google News RSS
Precios: CoinGecko API pública
Twitter/X: Nitter RSS + Playwright
Voz: edge-tts AlvaroNeural
Vídeo: MoviePy + ffmpeg (SadTalker en semana 2)
Stock video: Pexels Videos API
Gráficos: Matplotlib + mplfinance → MP4
Thumbnails: Pillow
YouTube: YouTube Data API v3 + Analytics (OAuth2)
TikTok: tiktok-uploader (Playwright)
Instagram: instagrapi (semana 2)
Telegram: python-telegram-bot
Web: FastAPI + Jinja2 + Tailwind CDN + Alpine CDN
BD: SQLite — UN solo archivo cryptoverdad.db
Deploy: Docker + Railway
CLI: rich (siempre)

## Arquitectura — 24 agentes en 5 capas
⚡ NEXUS CORE → nexus_core.py — Orquestador maestro

🔵 ORÁCULO:
  ARGOS    → argos.py    — Monitor precios + UrgencyDetector
  PYTHIA   → pythia.py   — RSS noticias + scoring 0-100
  RECON    → recon.py    — Analiza canales competidores
  VECTOR   → vector.py   — Detecta tendencias virales
  THEMIS   → themis.py   — Decide qué crear y cuánto

🟢 FORGE:
  CALÍOPE    → caliope.py    — ScriptWriter 7 modos
  HERMES     → hermes.py     — SEO Engine completo
  ECHO       → echo.py       — Voz edge-tts + .srt
  HEPHAESTUS → hephaestus.py — VideoEngine 4 capas
  IRIS       → iris.py       — Thumbnails A/B
  DAEDALUS   → daedalus.py   — Gráficos animados

🟡 HERALD:
  OLYMPUS → olympus.py — YouTube Expert
  RAPID   → rapid.py   — TikTok Expert
  AURORA  → aurora.py  — Instagram (semana 2)
  MERCURY → mercury.py — Telegram canal + bot privado
  PROTEUS → proteus.py — Channel Manager

🔴 SENTINEL:
  AGORA    → agora.py    — Comentarios YouTube
  SCROLL   → scroll.py   — Newsletter semanal
  CROESUS  → croesus.py  — Monetización + afiliados
  ARGONAUT → argonaut.py — Auditor + caducados

🟣 MIND:
  MNEME    → mneme.py    — LearningEngine
  KAIROS   → kairos.py   — Scheduler adaptativo
  ALETHEIA → aletheia.py — A/B Test Engine

## Reglas absolutas
1. Cada agente: clase Python con run(ctx: Context) -> Context
2. Comunicación entre agentes: SOLO via objeto Context
3. TODO output de terminal usa "rich"
4. TODO en SQLite. Nunca JSON sueltos para persistencia
5. Aviso legal AUTOMÁTICO al detectar palabras de inversión
6. Manejo de errores robusto. Nunca crash silencioso
7. Cada agente tiene su logger con su nombre en mayúsculas
8. Prompts de agentes en archivos .txt en prompts/
9. MNEME inyecta learning_context en ctx antes de CALÍOPE
10. Precios en tiempo real via marcador [PRECIO:BTC]

## Comportamiento eventos de precio
- Movimiento normal (<2x volatilidad) → registrar, incluir
  en próximo vídeo con tiempo verbal correcto
- EXCEPCIONAL (3x-5x volatilidad) → notificar + esperar 30min
- CRISIS (>=5x volatilidad BTC/ETH) → pipeline urgente automático

## Formatos de vídeo
CNN_BREAKING / DOCUMENTAL / EDUCATIVO /
REACCION_TWEET / PREDICCION_MES / SHORT_VERTICAL

## NEXUS Lite (versión actual)
SadTalker ELIMINADO. MuseV/MuseTalk ELIMINADOS. Avatar en pausa.
Modo actual: FULLSCREEN sin avatar (mejor rendimiento, más limpio).
Avatar (pendiente):
  HELIOS v3 (agents/forge/helios.py) — fal.ai + Kling Avatar v2 Pro
  Requiere: FAL_KEY con saldo en fal.ai/dashboard/billing
  PROMETHEUS / MuseTalk desactivados en forge_agent.py
Sin Instagram, sin Newsletter
Publica en: YouTube largo + Short + TikTok + Telegram

## Objetivo de negocio
Partner Program YouTube: 1.000 suscriptores + 4.000h watch time.
CADA decisión de NEXUS debe responder a: ¿esto me acerca más al Partner Program?
Vídeos cortos (<4 min) NO cuentan para watch time. Mínimo absoluto: 4 minutos.

## Los 9 flujos de contenido
| Modo       | Duración     | Palabras      | Notas                        |
|------------|--------------|---------------|------------------------------|
| urgente    | 4-6 min      | 600-800       | Noticias de última hora      |
| noticia    | 5-7 min      | 800-1.000     | Análisis de noticia del día  |
| analisis   | 8-12 min     | 1.200-1.500   | Análisis técnico profundo    |
| educativo  | 10-13 min    | 1.500-2.000   | Tutorial / explicación       |
| reaccion   | 4-6 min      | 600-800       | Reacción a tweet/noticia     |
| semanal    | 12-15 min    | 1.800-2.200   | Resumen semanal del mercado  |
| evergreen  | 8-10 min     | 1.200-1.400   | Contenido atemporal          |
| prediccion | 8-10 min     | 1.200-1.400   | Predicción de precio/evento  |
| serie      | 8-12 min/ep  | 1.200-1.500   | Serie educativa por episodio |

## Estructura narrativa obligatoria (todos los guiones)
HOOK (0-30s): dato sorprendente o pregunta imposible de ignorar
PROMESA (30-60s): qué va a aprender el espectador si se queda hasta el final
DESARROLLO: tensión narrativa cada 2-3 minutos (giro, dato inesperado, pregunta)
RESOLUCIÓN: conclusión con opinión directa de Carlos — sin ambigüedades
CTA natural: invitación sin forzar ("Si quieres saber cuándo publico el próximo...")

## Estado del proyecto
NEXUS CORE:   [x] core/nexus_core.py + context.py + urgency_detector.py + base_agent.py
ORÁCULO:      [x] ARGOS, PYTHIA, RECON, VECTOR, THEMIS (agents/oracle/)
FORGE:        [x] CALIOPE (9 modos, duraciones correctas), HERMES, ECHO, HEPHAESTUS v3,
              IRIS, DAEDALUS · HELIOS v3 (fal.ai Kling) — pendiente saldo FAL_KEY
              PROMETHEUS/MuseTalk desactivados temporalmente
HERALD:       [x] OLYMPUS (siempre private, OAuth2 headless Railway) · RAPID · MERCURY
SENTINEL:     [x] CROESUS · AGORA · SCROLL · ARGONAUT — sentinel_agent.py integrado
MIND:         [x] MNEME, KAIROS, ALETHEIA
PANEL WEB:    [x] web/app.py + 5 templates Tailwind/Alpine — puerto 8080 + PIN
BOT TELEGRAM: [~] MERCURY cubre canal — bot privado pendiente
DEPLOY:       [x] LIVE en Railway — proyecto nexus-cryptoverdad, servicio nexus
              KAIROS scheduler activo — pipeline diario 18:00 UTC (semana) / 12:00 (finde)
              Volumen persistente montado en /app/output
LAYOUT:       [x] Constantes globales _YT_*/SH_* en hephaestus.py — 0px negro garantizado
              YouTube: Ticker y=0/h=40 | Gráfico y=40/h=950 | Subs y=990/h=90
              Short:   Logo y=10/h=68 | Gráfico y=80/h=1700 | Subs y=1780/h=80 | Ticker y=1860/h=60

## Railway — Variables de entorno configuradas
GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PEXELS_API_KEY,
YOUTUBE_TOKEN_B64 (token.json local codificado en base64 — refresh_token activo)
Pendiente: COINGECKO_API_KEY (pro), FAL_KEY (saldo), YOUTUBE_CLIENT_SECRET_PATH (AGORA)

## Próximos pasos (orden sugerido)
1. Verificar primer vídeo real a las 18:00 UTC — revisar Railway logs:
   railway logs --tail 200
   Confirmar: OLYMPUS token refrescado ✓ · publicado https://youtu.be/xxxxx ✓
2. Cambiar OLYMPUS privacyStatus a "public" cuando el vídeo se vea correcto en YouTube
3. HELIOS v3 — añadir saldo en fal.ai → python test_helios.py para verificar
4. RAPID/TikTok — añadir tiktok-uploader a requirements.txt + subir cookies
5. BOT TELEGRAM privado (comandos /estado, /forzar, /parar)
6. AGORA — configurar YOUTUBE_CLIENT_SECRET_PATH en Railway para responder comentarios

## BUG CRÍTICO RESUELTO — HEPHAESTUS compositor (sesión 2026-04-05)
Causa raíz identificada y corregida:
  1. SpinnerColumn de rich usaba braille (⠴) → UnicodeEncodeError en cp1252
     → capturado por outer try/except → ctx.add_error() aunque video OK
     FIX: SpinnerColumn(spinner_name="line") — solo ASCII
  2. console.print(Panel(...)) del bloque success podía fallar con encoding
     FIX: envuelto en try/except independiente
  3. temp_audiofile se creaba en CWD (raíz del proyecto)
     FIX: nuevo helper _write_clip() que pone temp en output/video/
Verificado: dry-run "Bitcoin analisis de prueba" genera video 13MB + short 12MB
  sin errores, sin TEMP huérfanos en raíz. Errores: 0. Artefactos: todos ✓

## Notas de sesiones

### Sesión 2026-04-08 (tarde) — FULLSCREEN dinámico + SENTINEL completo

#### HELIOS v3 — fal.ai + Kling Avatar v2 Pro
- agents/forge/helios.py reescrito: usa fal_client.subscribe("fal-ai/kling-video/v2/pro")
- Bloqueado por saldo agotado en fal.ai — cuenta nueva sin créditos
- HELIOS y PROMETHEUS desactivados en forge_agent.py (modo sin avatar)
- test_helios.py actualizado para fal.ai (no Modal)

#### HEPHAESTUS v3 — Formato FULLSCREEN con escenas dinámicas
- Nuevo FORMAT_FULLSCREEN como formato por defecto (MODE_FORMAT_MAP)
- _compose_fullscreen() reescrito con:
  - Escenas dinámicas por segmento: precio | analisis | noticia | dato | general
  - Cross-fade suave 0.5s entre escenas (smoothstep)
  - Badge de tipo de escena en esquina superior derecha (color por tipo)
  - Logo CryptoVerdad siempre visible arriba-izquierda
  - Subtítulos sincronizados siempre visibles
  - Ticker BTC/ETH/SOL siempre visible en barra inferior
- _parse_script_segments() reescrito:
  - Detecta etiquetas explícitas de CALÍOPE línea a línea (no solo por párrafos dobles)
  - [PRECIO] [NOTICIA] [DATO:X] [ANÁLISIS] [GENERAL] → tipos de escena
  - Fallback por keywords si no hay etiquetas
  - Fusiona adyacentes del mismo tipo
- caliope_standard.txt y caliope_urgente.txt: añadidas instrucciones de etiquetas de escena
- Dry-run verificado: 5 escenas detectadas en guión urgente de 95 palabras
  precio(0s-3s) | noticia(3s-8s) | dato(8s-13s) | analisis(13s-19s) | general(19s-23s)
  Errores: 0. Tiempo total pipeline: 181s

#### SENTINEL — Completado
- AGORA (agora.py): comentarios YouTube con OAuth2 + Groq replies + spam filter
- SCROLL (scroll.py): newsletter Telegram lunes 10:00 UTC + guard double-send
- ARGONAUT (argonaut.py): auditor — archiva huérfanos, vacuum SQLite, health score
- sentinel_agent.py integrado en nexus_core.py como 6º paso del pipeline

### Sesión 2026-04-06 — PROMETHEUS + Layout telediario

#### Assets generados con Stable Diffusion (RTX 3050, float16)
- assets/avatar_carlos_base.png (512x768, ~565KB) — presentador fotorrealista SD
    runwayml/stable-diffusion-v1-5, 35 steps, guidance 8.0
    PROMETHEUS lo detecta automáticamente (prioridad sobre avatar_base.png Pillow)
- assets/studio_background.png (1920x1080, ~1.8MB) — plató CNN-style naranja regenerado SD
    runwayml/stable-diffusion-v1-5, 30 steps, guidance 7.5

#### Layout NOTICIARIO ajustado en hephaestus.py _compose_noticiario
  Antes (sesión 05)          →  Ahora (sesión 06)
  _AV_Y=80, _AV_H=880        →  _AV_Y=45, _AV_H=930    (cabeza sin recorte)
  _DYN_Y=80, _DYN_W=1200     →  _DYN_Y=45, _DYN_W=1220  (más alto y ancho)
  _DYN_H=780                 →  _DYN_H=880               (+100px)
  Marco naranja 3px           →  Marco naranja 5px
  Sin brightness              →  +30% brillo, +10% contraste al gráfico
  _LT_Y=820, _LT_H=60        →  _LT_Y=895, _LT_H=75     (más visible)
  Sin fade-in                 →  Fade-in 0.5s + visible 5.5s, fuente 28px

#### LatentSync + LivePortrait — DESBLOQUEADOS (sesión 2026-04-07)
  insightface 0.7.3 instalado en Python 3.11 con MSVC Build Tools
    Fix: DISTUTILS_USE_SDK=1 + MSSdk=1 + rc.exe en PATH (C:\ffmpeg\bin)
  ffmpeg instalado en C:\ffmpeg\bin\ffmpeg.exe (copia de imageio_ffmpeg v7.1)
    PATH inyectado en latsync_env y lp_env dentro de prometheus.py
  LivePortrait clonado en liveportrait/ desde KwaiVGI/LivePortrait
    Pesos descargados en liveportrait/pretrained_weights/ (via huggingface_hub)
    Fix cp1252: PYTHONIOENCODING=utf-8 en lp_env
    Driving template: liveportrait/assets/examples/driving/d0.mp4 (3.12s → looped)
    Loop automático para cubrir duración del audio (_run_liveportrait)
  LatentSync: timeout dinámico 60s + 10x audio_dur; encoding=utf-8 en subprocess
  CADENA ACTIVA: LivePortrait (movimiento) → LatentSync (lip-sync) → HEPHAESTUS

#### ALETHEIA fix (sesión 2026-04-06)
  Problema: "3.60" (porcentaje) y "24" (horas) detectados como precios BTC
  Fix 1: regex lookahead negativo — excluye números seguidos de h/horas/días/por ciento
  Fix 2: MIN_COIN_PRICE por moneda — BTC mínimo $1000, ETH $50, etc.
  Resultado: 3 warnings → 1 (el restante es legítimo: precio predicción vs real)

#### Dry-run verificado (sesión 2026-04-06)
  Comando: python main.py --dry-run --tema "..." --mode urgente
  Formato NOTICIARIO activado con --mode urgente (THEMIS elige ANALISIS para temas neutros)
  Errores: 0 (el único error es RAPID/TikTok cookies — esperado sin secrets/)
  Artefactos: Audio ✓ · Vídeo 1920x1080 ✓ · Short 1080x1920 ✓ · Thumbnail A/B ✓ · Gráfico ✓
  Pendiente revisar: lower third poco visible sobre fondo SD · imágenes Pexels irrelevantes sin API key

### Sesión 2026-04-05 — PROMETHEUS (motor avatar fotorrealista)
- PROMETHEUS: agents/forge/prometheus.py integrado en forge_agent.py
- context.py: campos avatar_path + metadata añadidos
- Fallback activo: avatar_base.png → Ken Burns mejorado → sin lip-sync

### Sesión 2026-04-04 — HEPHAESTUS v3 + fixes
- SadTalker eliminado. HEPHAESTUS reescrito como v3 (1752 líneas).
- Cadena fallback avatar: LatentSync → Ken Burns (SadTalker quitado)
- LatentSync clonado en latsync/ — falta descargar checkpoints:
    huggingface-cli download ByteDance/LatentSync --local-dir latsync/checkpoints
- Fondo estudio: assets/studio_background.png generado con Pillow (1.2MB)
  (Stable Diffusion requiere token HuggingFace — pendiente huggingface-cli login)
- OLYMPUS: privacyStatus siempre "private" hasta nuevo aviso
- ECHO preprocess_script: fix números decimales, miles españoles, porcentajes
- ALETHEIA: fix falsos positivos de precio ("67 mil" → 67000 antes de comparar)
  + negative lookahead para no capturar porcentajes como precios
- edge-tts: sigue dando 403 (rate limit Microsoft) — pyttsx3 activo como fallback
- DB schema: columna es 'id' (no 'pipeline_id') en tabla pipelines
- MoviePy v1.0.3 instalado — usar SIEMPRE from moviepy.editor import ...
  NO usar API v2 (with_audio, resized, subclipped, with_position)

### Sesión 2026-04-07 — HeyGem Docker (bloqueado)
- Docker Desktop instalado y Windows reiniciado
- Engine Linux devuelve 500 consistentemente — no arranca
- Diagnóstico: WSL2 no habilitado o setup inicial de Docker Desktop incompleto
- Próxima sesión: abrir Docker Desktop UI → completar wizard → relanzar contenedores

### Sesión 2026-04-09 — Producción profesional + 3 bugs críticos resueltos

#### CALÍOPE — Modo ANALISIS profesionalizado
- caliope_analisis.txt reescrito: estilo creador de contenido viral, temperatura=0.9
- Estructura de 6 bloques obligatorios: [PRECIO][ANALISIS][SENTIMIENTO][DOMINANCIA][ADOPCION][PREDICCION]
- Reglas estrictas: máx 12 palabras/frase, [PAUSA] tras datos, preguntas retóricas
- CTAs integrados: like a los 30s, suscripción a mitad, pregunta al cierre
- Prohibido lenguaje académico, anglicismos sin traducir

#### HEPHAESTUS — Producción visual completa
- Fear&Greed centrado (cx = w//2, radio = min(w,h)*0.35)
- Logo CryptoVerdad 260x68px con fondo semitransparente
- Sistema tipográfico: 10 tamaños de fuente diferenciados
- Música de fondo: 8% voz principal, generada por utils/music_generator.py
  - analisis: drone Do menor (C3+G3+Eb3) con vibrato
  - educativo: arpegios C4-E4-G4-C5
  - noticia: pulso 120bpm A3+E3+E4
  - urgente: tritono F3+B3 con modulación 3Hz
- Paletas por formato: urgente=rojo, noticia=naranja, educativo=azul, analisis=naranja
- 4 formatos FULLSCREEN visualmente distintos verificados con frames

#### HEPHAESTUS — 12 escenas dinámicas ANALISIS (rotación verificada)
- _SCENE_TEMPLATES["analisis"]: 12 tipos distintos con tiempos proporcionales
- _merge_with_minimum_scenes(): inyecta template cuando faltan fear_greed/dominancia/heatmap
- _get_base_frame(): fear_greed=gauge animado, dominancia=pie chart, heatmap=heatmap,
  volumen=reveal barras, dominancia_area=area chart, correlacion=tabla correlacion
- _make_fallback_frame(): fondo plano diferenciado por tipo cuando path no disponible
- Verificado con test_analisis_escenas.py: 12 frames distintos en output/frames/
  analisis_escena01_precio.png ... analisis_escena12_prediccion.png ✓

#### BUG 1 RESUELTO — Short 1080x1920 sin barras negras
- _crop_to_short(): antes escalaba por ancho (0.5625x) → frame 1080x607 sobre fondo negro
- Fix: escalar por ALTO (scale_factor = th / src_h = 1920/1080 = 1.777x) → 3413x1920
  luego crop centro horizontal a 1080px. Sin barras negras garantizado.

#### BUG 2 RESUELTO — Precios ETH/SOL en tiempo real
- context.py: añadidos campos eth_price: float = 0.0, sol_price: float = 0.0
- argos.py: escribe ctx.eth_price y ctx.sol_price desde respuesta CoinGecko
- daedalus.py: CACHE_TTL_PRICES=300 (5 min), eliminados valores hardcodeados
- hephaestus.py _extract_ticker_prices(): usa ctx.eth_price/sol_price como prioridad 1
- Verificado: ticker muestra BTC $72,334 | ETH $2,221 | SOL $84 (precios reales)

#### BUG 3 RESUELTO — 12 escenas del análisis rotaban correctamente
- Confirmado con test_analisis_escenas.py: Fear&Greed, Dominancia, Heatmap, etc. presentes
- El bug estaba ya resuelto por el trabajo de sesiones anteriores

#### DAEDALUS — Filtro heatmap reforzado
- Filtro KNOWN_CRYPTOS existente no era suficiente (FIGR_HELOC pasaba)
- Fix añadido: excluir cualquier símbolo con '_', ' ' o longitud > 8 chars
  (ningún token crypto legítimo tiene guion bajo en su símbolo)
- _KNOWN_SYMBOLS ampliado: wbt, usds, usde, fet, grt, sand, mana, ens, ldo, crv,
  mkr, snx, comp, yfi añadidos

#### ECHO — Voz diferenciada por formato
- _get_rate(ctx): devuelve (rate, pitch) según ctx.mode
  urgente: +15%, +8Hz | noticia: +10%, +5Hz
  analisis/standard: -8%, +0Hz | educativo/tutorial: -12%, +3Hz
  default: -5%, +0Hz
- Pitch en formato Hz (no %) — edge-tts requiere +NHz, no +N%

#### Tests disponibles
- test_analisis_escenas.py — 12 frames escenas analisis
- test_analisis_final.py — pipeline completo analisis con LLM
- test_educativo_visual.py — 4 frames educativo
- test_4formatos.py — comparación visual 4 formatos

### Sesión 2026-04-10 — 4 bugs resueltos + verificación

#### BUG 1 RESUELTO — ETH/SOL precios falsos en ticker
- Causa raíz: fallback SQLite de ARGOS estaba DENTRO del try/except principal.
  Cuando _fetch_prices() lanzaba excepción, el except la capturaba y el fallback
  nunca se ejecutaba. Además no había fallback para ETH/SOL, solo BTC.
- Fix en argos.py: bloque fallback movido FUERA del try/except.
  Itera sobre btc_price, eth_price, sol_price en orden.
  Usa db.get_last_coin_price(coin_id) para cada uno.
- Fix en db.py: métodos genéricos save_coin_price(coin_id, price) y
  get_last_coin_price(coin_id). Aliases de compatibilidad save_btc_price/get_last_btc_price.
- Verificado: dry-run muestra ETH=$2,187 SOL=$83 en tiempo real,
  guardados en SQLite (market_prices: ethereum ... guardado | solana ... guardado).

#### BUG 2 — Short 1080x1920 sin barras negras
- Código ya correcto desde sesión 2026-04-09: _crop_to_short() escala por altura
  (scale_factor = th/src_h = 1920/1080 = 1.777x) y recorta centro horizontal.
- No se encontró video short reciente que confirme visualmente — pendiente ver en producción.

#### BUG 3 RESUELTO — Anglicismos en el guión
- Causa raíz: clean_script() solo eliminaba markdown, no traducía inglés.
  Solo caliope_analisis.txt tenía regla "no English". Standard/urgente/noticia/educativo no.
- Fix en caliope.py: función _fix_english_words(script) con 25 reemplazos regex.
  bullish→alcista, bearish→bajista, market→mercado, weeks→semanas, etc.
  Se llama en run() tras clean_script() para todos los modos.
- Fix en prompts: regla "TODO en español / NUNCA usar..." añadida a los 4 archivos
  caliope_standard.txt, caliope_urgente.txt, caliope_noticia.txt, caliope_educativo.txt.
- Verificado: dry-run, grep de "bullish|bearish|market|weeks" no encuentra nada en script.

#### BUG 4 RESUELTO — 12 escenas análisis no rotan (3 BTC charts consecutivos)
- Causa raíz: _SCENE_TEMPLATES["analisis"] tenía precio+analisis+analisis en posiciones 1-3.
  Resultado: 3 escenas visualmente casi idénticas (BTC chart) al inicio.
- Fix en hephaestus.py: template reordenado para alternar BTC chart con gráficos visuales.
  Nueva secuencia: precio|analisis|fear_greed|dominancia|analisis|heatmap|volumen|
  dominancia_area|correlacion|adopcion|analisis|prediccion
  Nota: "adopcion" en pos 10 reemplaza la segunda "precio" del template anterior.
- Fix en test_analisis_escenas.py: TEMPLATE_ANALISIS local actualizado para coincidir.
- Verificado: log HEPHAESTUS confirma fear_greed en pos 3 (14s-20s), no en pos 4.
  Nunca más de 2 escenas BTC chart consecutivas.

#### BUG 5 RESUELTO — ETH/SOL $3,500/$170 en test scripts (residual del bug 1)
- Causa raíz: 4 test scripts (test_analisis_escenas, test_educativo_visual,
  test_analisis_final, test_4formatos) tenían ETH=3500 y SOL=170 hardcodeados.
  El pipeline real (ARGOS) funciona correctamente. Los tests bypaseaban ARGOS.
- Fix: todos los tests ahora usan db.get_last_coin_price("ethereum/solana") con
  fallback a 2200/85 si la DB está vacía. Ningún valor hardcodeado de precio ETH/SOL.

#### BUG 6 RESUELTO — Copyright: imagen de CoinTelegraph/CoinDesk como fondo
- Causa raíz: HEPHAESTUS descargaba la imagen del artículo RSS (ctx.news_image_url)
  y la usaba como fondo de escenas "noticia"/"adopcion". Esto viola copyright de medios.
- Fix en hephaestus.py _compose_fullscreen():
  1. Eliminada función _download_article_image() y toda lógica de descarga de medios
  2. Añadida función _make_news_title_frame(title): fondo #0A0A0A + título noticia en blanco
  3. Escenas noticia/adopcion usan SOLO: Pexels (CC0) o _make_news_title_frame()
  4. Nunca se convierte a "precio" cuando Pexels falla — siempre hay frame copyright-safe
- ctx.news_image_url se mantiene en Context para otros usos pero HEPHAESTUS lo ignora.

#### BUG 7 RESUELTO — Short: primeros 3-4s muestran líneas horizontales sin velas
- Causa raíz: _parse_zoom_events() creaba evento de zoom desde el primer subtítulo (t=0)
  porque menciona el precio actual. Esto activaba effective_crop casi de inmediato,
  dibujando S/R lines como líneas horizontales antes de que las velas sean visibles.
- Fix en chart_zoom_engine.py: añadido MIN_ZOOM_START = 3.0
  _parse_zoom_events(): skip subtítulos con start_s < MIN_ZOOM_START
  _inject_level_zoom_events(): t_r1 = max(MIN_ZOOM_START, dur*0.35)
- Resultado: gráfico BTC completo visible sin líneas los primeros 3s.

#### BUG 8 RESUELTO — Short: velas más altas salen del frame (clipping top)
- Causa raíz: ZOOM_Y_MARGIN=0.11 muy ajustado — mechas de velas altas superaban y_top del crop.
  Además y_top podía caer por encima de PLOT_TOP (zona de título del chart, no plot area).
- Fix en chart_zoom_engine.py:
  1. ZOOM_Y_MARGIN: 0.11 → 0.22 (doble de margen vertical, ±22% del rango de precio)
  2. y_top padding: -25 → -55 (más espacio sobre la vela superior)
  3. y_top = max(PLOT_TOP, y_top) — nunca entrar en zona de título del matplotlib
- Resultado: mechas de velas más altas visibles dentro del frame en Short 1080x1920.

### Sesión 2026-04-10 (tarde) — Duración vídeo + Short nativo

#### PROBLEMA CRÍTICO DE NEGOCIO
Vídeos de 35 segundos son insuficientes para Partner Program (4.000h watch time).
Objetivo: vídeos largos de 8-12 minutos, Shorts de 45-60 segundos independientes.

#### FIX 1 RESUELTO — Duración mínima por formato
- Causa raíz: max_tokens demasiado bajo (urgente: 600 tokens ≈ 90 palabras)
  y _min_words sin umbrales reales (urgente: 150 palabras, cuando debería ser 600).
- Fix en caliope.py:
  max_tokens: urgente→2500, analisis→4096, noticia→2500, educativo→4096
  _min_words: analisis→1200, standard→1000, noticia→800, urgente→600, educativo→1500
  Retry loop hasta 3 intentos con instrucción de profundidad creciente.

#### FIX 2 RESUELTO — Estructura narrativa HOOK/PROMESA/DESARROLLO/RESOLUCIÓN/CTA
- Fix en prompts: caliope_analisis.txt y caliope_urgente.txt reescritos con:
  - HOOK (30s): dato sorprendente o pregunta que no se puede ignorar
  - PROMESA (30-60s): qué va a aprender el espectador si se queda
  - DESARROLLO con tensión narrativa cada 60-90 palabras
  - RESOLUCIÓN: conclusión con opinión directa de Carlos
  - CTA natural: invitación sin forzar ("Si quieres saber cuándo publico el próximo análisis...")
- Objetivo: 1.200-1.500 palabras (analisis), 600-800 palabras (urgente).
- caliope_noticia.txt: añadida estructura HOOK+PROMESA, objetivo 800-1.000 palabras.

#### FIX 3 RESUELTO — Short como formato independiente (45-60s)
- Causa raíz: Short era un crop del vídeo largo — contenido no nativo, sin estructura propia.
- Fix en caliope.py: _generate_short_script() genera 150 palabras con su propio prompt
  caliope_short.txt (hook 5s + desarrollo 40s + CTA 10s). Se llama al final de run() para
  todos los modos excepto short/thread.
- Fix en echo.py: si ctx.short_script existe, genera ctx.short_audio_path = {pid}_short.mp3
  con rate +5%, pitch +3Hz (tono dinámico para short).
- Fix en context.py: añadidos campos short_script: str y short_audio_path: str.
- Fix en hephaestus.py Paso 8: si short_audio_path existe, llama _compose_short_vertical
  con audio y subtítulos propios del short. Fallback: _crop_to_short si no hay short propio.

#### FIX 4 RESUELTO — Short centrado verticalmente (sin tercio superior negro)
- Causa raíz: _compose_short_vertical tenía chart en y=55 (dentro del área del logo),
  ticker pegado en y=0, y avatar en zona fija. Sin padding uniforme arriba/abajo.
- Fix en hephaestus.py _compose_short_vertical: layout refactorizado con constantes:
  TICKER_H=40 (y=0), LOGO en y=52, CHART en y=144 (h=33%), GAP=40,
  AVATAR en y~820 (h=29%), LABEL bajo avatar, SUBS en y=1778 (h=130).
  Padding superior e inferior simétrico. Ticker ahora en la parte superior correcta.

### Sesión 2026-04-10 (noche) — Layout pixel-perfect + forced_mode + ECHO fix

#### Layout pixel-perfect hephaestus.py
- Constantes globales _YT_* y _SH_* añadidas después de FPS=30 (línea 83)
- _compose_fullscreen: Ticker movido de y=1024 (bottom) a y=0 (top), h 56→40
  _SUB_Y 956→990, _SUB_H 68→90. Todo via _YT_* — cero hardcoding.
- _compose_short_vertical: layout completo via _SH_* — cero hardcoding
  Logo y=10/h=68, Gráfico y=80/h=1700, Subs y=1780/h=80, Ticker y=1860/h=60
- Verificado matemáticamente: YouTube 1080/1080px, Short 1920/1920px, 0 gaps

#### BUG RESUELTO — ECHO velocidad incorrecta en analisis
- Causa raíz: condición `if mode == "urgente" or is_urgent` en echo.py:461
  Si el pipeline era urgente (is_urgent=True) usaba voz urgente (+15%/+8Hz)
  incluso en modo analisis (debería ser -8%/+0Hz).
- Fix: condición simplificada a `if mode == "urgente":` — solo modo, no flag.

#### BUG RESUELTO — THEMIS sobreescribía --mode analisis del CLI
- Causa raíz: ctx.mode = strategy["mode"] en themis.py:298 era incondicional.
  THEMIS podía elegir "standard" aunque el usuario pasara --mode analisis.
- Fix: nuevo campo forced_mode en Context. nexus_core.py lo asigna en run_pipeline().
  THEMIS respeta forced_mode si está definido, solo sugiere si no hay forzado.

### Sesión 2026-04-11 — Deploy Railway en producción

#### Deploy completo en Railway
- Proyecto: nexus-cryptoverdad · Servicio: nexus
- .gitignore + git init — build context reducido de 837MB a 4.7MB
  (excluye latsync/ 8.1GB, liveportrait/ 709MB, sadtalker/ 2.5GB, output/ 766MB)
- railway.toml: CMD python main.py --auto · healthcheckPath /health · restart on_failure
- Volumen persistente montado en /app/output (Railway Dashboard → Volumes)
- main.py --auto: KAIROS en daemon thread + uvicorn panel web en hilo principal

#### Bugs de deploy resueltos (Debian trixie)
- libgl1-mesa-glx no existe → reemplazado por libgl1
- libxrender-dev → libxrender1
- playwright install --with-deps falla (ttf-ubuntu-font-family) → deps manuales en apt-get
- libespeak.so.1 no encontrado → añadidos espeak + libespeak1 (paquetes legacy)
- ImportError Kairos → clase es KAIROS (mayúsculas) en agents/mind/kairos.py

#### ARGOS — CoinGecko 429 en Railway (IP compartida)
- _fetch_prices() NUNCA lanza excepción — retorna hardcoded fallback si todo falla
- Orden: CoinGecko API → SQLite caché → hardcoded fallback (BTC 72k/ETH 2200/SOL 84)
- _get() con retry exponencial y backoff (MAX_RETRIES=3, CACHE_TTL=300s)
- ORÁCULO eliminado de la lista de pasos fatales — sus errores no detienen el pipeline
- COINGECKO_API_KEY en .env → usa endpoint pro-api.coingecko.com con menor rate limit

#### ECHO — TTS fallback chain (3 niveles)
- edge-tts → pyttsx3 → silencio ffmpeg (anullsrc, duración estimada del guión)
- pyttsx3==2.90 añadido a requirements.txt
- apt-get: espeak-ng libespeak-ng1 espeak libespeak1 (cubre libespeak.so.1)

#### OLYMPUS — OAuth2 headless Railway
- Eliminado InstalledAppFlow.run_local_server() — incompatible con servidor sin pantalla
- _build_service() reescrito: YOUTUBE_TOKEN_B64 → YOUTUBE_TOKEN → token.json en disco
- Construye google.oauth2.credentials.Credentials directamente desde token_data dict
- Solo llama creds.refresh(Request()) si el token está caducado (no abre browser)
- Si no hay token → ctx.add_warning() (no error) — pipeline continúa sin YouTube
- YOUTUBE_TOKEN_B64 subido a Railway (token.json local en base64, refresh_token activo)

#### Estado al cierre de sesión
- Container arrancado a las 17:20 UTC sin errores
- KAIROS scheduled: primer pipeline real a las 18:00 UTC (sábado → 12:00)
  Nota: KAIROS detectó sábado → hora óptima 12:00 → próximo día domingo 12:00
- Pendiente confirmar: OLYMPUS sube vídeo correctamente en primer pipeline real

### Sesión 2026-04-04 — SadTalker integrado (obsoleto, ver arriba)
