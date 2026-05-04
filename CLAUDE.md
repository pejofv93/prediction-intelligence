# NEXUS v1.0 — Sistema Autónomo CryptoVerdad

## Identidad
Canal: CryptoVerdad · @CryptoVerdad
Tagline: "Crypto sin humo. Análisis real, opinión directa."
Paleta: #0A0A0A · #F7931A · #FFFFFF
Voz: Coqui TTS es/css10/vits (voz masculina española natural)

## Stack (100% gratis)
LLM: Groq (llama-3.3-70b-versatile) + Ollama fallback
Noticias: feedparser + Google News RSS
Precios: CoinGecko API pública (con retry + caché SQLite 5min)
Twitter/X: Nitter RSS + Playwright
Voz: Coqui TTS (tts_models/es/css10/vits, ~100MB) → edge-tts → pyttsx3 → silencio-ffmpeg
  Kokoro descartado: voces ES robóticas. edge-tts/ElevenLabs bloqueados en IPs Railway.
  TTS_HOME=/app/output/models/tts — volumen persistente Railway
  Speed por modo: urgente=1.50, noticia=1.48, analisis=1.45, educativo=1.40
  [PAUSA]→0.3s silencio real · [PAUSA_LARGA]→0.6s (ffmpeg concat de segmentos)
Vídeo: MoviePy v1.0.3 + ffmpeg — SIEMPRE from moviepy.editor import ...
  bitrate="4000k" audio_bitrate="192k" en write_videofile → ~140MB por 4-5 min
Stock video: Pexels Videos API
Gráficos: Matplotlib + mplfinance → MP4
Thumbnails: Pillow (RGBA via paste+mask, sin alpha_composite — compatible Railway)
YouTube: YouTube Data API v3 OAuth2 (YOUTUBE_TOKEN_B64 → token.json headless)
TikTok: tiktok-uploader (Playwright) — requiere cookies
Instagram: instagrapi (pendiente)
Telegram: python-telegram-bot
Web: FastAPI + Jinja2 + Tailwind CDN + Alpine CDN (puerto 8080 + PIN)
BD: SQLite — UN solo archivo cryptoverdad.db
Deploy: Docker + Railway (proyecto nexus-cryptoverdad, servicio nexus)
CLI: rich (siempre)

## Arquitectura — 24 agentes en 5 capas
⚡ NEXUS CORE → nexus_core.py — Orquestador maestro
  Bloqueo SEO: si ctx.seo_score < 70 en _run_herald() → publicación bloqueada

🔵 ORÁCULO:
  ARGOS    → argos.py    — Monitor precios BTC/ETH/SOL + 24h% desde historial SQLite
  PYTHIA   → pythia.py   — RSS noticias + scoring 0-100
  RECON    → recon.py    — Analiza canales competidores (requiere YOUTUBE_API_KEY)
  VECTOR   → vector.py   — Detecta tendencias virales (pytrends)
  THEMIS   → themis.py   — Decide qué crear + auto-topic desde noticias/precios

🟢 FORGE:
  CALÍOPE    → caliope.py    — ScriptWriter 9 modos, Jaccard similarity para bloques únicos
  HERMES     → hermes.py     — SEO Engine: 5 variantes de título, selecciona mayor score
  ECHO       → echo.py       — Coqui TTS + cadena fallback, registra ctx.tts_engine
  HEPHAESTUS → hephaestus.py — VideoEngine FULLSCREEN 1920x1080, Short 1080x1920 nativo
  IRIS       → iris.py       — Thumbnails A/B
  DAEDALUS   → daedalus.py   — Gráficos animados (12 tipos: fear/greed, dominancia, heatmap...)

🟡 HERALD:
  OLYMPUS → olympus.py — YouTube (público, OAuth2 headless, persiste youtube_url en pipelines)
  RAPID   → rapid.py   — TikTok (requiere cookies)
  AURORA  → aurora.py  — Instagram (pendiente)
  MERCURY → mercury.py — Telegram canal + bot privado
  PROTEUS → proteus.py — Channel Manager

🔴 SENTINEL:
  AGORA    → agora.py    — Comentarios YouTube (requiere YOUTUBE_CLIENT_SECRETS_B64)
  SCROLL   → scroll.py   — Newsletter Telegram lunes 10:00 UTC
  CROESUS  → croesus.py  — Monetización + afiliados
  ARGONAUT → argonaut.py — Auditor: huérfanos, vacuum SQLite, health score

