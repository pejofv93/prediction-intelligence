# Estado del Sistema — 4 Mayo 2026

## Servicios Cloud Run activos

| Servicio | Revisión actual | URL |
|---|---|---|
| `polymarket-agent` | `00225-q9z` | https://polymarket-agent-cragcibmwq-ew.a.run.app |
| `dashboard` | `00174-trq` | https://dashboard-cragcibmwq-ew.a.run.app |
| `sports-agent` | `00259-g5m` | https://sports-agent-cragcibmwq-ew.a.run.app |
| `telegram-bot` | `00185-47s` | https://telegram-bot-cragcibmwq-ew.a.run.app |
| `market-sentinel` | `00005-9t2` | https://market-sentinel-cragcibmwq-ew.a.run.app |

Proyecto GCP: `prediction-intelligence` · Región: `europe-west1`

---

## Mejoras implementadas hoy (commits de la sesión)

### `f648976` — Bloque A/B/C: Sports signals
- **Block A — Contexto externo lesiones/rotaciones** (`value_bet_engine.py`, `basketball_analyzer.py`)
  - 3 queries Tavily en paralelo para lesiones/rotaciones antes de generar señal
  - Penaliza confianza: −15% si titular clave lesionado, −10% si rotación detectada, cap 0.75
  - NBA: detección back-to-back desde calendarios ESPN ±5% prob adjustment
- **Block B — Detector odds movement** (`line_movement.py`, `value_bet_engine.py`)
  - `_detect_odds_movement()` compara snapshots 6h y 24h en `odds_history` Firestore
  - Flags: `SMART_MONEY` (bajada >10% en 6h) / `FADING` (subida >15% en 24h)
  - Añade `odds_movement` y `external_context` al doc de predicción
- **Block C — Dedup máx 2 señales por partido** (`main.py`)
  - Agrupa por `market_type` (h2h vs alternativos), guarda solo el de mejor edge
  - Elimina extras de Firestore

### `f076770` — Bloque D: Wallet tracker Polymarket
- `wallet_tracker.py` nuevo — CLOB API inteligencia de ballenas
- Heurística: >10 trades y win_rate >65% → smart wallet
- `check_wallet_activity()` ajusta confidence ±0.10 si ballena detectada
- Integrado en `main.py` bloque analyze + línea 🐋 en alert Telegram

### `254d2c7` — Bloque E: Spread + correlación
- **E1** — Spread orderbook >8% → mercado ilíquido → confidence ×0.80, factor `illiquid_spread_XX%`
- **E2** — Si ≥3 keywords compartidas con mercados correlacionados y precio diverge >15% → pull real_prob 30% hacia media correlacionada, recalcula edge y recommendation

### `409f316` — Bloque F: Umbrales dinámicos + news trigger
- Umbrales dinámicos por categoría (≥5 outcomes): accuracy <50% → +3% edge mínimo, >70% → −2%
- `news_trigger.py`: busca top 10 mercados cada 30min con DDG/Yahoo News, detecta keywords de alto impacto → POST `/analyze-urgent`
- Endpoints nuevos en `main.py`: `POST /run-news-trigger` y `POST /analyze-urgent?market_id=`
- GH Actions `poly-news-trigger.yml`: cron `*/30 * * * *`

### `3904755` — Bloque G: Dashboard status field
- `services/dashboard/api/predictions.py`: status calculado en query-time (sin writes Firestore)
  - `RESUELTO` — tiene `result`
  - `OBSOLETA` — sin resultado, >48h desde `match_date`
  - `PENDIENTE_RESULTADO` — sin resultado, >2h desde `match_date`
  - `PENDIENTE` — sin resultado, <2h desde `match_date`

### `7863cc2` — Fix wallet tracker CLOB 401
- CLOB `/trades` requiere L2 auth (no es endpoint público)
- Guard en `POLYMARKET_CLOB_KEY` env var: si no configurado → skip inmediato, sin llamadas HTTP
- Lookup correcto de `condition_id` (hex) desde `poly_markets` Firestore antes de llamar CLOB
- Elimina completamente el ruido de 401 en logs

