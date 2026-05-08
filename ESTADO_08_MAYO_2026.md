# Estado del Sistema — 8 de mayo de 2026

## Servicios Cloud Run activos

| Servicio | Revisión | Timeout | Memoria | CPU | URL |
|---|---|---|---|---|---|
| polymarket-agent | `00287-gb5` | 1800s | 256Mi | 1 | https://polymarket-agent-327240737877.europe-west1.run.app |
| sports-agent | `00342-mqk` | 1800s | 512Mi | 1 | https://sports-agent-327240737877.europe-west1.run.app |
| telegram-bot | `00225-zt2` | 60s | 256Mi | 1 | https://telegram-bot-327240737877.europe-west1.run.app |
| dashboard | `00212-hs5` | 60s | 512Mi | 1 | https://dashboard-cragcibmwq-ew.a.run.app |

Proyecto GCP: `prediction-intelligence` · Región: `europe-west1` · Cuenta: `pejocanal@gmail.com`

> **Deploy:** `gcloud run deploy` con `CLOUDSDK_PYTHON=C:\Python311\python.exe` y
> `gcloud config set core/custom_ca_certs_file` configurado — SSL resuelto localmente.
> Nunca usar `railway up`.

---

## Deploys del día (8 mayo 2026)

| Hora | Commit | Servicio | Descripción | Estado |
|---|---|---|---|---|
| sesión mañana | `cdcccfe` | sports-agent → `00341-phv` | Contexto situacional: posición tabla + forma + momentum | ✅ |
| sesión mañana | `44269cb` | sports-agent → `00342-mqk` | MOTIVATION_CHECK + fatiga + REST_ADVANTAGE | ✅ |
| sesión mañana | `8fcb6f5` | polymarket-agent → `00287-gb5` | Prompt: correlaciones detalladas + 5 headlines + sports context | ✅ |

---

## Mejoras implementadas hoy

### 1. `value_bet_engine.py` — Contexto situacional en `generate_signal()` (commit `cdcccfe`)

**Bloque 5g** (antes de umbrales de intensidad):

| Check | Condición | Efecto en confianza |
|---|---|---|
| Posición en tabla | rival >3 puestos mejor | ×0.90 (−10%) |
| Posición en tabla | rival >6 puestos mejor | ×0.80 (−20%) |
| Forma comparada | rival_form > sel_form + 20 pts | ×0.90 (−10%) |
| Momentum rival | rival con ≥2 victorias consecutivas | flag `rival_momentum` en `factors` |
| Mala racha propia | equipo seleccionado con ≥2 derrotas | ×0.85 (−15%) + flag `bad_streak` |

- Siempre añade `factors["rival_form"]` para transparencia.
- Log: `CONTEXT_PENALTY(match_id): conf X.XX→Y.YY (razones)`

### 2. `value_bet_engine.py` + `data_enricher.py` — MOTIVATION_CHECK + fatiga + descanso (commit `44269cb`)

**MOTIVATION_CHECK** (reemplaza el bloque de standings — aplica a ambos equipos):

| Estado del equipo | Factor |
|---|---|
| Matemáticamente descendido | ×0.85 (−15%) — ya no descarta, penaliza |
| Matemáticamente campeón | ×0.90 (−10%) — ya no descarta, penaliza |
| Sin nada en juego (`nothing_at_stake`) | ×0.80 (−20%) — igual que antes |
| Top-4 con >5pts de ventaja sobre 5º | ×0.95 (−5%) |
| Zona de descenso (últimos 3, no descendido) | ×1.05 (+5%) — motivación extra |

- Factores se multiplican entre equipos; cap entre 0.70 y 1.05.
- Log: `MOTIVATION_CHECK(match_id): Team1:status | Team2:status`

**Fatiga** (`data_enricher.py`):
- `_days_since_last()`: calcula días entre `match_date` y el último partido en `raw_matches`.
- Añade `home_days_rest` y `away_days_rest` al `enriched_match`.
- Flags implícitos: ≤2d = fatiga alta, 3d = fatiga media, ≥7d = descanso largo.

