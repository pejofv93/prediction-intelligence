# Estado del Sistema — 14 de Mayo 2026

## Servicios Cloud Run activos

| Servicio | Revisión activa | URL |
|---|---|---|
| sports-agent | `sports-agent-00365-z8p` | https://sports-agent-cragcibmwq-ew.a.run.app |
| telegram-bot | `telegram-bot-00257-854` | https://telegram-bot-cragcibmwq-ew.a.run.app |
| polymarket-agent | `polymarket-agent-00309-ck5` | https://polymarket-agent-cragcibmwq-ew.a.run.app |
| dashboard | `dashboard-00237-jhx` | https://dashboard-cragcibmwq-ew.a.run.app |
| market-sentinel | `market-sentinel-00005-9t2` | https://market-sentinel-cragcibmwq-ew.a.run.app |

Región: `europe-west1` · Proyecto: `prediction-intelligence` · Account: `pejocanal@gmail.com`

---

## Commits implementados hoy (14/05/2026)

### FIX 1 — NBA O/U edge espurio +48% (`basketball_analyzer.py`)
**Commit:** `882423f`

**Problema:** Cuando `goals_home`/`goals_away` vienen a 0 del Firestore (datos stale o campo
incorrecto), `exp_total ≈ 3.2` (solo el home advantage). Con una línea NBA de 220.5, el z-score
era `(220.5 − 3.2) / 14.4 = 15` → p_under ≈ 100% → edge fake del 48%.

**Fix:** Guard `if exp_total < 50: tot = None` antes del análisis O/U. La señal se descarta
silenciosamente en lugar de generar un edge imposible. Lo mismo para H1 totals con flag
`_h1_scale_ok`.

---

### FIX 2 — Alertas NBA/ACB/Tenis no llegaban a Telegram (`basketball_analyzer.py`, `tennis_analyzer.py`)
**Commit:** `882423f`

**Problema:** `_save_and_alert` era una función síncrona que usaba
`loop.create_task(_send_telegram_alert(...))` — fire-and-forget que no se ejecutaba de forma
fiable en Python 3.10+ (la task podía ser garbage-collected o el loop context incorrecto).
El fútbol funcionaba porque su `_save_and_alert` ya era `async` con `await`.

**Fix:** `_save_and_alert` → `async def` + todos los call sites actualizados a `await _save_and_alert(...)`.
Aplicado en `basketball_analyzer.py` y `tennis_analyzer.py` (6 call sites en cada uno).

---

### FIX 3 — Tenis: fallback The Odds API para Roland Garros (`tennis_collector.py`)
**Commit:** `a4720a0`

**Problema:** `tennisapi1.p.rapidapi.com` devuelve 404 desde 2026-05-01. Con Roland Garros
empezando el 19 de mayo (5 días), el collector no podía descubrir partidos.

**Fix:** Añadido `collect_tennis_from_odds_api()` con lista `_FALLBACK_TENNIS_KEYS` que incluye:
- `tennis_atp_french_open` / `tennis_wta_french_open` (Roland Garros)
- ATP/WTA principales
- Outros torneos Slam

El collector principal llama al fallback si RapidAPI devuelve 0 torneos. Usa el endpoint
`/v4/sports/{sport_key}/events` de The Odds API.

---

### FIX 4 — Daily report: formato compacto de tiers (`shared/model_health.py`)
**Commit:** `e7bf17f`

**Problema:** El bloque de tiers listaba señales individuales — texto demasiado largo para Telegram.

**Fix:** `_tier_line()` compacto:
```
🔥 Fuertes: 33 | Win rate: X% | ROI: X%
✅ Detectadas: 47 | Win rate: X% | ROI: X%
📊 Moderadas: 39 | Win rate: X% | ROI: X%
📈 Total: 119 señales | Win rate: 33% | ROI: -21%
```
Solo muestra Win rate / ROI si hay señales resueltas en ese tier.

---

### FIX 5 — Polymarket: ancla ±15% para mercados sin datos externos (`groq_analyzer.py`, `alert_manager.py`)
**Commit:** `0125f55`

**Problema:** Para mercados de categoría `geopolitics`, `politics`, `business`, `other` sin
datos DDG/precios spot, el LLM inventaba probabilidades sin ancla objetiva, generando edges
espurios similares al bug ELO=1500 de sports.

**Fix — 4 capas:**

1. **Contexto de categoría actualizado** en `_build_category_context()`: geopolitics, politics,
   business y other ahora incluyen instrucción explícita de ancla ±15% cuando no hay datos.

2. **Instrucción en prompt** (`data_quality == "improvised"`): bloque añadido al user_prompt
   indicando el rango permitido `[price_yes − 0.15, price_yes + 0.15]`.

3. **Hard cap post-LLM**: si el LLM ignora la instrucción, `real_prob` se fuerza al rango
   `±15%` de `price_yes` y `edge` se recalcula.

4. **Campo `data_quality`** en `poly_predictions` Firestore:
   - `"external_data"` — tiene headlines DDG, precio spot o contexto deportivo
   - `"market_only"` — orderbook/momentum pero sin DDG data
   - `"improvised"` — sin datos externos verificables

5. **Telegram alert** muestra `⚠️ Sin datos externos verificables — ancla: precio mercado ±15%`
   cuando `data_quality == "improvised"`.

---

## Estado de deploys del día

| Servicio | Revisión anterior | Revisión nueva | Motivo |
|---|---|---|---|
| sports-agent | `00359-84x` (13/05) | `00365-z8p` | FIX 1+2+3 (commit 882423f + a4720a0) |
| telegram-bot | `00255-nhs` (13/05) | `00257-854` | FIX 5 alert_manager.py (commit 0125f55) |
| polymarket-agent | `00302-kq5` (13/05) | `00309-ck5` | FIX 5 groq_analyzer.py (commit 0125f55) |
| dashboard | sin cambios | `00237-jhx` | — |