🟣 MIND:
  MNEME    → mneme.py    — LearningEngine (analytics YouTube cada 6h via _update_video_analytics)
  KAIROS   → kairos.py   — Scheduler: 10:00 UTC todos los días, grace period 3h al arrancar
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
- Movimiento normal (<2x volatilidad) → registrar, incluir en próximo vídeo
- EXCEPCIONAL (3x-5x volatilidad) → notificar + esperar 30min
- CRISIS (>=5x volatilidad BTC/ETH) → pipeline urgente automático
  is_urgent=True SOLO si score≥70 AND (emergency keyword OR move≥5% en topic)

## Formatos de vídeo
FORMAT_FULLSCREEN (por defecto) — 12 escenas dinámicas para analisis:
  precio | analisis | fear_greed | dominancia | analisis | heatmap | volumen |
  dominancia_area | correlacion | adopcion | analisis | prediccion
FORMAT_SHORT_VERTICAL — Short nativo 1080x1920 con guión propio (150 palabras)

## NEXUS Lite (modo actual)
SadTalker/MuseV/MuseTalk/PROMETHEUS/LatentSync/LivePortrait: DESACTIVADOS
Modo: FULLSCREEN sin avatar (mejor rendimiento, más limpio)
Avatar futuro: HELIOS v3 (fal.ai + Kling Avatar v2 Pro) — requiere saldo FAL_KEY
Publica en: YouTube largo (público) + Short + TikTok + Telegram

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
NEXUS CORE:   [x] nexus_core.py + context.py + urgency_detector.py + base_agent.py
              [x] Bloqueo SEO < 70 en _run_herald()
              [x] _run_crisis_pipeline() — 3 escenas, <90s, para CRISIS >=5x volatilidad
              [x] validate_before_publish() — quality gate 5 checks + notif Telegram
              [x] _probe_resolution() + _force_1080p() — ffprobe post-render
              [x] _check_disk_space() — aborta si <500MB libres + alerta Telegram
              [x] _cleanup_pipeline_temps() — borra temps tras subida exitosa YouTube
ORÁCULO:      [x] ARGOS (precios reales + 24h% + _fetch_onchain_signals: mempool/hash/F&G)
              [x] PYTHIA (scoring 7 capas + _deduplicate_articles + 10 RSS feeds)
              [x] THEMIS (_generate_topic_from_news — LLM genera topic desde noticias)
              [~] RECON — placeholder, requiere YOUTUBE_API_KEY en Railway
              [~] VECTOR — pytrends en requirements.txt, integración parcial
FORGE:        [x] CALÍOPE (Jaccard fix real: script_raw pre-clean_script)
              [x] HERMES (5 variantes título, keyword sin puntuación, score real)
              [x] ECHO (Coqui TTS verificado en producción, ctx.tts_engine registrado)
              [x] HEPHAESTUS v3 (FULLSCREEN 1920x1080, lower thirds, crisis_mode=3 escenas)
              [x] IRIS (thumbnails A/B + sentimiento: rojo si BTC<-3%, banner alerta si F&G<25)
              [x] DAEDALUS (Binance OHLCV primario + generate_animated_chart_video)
              [~] HELIOS v3 — pendiente saldo FAL_KEY
HERALD:       [x] OLYMPUS (_add_end_screens + _enrich_description_with_affiliates)
              [x] PROTEUS (nuevo: Twitter thread + LinkedIn + blog HTML con schema SEO)
              [x] AURORA (Instagram Reels con thumbnail cover + youtube_url en caption)
              [~] RAPID — Playwright falla sin cookies TikTok
              [x] MERCURY (Telegram canal)
SENTINEL:     [x] AGORA · SCROLL · CROESUS · ARGONAUT — sentinel_agent.py integrado
              [~] AGORA comentarios — requiere YOUTUBE_CLIENT_SECRETS_B64 en Railway
MIND:         [x] MNEME (YouTube Analytics retention: avg_view_percentage, watch_time)
              [x] KAIROS (10:00 UTC + grace 3h + _process_ab_swap_queue)
              [x] KAIROS VOLUME_GUARDIAN — 03:00 UTC diario + health check 6h via MERCURY
              [x] ALETHEIA (A/B real: _select_ab_thumbnail con confidence + ab_swap_queue)
PANEL WEB:    [x] web/app.py + 5 templates + /force-pipeline + /pipeline/stream SSE + /analytics
              [x] dashboard.html: Alpine.js live status card conectado al SSE
