# prediction-intelligence — Knowledge Base
**Última actualización:** 2026-04-28  
**Repo:** github.com/pejofv93/prediction-intelligence  
**Proyecto GCP:** prediction-intelligence  
**Región Cloud Run:** europe-west1

---

## Arquitectura general

Monorepo Python con 5 servicios Cloud Run + shared library:

```
services/
  sports-agent/       → análisis deportivo (football, basketball, tennis)
  polymarket-agent/   → análisis mercados de predicción Polymarket
  telegram-bot/       → bot privado + canal alertas
  dashboard/          → UI web (FastAPI + Jinja2)
shared/               → biblioteca compartida (copiada a cada servicio en deploy)
scripts/              → herramientas admin
.github/workflows/    → CI/CD + jobs periódicos
```

**Deploy:** `deploy.yml` → push a main → `gcloud run deploy` los 4 servicios principales.  
**BD:** Firestore (proyecto prediction-intelligence, base de datos `(default)`)  
**Prefijo colecciones prod:** `prod` → `prodpredictions`, `prodenriched_markets`, etc.

---

## shared/ — Biblioteca compartida

| Archivo | Rol |
|---|---|
| `config.py` | Todas las vars de entorno + thresholds (`SPORTS_MIN_EDGE=0.08`, `POLY_MIN_EDGE=0.08`) |
| `firestore_client.py` | `col(name)` sync + `async_col(name)` async. Singleton de cliente. |
| `groq_client.py` | Cliente Groq con rotación de modelos y `GROQ_CALL_DELAY` |
| `model_health.py` | Health check: si `total_predictions < 20` → skip sin tocar thresholds |
| `shadow_engine.py` | Shadow trading: registra señales, cierra trades, calcula CLV |
| `unified_score.py` | Puntuación unificada con time-decay factor |
| `report_generator.py` | Formato de reportes Telegram |
| `api_quota_manager.py` | Cuotas diarias: `the_odds_api(500)`, `api_sports(100)`, `football_data(500)`, `oddspapi(50)`, `apifootball(80)` |

---

## polymarket-agent

### Pipeline de datos
```
/run-scan → scanner.py (Gamma API, limit=200, min_vol=500)
         → poly_markets en Firestore (hasta 145-200 mercados)

/run-enrich → market_enricher.py (lee poly_markets, limit=100)
           → enriched_markets en Firestore
           → guarda: question, volume_24h, price_momentum, volume_spike,
                     smart_money, orderbook, correlations, arbitrage,
                     news_sentiment, data_quality, enriched_at

/run-analyze → lee enriched_markets (order_by enriched_at DESC, limit=100)
            → balanceo: top 5 por categoría (crypto/politics/economy/sports/geopolitics/other)
            → groq_analyzer.py → poly_predictions en Firestore
            → alert_engine.py → POST telegram-bot si edge>0.08 AND conf>0.65
```

### Validador de consistencia de probabilidad (2026-04-26)
`groq_analyzer._validate_prob_in_reasoning(real_prob, reasoning)`:
- Extrae menciones de probabilidad del texto `reasoning` (patrones `0.xxx` y `N%`)
- Si alguna difiere de `real_prob` del JSON estructurado en más de ±0.10 → warning log
- Prepende `[prob estructurada: XX%]` al reasoning para que el mensaje Telegram sea inequívoco
- `real_prob` del JSON es siempre el valor canónico — el reasoning es informacional

### Validador de precio crypto
En `groq_analyzer._validate_crypto_price_prediction()`:
- Precios hardcoded (actualizar si hay cambio >20%): BTC≈$94k, ETH≈$3.2k, SOL≈$140
- Caps de probabilidad máxima:
  - variación > 200% en cualquier plazo → prob máxima 0.15
  - variación > 100% en < 12 meses → prob máxima 0.25
  - variación > 50% en < 3 meses → prob máxima 0.35
  - caída > 80% → prob máxima 0.10

