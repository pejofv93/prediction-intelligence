# Estado del Sistema — 13 de Mayo 2026

## Servicios Cloud Run activos

| Servicio | Revisión activa | URL |
|---|---|---|
| sports-agent | `sports-agent-00359-84x` | https://sports-agent-cragcibmwq-ew.a.run.app |
| telegram-bot | `telegram-bot-00247-v4n` | https://telegram-bot-cragcibmwq-ew.a.run.app |
| polymarket-agent | `polymarket-agent-00302-kq5` | https://polymarket-agent-cragcibmwq-ew.a.run.app |
| dashboard | `dashboard-00233-wj8` | https://dashboard-cragcibmwq-ew.a.run.app |
| market-sentinel | `market-sentinel-00005-9t2` | https://market-sentinel-cragcibmwq-ew.a.run.app |

Región: `europe-west1` · Proyecto: `prediction-intelligence`

---

## Fixes implementados hoy (13/05/2026)

### FIX 1 — odds-api.io 429 rate limit (`value_bet_engine.py`)
**Commit:** `0a6b773`

**Problema:** `_ODDSAPIIO_CACHE` no almacenaba resultados vacíos (cuando odds-api.io devolvía
429 o 0 eventos). Esto causaba CACHE_MISS constante (~cada 2s por liga), llamando a
`get_league_odds` en cada partido analizado. El cliente interno (`_EVENT_CACHE`) servía desde
su caché pero el bucle agotaba los 100 req/hora del plan gratuito en minutos.

**Fix:** `_ODDSAPIIO_CACHE` ahora almacena siempre el resultado:
- Eventos reales → TTL 24h
- Vacío/error (429) → TTL 30 min (`_ODDSAPIIO_CACHE_ERR_TTL`)
- El campo `is_error: bool` distingue ambos casos

**Verificado en logs post-deploy:**
- FL1: 1 CACHE_MISS → 7 partidos con `CACHE_HIT → 3 eventos err=False`
- PD: 1 CACHE_MISS → todos los partidos con `CACHE_HIT → 0 eventos err=True`

### FIX 2 — The Odds API NBA 401 (`basketball_analyzer.py`)
**Commit:** `0a6b773`

**Problema:** `basketball_nba` devuelve HTTP 401 con la key actual — el plan free no incluye
NBA. Cada analyze de baloncesto spameaba dos requests 401 por partido.

**Fix:** Early return `[]` para `sport_key == "basketball_nba"` antes del request HTTP.
El análisis NBA sigue operativo usando odds-api.io o modelo puro si aquella también falla.

### Fix previo del deploy (mismo día)
**Problema:** Deploy incorrecto via `gcloud builds submit services/sports-agent` sin copiar
`shared/` previamente → `ModuleNotFoundError: No module named 'shared'` en producción
(revisión `sports-agent-00358-9p5` rota).

**Causa raíz:** El Dockerfile de sports-agent espera que `shared/` sea copiado al directorio
del servicio antes del build context (documentado en Makefile raíz).

**Fix:** Segundo deploy con el proceso correcto:
```bash
cp -r shared/ services/sports-agent/shared/
gcloud run deploy sports-agent --source services/sports-agent \
  --project=prediction-intelligence --region=europe-west1 \
  --account=pejocanal@gmail.com --quiet
rm -rf services/sports-agent/shared/
```

---

## Métricas del día (13/05/2026)

### Sports Agent
- **Analyze 04:49 UTC:** 6 señales enviadas a Telegram (sent=True)
  - 540706 VfB Stuttgart edge=16.6%
  - 540710 edge=0% (sintético)
  - 542664 Racing Club de Lens edge=11.6%
  - 542665 Stade Brestois 29 edge=5.7%
  - 542703 edge=17.0%
  - 544573 edge=0% (sintético)
- **Analyze 10:17 UTC:** 7 señales generadas, todas deduplicadas (ya enviadas ayer 12/05 20:43 UTC)
- **Analyze 11:57 UTC (post-fix):** 7 señales generadas, todas deduplicadas (ya enviadas en analyze 04:49)
- **Sports collect:** último a 08:36 UTC ✓
- **Sports enrich:** activo

### Polymarket Agent
- **Analyze 11:56 UTC:** 90/90 mercados enriquecidos en 85.5s
  - 98 mercados analizados en últimas 12h
  - 85 excluidos por cooldown de rotación
  - 18 mercados analizados en esta pasada
  - 0 alertas nuevas (filtros: SKIP_EDGE, SKIP_CONF, SKIP_REC, PASS)
- **Alertas enviadas hoy:** 8 (polymarket) + 26 (sports) = **34 total**

### APIs — Estado de cuotas
| API | Key | Estado |
|---|---|---|
| odds-api.io (`ODDSAPIIO_KEY`) | `6c4644...` | Válida — rate limit 429 corregido con caché |
| The Odds API (`ODDS_API_KEY`) | `0c42d51...` | Cuota mensual **agotada** (0 restantes) |
| OddsPapi (`ODDSPAPI_KEY`) | `7e937978...` | Cuota mensual **agotada** |

---

## Schedulers activos (GitHub Actions)