BOT TELEGRAM: [~] MERCURY cubre canal — bot privado pendiente
DEPLOY:       [x] LIVE en Railway — pipeline diario 10:00 UTC
              [x] Primer pipeline con Coqui TTS: 2026-04-14 (d711feb0, 4:37 min, 0 errores)
              [x] YouTube URL activa: https://youtu.be/jf7cLzvRAoY
              [x] 18 mejoras desplegadas: commit 5777838 (2026-04-16)
              Volumen persistente: /app/output (audio, video, charts, models)

## Railway — Variables de entorno
Configuradas: GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PEXELS_API_KEY,
              YOUTUBE_TOKEN_B64 (token.json en base64, refresh_token activo)
Pendiente:    CRON_SECRET (para /force-pipeline), YOUTUBE_API_KEY (RECON),
              YOUTUBE_CLIENT_SECRETS_B64 (AGORA — base64 del client_secret.json),
              COINGECKO_API_KEY (pro), FAL_KEY con saldo (HELIOS v3)

## Layout pixel-perfect (constantes en hephaestus.py)
YouTube 1920x1080: Ticker y=0/h=40 | Gráfico y=40/h=950 | Subs y=990/h=90
Short  1080x1920:  Logo y=10/h=68 | Gráfico y=80/h=1700 | Subs y=1780/h=80 | Ticker y=1860/h=60

## Próximos pasos (orden prioridad negocio)
1. [URGENTE] Dry-run local antes del próximo pipeline Railway — 4 bugs originales sin verificar:
   a) Resolución 1920x1080 real (ffprobe confirma, no solo log)
   b) Gráfico BTC con datos Binance actuales (no caché $74k)
   c) Thumbnail upload (requiere canal verificado con teléfono en YouTube Studio)
   d) Quality gate ejecutándose en orden correcto (antes de OLYMPUS)
   Comando: python main.py --dry-run (o ejecutar pipeline sin --auto)
2. Verificar pipeline 2026-04-16 10:00 UTC — railway logs --tail 100
   Esperar: "PROTEUS: Twitter thread guardado", "ARGOS: on-chain signals", "Jaccard (pre-limpieza)"
3. RAPID/TikTok — subir cookies tiktok-uploader → publicación automática
4. RECON — añadir YOUTUBE_API_KEY en Railway Dashboard (distinta del OAuth2)
5. AGORA — añadir YOUTUBE_CLIENT_SECRETS_B64 en Railway (base64 del client_secret.json)
6. Railway Pro plan → activar cron [[crons]] en railway.toml (actualmente Hobby)
7. BOT TELEGRAM privado (comandos /estado, /forzar, /parar)
8. HELIOS v3 — añadir saldo en fal.ai → python test_helios.py

## Notas críticas de implementación

### MoviePy
- Usar SIEMPRE: `from moviepy.editor import ...`
- NO usar API v2 (with_audio, resized, subclipped, with_position)
- bitrate="4000k", audio_bitrate="192k" obligatorio en _write_clip()
- temp_audiofile siempre en output/video/ (no en CWD)

### ARGOS — CoinGecko en Railway
- IPs Railway comparten rate limit → 429 frecuente
- Orden: CoinGecko API → caché SQLite (5min) → fallback hardcoded (BTC 74k/ETH 2340/SOL 130)
  Fallback actualizado 2026-04-16 con precios Binance spot verificados
- ORÁCULO no es fatal — sus errores no detienen el pipeline
- 24h% calculado desde oracle_prices SQLite cuando CoinGecko devuelve 0.0
- _fetch_onchain_signals(): mempool.space (congestión fees), blockchain.info (hash rate),
  alternative.me (Fear & Greed) — sin API keys, fallback 50 neutral si cualquiera falla

### OLYMPUS — OAuth2 headless
- NUNCA InstalledAppFlow.run_local_server() — incompatible con Railway
- YOUTUBE_TOKEN_B64 → decodifica → Credentials directamente → refresh solo si caducado
- privacyStatus: "public" (siempre desde sesión 2026-04-14)
- youtube_url persistida en tabla pipelines via db.update_pipeline_youtube_url()

### HERMES — SEO scoring
- keyword = _extract_keyword(topic): strip puntuación con re.sub(r"[^\w]", "", w)
  "¿BTC a $70,000?" → keyword "btc" (no "¿btc")
- 5 variantes de título → max(score) → selección automática
- Bloqueo real en NexusCore._run_herald(): if seo_score < 70: return ctx