**Bloque 5h — REST_ADVANTAGE** (`value_bet_engine.py`):

| Condición | Efecto |
|---|---|
| Rival descansó ≥3d más que seleccionado | ×0.90 (−10%) + `factors["rest_disadvantage"]` |
| Seleccionado descansó ≥3d más que rival | ×1.05 (+5%) + `factors["rest_advantage"]` |

- Log: `REST_ADVANTAGE(match_id): home=Xd away=Yd delta=±Nd`
- Ambos factores aparecen en el log `CONTEXT_PENALTY` final.

### 3. `groq_analyzer.py` — 3 mejoras al user_prompt del LLM (commit `8fcb6f5`)

**Correlaciones detalladas** (mejora 1):

Antes:
```
Correlaciones: 3 mercados relacionados
```
Ahora (hasta 5 mercados con pregunta completa + precio):
```
Mercados correlacionados (3):
  · [72% YES ↑] Will Trump sign the executive order on immigration before June?
  · [31% YES ↓] Will the Fed cut rates before the June FOMC meeting?
  · [61% YES ↑] Will Bitcoin exceed $100k by end of Q3 2026?
```

**Headlines** (mejora 2): `[:2]` → `[:5]` — el LLM recibe hasta 5 titulares de noticias.

**Sports context** (mejora 3): instrucción estructurada en 3 pasos obligatorios antes de estimar `real_prob`:
1. POSICIÓN EN TABLA — motivación según zona (top-4 vs descenso)
2. ÚLTIMO RESULTADO — momentum (victorias/derrotas consecutivas)
3. CUOTAS DE BOOKMAKERS — ancla primaria (implied_prob = 1/cuota)
- Si faltan datos → indicar en `key_factors` y reducir `confidence` −0.05

---

## Pipeline de ajustes de confianza en `generate_signal()` — orden completo

```
ANTES de ensemble_probability():
  1. POISSON_GUARD          sin Poisson → ELO sintético o Form sintético o descarta
  2. QUALITY_GUARD          sin datos reales → descarta

  3. MOTIVATION_CHECK       relegated×0.85 / champion×0.90 / nothing_at_stake×0.80 /
                            top4_seguro×0.95 / zona_descenso×1.05 (cap 0.70–1.05)

DESPUÉS de seleccionar team_to_back:
  4. UNDERDOG_EXTREMO       vs top-6 con odds>umbral → descarta
  5. RIVAL_ELITE            vs top-3 → edge×0.80; bottom-6 vs top-3 → descarta
  6. AWAY_FILTERS           zona muerta 2.5–3.5 / PD>2.5 / gate CL/EL/ECL
  7. AWAY_ADDL              odds>6.00 → descarta; odds>4.00 fuera de CL → cap 70%
  8. FORM_POISSON_GATE      form<0.25 y poisson<0.20 → descarta

  9. MOTIVATION_APPLY       aplica _standings_confidence_adj calculado en paso 3

 10. 5g — CONTEXT_PENALTY
       i)  Posición tabla: rival>3 pos → ×0.90; rival>6 pos → ×0.80
       ii) Forma comparada: rival_form > sel+20 → ×0.90; siempre añade rival_form a factors
       iii) Momentum: rival ≥2W → rival_momentum en factors; sel ≥2L → ×0.85 + bad_streak

 11. 5h — REST_ADVANTAGE
       rival descansó ≥3d más → ×0.90; sel descansó ≥3d más → ×1.05

 12. INTENSITY_THRESHOLDS   fuerte/moderada/detectada (EV, conf, odds)
 13. CL/EL/ECL_GATE         CL: EV>15%+conf>75%; EL/ECL: EV>10%+conf>68%
 14. PARTIAL_QUALITY_DISC   data_quality=partial → conf×0.90, re-evalúa tiers

DESPUÉS de todos los filtros:
 15. EXTERNAL_CONTEXT       lesiones/rotaciones (context_analyzer) → conf×adj
```

---

## Filtros post-LLM activos (polymarket-agent) — pipeline completo