### Thresholds de alerta
- `POLY_MIN_EDGE = 0.08`, `POLY_MIN_CONFIDENCE = 0.65`
- Re-alerta permitida si: >24h desde última + precio cambió >5%
- Dedup: `prodalerts_sent`, `alert_key = f"{market_id}_{round(edge, 2)}"`
- Dedup guardado DESPUÉS del POST exitoso (evita bloqueos en cold-start)

### Colecciones Firestore
| Colección | Contenido |
|---|---|
| `prodpoly_markets` | Mercados activos del scan |
| `prodenriched_markets` | Mercados enriquecidos |
| `prodpoly_predictions` | Predicciones Groq (edge, confidence, real_prob, category, reasoning) |
| `prodalerts_sent` | Registro dedup de alertas |
| `prodpoly_price_history` | Snapshots de precio |

### Crons de workflows (CEST = UTC+2)
| Workflow | Cron UTC | Hora Madrid (CEST) |
|---|---|---|
| `polymarket-scan.yml` | `51 */2 * * *` | cada 2h a :51 |
| `polymarket-enrich.yml` | `40 */2 * * *` | cada 2h a :40 |
| `polymarket-analyze.yml` | `0 4,10,16,22 * * *` | 06:00/12:00/**18:00**/00:00 |

**Fix 2026-04-26:** `polymarket-analyze` ajustado a UTC+2 (antes disparaba a las 20:00 Madrid, no a las 18:00). `polymarket-enrich` sigue cada 2 horas como antes.

---

## sports-agent

### Pipeline de datos
```
/run-collect → football_api.py (football-data.org, 6.5s rate limit)
            → allsports_client.py (selecciones + Sudamérica)
            → tennis_collector.py (ATP/WTA)
            → basketball_collector.py (NBA + EuroLeague)
            → api_sports_client.py (NFL/MLB/NHL/MMA — sin analyzer aún)
            → upcoming_matches en Firestore

/run-enrich → data_enricher.py + enrichers/ (Poisson, ELO, form, H2H)
           → enriched_matches en Firestore

/run-analyze → value_bet_engine.generate_signal() para cada enriched_match
            → football_markets.generate_football_extra_signals() (BTTS/AH/Totals/DC)
            → corners_bookings.generate_corners_signals()
            → tennis_analyzer.generate_tennis_signals()
            → basketball_analyzer.generate_basketball_signals()
            → player_props.generate_player_props_signals()
            → predpredictions en Firestore
            → alerta Telegram si edge > SPORTS_ALERT_EDGE (0.08)
```

### Crons sports (Madrid CEST)
| Workflow | Cron UTC | Hora Madrid |
|---|---|---|
| `sports-collect.yml` | `0 */6 * * *` | 02/08/14/20 |
| `sports-enrich.yml` | `30 */6 * * *` | 02:30/08:30/14:30/20:30 |
| `sports-analyze.yml` | `0 */6 * * *` | 02/08/14/20 |

### Fuentes de odds — cadena de prioridad (2026-04-28)
```
Para h2h (1X2) — fetch_bookmaker_odds() en value_bet_engine.py:
  1. Firestore odds_cache (TTL 4h)
  2. odds-api.io (PRIMARIA — 100 req/h; 1 request /events para todas las ligas)
     → _fetch_all_soccer_events() → filtro local por keywords de liga
     → _fetch_odds_batch(bookmakers=bet365,bwin,1xbet,betfair,unibet)
  3. The Odds API (secundaria — 500/mes; agotada hasta ~1 mayo)
  4. OddsPapi (terciaria — 250/mes; agotada hasta ~1 mayo)
  5. Fallback Poisson sintético (si all_sources_down=True)

Para BTTS / Double Chance / Asian Handicap / Totals 3.5:
  1. OddsPapi v1 → _fetch_oddspapi_league()
  2. The Odds API (del caché con markets expandidos)
  3. API-Football → apifootball_odds.get_match_odds()

Para Corners / Bookings (1X2):
  1. OddsPapi v4 → _fetch_fixtures_for_date()
  → guarda en prodpredictions
```