### CALÍOPE — Unicidad de bloques
- _check_block_uniqueness usa Jaccard similarity (umbral 0.70)
- Stopwords incluyen términos crypto: bitcoin, btc, eth, sol, precio, mercado...
- max_tokens: analisis=4096, educativo=4096, noticia/urgente=2500
- _min_words: analisis=1200, educativo=1500, noticia=800, urgente=600

### KAIROS — Scheduler
- DEFAULT_HOURS: 10h UTC todos los días (12:00 España verano)
- Grace period al arrancar: si el pipeline del día era en las últimas 3h → ejecuta inmediatamente
- get_optimal_hour() requiere min_samples=3 — sin datos suficientes usa 10h
- _reset_stale_defaults() en __init__ limpia filas sample_size=0

### ECHO — TTS chain
- Coqui: singleton _kokoro_instance, descarga lazy en output/models/tts
- [PAUSA]/[PAUSA_LARGA] → segmentos + ffmpeg concat (silencio real)
- Limpieza nuclear: re.sub(r'\[.*?\]','') antes del TTS (ningún bracket llega)
- ctx.tts_engine registra motor usado para diagnóstico

### AGORA — Comentarios YouTube
- YOUTUBE_CLIENT_SECRETS_B64: base64 del client_secret.json → decodifica a tempfile en runtime
- YOUTUBE_TOKEN_B64 (el mismo OAuth2 de OLYMPUS) se reutiliza para leer el token
- No necesita ningún archivo en disco — compatible Railway headless

### DAEDALUS — Fuente OHLCV
- Binance klines API primaria: api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=180
  (180 velas de 4h = 30 días, sin API key, sin rate limit en Railway)
- CoinGecko fallback si Binance falla
- Caché SQLite 1h — invalidada si cierre más reciente ±10% del ctx.btc_price
- generate_animated_chart_video(): FuncAnimation 24fps, velas progresivas + zoom + parpadeo precio

### HEPHAESTUS — Crisis mode
- ctx.crisis_mode = True → solo 3 escenas: [precio, analisis, prediccion]
- _draw_lower_third(): slide-in ease-out cúbico, fondo semitransparente, borde acento #F7931A
- Solo se activa en escenas "analisis" cuando ctx.support_levels no está vacío

### PROTEUS — Content repurposing
- Output: output/repurposed/{pipeline_id}/ (twitter_thread.txt, linkedin_post.txt, blog.html)
- _extract_key_insights(): puntúa frases por densidad de datos ($X, %, años, énfasis)
- Blog HTML incluye schema SEO, meta tags, embed YouTube, paleta #0A0A0A/#F7931A
- Ejecutado en herald_agent.py inmediatamente después de OLYMPUS (y en run_urgent())

### ALETHEIA — A/B real
- ab_swap_queue tabla SQLite: pipeline_id, check_at (+2h), youtube_video_id, status
- _select_ab_thumbnail(): si confidence < 0.3 → alterna A/B; si >= 0.3 → usa ganador histórico
- KAIROS procesa cola pendiente en cada ciclo del scheduler

### DB — Esquema relevante
- tabla pipelines: id, topic, mode, status, youtube_url, seo_score, created_at
- tabla videos: id, pipeline_id, youtube_id, views, likes,
                avg_view_percentage, avg_duration_seconds, watch_time_minutes (añadido MNEME)
- tabla oracle_prices: coin, price, recorded_at (base para 24h%)
- tabla market_prices: coin_id, price, recorded_at
- tabla ab_swap_queue: pipeline_id, check_at, youtube_video_id, status (añadido ALETHEIA)

## Historial de sesiones (resumen)

### Sesión 2026-05-04 — Fix Volume Full + Monitoreo permanente

**Causa raíz del pipeline parado desde 30/04:**
nexus-volume Railway al 100% — pipeline crashea silenciosamente al intentar escribir MP4/WAV.

**Acciones implementadas:**

PASO 1 (Diagnóstico): El diagnóstico debe hacerse en Railway con:
  `railway run bash` → `df -h /app/output` + `du -h --max-depth=2 /app/output`
  Candidatos más probables: /app/output/video (MP4 acumulados), /app/output/audio (WAVs), temp_frames

PASO 2 (Limpieza):
- `scripts/cleanup_volume.py` creado — dry-run por defecto, --confirm para borrar
- Nunca borra: cryptoverdad.db, models/, .json configs, 3 MP4 más recientes, pipelines recientes
- Borra: media >7 días, temp_frames, MoviePy temps, logs >14 días
- Ejecutar: `railway run python scripts/cleanup_volume.py --confirm`