### `4203bc0` — Fix Groq señales contradictorias ← **CRÍTICO**
- **Problema**: `edge < 0 pero rec=BUY_YES` → antes se descartaba → 0 señales
- **Fix en `SYSTEM_PROMPT`**: regla explícita — rec DEBE coincidir con signo del edge
- **Fix en `user_prompt`**: verificación final por-mercado con precio YES real
- **Fix en post-parse**: auto-corrección `BUY_YES→BUY_NO` (o viceversa) en lugar de descarte
- **Resultado inmediato**: `analizados=0 alertas=0` → `analizados=5 alertas=3` en primer run post-deploy

---

## Métricas actuales (4 mayo 2026)

### Polymarket
| Run | Hora UTC | Total | Analizados | Alertas |
|---|---|---|---|---|
| Post-fix groq | 21:17 | 5 | 5 | **3** |
| Pre-fix groq | 20:56 | 5 | 0 | 0 |
| 20:06 | 20:06 | 6 | 2 | 1 |
| 11:43 | 11:43 | 6 | 3 | 1 |
| 06:33 | 06:33 | 5 | 3 | 1 |

- Accuracy global Polymarket: **15 outcomes registrados** (< 20 → umbral_mode=base activo)
- `POLY_MIN_EDGE = 0.08` · `POLY_MIN_CONFIDENCE = 0.65`
- Alertas del día: ~6 señales BUY_NO en mercados geopolíticos (EE.UU.-Irán, Ormuz, AI models)
- Shadow trades activos: sí (shadow_engine registra cada señal)

### Sports
- Señales enviadas hoy: ≥2 alertas Telegram confirmadas en logs (20:19 UTC)
- Sports min edge: `0.08` (8%)
- Copa Libertadores: sin cuotas bookmaker disponibles en API (0 señales estas ligas)
- NBA: 2 partidos analizados en último run, 2 alertas

### news_trigger (nuevo, primer día)
- **Funcionando**: encuentra HIGH IMPACT en 2/10 mercados (elecciones 2028, Tribunal Supremo)
- **Pendiente**: `POLY_AGENT_URL` + `CLOUD_RUN_TOKEN` sin configurar en GH Secrets → trigger no se ejecuta

---

## Schedulers activos (GitHub Actions)

| Workflow | Cron | Descripción |
|---|---|---|
| `polymarket-scan` | `0 */2 * * *` | Escanea nuevos mercados Polymarket cada 2h |
| `polymarket-enrich` | `30 3,9,15,21 * * *` | Enriquece mercados 4×/día (03:30, 09:30, 15:30, 21:30 UTC) |
| `polymarket-analyze` | `0 4,10,16,22 * * *` | Analiza con Groq 4×/día (04:00, 10:00, 16:00, 22:00 UTC) |
| `polymarket-learn` | `30 3 * * *` | Learning engine Polymarket diario a las 03:30 UTC |
| `polymarket-resolve` | `0 3 * * *` | Resuelve mercados cerrados a las 03:00 UTC |
| `poly-news-trigger` | `*/30 * * * *` | Busca breaking news cada 30min ← NUEVO |
| `sports-collect` | `0 */6 * * *` | Recoge partidos cada 6h |
| `sports-enrich` | `30 */6 * * *` | Enriquece partidos 30min después del collect |
| `sports-analyze` | `0 1,7,13,19 * * *` | Analiza señales 4×/día (01:00, 07:00, 13:00, 19:00 UTC) |
| `learning-engine` | `0 2 * * *` | Learning engine sports diario a las 02:00 UTC |
| `weekly-report` | `0 8 * * 1` | Informe semanal lunes 08:00 UTC |
| `daily-report` | `0 7 * * *` | Informe diario 07:00 UTC (09:00 España) |
| `run-backtest-sports` | `0 2 1 * *` | Backtest mensual, día 1 a las 02:00 UTC |

---

## Estado de APIs y secrets

### GCP / Cloud Run
- Proyecto: `prediction-intelligence`
- Cuenta de servicio: `pejocanal@gmail.com`
- GH Secret `GCP_SA_KEY`: configurado ✓