**Estado APIs (2026-04-28):**
| API | Estado | Límite | Notas |
|---|---|---|---|
| **odds-api.io** | ✅ Activa | 100 req/hora | Free tier. 1 req/analyze para events. `/odds/multi` necesita param `bookmakers`. TTL 3600s en 429. |
| **football-data.org** | ✅ Activa | 10 req/min | 6.5s delay. Team stats tardan ~30min con 89 partidos. |
| **The Odds API** | ⏳ Agotada | 500/mes | Renova ~1 mayo 2026 |
| **OddsPapi v1+v4** | ⏳ Agotada | 250/mes | Renova ~1 mayo; pendiente GitHub Secret `ODDSPAPI_KEY` |
| **API-Football** (RapidAPI) | ✅ Activa | 100 req/día | `FOOTBALL_RAPID_API_KEY`. apifootball_odds.py fallback activo. |
| **API-Basketball** (RapidAPI) | ❌ No suscrito | 500/mes free | Necesita suscripción manual en rapidapi.com |
| **Tennis API** (tennisapi1) | ✅ Activa | — | Host corregido, endpoint `/atp/tournaments` |
| **allsports_client** | 🚫 Desactivado | — | `COLLECTOR_DISABLED=True`. Endpoint `/football/` → 404 |
| **api-mma (MMA)** | 🚫 Desactivado | — | `_DISABLED_SPORTS`. api-mma.p.rapidapi.com → 404 |
| **DuckDuckGo** | ✅ Activa | — | Reemplazó Tavily en polymarket news_sentiment |

### Fallback apifootball_odds.py (NUEVO 2026-04-26)
`services/sports-agent/collectors/apifootball_odds.py`:
- Reutiliza `FOOTBALL_RAPID_API_KEY` (api-football-v1.p.rapidapi.com)
- `/v3/fixtures?date=DATE&league=ID` → cache TTL 12h (1 req/liga/día)
- `/v3/odds?fixture=ID` → cache TTL 1h (1 req/partido)
- Parsea: BTTS (bet 5), Goals O/U (bet 15), Double Chance (bet 6), AH (bet 4)
- Se activa en `football_markets.py` cuando `not op_ev` (OddsPapi no disponible)
- Log de inicio: `"apifootball_odds: módulo cargado — FOOTBALL_RAPID_API_KEY presente: True/False"`

### Bug activo — mercados alternativos sin señales (investigando)
**Síntoma:** `sports-analyze` corre verde (114s) pero no llegan alertas de BTTS/totales/AH/corners.

**Causas root encontradas y corregidas (2026-04-26):**
1. `if not op_ev and not event` → solo se activaba si AMBAS fuentes eran None → caché h2h-only de The Odds API impedía el fallback. **Fix:** `if not op_ev`
2. `PL` eliminada de `_ODDS_SPORT_MAP` en mayo 2025, nunca re-añadida para 25/26. `ELC` también faltaba. **Fix:** re-añadidas ambas.
3. `corners_bookings.save_signals()` escribía a `corners_signals` en vez de `predictions`. **Fix:** ahora escribe a `predictions`, un doc por señal.
4. `markets=h2h` en The Odds API → ningún parser de BTTS/spreads encontraba datos. **Fix:** `markets=h2h,btts,spreads,totals,alternate_totals,double_chance,draw_no_bet,team_totals`

**Pendiente de verificar** en próximo analyze post-deploy:
- Log diagnóstico en `_bg_analyze`: buscar `"ligas en enriched="` en Cloud Run → confirmar PL/ELC aparecen con cobertura
- Log en `football_markets.py`: buscar `"fallback API-Football activo"` → confirma que apifootball_odds se ejecuta
- Comprobar `prodpredictions` por docs con `market_type=btts/asian_handicap/totals_3.5`
- Si sigue sin señales: posible que API-Football no tenga odds para esas ligas/fechas (cobertura variable)