PASO 3 (Política permanente):
- `nexus_core.py._check_disk_space()`: verifica espacio antes de cada pipeline
  Si <500MB libres → notifica Telegram + aborta con mensaje claro
- `nexus_core.py._cleanup_pipeline_temps()`: tras subida exitosa a YouTube, borra
  automáticamente temp_frames, WAV intermedios, clips Pexels, TEMP_MPY_*
- `kairos.py`: VOLUME_GUARDIAN — limpieza diaria 03:00 UTC via cleanup_volume.py
- `kairos.py._maybe_run_volume_health_check()`: llama MERCURY.volume_health_check() cada 6h

PASO 4 (Monitoreo):
- `mercury.py.volume_health_check()`: nuevo método independiente del pipeline
  >70% → alerta Telegram amarilla
  >85% → alerta roja + cleanup automático inmediato
  >95% → CRISIS alert urgente
  Formato: emoji + nivel + uso% + top 3 consumidores + acción tomada
- Tabla `volume_cleanup_log` en SQLite: registra cada ejecución del guardian

**Para verificar fix (en Railway):**
```
railway run python scripts/cleanup_volume.py                    # ver qué va a borrar
railway run python scripts/cleanup_volume.py --confirm          # borrar
railway run python -c "
from database.db import DBManager
from agents.herald.mercury import MERCURY
import json
db = DBManager('/app/output/cryptoverdad.db')
m = MERCURY({}, db)
print(json.dumps(m.volume_health_check(), indent=2))
"                                                               # estado del volumen
```

**Estado tras fix:**
- Pipeline: pendiente verificar (ejecutar cleanup + railway up)
- Primer pipeline post-fix: pendiente (lanzar manualmente después de cleanup)

**Pendiente para próxima sesión:**
- Verificar los 4 bugs originales (resolución, gráfico BTC, thumbnails, quality gate)
- Validar que los 18 cambios del 30/04 funcionan correctamente
- ETH/SOL en ticker, Short barras, CALÍOPE inglés, 12 escenas rotación

### Sesión 2026-04-16 — Level-up: 18 mejoras en 5 equipos paralelos

**Qué se hizo:**
Deploy de 18 mejoras implementadas con 5 agentes paralelos (commit 5777838):

FORGE (4): Jaccard fix real en CALÍOPE (script_raw pre-clean_script) · gráfico animado en
DAEDALUS (FuncAnimation, velas progresivas, zoom, sinc audio) · lower thirds en HEPHAESTUS
(slide-in ease-out cúbico) · thumbnails emocionales en IRIS (rojo si BTC<-3%, banner ALERTA
si fear_greed<25 o caída>7%)

ORÁCULO (3): PYTHIA scoring 7 capas + 10 RSS feeds + deduplicación · ARGOS _fetch_onchain_signals
(mempool.space + blockchain.info + alternative.me, sin API keys) · THEMIS _generate_topic_from_news
(LLM genera topic específico cuando el topic es genérico o vacío)

HERALD (3): OLYMPUS _add_end_screens + _enrich_description_with_affiliates (Binance/Coinbase/Ledger) ·
PROTEUS nuevo agente (Twitter thread 5 tweets + post LinkedIn + blog HTML schema SEO) ·
AURORA upload con thumbnail cover + youtube_url explícito en caption

MIND (3): ALETHEIA A/B real (_select_ab_thumbnail con confidence SQLite + ab_swap_queue) ·
MNEME YouTube Analytics v2 (avg_view_percentage, watch_time → learning_context para CALÍOPE) ·
KAIROS _process_ab_swap_queue en cada ciclo del scheduler

CORE (5): _run_crisis_pipeline (>=5x volatilidad → 3 escenas, <90s) · context.py 12 nuevos
campos (crisis_mode, event_type, onchain_signals, thumbnail_sentiment, chart_animated_path...) ·
/pipeline/stream SSE en web/app.py · dashboard.html Alpine.js live status card ·
mplfinance añadido a requirements.txt

**Bugs originales detectados pero SIN verificar en producción:**
- BUG 1 (resolución): ffprobe post-render añadido pero no confirmado con pipeline real
- BUG 2 (gráfico BTC stale): Binance OHLCV implementado pero caché puede tener datos viejos
- BUG 3 (thumbnails): thumbnails.set falla 403 si canal sin verificar teléfono — sin fix real
- BUG 4 (quality gate): validate_before_publish implementado pero orden de ejecución sin probar

