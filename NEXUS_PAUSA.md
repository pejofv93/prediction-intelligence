# NEXUS — En Pausa
Fecha: 2026-05-11

## Estado al pausar
- Último vídeo publicado: https://youtu.be/ybybf4_bh0I
- Pipeline: funcionando (fixes volumen + ECHO 600s + Short desacoplado + PIL ANTIALIAS + 6 bugs más aplicados)
- Railway: CANCELADO por coste inesperado (€30)

## Archivos críticos en local
- `cryptoverdad_backup.db` — base de datos completa (raíz del repo)
- `railway_env_backup.txt` — variables de entorno completas (raíz del repo)

## Variables necesarias para relanzar
```
GROQ_API_KEY
GEMINI_API_KEY
OPENROUTER_API_KEY
CEREBRAS_API_KEY
ELEVENLABS_API_KEY
FAL_KEY
PEXELS_API_KEY
YOUTUBE_TOKEN_B64
YOUTUBE_TOKEN
YOUTUBE_CLIENT_SECRET
YOUTUBE_CLIENT_SECRETS_B64
YOUTUBE_API_KEY
TIKTOK_SESSION_ID
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_ADMIN_ID
INSTAGRAM_USERNAME
INSTAGRAM_PASSWORD
CRON_SECRET
WEB_PIN
PYTHONUTF8
PYTHONIOENCODING
NEXUS_ENV
```
(Valores completos en `railway_env_backup.txt`)

## Bugs pendientes para cuando se relance
1. Coqui TTS truncando → gTTS de facto (voz peor calidad)
2. 12 escenas análisis no rotan (variedad visual)
3. ETH/SOL ticker precios incorrectos
4. CALÍOPE genera inglés en algunos modos
5. Shorts: verificar barras negras en producción

## Próximos pasos al relanzar (orden)
1. Short nativo: ECHO genera audio separado para `ctx.short_script` → activa ruta `_compose_short_vertical`
2. Verificar Mercury Telegram con fix Markdown (retry sin parse_mode)
3. RAPID/TikTok: subir cookies `tiktok-uploader`
4. ETH/SOL ticker fix en argos.py
5. CALÍOPE modo inglés: revisar prompt system language

## Destino de migración
Decidir entre:
- **Hetzner CX22 (€3.79/mes)** — RECOMENDADO
- Oracle Cloud segunda VM (verificar cupo disponible)

## Para relanzar en el nuevo servidor
```bash
# 1. Clonar repo
git clone <repo-url> nexus && cd nexus

# 2. Crear .env con las variables de railway_env_backup.txt
cp railway_env_backup.txt .env  # luego editar formato KEY=VALUE

# 3. Copiar DB al volumen del servidor
mkdir -p output
cp cryptoverdad_backup.db output/cryptoverdad.db

# 4. Build y arranque
docker compose build && docker compose up -d

# 5. Verificar
python main.py --dry-run

# 6. Confirmar notificación Telegram llega

# 7. Lanzar primer pipeline real
```

## Commits clave de referencia
- `2d33adc` — 6 bugs post-pipeline (Short PIL + QG desacoplado + Mercury + MNEME + ARGONAUT + DB)
- `b053f61` — docs sesión 2026-05-04
- `5777838` — 18 mejoras (FORGE/ORÁCULO/HERALD/MIND/CORE)
- `d711feb0` — primer pipeline Coqui TTS verificado (2026-04-14)

## Vídeos publicados
- https://youtu.be/jf7cLzvRAoY (primer pipeline real, 2026-04-14)
- https://youtu.be/ybybf4_bh0I (pipeline 183bf153, 2026-05-04)