**Comandos de diagnóstico:**
```bash
# Logs Cloud Run
gcloud logging read 'resource.labels.service_name="sports-agent" AND textPayload=~"ligas en enriched|fallback API-Football|BTTS|btts"' \
  --project=prediction-intelligence --limit=20 --format="value(textPayload)" --freshness=3h

# Query Firestore (requiere IAM Firestore — usar cuenta con permisos)
# Accounts actuales (pejocanal@gmail.com / pejofeve@gmail.com) → 403 Firestore
# Solución: añadir rol roles/datastore.viewer en IAM Console
```

### Cobertura de ligas — mapa completo (2026-04-26)

**Fútbol masculino Europa** (h2h + BTTS/AH/totals):
PL, ELC, PD, SD, BL1, BL2, SA, SB, FL1, FL2, CL, EL, ECL, PPL, DED, TU1

**Fútbol masculino Sudamérica** (h2h + BTTS/AH/totals):
BSA, ARG, CLI, CSUD

**Selecciones / torneos** (solo h2h, Poisson excluido):
NL, WCQ (Europa, AllSports ID 1182), CAM, EC/Euro, WC

**Sin colector activo — mapa listo para cuando se implemente:**
- WCQ_CONMEBOL, WCQ_CONCACAF, WCQ_AFC, WCQ_CAF — eliminatorias otras confederaciones
- INTL — friendlies internacionales
- W_WWC, W_WEURO, W_WNATIONS, W_WCL — fútbol femenino internacional
- W_WSL, W_NWSL, W_LIGA_F, W_D1F, W_FRAUEN_BL — ligas domésticas femeninas (Poisson ✓ cuando haya datos)
- ACB, NCAA_BB, FIBA_WC, EUROBASKET — baloncesto adicional

**Tenis (via tennis_analyzer._TENNIS_SPORT_KEYS, independiente de _ODDS_SPORT_MAP):**
Grand Slams: AUS_OPEN, FRENCH_OPEN, WIMBLEDON, US_OPEN (ATP + WTA)
Masters 1000 activos en primavera: BARCELONA, MADRID, ROME, MUNICH (+ WTA MADRID, ROME, STUTTGART)
Fallback genérico: ATP → `tennis_atp`, WTA → `tennis_wta`, ITF → `tennis_itf`

**IDs de API-Football marcados `⚠️ verify`** (8 entradas — confirmar en prod):
W_WWC(8), CAM(9), W_WEURO(50), W_FRAUEN_BL(57), W_WCL(545), W_WSL(253), W_NWSL(264), W_D1F(519), W_LIGA_F(750), W_WNATIONS(956)

**The Odds API keys marcadas `⚠️ verify`:**
WCQ_CONCACAF, WCQ_AFC, WCQ_CAF, W_WEURO, W_WNATIONS, W_WCL, W_LIGA_F, W_D1F, W_FRAUEN_BL, ACB, FIBA_WC, EUROBASKET
→ Verificar con: `curl "https://api.the-odds-api.com/v4/sports?apiKey=KEY"`

### Modelos de predicción
- **Ensemble principal:** Poisson (0.40) + ELO (0.25) + Form (0.20) + H2H (0.15)
- **Thresholds:** `SPORTS_MIN_EDGE=0.08`, `SPORTS_MIN_CONFIDENCE=0.65`
- `_FOOTBALL_SPORT_KEYS`: ligas domésticas con Poisson válido (Europa + BSA/ARG/CLI/CSUD + ligas femeninas cuando tengan datos)
- Competiciones internacionales de selecciones: **excluidas** de `_FOOTBALL_SPORT_KEYS` (Poisson no fiable sin historial estable de equipo)