```
 1. CLOSING_SOON_BLEND      días<48h → blend 50% LLM + 50% precio mercado
 2. BIAS_CORRECTION         calibración histórica por categoría (n≥5)
 3. NEAR_TARGET_FLOOR       target <10% lejos → floor 60%, bloquear contraria
 4. PRICE_MOVE_CAP          variación >100% en <1 año → cap 25%; >50% en <3m → cap 35%
 5. CRYPTO_VALIDATE         caps por magnitud (>200%→15%, >100%<1año→25%, >50%<3m→35%)
 6. MLB_NO_DATA_EXTREME     prob <10% o >90% en MLB → PASS
 7. TITLE_RACE_CHECK        >10pts detrás del líder + <90d → cap 15%
 8. NBA_PLAYOFF_FLOOR       ESPN win_prob >85% → floor 75% + bloquear BUY_NO
 9. NBA_SERIES_STATE_FLOOR  2-0→75%, 3-0→90%, 3-1→85% + bloquear BUY_NO si ≥2-0
10. SPORTS_EDGE_SUSPICIOUS  edge >40% sin datos DDG → PASS
11. SPORTS_ODDS_DIVERGE     implied odds difiere >40% del modelo → PASS
12. TEAM_ELIMINATED         equipo eliminado de torneo → descartado antes del LLM
13. TENNIS/UFC_NO_DATA      sin odds DDG → descartado antes del LLM
14. LOW_PRICE_GEO_FILTER    edge <20% en geo/politics con price <15% → PASS
15. real_prob > 1.0         LLM devolvió porcentaje → divide /100 o descarta
16. CORR_INCONSISTENCY      inconsistencia >15% con mercados correlacionados → pull 30%
```

---

## Schedulers activos

| Workflow | Cron (UTC) | Horario España (CEST) | Estado |
|---|---|---|---|
| `sports-collect` | `0 */6 * * *` | 02:00, 08:00, 14:00, 20:00 | ✅ |
| `sports-enrich` | `30 */6 * * *` | 02:30, 08:30, 14:30, 20:30 | ✅ |
| `sports-analyze` | `0 1,7,13,19 * * *` | 03:00, 09:00, 15:00, 21:00 | ✅ |
| `polymarket-scan` | `0 */2 * * *` | cada 2h | ✅ |
| `polymarket-enrich` | `30 3,9,15,21 * * *` | 05:30, 11:30, 17:30, 23:30 | ✅ |
| `polymarket-analyze` | `0 4,10,16,22 * * *` | 06:00, 12:00, 18:00, 00:00 | ✅ |
| `poly-price-monitor` | `*/30 * * * *` | cada 30 min | ✅ |
| `poly-news-trigger` | `*/30 * * * *` | cada 30 min | ✅ |
| `polymarket-resolve` | `0 3 * * *` | 05:00 | ✅ |
| `polymarket-learn` | `30 3 * * *` | 05:30 | ✅ |
| `learning-engine` | `0 2 * * *` | 04:00 | ✅ |
| `daily-report` | `0 7 * * *` | 09:00 | ✅ |

> GitHub Actions cron puede tener hasta 60 min de delay en horas de alta carga.

---

## Métricas actuales (último ciclo completo conocido)

### polymarket-agent (07:35 UTC 7-mayo, 455s)
- Enrich: 115 mercados en 111s
- Analyze: 30 total · 27 analizados · 2 alertas · 3 skip_vol · 0 errores

### sports-agent (09:34 UTC 7-mayo, 227s)
- Señales generadas con pipeline situacional completo
- MOTIVATION_CHECK / CONTEXT_PENALTY / REST_ADVANTAGE activos desde hoy

---

## Thresholds y configuración

### Polymarket
| Parámetro | Valor |
|---|---|
| `POLY_MIN_EDGE` | 0.08 |
| `POLY_MIN_CONFIDENCE` | 0.65 |
| `GROQ_CALL_DELAY` | 4s |
| Ciclo max mercados | 40 |
| Cooldown alto volumen (>$100k) | 4h |
| Cooldown medio (>$10k) | 6h |
| Cooldown bajo | 12h |
| Price move threshold | 8% en <1h |
| Dedup price alerts | 2h |
| Stream limit enriched_markets | 150 docs |
| Freshness guard analyze | 90 min |