### APIs configuradas en GH Secrets
| Secret | Estado | Uso |
|---|---|---|
| `GROQ_API_KEY` | ✓ configurado | LLM analysis Polymarket |
| `ODDS_API_KEY` | ✓ configurado | Cuotas bookmakers sports |
| `TAVILY_API_KEY` | ✓ configurado | Contexto externo lesiones/rotaciones |
| `TELEGRAM_BOT_URL` | ✓ configurado | Envío alertas |
| `TELEGRAM_CHAT_ID` | ✓ configurado | ID grupo con topics |
| `POLY_AGENT_URL` | ✓ configurado | URL polymarket-agent |
| `CLOUD_RUN_TOKEN` | **✗ FALTA** | Token para `/run-news-trigger` → no se ejecuta el forward trigger |
| `POLYMARKET_CLOB_KEY` | **✗ FALTA** | Auth CLOB API → whale tracking desactivado |

### APIs en Cloud Run env vars
- `FIRESTORE_PROJECT_ID`: prediction-intelligence ✓
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` ✓
- `GROQ_API_KEY`, `ODDS_API_KEY`, `TAVILY_API_KEY` ✓

---

## Pendientes (por prioridad)

### P0 — Crítico para funcionalidad
1. **Añadir `CLOUD_RUN_TOKEN` en GH Secrets**
   - Es el bearer token de la service account para llamar a Cloud Run autenticado
   - Sin él, `poly-news-trigger` detecta noticias pero no puede disparar `/analyze-urgent`
   - Obtener: `gcloud auth print-identity-token --account=pejocanal@gmail.com`
   - Añadir en: GitHub → Settings → Secrets → `CLOUD_RUN_TOKEN`

### P1 — Mejora de señales
2. **`POLYMARKET_CLOB_KEY` para whale tracking**
   - Requiere cuenta Polymarket con API key CLOB Level 2
   - Hasta que esté, el wallet_tracker está desactivado (log informativo, sin errores)
3. **Copa Libertadores / CONMEBOL sin cuotas bookmaker**
   - The Odds API free plan no devuelve cuotas para CLI (Copa Libertadores)
   - Opciones: (a) upgrade plan Odds API, (b) scraping alternativo, (c) ignorar estas ligas

### P2 — Observabilidad y calidad
4. **`prob_consistency` WARNING frecuente**
   - Groq produce `real_prob` en JSON que diverge del texto reasoning
   - El sistema ya usa el JSON (correcto), pero genera ruido de logs
   - Fix posible: bajar a DEBUG si la auto-corrección ya cubre el caso
5. **Accuracy Polymarket baja muestra (15 outcomes)**
   - `umbral_mode=base` activo hasta 20+ outcomes
   - Umbrales dinámicos por categoría y learned thresholds activarán solos al acumular
6. **Verificar topic Telegram correcto para cada tipo de señal**
   - Sports → topic sports
   - Polymarket → topic polymarket
   - Confirmar IDs configurados en telegram-bot

### P3 — Futuro
7. **Implementar step 8 learning_engine sports** (marcar predicciones obsoletas >48h)
8. **news_trigger: errores Yahoo News** (2/10 mercados fallan fetch → explorar Bing News API)
9. **market-sentinel**: servicio activo sin uso claro documentado — revisar qué hace

---

## Comandos útiles

```bash
# Deploy completo (polymarket-agent)
cp -r shared services/polymarket-agent/shared
gcloud run deploy polymarket-agent \
  --source services/polymarket-agent \
  --project prediction-intelligence \
  --region europe-west1 \
  --account pejocanal@gmail.com \
  --allow-unauthenticated --timeout=1200 \
  --min-instances=0 --memory=256Mi --cpu=1 --quiet
rm -rf services/polymarket-agent/shared

# Deploy completo (dashboard)
cp -r shared services/dashboard/shared
gcloud run deploy dashboard \
  --source services/dashboard \
  --project prediction-intelligence \
  --region europe-west1 \
  --account pejocanal@gmail.com \
  --allow-unauthenticated --timeout=300 \
  --min-instances=1 --memory=512Mi --cpu=1 --quiet
rm -rf services/dashboard/shared

# Forzar analyze Polymarket manual
gh workflow run polymarket-analyze.yml --ref main

# Forzar enrich Polymarket
gh workflow run polymarket-enrich.yml --ref main

# Forzar sports analyze
gh workflow run sports-analyze.yml --ref main

# Forzar news trigger
gh workflow run poly-news-trigger.yml --ref main

