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
  AGORA    → agora.py    — Comentarios YouTube (requiere YOUTUBE_CLIENT_SECRET_PATH)
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
              Bloqueo SEO < 70 implementado en _run_herald()
ORÁCULO:      [x] ARGOS (precios reales + 24h% historial) · PYTHIA · THEMIS (auto-topic)
              [~] RECON — placeholder, requiere YOUTUBE_API_KEY en Railway
              [~] VECTOR — pytrends en requirements.txt, integración parcial
FORGE:        [x] CALÍOPE (9 modos, Jaccard uniqueness, max_tokens correctos)
              [x] HERMES (5 variantes título, keyword sin puntuación, score real)
              [x] ECHO (Coqui TTS verificado en producción, ctx.tts_engine registrado)
              [x] HEPHAESTUS v3 (FULLSCREEN, Short nativo, bitrate 4000k, 1920x1080)
              [x] IRIS · DAEDALUS (12 tipos de gráfico)
              [~] HELIOS v3 — pendiente saldo FAL_KEY
HERALD:       [x] OLYMPUS (público, OAuth2 headless, youtube_url persistida en pipelines)
              [~] RAPID — Playwright falla sin cookies TikTok
              [x] MERCURY (Telegram canal)
SENTINEL:     [x] AGORA · SCROLL · CROESUS · ARGONAUT — sentinel_agent.py integrado
              [~] AGORA comentarios — requiere YOUTUBE_CLIENT_SECRET_PATH en Railway
MIND:         [x] MNEME (analytics cada 6h) · KAIROS (10:00 UTC + grace 3h) · ALETHEIA
PANEL WEB:    [x] web/app.py + 5 templates + /force-pipeline endpoint (requiere CRON_SECRET)
BOT TELEGRAM: [~] MERCURY cubre canal — bot privado pendiente
DEPLOY:       [x] LIVE en Railway — pipeline diario 10:00 UTC
              [x] Primer pipeline con Coqui TTS: 2026-04-14 (d711feb0, 4:37 min, 0 errores)
              [x] YouTube URL activa: https://youtu.be/jf7cLzvRAoY
              Volumen persistente: /app/output (audio, video, charts, models)

## Railway — Variables de entorno
Configuradas: GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PEXELS_API_KEY,
              YOUTUBE_TOKEN_B64 (token.json en base64, refresh_token activo)
Pendiente:    CRON_SECRET (para /force-pipeline), YOUTUBE_API_KEY (RECON),
              YOUTUBE_CLIENT_SECRET_PATH (AGORA), COINGECKO_API_KEY (pro),
              FAL_KEY con saldo (HELIOS v3)

## Layout pixel-perfect (constantes en hephaestus.py)
YouTube 1920x1080: Ticker y=0/h=40 | Gráfico y=40/h=950 | Subs y=990/h=90
Short  1080x1920:  Logo y=10/h=68 | Gráfico y=80/h=1700 | Subs y=1780/h=80 | Ticker y=1860/h=60

## Próximos pasos (orden prioridad negocio)
1. Verificar próximo pipeline 2026-04-15 10:00 UTC:
   railway logs --tail 50 | grep -E "(SEO Score|Motor TTS|Short|youtube_url|Errores)"
   Esperar: SEO Score ≥ 70 (fix keyword activo), "Motor: Coqui TTS", Short generado
2. RAPID/TikTok — subir cookies tiktok-uploader → publicación automática
3. RECON — añadir YOUTUBE_API_KEY en Railway Dashboard (distinta del OAuth2)
4. AGORA — configurar YOUTUBE_CLIENT_SECRET_PATH para responder comentarios
5. Railway Pro plan → activar cron [[crons]] en railway.toml (actualmente Hobby)
6. BOT TELEGRAM privado (comandos /estado, /forzar, /parar)
7. HELIOS v3 — añadir saldo en fal.ai → python test_helios.py

## Notas críticas de implementación

### MoviePy
- Usar SIEMPRE: `from moviepy.editor import ...`
- NO usar API v2 (with_audio, resized, subclipped, with_position)
- bitrate="4000k", audio_bitrate="192k" obligatorio en _write_clip()
- temp_audiofile siempre en output/video/ (no en CWD)

### ARGOS — CoinGecko en Railway
- IPs Railway comparten rate limit → 429 frecuente
- Orden: CoinGecko API → caché SQLite (5min) → fallback hardcoded (BTC 72k/ETH 2200/SOL 84)
- ORÁCULO no es fatal — sus errores no detienen el pipeline
- 24h% calculado desde oracle_prices SQLite cuando CoinGecko devuelve 0.0

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

### DB — Esquema relevante
- tabla pipelines: id, topic, mode, status, youtube_url, seo_score, created_at
- tabla videos: id, pipeline_id, youtube_id, views, likes (actualizado cada 6h)
- tabla oracle_prices: coin, price, recorded_at (base para 24h%)
- tabla market_prices: coin_id, price, recorded_at

## Historial de sesiones (resumen)

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