### Sports — value_bet_engine
| Parámetro | Valor |
|---|---|
| `SPORTS_MIN_EDGE` | 0.08 |
| `SPORTS_MIN_CONFIDENCE` | 0.65 |
| PL min_edge override | 0.072 |
| BL1/SA/FL1 min_edge | 0.096 |
| CL gate | EV>15% AND conf>75% |
| EL/ECL gate | EV>10% AND conf>68% |
| AWAY zona muerta | 2.5–3.5 (descarta) |
| NBA AWAY extremo | odds>6.00 → descarta siempre |
| NBA AWAY gate | odds>4.00 + win_prob<20% → descarta |
| MOTIVATION_CHECK cap | 0.70–1.05 |
| Posición tabla rival Δ>3 | ×0.90 |
| Posición tabla rival Δ>6 | ×0.80 |
| Forma rival ventaja >20pts | ×0.90 |
| Racha mala propia ≥2L | ×0.85 |
| Rest disadvantage ≥3d | ×0.90 |
| Rest advantage ≥3d | ×1.05 |

### Groq
| Modelo | Prioridad |
|---|---|
| `llama-3.3-70b-versatile` | Principal |
| `llama3-70b-8192` | Fallback 1 |
| `gemma2-9b-it` | Fallback 2 |
| `llama-3.1-8b-instant` | Fallback 3 |

---

## Estado de APIs

| API | Estado | Notas |
|---|---|---|
| Groq | ✅ Activo | Rotación de 4 modelos; fallback básico si TPD agotado |
| Gamma API (Polymarket) | ✅ Activo | Sin autenticación, sin rate limit conocido |
| odds-api.io | ✅ Primaria sports | 5000/h, sin problemas |
| The Odds API | ✅ Secundaria | 500/mes, quota gestionada |
| OddsPapi | ✅ Terciaria | 250/mes |
| Optic Odds | ✅ Cuaternaria | 1000/mes, fallback |
| DuckDuckGo HTML | ✅ Activo | Timeout 4s, cache 2-4h; tenis/UFC dependen de él |
| CoinGecko | ✅ Activo | Via `correlation_tracker.get_crypto_price()`, cache 60s |
| ESPN Scoreboard | ✅ Activo | Sin API key; NBA series state + win_prob |
| Tavily | ✅ Activo | Injury search NBA |
| Firestore | ✅ Activo | GCP, proyecto `prediction-intelligence` |
| Telegram Bot | ✅ Activo | Canal con topics 3 (Polymarket) y 4 (Sports/Report) |

---

## Estructura Firestore

| Colección | Contenido |
|---|---|
| `poly_markets` | Mercados activos Polymarket (scanner) |
| `enriched_markets` | Mercados enriquecidos (noticias, orderbook, correlaciones) |
| `poly_predictions` | Análisis LLM: real_prob + edge + recommendation |
| `poly_price_history` | Snapshots de precio cada 30 min |
| `alerts_sent` | Dedup de alertas enviadas (cooldown) |
| `shadow_trades` | Seguimiento virtual de señales |
| `poly_model_weights` | Thresholds aprendidos por learning engine |
| `team_stats` | Stats de equipos (fútbol + NBA) |
| `enriched_matches` | Partidos enriquecidos: Poisson, ELO, forma, H2H, fatiga |
| `prod_signals` | Señales sports generadas |
| `prodmatch_results` | Resultados reales para backtest |
| `standings` | Clasificaciones por liga (MOTIVATION_CHECK) |
| `market_correlations` | Pares de mercados correlacionados |

---

## Fix SSL gcloud — resuelto hoy localmente

El problema de `SSLCertVerificationError` al hacer deploy desde Windows quedó resuelto:

```powershell
# Configuración permanente (ya aplicada en $PROFILE de PowerShell):
$env:CLOUDSDK_PYTHON = "C:\Python311\python.exe"
$env:SSL_CERT_FILE   = "C:\Python311\Lib\site-packages\certifi\cacert.pem"

# Configuración gcloud (ya aplicada, persiste en ~/.config/gcloud):
gcloud config set core/custom_ca_certs_file "C:\Python311\Lib\site-packages\certifi\cacert.pem"
```