---

## Schedulers activos (GitHub Actions)

| Workflow | Cron (UTC) | Descripción |
|---|---|---|
| `sports-collect` | `0 */6 * * *` | Recopila fixtures cada 6h |
| `sports-enrich` | `30 */6 * * *` | Enriquece stats cada 6h |
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

## Estado de APIs de odds

| API | Key (prefix) | Estado |
|---|---|---|
| odds-api.io (`ODDSAPIIO_KEY`) | `6c4644...` | Operativa — caché 24h OK / 30min error |
| The Odds API (`ODDS_API_KEY`) | `0c42d51...` | Cuota mensual **AGOTADA** (renueva 1 junio) |
| OddsPapi (`ODDSPAPI_KEY`) | `7e937978...` | Cuota mensual **AGOTADA** (renueva 1 junio) |

**Arquitectura de fuentes de odds (estado actual):**
```
Fútbol:
  Primaria:  odds-api.io (ODDSAPIIO_KEY) — 100 req/hora — operativa
  Secundaria: The Odds API               — AGOTADA hasta 1 junio
  Terciaria:  OddsPapi                   — AGOTADA hasta 1 junio

Baloncesto NBA:
  The Odds API — EXCLUIDA (401 plan free no cubre NBA)
  odds-api.io  — cobertura pendiente verificar
  Fallback: modelo puro (sin odds reales → sin alertas)

Baloncesto Euroleague:
  The Odds API — AGOTADA hasta 1 junio
  odds-api.io  — cobertura pendiente verificar

Tenis:
  RapidAPI tennisapi1 — ROTO desde 2026-05-01 (404)
  The Odds API fallback — activo para Roland Garros + Slams (FIX 3)
```

---

## Problemas conocidos sin resolver

### Crítico
1. **ELO en DEFAULT para todos los equipos** — `generate_signal` siempre muestra
   "ELO en DEFAULT → señal ELO excluida del ensemble". El modelo opera solo con
   Poisson + form. Requiere poblar la colección `team_stats` en Firestore con ELO ratings reales.

2. **Roland Garros (19 mayo)** — El fallback de tenis via The Odds API está implementado,
   pero The Odds API tiene la cuota mensual agotada. Si no se renueva antes del 19/05 o se
   consigue otra fuente, no habrá partidos de tenis disponibles.
   **Acción requerida:** verificar si The Odds API tiene cuota restante para sports_key
   `tennis_atp_french_open` (pueden ser contadores separados) o activar otra fuente.

### Moderado
3. **odds-api.io PD (La Liga)** — devuelve 0 eventos consistentemente. No es rate limit,
   es posible falta de cobertura para ese slug. Verificar mapeo correcto de liga.

4. **NBA en odds-api.io** — sin verificar si `ODDSAPIIO_KEY` tiene cobertura NBA.
   Hasta que se confirme, NBA opera sin odds reales.

5. **Señales sintéticas en daily report** — algunas señales aparecen como `POISSON_SYNTHETIC`
   (sin odds reales). Con The Odds API y OddsPapi agotadas, esto es esperado hasta 1 junio.

### Bajo
6. **market-sentinel** — revisión `00005-9t2` sin cambios ni logs recientes.
   Verificar si tiene scheduler propio o fue sustituido funcionalmente.

7. **Short barras negras (NEXUS)** — bug conocido en hephaestus.py, fuera del scope
   de prediction-intelligence.

---

## Pendientes (orden de prioridad)

1. **[URGENTE antes 19/05]** Verificar cuota The Odds API para Roland Garros específicamente.
   Si agotada → buscar fuente alternativa de partidos de tenis (Sofascore scraping, ATP API, etc.)

2. **ELO ratings** — poblar `team_stats` en Firestore. Sin ELO, el ensemble está incompleto
   y la calibración de confianza es menos precisa.

3. **Verificar señales de tenis post-FIX 2** — confirmar que tras el fix `async _save_and_alert`,
   las señales de tenis llegan al topic Sports de Telegram.

4. **Verificar cobertura odds-api.io** — comprobar si tiene NBA y PD/LaLiga con requests
   directos a la API (slug correcto).

5. **data_quality en dashboard** — mostrar el campo en la vista de señales Polymarket del dashboard.

---

## Prompt de continuación

```
Contexto: prediction-intelligence-ok en Cloud Run (europe-west1, proyecto prediction-intelligence).
Account: pejocanal@gmail.com

Estado al 14/05/2026:
- sports-agent-00365-z8p: FIX NBA O/U espurio + alertas async basket/tenis + tennis fallback Odds API
- polymarket-agent-00309-ck5: ancla ±15% + data_quality field para mercados sin datos externos
- telegram-bot-00257-854: warning ⚠️ improvised en alertas Polymarket
- The Odds API y OddsPapi: cuotas AGOTADAS (renuevan 1 junio)
- ELO DEFAULT para todos los equipos (problema sin resolver)
- Roland Garros empieza 19/05 — tennis fallback implementado pero The Odds API puede estar agotada

Pendiente prioritario:
1. Verificar cuota The Odds API para tennis_atp/wta_french_open antes del 19/05
2. ELO ratings en Firestore (team_stats)
3. Confirmar señales tenis llegan a Telegram tras fix async
4. Verificar cobertura odds-api.io NBA + La Liga

Proceso de deploy:
  cp -r shared/ services/<servicio>/shared/
  gcloud run deploy <servicio> --source services/<servicio> \
    --project=prediction-intelligence --region=europe-west1 \
    --account=pejocanal@gmail.com --quiet
  rm -rf services/<servicio>/shared/
```