| Workflow | Cron (UTC) | Descripción |
|---|---|---|
| `sports-collect` | `0 */6 * * *` | Recopila fixtures cada 6h |
| `sports-enrich` | `30 */6 * * *` | Enriquece stats cada 6h (+30min offset) |
| `sports-analyze` | `0 1,7,13,19 * * *` | Analiza y genera señales 4×/día |
| `polymarket-scan` | `0 */2 * * *` | Escanea mercados cada 2h |
| `polymarket-enrich` | `30 3,9,15,21 * * *` | Enriquece mercados 4×/día |
| `polymarket-analyze` | `0 4,10,16,22 * * *` | Analiza y genera señales 4×/día |
| `poly-price-monitor` | `*/30 * * * *` | Monitor de precios cada 30min |
| `poly-news-trigger` | `*/30 * * * *` | Trigger por noticias cada 30min |
| `polymarket-learn` | `30 3 * * *` | Learning engine diario 03:30 UTC |
| `polymarket-resolve` | `0 3 * * *` | Cierra mercados resueltos 03:00 UTC |
| `daily-report` | `0 7 * * *` | Reporte diario 07:00 UTC (09:00 Madrid) |
| `weekly-report` | `0 8 * * 1` | Reporte semanal lunes 08:00 UTC |

---

## Pendientes (orden de prioridad)

### Crítico
1. **ELO en DEFAULT para todos los equipos** — todos los `generate_signal` muestran
   "ELO en DEFAULT para ambos equipos → señal ELO excluida del ensemble". Sin ELO real
   el modelo solo usa Poisson + form. Requiere poblar la colección `team_stats` con ratings ELO.

2. **odds-api.io PD (La Liga)** — devuelve 0 eventos consistentemente (no es rate limit,
   es ausencia de cobertura para ese slug). Confirmar si odds-api.io tiene cobertura de LaLiga
   o mapear a un slug distinto.

3. **The Odds API cuota mensual agotada** — se renueva el 1 de junio. Hasta entonces
   solo funciona odds-api.io. Basketball_euroleague también sin odds hasta renovación.

### Moderado
4. **Proceso de deploy documentado** — añadir Makefile target `deploy-sports` que automatice
   el `cp shared/ → gcloud run deploy → rm shared/` para evitar el error de hoy.

5. **NBA odds-api.io cobertura** — verificar si `ODDSAPIIO_KEY` tiene cobertura NBA para
   reactivar señales de baloncesto con odds reales (actualmente modelo puro sin alertas).

6. **PL (Premier League) en odds-api.io** — verificar cobertura real. Los partidos PL hoy
   generaron señales pero sin odds externas (POISSON_SYNTHETIC).

### Bajo
7. **market-sentinel** — revisión `00005-9t2` parece inactiva (sin logs recientes). Verificar
   si tiene algún scheduler propio o si fue reemplazado por otro servicio.

8. **Backtest sports** — `run-backtest-sports` activo pero sin ejecuciones recientes confirmadas.
   Verificar que el backtester tiene datos suficientes para métricas de accuracy.

---

## Arquitectura de fuentes de odds (estado actual)

```
Fútbol:
  Primaria:   odds-api.io (ODDSAPIIO_KEY) — 100 req/hora — caché 24h ok / 30min error
  Secundaria: The Odds API (ODDS_API_KEY) — AGOTADA hasta 1 junio
  Terciaria:  OddsPapi (ODDSPAPI_KEY)     — AGOTADA hasta 1 junio

Baloncesto NBA:
  The Odds API — EXCLUIDA (401 plan free)
  odds-api.io  — cobertura por verificar
  Fallback:    modelo puro sin alertas

Baloncesto Euroleague:
  The Odds API — AGOTADA hasta 1 junio
  odds-api.io  — cobertura por verificar
```

---

## Prompt de continuación

Para retomar el trabajo en la próxima sesión:

```
Contexto: prediction-intelligence-ok en Cloud Run (europe-west1, proyecto prediction-intelligence).
Account: pejocanal@gmail.com

Estado al 13/05/2026:
- sports-agent-00359-84x desplegado y operativo
- Fix cache odds-api.io 30min + exclusión NBA de The Odds API (commit 0a6b773)
- 34 alertas enviadas hoy (26 sports + 8 polymarket)
- The Odds API y OddsPapi: cuotas mensuales AGOTADAS (renuevan 1 junio)
- odds-api.io: operativo, 100 req/hora, caché fix activo
- ELO DEFAULT para todos los equipos (problema mayor sin resolver)

Pendiente prioritario:
1. Verificar cobertura NBA en odds-api.io (ODDSAPIIO_KEY)
2. Verificar cobertura PD/LaLiga en odds-api.io
3. Poblar ELO ratings en Firestore (colección team_stats)
4. Añadir Makefile target deploy-sports para automatizar cp shared/ + deploy + cleanup

Proceso correcto de deploy sports-agent:
  cp -r shared/ services/sports-agent/shared/
  gcloud run deploy sports-agent --source services/sports-agent \
    --project=prediction-intelligence --region=europe-west1 \
    --account=pejocanal@gmail.com --quiet
  rm -rf services/sports-agent/shared/
```