Causa raíz: Python 3.13 bundled de gcloud rechaza certificados proxy corporativos con
`Basic Constraints` no marcado como crítico (restricción nueva de Python 3.13).
Solución: usar Python 3.11 + certifi con 109 certificados Windows añadidos.

---

## Comandos útiles

### Deploy manual

```powershell
# Variables de entorno necesarias (ya en $PROFILE):
$env:CLOUDSDK_PYTHON = "C:\Python311\python.exe"
$env:SSL_CERT_FILE   = "C:\Python311\Lib\site-packages\certifi\cacert.pem"

# sports-agent
xcopy /E /I /Y shared services\sports-agent\shared
gcloud run deploy sports-agent --source services/sports-agent `
  --project prediction-intelligence --region europe-west1 `
  --account pejocanal@gmail.com --allow-unauthenticated `
  --timeout=1800 --min-instances=0 --memory=512Mi --cpu=1 --quiet
rmdir /S /Q services\sports-agent\shared

# polymarket-agent
xcopy /E /I /Y shared services\polymarket-agent\shared
gcloud run deploy polymarket-agent --source services/polymarket-agent `
  --project prediction-intelligence --region europe-west1 `
  --account pejocanal@gmail.com --allow-unauthenticated `
  --timeout=1800 --min-instances=0 --memory=256Mi --cpu=1 --quiet
rmdir /S /Q services\polymarket-agent\shared
```

### Trigger workflows

```bash
gh workflow run polymarket-analyze.yml --ref main
gh workflow run sports-analyze.yml --ref main
gh workflow run sports-collect.yml --ref main
gh workflow run sports-enrich.yml --ref main
gh workflow run daily-report.yml --ref main
```

### Ver logs Cloud Run (últimas 2h)

```bash
# polymarket-agent
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=polymarket-agent" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=50 --format="value(timestamp,textPayload)" --freshness=2h

# sports-agent
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=sports-agent" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=50 --format="value(timestamp,textPayload)" --freshness=2h
```

### Buscar logs específicos

```bash
# Ver MOTIVATION_CHECK en sports-agent
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=sports-agent \
   AND textPayload=~\"MOTIVATION_CHECK\"" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=20 --freshness=6h --format="value(timestamp,textPayload)"

# Ver CONTEXT_PENALTY + REST_ADVANTAGE
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=sports-agent \
   AND textPayload=~\"CONTEXT_PENALTY|REST_ADVANTAGE\"" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=20 --freshness=6h --format="value(timestamp,textPayload)"

# Ver alertas enviadas hoy
gcloud logging read \
  "resource.type=cloud_run_revision AND textPayload=~\"alerta enviada\"" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=20 --freshness=24h --format="value(timestamp,textPayload)"
```

### Verificar revisiones activas

```bash
gcloud run services list --project=prediction-intelligence \
  --account=pejocanal@gmail.com --region=europe-west1 \
  --format="table(metadata.name,status.latestReadyRevisionName)"
```

---

## Pendientes

### Alta prioridad

- [ ] **Verificar MOTIVATION_CHECK en producción**: buscar en logs `MOTIVATION_CHECK` en el próximo sports-analyze para confirmar que relegated/champion ya no descartan sino que penalizan.
- [ ] **Verificar CONTEXT_PENALTY en producción**: buscar en logs `CONTEXT_PENALTY` y `REST_ADVANTAGE` — confirmar que `home_days_rest` / `away_days_rest` se calculan correctamente desde `raw_matches`.
- [ ] **Verificar correlaciones en prompt Polymarket**: en el próximo polymarket-analyze, confirmar que el LLM recibe preguntas completas con `↑/↓` en lugar del simple conteo.
- [ ] **Verificar 5 headlines en prompt**: confirmar que `news_sentiment["headlines"][:5]` llega al LLM.
- [ ] **NBA playoffs verify**: confirmar que `NBA_SERIES_STATE_FLOOR` y `NBA_UNDERDOG_EXTREME` siguen activos con las series de playoffs actuales.