### Colecciones Firestore sports-agent
| Colección | Contenido |
|---|---|
| `produpcoming_matches` | Próximos partidos (todas las ligas/deportes) |
| `prodenriched_matches` | Partidos enriquecidos con Poisson+ELO+form+H2H |
| `prodpredictions` | Señales de value bet (h2h, BTTS, AH, corners, totals, player_props) |
| `prodteam_stats` | Stats por equipo (TTL 6h en collect) |
| `prodh2h_data` | Histórico H2H (TTL 6h en collect) |
| `prodleague_odds_cache` | Cache The Odds API por liga (TTL 8h) |
| `prodapi_quotas` | Contadores de cuota diarios por API |
| `prodcorners_signals` | **Colección legacy — ya no se usa** (fix 2026-04-26) |

---

## telegram-bot

URL: `https://telegram-bot-327240737877.europe-west1.run.app`

**Endpoints:**
- `POST /send-alert` — recibe `{"type": "sports"|"polymarket", "data": {...}}` con header `x-cloud-token`
- `POST /webhook` — webhook Telegram para comandos del bot

**Mensaje Polymarket (formato actual):**
```
🔮 OPORTUNIDAD POLYMARKET
❓ {question}
📈 Precio mercado YES: XX%
🎯 Probabilidad estimada: XX%
💎 Edge: +XX% | Confianza: XX%
Recomendación: BUY_YES|BUY_NO|PASS|WATCH
[🐋 SMART MONEY detectado]
💭 {reasoning}  ← prefijado con [prob estructurada: XX%] si hay inconsistencia
```

---

## GitHub Actions Workflows

| Workflow | Cron UTC | Hora Madrid (CEST) | Descripción |
|---|---|---|---|
| `deploy.yml` | push a main | — | Deploy todos los servicios |
| `sports-collect.yml` | `0 */6 * * *` | 02/08/14/20 | Trigger /run-collect |
| `sports-enrich.yml` | `30 */6 * * *` | 02:30/08:30/14:30/20:30 | Trigger /run-enrich |
| `sports-analyze.yml` | `0 */6 * * *` | 02/08/14/20 | Trigger /run-analyze |
| `polymarket-scan.yml` | `51 */2 * * *` | cada 2h | Trigger /run-scan |
| `polymarket-enrich.yml` | `40 */2 * * *` | cada 2h | Trigger /run-enrich |
| `polymarket-analyze.yml` | `0 4,10,16,22 * * *` | 06/12/**18**/00 | Trigger /run-analyze |
| `daily-report.yml` | `0 9 * * *` | 11:00 | Reporte matutino |
| `weekly-report.yml` | `0 9 * * 1` | 11:00 lun | Resumen semanal |