# Ver logs polymarket-agent (últimos 10 min)
gcloud logging read \
  'resource.type="cloud_run_revision" resource.labels.service_name="polymarket-agent"' \
  --project=prediction-intelligence --freshness=10m --limit=50 \
  --format="value(timestamp,textPayload)" --account=pejocanal@gmail.com

# Ver logs sports-agent
gcloud logging read \
  'resource.type="cloud_run_revision" resource.labels.service_name="sports-agent"' \
  --project=prediction-intelligence --freshness=10m --limit=50 \
  --format="value(timestamp,textPayload)" --account=pejocanal@gmail.com

# Ver revisiones actuales de todos los servicios
gcloud run services list \
  --project prediction-intelligence --region europe-west1 \
  --account pejocanal@gmail.com \
  --format="table(metadata.name,status.latestReadyRevisionName,status.url)"

# Ver últimos runs GH Actions
gh run list --limit 10

# Obtener identity token para CLOUD_RUN_TOKEN
gcloud auth print-identity-token --account=pejocanal@gmail.com
```

---

## Arquitectura de archivos nuevos (esta sesión)

```
services/polymarket-agent/
├── wallet_tracker.py          ← NUEVO: whale tracking CLOB (requiere POLYMARKET_CLOB_KEY)
├── news_trigger.py            ← NUEVO: DDG news search + /analyze-urgent trigger
└── groq_analyzer.py           ← MODIFICADO: auto-corrección rec contradictoria

.github/workflows/
└── poly-news-trigger.yml      ← NUEVO: cron */30 * * * *

services/sports-agent/
├── analyzers/value_bet_engine.py   ← Bloque A (Tavily) + Bloque B (odds movement)
├── analyzers/basketball_analyzer.py ← Bloque A (NBA injuries/back-to-back)
├── analyzers/line_movement.py      ← Bloque B (SMART_MONEY/FADING detector)
└── main.py                         ← Bloque C (dedup ≤2 señales/partido)

services/telegram-bot/
└── alert_manager.py           ← Bloque D (línea 🐋 whale) + odds movement line

services/dashboard/
└── api/predictions.py         ← Bloque G (status PENDIENTE/OBSOLETA/etc.)
```

---

## Prompt de continuación (nueva conversación)

```
Proyecto: prediction-intelligence-ok (C:\Users\Usuario\prediction-intelligence-ok)
GCP project: prediction-intelligence · Región: europe-west1
Cuenta: pejocanal@gmail.com

Estado a 4 mayo 2026 — ver ESTADO_04_MAYO_2026.md en la raíz del proyecto.

SERVICIOS ACTIVOS:
- polymarket-agent: 00225-q9z
- dashboard: 00174-trq
- sports-agent: 00259-g5m
- telegram-bot: 00185-47s

RESUMEN DE LO IMPLEMENTADO:
Esta sesión implementó 7 bloques de mejoras (A-G) más 2 fixes críticos:
- Bloques A/B/C: sports — Tavily injuries, odds movement SMART_MONEY/FADING, dedup ≤2/partido
- Bloque D: wallet_tracker.py (CLOB ballenas, requiere POLYMARKET_CLOB_KEY)
- Bloque E: illiquid spread >8% + corrección por correlación de mercados
- Bloque F: umbrales dinámicos por categoría + news_trigger cada 30min
- Bloque G: dashboard status field (RESUELTO/OBSOLETA/PENDIENTE_RESULTADO/PENDIENTE)
- Fix wallet_tracker: CLOB 401 eliminado, guard POLYMARKET_CLOB_KEY
- Fix groq: señales contradictorias auto-corregidas (BUY_YES↔BUY_NO) en lugar de descartadas
  → Resultado: 0 alertas → 3 alertas en primer run post-fix

PENDIENTES P0 (hacer primero):
1. Añadir CLOUD_RUN_TOKEN en GH Secrets para que news_trigger pueda disparar /analyze-urgent
   (gcloud auth print-identity-token --account=pejocanal@gmail.com)
2. Copa Libertadores sin cuotas bookmaker → explorar alternativa a Odds API free plan

DEPLOY: siempre con cp -r shared services/<servicio>/shared antes de gcloud run deploy,
        y rm -rf services/<servicio>/shared después.
```