**Errores y warnings de la sesión:**
- UnicodeEncodeError en terminal Windows al imprimir emojis con cp1252 (no afecta Railway)
- ffprobe no disponible en PATH local Windows (funciona en Railway Linux)
- FORGE agent hizo `railway up --detach` antes de que completaran ORÁCULO/HERALD/MIND
  → se hizo un segundo `railway up` final con todos los cambios (commit 5777838)
- Hardcoded fallback en argos.py corregido: BTC $74k/ETH $2.34k/SOL $130 (Binance spot verificado)

**Siguiente paso concreto:**
Dry-run local para confirmar los 4 bugs antes del próximo pipeline Railway (10:00 UTC):
  python main.py --dry-run  (o equivalente sin --auto)
Luego: railway logs --tail 100 para buscar los nuevos logs de PROTEUS, ARGOS on-chain y Jaccard.

### Sesión 2026-04-15 — AGORA refactor + deploy verificado
- AGORA: YOUTUBE_CLIENT_SECRET_PATH → YOUTUBE_CLIENT_SECRETS_B64 (base64, como OLYMPUS)
  agora.py decodifica a tempfile en runtime — sin archivos en disco, compatible Railway
- Imports añadidos: base64, tempfile
- Deploy Railway verificado 08:10 UTC — arranque limpio, KAIROS activo, próximo pipeline 10:00 UTC

### Sesión 2026-04-14 — Auditoría brutal + 12 fixes de producción
- Auditoría reveló: KAIROS nunca ejecutaba en Railway (solo programaba futuro),
  youtube_url NULL en DB, SEO score 50 por keyword con puntuación, 50 falsos positivos
  en CALÍOPE uniqueness, bitrate MP4 sin especificar (~4MB para 5min), TTS silencio
- 10 fixes implementados: KAIROS grace+cron, OLYMPUS URL, HERMES 5 títulos,
  CALÍOPE Jaccard, ECHO tts_engine, HEPHAESTUS bitrate 4000k, ARGOS 24h%, VECTOR pytrends, MNEME analytics
- 2 fixes adicionales: HERMES keyword strip puntuación, NexusCore bloqueo SEO<70
- Primer pipeline real con Coqui TTS verificado: d711feb0, 4:37 min, 0 errores
  YouTube: https://youtu.be/jf7cLzvRAoY · Topic: "¿BTC a $70,000?"

### Sesión 2026-04-13 — Coqui TTS + scheduling
- Kokoro TTS → Coqui TTS (voces robóticas vs voz natural española)
- KAIROS: 18:00 UTC → 10:00 UTC + _reset_stale_defaults
- THEMIS: auto-topic desde noticias (no más "análisis crypto diario")
- URGENCY_DETECTOR: menos ruido (is_urgent solo si score≥70 AND keyword/move≥5%)

### Sesión 2026-04-12 — 3 bugs producción + Kokoro TTS (luego reemplazado)
- ECHO: ElevenLabs bytes→int fix + Kokoro como motor (luego descartado)
- IRIS: _apply_dark_overlay con paste+mask (sin RGBA, compatible Railway)
- HEPHAESTUS: force_size en todos los _write_clip principales

### Sesión 2026-04-11 — Deploy Railway en producción
- .gitignore: excluye latsync/8.1GB, liveportrait/709MB, sadtalker/2.5GB
- railway.toml: CMD python main.py --auto · healthcheck /health
- OAuth2 headless: YOUTUBE_TOKEN_B64 → Credentials sin browser
- ARGOS: fallback nunca lanza excepción, retry exponencial CoinGecko

### Sesión 2026-04-10 — Duración vídeo + Short nativo + layout
- max_tokens y _min_words corregidos para vídeos 4-12 min
- Short como formato independiente (150 palabras, audio propio)
- Constantes _YT_*/SH_* en hephaestus.py — layout pixel-perfect

### Sesiones 2026-04-04 a 2026-04-09 — Construcción base
- HEPHAESTUS v3 (1920x1080 garantizado, 12 escenas ANALISIS)
- SENTINEL completo (AGORA, SCROLL, ARGONAUT, CROESUS)
- MIND completo (MNEME, KAIROS, ALETHEIA)
- FORGE completo (CALÍOPE 9 modos, HERMES, ECHO, IRIS, DAEDALUS)
- PROMETHEUS/LatentSync/LivePortrait construidos pero desactivados
- Assets SD: avatar_carlos_base.png + studio_background.png