**Secretos necesarios:**
- `GCP_SA_KEY`, `CLOUD_RUN_TOKEN`, `FIRESTORE_COLLECTION_PREFIX=prod`
- `GROQ_API_KEY`, `COINGECKO_API_KEY`
- `FOOTBALL_API_KEY`, `FOOTBALL_RAPID_API_KEY`, `ODDS_API_KEY`
- `ODDSPAPI_KEY` ← **pendiente añadir en GitHub Secrets** (key existe, secret no creado aún)
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_BOT_URL`

---

## Cloud Run — Revisiones activas

| Servicio | Revisión (aprox.) | Memory | Timeout |
|---|---|---|---|
| sports-agent | post-commit 4179158 | 512Mi | 1800s |
| polymarket-agent | post-commit 4179158 | 256Mi | 300s |
| telegram-bot | — | 256Mi | 60s |
| dashboard | — | 512Mi | 60s |

**Todos con `min-instances=0`** → cold-start frecuente. Los workflows usan streaming (`StreamingResponse` con pings cada 30s) para mantener la conexión viva en Cloud Run. Timeout configurado a 1800s en sports-agent (collect puede tardar ~15min).

**Problema conocido:** tareas de background muy largas (>8-10 min) pueden ser cortadas si Cloud Run decide escalar a cero antes de que terminen. Solución: `min-instances=1` (~$15-30/mes).

**Acceso Firestore local:** cuentas `pejocanal@gmail.com` y `pejofeve@gmail.com` reciben 403. El ADC local tiene token válido pero sin rol `roles/datastore.viewer`. Para diagnóstico local usar Cloud Console o añadir el rol en IAM.

---

## Variables de entorno (Cloud Run prod)

```
GOOGLE_CLOUD_PROJECT=prediction-intelligence
FIRESTORE_COLLECTION_PREFIX=prod
GROQ_API_KEY=gsk_...
CLOUD_RUN_TOKEN=b299426b...
TELEGRAM_BOT_URL=https://telegram-bot-327240737877.europe-west1.run.app
FOOTBALL_API_KEY=...            # football-data.org
FOOTBALL_RAPID_API_KEY=...      # API-Football + API-Sports + Tennis API
ODDS_API_KEY=...                # The Odds API (agotada hasta ~1 mayo 2026)
ODDSPAPI_KEY=...                # OddsPapi (agotada hasta ~1 mayo; pendiente GitHub Secret)
# COINGECKO_API_KEY no crítica (polymarket usa CoinGecko gratis)
# TAVILY_API_KEY eliminada (reemplazada por DuckDuckGo, sin API key)
```

---

## Firestore — Esquema relevante

### prodmodel_weights/current
```json
{
  "min_edge_threshold": 0.08,
  "min_confidence": 0.65,
  "total_predictions": 6,
  "correct_predictions": 0,
  "version": 4,
  "weights": {"poisson": 0.41, "elo": 0.23, "form": 0.20, "h2h": 0.16},
  "health_override": "manual_reset_20260424"
}
```

### prodpredictions (esquema unificado post 2026-04-26)
```json
{
  "match_id": "12345_btts_sí",
  "home_team": "Real Madrid",
  "away_team": "Barcelona",
  "sport": "football",
  "league": "PD",
  "market_type": "btts",
  "selection": "BTTS Sí",
  "odds": 1.85,
  "calculated_prob": 0.62,
  "edge": 0.08,
  "confidence": 0.71,
  "kelly_fraction": 0.04,
  "data_source": "poisson_extras",
  "odds_source": "oddspapi|theoddsapi|api-football",
  "created_at": "2026-04-26T...",
  "result": null,
  "correct": null
}
```

---

## Notas de implementación críticas

### Firestore en Cloud Run (min-instances=0)
- El canal gRPC se establece en el primer `GetDocument` (unary) al arrancar via `retroactive_eval`
- `stream()` SIN limit puede tardar > 5 min → usar `limit(N).stream(timeout=60.0)`
- Monkey-patch en polymarket-agent `main.py` para `_retry_query_after_exception`:
  ```python
  from google.cloud.firestore_v1.query import Query as _FSQuery
  _orig = _FSQuery._retry_query_after_exception
  def _safe(self, exc, retry, transaction):
      try: return _orig(self, exc, retry, transaction)
      except AttributeError: return False
  _FSQuery._retry_query_after_exception = _safe
  ```

### MoviePy (NEXUS / CryptoVerdad — otro proyecto, ignorar en sports context)
- Usar SIEMPRE `from moviepy.editor import ...`

### Deploy manual de un servicio
```bash
cp -r shared/ services/sports-agent/shared/
gcloud run deploy sports-agent \
  --source services/sports-agent \
  --project prediction-intelligence --region europe-west1 \
  --allow-unauthenticated --timeout=1800 --min-instances=0 --memory=512Mi --cpu=1 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=prediction-intelligence,FOOTBALL_API_KEY=...,FOOTBALL_RAPID_API_KEY=...,CLOUD_RUN_TOKEN=...,TELEGRAM_BOT_URL=...,FIRESTORE_COLLECTION_PREFIX=prod,ODDS_API_KEY=...,ODDSPAPI_KEY=..."