### Media prioridad

- [ ] **Backtest sports con nuevos filtros situacionales**: correr backtest de `generate_signal()` con los filtros 5g+5h para validar impacto neto en accuracy — los filtros de motivación/fatiga/descanso son nuevos y sin validación empírica todavía.
- [ ] **`home_days_rest` con datos escasos**: si `raw_matches` tiene pocos partidos o fechas antiguas, `_days_since_last()` devuelve `None` → los filtros 5h no aplican. Evaluar si añadir fallback desde fecha del partido anterior en `upcoming_matches`.
- [ ] **Standings con `position` y `points`**: el MOTIVATION_CHECK para top4/zona-descenso requiere que el documento Firestore `standings/{league}.teams.{team_id}` tenga campos `position` y `points`. Verificar que el colector de standings los escribe.
- [ ] **Polymarket-analyze schedule**: el cron de las 04:00 UTC tuvo 2h28min de delay ayer. Evaluar retry o cambiar a `workflow_dispatch` puro.

### Baja prioridad

- [ ] **weekly-report**: workflow sin schedule activo visible — verificar configuración.
- [ ] **poly-reset-cooldown**: workflow temporal — evaluar si eliminar.
- [ ] **Optic Odds**: verificar cuota mensual antes de fin de mes.
- [ ] **`standings` collector**: asegurarse de que escribe `position` y `points` además de los flags `mathematically_relegated` / `mathematically_champion` / `nothing_at_stake`.

---

## Prompt de continuación para próxima sesión

```
Proyecto: prediction-intelligence-ok (sports betting + polymarket)
Repo: github.com/pejofv93/prediction-intelligence
Deploy: gcloud run deploy desde Windows (SSL resuelto con Python 3.11 + certifi Windows)
gcloud: pejocanal@gmail.com · proyecto: prediction-intelligence · región: europe-west1

Estado al 8 mayo 2026 (ver ESTADO_08_MAYO_2026.md):

SERVICIOS ACTIVOS:
- polymarket-agent rev 00287-gb5 (256Mi / 1800s)
- sports-agent rev 00342-mqk (512Mi / 1800s)
- telegram-bot rev 00225-zt2, dashboard rev 00212-hs5

MEJORAS DE HOY (3 commits):
1. cdcccfe — sports: bloque 5g en generate_signal() — posición tabla + forma + momentum
   → CONTEXT_PENALTY log con factores: rival_position_better, rival_form, rival_momentum, bad_streak
2. 44269cb — sports: MOTIVATION_CHECK (replaced return[] con ×adj) + fatiga (days_rest) + REST_ADVANTAGE
   → MOTIVATION_CHECK log; home/away_days_rest en enriched_match; bloque 5h REST_ADVANTAGE
3. 8fcb6f5 — polymarket: correlaciones con pregunta+dirección (hasta 5), 5 headlines, sports context enriquecido

VERIFICAR EN PRÓXIMA SESIÓN:
1. ¿MOTIVATION_CHECK aparece en logs sports-agent? (buscar "MOTIVATION_CHECK" en Cloud Logging)
2. ¿home_days_rest / away_days_rest se calculan? (buscar "REST_ADVANTAGE" en logs)
3. ¿standings Firestore tiene campos "position" y "points" por equipo? (necesario para top4/zona descenso)
4. ¿Correlaciones con preguntas llegan al LLM en polymarket? (buscar "Mercados correlacionados" en prompt logs)

THRESHOLDS CLAVE:
- POLY: MIN_EDGE=0.08, MIN_CONF=0.65
- SPORTS: MIN_EDGE=0.08, MIN_CONF=0.65, CL: EV>15%+conf>75%, EL/ECL: EV>10%+conf>68%
- MOTIVATION: cap 0.70–1.05 (multiplicativo)
- CONTEXT: posición rival Δ>3→×0.90, Δ>6→×0.80; forma rival +20pts→×0.90; racha 2L→×0.85
- REST: rival +3d→×0.90, propio +3d→×1.05
```