```

### Reset de threshold en Firestore
```bash
GOOGLE_CLOUD_PROJECT=prediction-intelligence FIRESTORE_COLLECTION_PREFIX=prod \
python scripts/reset_threshold.py
```

---

---

## ESTADO OPERATIVO (2026-04-28)

### Servicios Cloud Run activos
| Servicio | URL | Último commit | Estado |
|---|---|---|---|
| sports-agent | sports-agent-cragcibmwq-ew.a.run.app | 3190693 | ✅ OK |
| polymarket-agent | — | — | ✅ OK |
| telegram-bot | telegram-bot-327240737877.europe-west1.run.app | — | ✅ OK |
| dashboard | — | — | ✅ OK |

### Colectores sports-agent
| Colector | Archivo | Estado | Razón |
|---|---|---|---|
| Football (football-data.org) | `football_api.py` | ✅ Activo | Fuente principal ligas europeas + Copa Lib |
| Odds primarias | `odds_apiio_client.py` | ✅ Activo | 1 req/analyze para todos los eventos soccer |
| Tennis | `tennis_collector.py` | ✅ Activo | Host: tennisapi1.p.rapidapi.com, endpoint: /atp/tournaments |
| Basketball | `basketball_collector.py` | ✅ Activo | Euroleague + NBA (NBA sin suscripción API-Basketball) |
| AllSports | `allsports_client.py` | 🚫 Desactivado | `COLLECTOR_DISABLED=True` — /football/ → 404 |
| MMA/UFC | `api_sports_client.py` | 🚫 Desactivado | `_DISABLED_SPORTS` — api-mma 404 |

### Pipeline diario (última ejecución exitosa: 2026-04-28)
- **Collect**: 30min (89 partidos, 167 equipos, team_stats con raw_matches ≥ 3)
- **Enrich**: ~30s si docs frescos (<6h), ~15min si todos stale
- **Analyze**: ~149s, **18 señales generadas** (POISSON_SYNTHETIC, sin odds reales)
  - Señales europeas (PL, BL1, SA, DED): Poisson real del collect
  - Señales Copa Lib (CLI, BSA): ELO sintético (exención Poisson activa)

---

## BUGS CONOCIDOS / PENDIENTES (2026-04-28)

### 🔴 Alta prioridad

**1. odds-api.io — /odds/multi sin cuotas reales**
- `/odds/multi?eventIds=...&bookmakers=bet365,bwin,...` añadido (param era obligatorio → 400 "Missing bookmakers")
- No confirmado funcionando: rate limit 429 activo al testearlo. Verificar en próximo analyze post-reset (~18:30 UTC)
- Si funciona: 18 señales POISSON_SYNTHETIC → señales con edge real de bookmaker

**2. NBA — sin suscripción API-Basketball**
- `api-basketball.p.rapidapi.com` → 403 "You are not subscribed"
- Fix: suscribirse en rapidapi.com (plan Free, 500 req/mes, misma key `FOOTBALL_RAPID_API_KEY`)
- Sin acción de código — solo activación manual

**3. The Odds API + OddsPapi agotadas**
- Ambas resetean ~1 mayo 2026
- Al renovar OddsPapi: añadir `ODDSPAPI_KEY` como GitHub Secret

### 🟡 Media prioridad

**4. shadow_trades ROI 0% / win_rate 0%**
- Fix desplegado (2026-04-28): `learning_engine.run_daily_learning()` ahora sincroniza shadow_trades
- Efectivo en el próximo run del learning-engine (02:00 UTC)
- Hasta entonces, daily-report muestra ROI/win_rate 0%

**5. allsports_client desactivado — ligas sin cobertura**
- Afecta: NL (UEFA Nations League), WCQ Europa (1182), ARG (Liga Argentina), CSUD (Copa Sudamericana), CAM
- Necesita nuevo endpoint o nueva API para estas ligas

**6. Enrich timeout con 89 partidos**
- Con stale_threshold 6h, si todos los docs tienen >6h el enrich procesa todos (~15-30min)
- `sports-enrich.yml` tiene `--max-time 1800` y `timeout-minutes: 35` — debería aguantar

### 🟢 Baja prioridad / backlog

**7. Firestore IAM acceso local**
- `pejocanal@gmail.com` y `pejofeve@gmail.com` → 403 Firestore en local
- Fix: añadir `roles/datastore.viewer` en GCP IAM Console

**8. IDs API marcados ⚠️ sin verificar**
- Verificar el 1 mayo con cuotas renovadas: W_WWC(8), CAM(9), W_WEURO(50), W_FRAUEN_BL(57), W_WCL(545), W_WSL(253), W_NWSL(264), W_D1F(519), W_LIGA_F(750), W_WNATIONS(956)

**9. Colectores pendientes (mapa listo)**
- WCQ_CONCACAF / WCQ_AFC / WCQ_CAF
- INTL (friendlies)
- Fútbol femenino (W_WSL, W_NWSL, W_LIGA_F, W_D1F, W_FRAUEN_BL)
- Baloncesto adicional (ACB, NCAA_BB, FIBA_WC, EUROBASKET)

**10. `min-instances=1` en sports-agent**
- Evita cold-start y truncado de análisis >8min (~$15-30/mes)

---

## Estado del proyecto (2026-04-28)

### Sesiones completadas ✅

- [x] **Sesión 1** — Arquitectura base, pipeline collect→enrich→analyze, deploy Cloud Run
- [x] **Sesión 2** — Polymarket agent, scan→enrich→analyze→Telegram
- [x] **Sesión 3** — Mapa de ligas comprehensivo, fuentes de odds, corners/BTTS/AH
- [x] **Sesión 4** — Fix mercados alternativos (PL/ELC re-añadidas, corners→predictions, The Odds API markets expandidos)
- [x] **Sesión 5** — API-Football fallback (apifootball_odds.py), prob consistency validator polymarket
- [x] **Sesión 6** — Diagnóstico completo sports-agent: Poisson guard, collect team_stats, enrich timeout, shadow_trades sync (2026-04-28)
- [x] **Sesión 7** — odds-api.io: 1 request/analyze, bookmakers param, rate-limited TTL 3600s, colectores muertos desactivados, NBA/tenis hosts (2026-04-28)

### Métricas actuales (2026-04-28 17:39 UTC)
- **Señales generadas hoy:** 18 (POISSON_SYNTHETIC — sin odds externas reales aún)
- **Predicciones totales:** 29 (15 resueltas, 14 pendientes)
- **Collect:** 30min / 89 partidos / 167 equipos
- **odds-api.io rate limit:** 100 req/hora — agotado en el analyze de 17:18 UTC; reset ~18:18 UTC

### Próximos pasos prioritarios
1. **[URGENTE] Verificar /odds/multi con bookmakers** → lanzar analyze post-reset 18:30 UTC y buscar "con odds" > 0
2. **[URGENTE] Suscribir NBA en RapidAPI** → rapidapi.com, API-Basketball, plan Free
3. **[MEDIA] Renovar The Odds API + OddsPapi ~1 mayo** → añadir `ODDSPAPI_KEY` GitHub Secret al renovar
4. **[MEDIA] Añadir IAM Firestore** a pejofeve@gmail.com para diagnóstico local
5. **[BAJA] Reactivar allsports_client** → encontrar nuevo endpoint o API para NL/WCQ/ARG/CSUD
6. **[BAJA] Colector fútbol femenino** — WSL + NWSL primer paso
7. **[BAJA] Actualizar precios hardcoded validador crypto** (BTC $94k / ETH $3.2k)
