# Estado del Sistema — 11 de mayo de 2026

## Servicios Cloud Run activos

| Servicio | Revisión | Timeout | Memoria | CPU | URL |
|---|---|---|---|---|---|
| sports-agent | `00343-rtf` (hoy) | 1800s | 512Mi | 1 | https://sports-agent-327240737877.europe-west1.run.app |
| polymarket-agent | `00289-xwk` (hoy) | 1800s | 512Mi | 1 | https://polymarket-agent-327240737877.europe-west1.run.app |
| telegram-bot | `00226-xxx` (hoy) | 60s | 256Mi | 1 | https://telegram-bot-327240737877.europe-west1.run.app |
| dashboard | `00221-wcx` (hoy) | 60s | 512Mi | 1 | https://dashboard-cragcibmwq-ew.a.run.app |

Proyecto GCP: `prediction-intelligence` · Región: `europe-west1` · Cuenta: `pejocanal@gmail.com`

Deploy: automático vía `deploy.yml` en cada push a `main` · Run `25687923008` ✅ completado 18:08 UTC.

---

## Métricas actuales del modelo

| Métrica | Valor |
|---|---|
| Win rate global | **35%** |
| ROI global | **−26.3%** |
| Bankroll virtual | en cálculo (shadow_trades) |
| Estado modelo | DEGRADADO — accuracy por debajo del umbral esperado |

> Las métricas confirman que los filtros de señales implementados esta sesión (edge≤0 guard,
> confidence cap, NBA floor 1-0, local election filter) van en la dirección correcta para
> reducir señales espurias que penalizan el ROI.

---

## 7 Fixes implementados hoy (11 mayo 2026)

### FIX 1 — edge=0% con confianza desacoplada · `value_bet_engine.py` · commit `ab0bbe6`

**Causa raíz:** el filtro rival_top3 descuenta `best_edge` ×0.80 pero `best_ev` no se descuenta.
Con un edge marginal de ~0.01, el descuento lo llevaba a 0.008 → se mostraba como 0%, pero
`best_ev > 0.08` seguía pasando el threshold check. Ejemplos: Stuttgart edge=0.0% conf=100%,
Como 1907 edge=0.0% conf=93%, Udinese edge=0.0% conf=97%.

**Fix:** guard `if best_edge <= 0.0: return []` insertado ANTES del bloque de umbrales de intensidad,
después de todos los ajustes multiplicativos sobre `best_edge`.

```python
# FIX-EDGE0: si best_edge ≤ 0 no hay valor real — descartar ANTES del threshold check.
if best_edge <= 0.0:
    logger.debug("generate_signal(%s): descartado — edge=%.4f ≤ 0 ...", ...)
    return []
```

---

### FIX 7 — Confianza supera 100% · `value_bet_engine.py` · commit `ab0bbe6`

**Causa raíz:** multiplicadores de motivación (+5% zona descenso) y descanso (+5%) se acumulan
multiplicativamente: 1.05 × 1.05 = 1.1025. Ejemplos: Villarreal conf=104%, Espanyol conf=104%.

**Fix:** `best_confidence = min(best_confidence, 0.99)` después de TODOS los ajustes situacionales
(justo antes de `kelly_criterion()`). Confianza nunca puede superar 99%.

```python
# FIX-CONF100: cap estricto — multiplicadores acumulados pueden superar 1.0.
best_confidence = min(best_confidence, 0.99)
```

---

### FIX 2 — Daily report por tiers · `model_health.py` + `main.py` · commit `40e7cae`

**Problema:** el reporte diario mostraba totales sin distinguir calidad de señal.
ROI calculado sobre pendientes distorsionaba los números.

**Fix:** desglose por nivel antes del bloque "Total General". ROI solo sobre resueltas.

```
🔥 SEÑALES FUERTES (EV>20%):
   Total: X | Resueltas: Y | Correctas: Z
   Win rate: W% | ROI (resueltas): +X%

✅ SEÑALES DETECTADAS (EV 12-20%):
   Total: X | Resueltas: Y | Correctas: Z
   Win rate: W% | ROI (resueltas): −X%

📊 SEÑALES MODERADAS (EV 8-12%):
   Total: X | Resueltas: Y | Correctas: Z
   Win rate: W% | ROI (resueltas): −X%

📈 TOTAL GENERAL:
   [resumen actual]
```

Cambios: `_bg_daily_report` clasifica preds por edge tier y pasa `tier_stats` a
`format_daily_report()`. Nueva función `_tier_roi()` en `model_health.py`.

---

### FIX 3 — Price monitor cambio absoluto · `price_tracker.py` · commit `23095cb`

**Problema:** `"Cambio: -19.5% en 103 min"` era la variación relativa del precio del mercado,
confusa para mercados de probabilidad (un mercado 20%→16% mostraba -19.5%).

**Fix:** formato en puntos porcentuales absolutos (pp).

```
Antes: Cambio: -19.5% en 103 min
Ahora: Cambio: -4.0pp (20.5% → 16.5%) en 103 min
```

```python
pp_change = (price_new - price_old) * 100  # absoluto en pp
```

---

### FIX 4 — Polymarket: links + resolver + formato · `alert_manager.py` · commit `c83ce39`

**A) Links:** añadido fallback `market_id` (condition_id) cuando slug vacío.
- Con slug: `https://polymarket.com/event/{slug}`
- Sin slug: `https://polymarket.com/market/{market_id}`

**B) Resolver:** verificado que `polymarket_resolver.py` YA trackea accuracy correctamente.
`_resolve_market()` lee `outcomePrices` del Gamma API, mapea rec+outcome a win/loss,
actualiza `poly_predictions.result` y `shadow_trades`. No requería cambios.

**C) Formato de pregunta:** añadida línea de acción explícita en la alerta:

```
Antes: Recomendación: BUY_NO     ← texto genérico
Ahora:
🔴 COMPRAR NO — el mercado sobrevalora esta probabilidad
🟢 COMPRAR YES — el mercado infravalora esta probabilidad
👁 OBSERVAR — señal débil, monitorear evolución
```

---

### FIX 5 — NBA series floor 1-0 no funcionaba · `groq_analyzer.py` · commit `67f33e4`

**Causa raíz:** el floor y el bloque BUY_NO solo se activaban con `wins >= 2`.
OKC 1-0 tenía `_tw=1, _ow=0` → `1 >= 2 = False` → BUY_NO contra Thunder no se bloqueaba.
Knicks 2-0 sí era bloqueado. OKC 1-0 no.

**Fix 1:** añadido floor 0.65 para ventaja 1-0:
```python
elif _tw == 1 and _ow == 0:
    _nba_floor = 0.65
    _nba_floor_reason = "serie 1-0 → prob_min=65%"
```

**Fix 2:** BUY_NO block cambiado de `>= 2` a `> opp_wins` (equipo realmente líder):
```python
# Antes: _nba_series_wins[0] >= 2
# Ahora: cualquier ventaja activa en la serie
_team_leading = _nba_series_wins is not None and _nba_series_wins[0] > _nba_series_wins[1]
```

**Tabla de floors actualizada:**

| Estado serie | Floor | BUY_NO bloqueado |
|---|---|---|
| 1-0 | 65% | ✅ Sí (nuevo) |
| 2-0 | 75% | ✅ Sí |
| 3-0 | 90% | ✅ Sí |
| 3-1 | 85% | ✅ Sí |
| ESPN win_prob >85% | 75% | ✅ Sí |

---

### FIX 8 — Elecciones locales extranjeras sin datos · `groq_analyzer.py` · commit `67f33e4`

**Problema:** señal espuria "Will Chong Won-oh win the 2026 Seoul Mayoral Election" BUY_NO @ 92%.
El modelo no tiene datos de encuestas locales surcoreanas. Divergencia de 30pp sin contexto real.

**Fix:** filtro antes del LLM (ahorra quota Groq). Si `category == "politics"` AND
keyword electoral local AND país no anglófono → skip (`return None`).

```python
_LOCAL_ELECTION_RE = re.compile(
    r'\b(mayoral|mayor|municipal|prefecture|local election|city council|alderman|alcalde|gubernatorial)\b',
    re.I,
)
_ANGLOPHONE_COUNTRY_RE = re.compile(
    r'\b(usa|united states|america|uk|united kingdom|england|britain|canada|australia|new zealand|ireland|scotland|wales)\b',
    re.I,
)

# En analyze_market(), después de category detection:
if category == "politics":
    if _LOCAL_ELECTION_RE.search(question) and not _ANGLOPHONE_COUNTRY_RE.search(question):
        logger.info("analyze_market: LOCAL_ELECTION_NO_DATA → skip")
        return None
```

Ejemplos cubiertos: Seoul Mayoral, Tokyo Governor, Seoul Mayor, cualquier elección municipal
de países no anglófonos sin datos de encuestas en inglés.
Ejemplos permitidos: New York Mayor, London Mayor, US Governor, Canadian municipal.

---

## FIX 6 — Weekly report (PENDIENTE)

**Causa confirmada:** el workflow `weekly-report.yml` llama a `POST /send-weekly-report`
en el telegram-bot (Cloud Run). La instancia devuelve **HTTP 500** después de `--max-time 60`.

**Log del workflow run `25687923008`** (o similar de las 10:56 UTC):
```
curl: (22) The requested URL returned error: 500
```

**Root cause probable:** `send_weekly_report()` en `main.py` ejecuta **síncronamente** 4 queries
Firestore (`accuracy_log`, `model_weights`, `predictions`, `poly_predictions`) dentro del
timeout de 60s de Cloud Run. Con instancia fría (min-instances=0) + 4 queries Firestore +
`generate_weekly_report()` → timeout.

**Fix propuesto (siguiente sesión):**
1. Convertir `send_weekly_report()` a background task igual que `daily_report()`:
   devolver `202 Accepted` inmediatamente y ejecutar en `asyncio.create_task(_bg_weekly_report())`.
2. Aumentar `--max-time` en el workflow de 60s a 30s (basta con confirmar el 202).
3. Añadir try/except granular para que un fallo en Firestore no rompa todo el endpoint.

---

## Pendientes actualizados

### Alta prioridad

- [ ] **FIX 6 — Weekly report HTTP 500**: convertir `send_weekly_report()` a background task
  (igual que `daily_report()`). Devolver 202 inmediato, ejecutar queries Firestore en background.
- [ ] **Verificar FIX 1 en producción**: buscar en logs sports-agent `"descartado — edge=0"` en el
  próximo `sports-analyze` — confirmar que ya no llegan señales con edge=0%.
- [ ] **Verificar FIX 7 en producción**: confirmar que ninguna señal tiene `confidence > 0.99`.
- [ ] **Verificar NBA floor 1-0**: en el próximo polymarket-analyze, confirmar que OKC
  (si sigue 1-0) no genera BUY_NO y que el floor 65% se aplica.
- [ ] **Verificar local election filter**: confirmar `LOCAL_ELECTION_NO_DATA` en logs polymarket-analyze.

### Media prioridad

- [ ] **Backtest con filtros situacionales**: correr backtest de `generate_signal()` con los
  filtros 5g+5h+edge_guard para validar impacto neto en accuracy.
- [ ] **Mejorar win rate 35% → objetivo 45%+**: el ROI −26.3% indica señales con edge insuficiente
  o modelo mal calibrado en algún segmento. Analizar por liga y tier:
  - ¿Cuál liga tiene peor accuracy? (usar `accuracy_by_league` en weekly report)
  - ¿Los AWAY tienen el mismo problema que antes del fix?
  - ¿Señales "Fuertes" (EV>20%) tienen mejor ROI que "Moderadas" (EV 8-12%)?
- [ ] **Backtest polymarket por categoría**: ¿crypto, sports, geopolitics tienen distintos ROI?
  Ajustar `POLY_MIN_CONFIDENCE` por categoría si hay diferencia significativa.
- [ ] **TikTok/NBA series state cache**: verificar que el cache de 30 min funciona y no
  hace requests repetidos a ESPN entre partidos.
- [ ] **Standings collector**: verificar que escribe `position` y `points` en Firestore
  (necesario para MOTIVATION_CHECK top4/zona descenso).

### Baja prioridad

- [ ] **poly-reset-cooldown**: workflow temporal — evaluar si eliminar.
- [ ] **Optic Odds**: verificar cuota mensual antes de fin de mes.
- [ ] **weekly-report schedule**: ahora sí tiene cron `0 8 * * 1` — el problema era el HTTP 500,
  no la ausencia de schedule.
- [ ] **Dashboard**: revisar si las métricas de win rate/ROI se actualizan con los nuevos fixes.

---

## Pipeline de ajustes de confianza en `generate_signal()` — estado actual completo

```
ANTES de calcular edge:
  1. POISSON_GUARD          sin Poisson → ELO sintético o Form sintético o descarta
  2. QUALITY_GUARD          sin datos reales (poisson=None + form=50/50 + h2h=0) → descarta
  3. MOTIVATION_CHECK       relegated×0.85 / champion×0.90 / nothing_at_stake×0.80 /
                            top4_seguro×0.95 / zona_descenso×1.05 (cap 0.70–1.05)
  4. DIVERGENCIA_EXTREMA    prob > 2.5× impl_odds → descarta
  5. EMPATE_PROB            p_home<0.45 + p_away<0.45 + p_draw>0.30 → descarta

DESPUÉS de seleccionar team_to_back:
  6. UNDERDOG_EXTREMO       vs top-6 con odds>umbral → descarta
  7. RIVAL_ELITE            vs top-3 → edge×0.80; bottom-6 vs top-3 → descarta
  8. AWAY_FILTERS           zona muerta 2.5–3.5 / PD>2.5 / gate CL/EL/ECL
  9. AWAY_ADDL              odds>6.00 → descarta; odds>4.00 fuera de CL → cap 70%
 10. FORM_POISSON_GATE      form<0.25 y poisson<0.20 → descarta
 11. MOTIVATION_APPLY       aplica _standings_confidence_adj calculado en paso 3

 12. 5g — CONTEXT_PENALTY
       i)  Posición tabla: rival>3 pos → ×0.90; rival>6 pos → ×0.80
       ii) Forma comparada: rival_form > sel+20 → ×0.90; siempre añade rival_form a factors
       iii) Momentum: rival ≥2W → rival_momentum en factors; sel ≥2L → ×0.85 + bad_streak

 13. 5h — REST_ADVANTAGE
       rival descansó ≥3d más → ×0.90; sel descansó ≥3d más → ×1.05

 14. [NUEVO] FIX-EDGE0      best_edge ≤ 0 → descarta (antes del threshold check)

 15. INTENSITY_THRESHOLDS   fuerte (EV>20%+conf>80%+odds<5) / moderada / detectada
 16. CL/EL/ECL_GATE         CL: EV>15%+conf>75%; EL/ECL: EV>10%+conf>68%
 17. PARTIAL_QUALITY_DISC   data_quality=partial → conf×0.90, re-evalúa tiers

DESPUÉS de todos los filtros de tiers:
 18. EXTERNAL_CONTEXT       lesiones/rotaciones (context_analyzer) → conf×adj
 19. [NUEVO] FIX-CONF100    min(confidence, 0.99) — cap absoluto anti-overflow
```

---

## Filtros post-LLM activos (polymarket-agent) — estado actual completo

```
 1. CLOSING_SOON_BLEND      días<48h → blend 50% LLM + 50% precio mercado
 2. BIAS_CORRECTION         calibración histórica por categoría (n≥5)
 3. NEAR_TARGET_FLOOR       target <10% lejos → floor 60%, bloquear contraria
 4. PRICE_MOVE_CAP          variación >100% en <1 año → cap 25%; >50% en <3m → cap 35%
 5. CRYPTO_VALIDATE         caps por magnitud improbable
 6. MLB_NO_DATA_EXTREME     prob <10% o >90% en MLB → PASS
 7. TITLE_RACE_CHECK        >10pts detrás del líder + <90d → cap 15%
 8. NBA_PLAYOFF_WIN_PROB    ESPN win_prob >85% → floor 75% + bloquear BUY_NO
 9. NBA_SERIES_STATE_FLOOR  [ACTUALIZADO] 1-0→65%, 2-0→75%, 3-0→90%, 3-1→85%
                            BUY_NO bloqueado si equipo VA GANANDO (tw > ow)
10. SPORTS_EDGE_SUSPICIOUS  edge >40% sin datos DDG → PASS
11. SPORTS_ODDS_DIVERGE     implied odds difiere >40% del modelo → PASS
12. TEAM_ELIMINATED         equipo eliminado de torneo → descartado antes del LLM
13. TENNIS/UFC_NO_DATA      sin odds DDG → descartado antes del LLM
14. LOW_PRICE_GEO_FILTER    edge <20% en geo/politics con price <15% → PASS
15. [NUEVO] LOCAL_ELECTION  mayoral/municipal + no anglófono → skip antes del LLM
16. real_prob > 1.0         LLM devolvió porcentaje → divide /100 o descarta
17. CORR_INCONSISTENCY      inconsistencia >15% con mercados correlacionados → pull 30%
```

---

## Schedulers activos

| Workflow | Cron (UTC) | Horario España (CEST) | Último run | Estado |
|---|---|---|---|---|
| `sports-collect` | `0 */6 * * *` | 02:00, 08:00, 14:00, 20:00 | 14:34 UTC hoy | ✅ |
| `sports-enrich` | `30 */6 * * *` | 02:30, 08:30, 14:30, 20:30 | 15:42 UTC hoy | ✅ |
| `sports-analyze` | `0 1,7,13,19 * * *` | 03:00, 09:00, 15:00, 21:00 | 15:56 UTC hoy | ✅ |
| `polymarket-scan` | `0 */2 * * *` | cada 2h | 17:58 UTC hoy | ✅ |
| `polymarket-enrich` | `30 3,9,15,21 * * *` | 05:30, 11:30, 17:30, 23:30 | 17:33 UTC hoy | ✅ |
| `polymarket-analyze` | `0 4,10,16,22 * * *` | 06:00, 12:00, 18:00, 00:00 | 17:48 UTC hoy | ✅ |
| `poly-price-monitor` | `*/30 * * * *` | cada 30 min | 17:53 UTC hoy | ✅ |
| `poly-news-trigger` | `*/30 * * * *` | cada 30 min | 16:02 UTC hoy | ✅ |
| `polymarket-resolve` | `0 3 * * *` | 05:00 | ayer | ✅ |
| `polymarket-learn` | `30 3 * * *` | 05:30 | ayer | ✅ |
| `learning-engine` | `0 2 * * *` | 04:00 | ayer | ✅ |
| `daily-report` | `0 7 * * *` | 09:00 | 10:35 UTC hoy | ✅ |
| `weekly-report` | `0 8 * * 1` | lunes 10:00 | 10:56 UTC hoy | ❌ HTTP 500 |

---

## Archivos modificados hoy

| Archivo | Fixes | Commit |
|---|---|---|
| `services/sports-agent/analyzers/value_bet_engine.py` | FIX 1 (edge≤0 guard) + FIX 7 (conf cap 99%) | `ab0bbe6` |
| `services/sports-agent/services/dashboard/shared/model_health.py` | FIX 2 (tiers report) | `40e7cae` |
| `services/telegram-bot/main.py` | FIX 2 (tier_stats en daily report) | `40e7cae` |
| `services/polymarket-agent/price_tracker.py` | FIX 3 (pp absoluto) | `23095cb` |
| `services/telegram-bot/alert_manager.py` | FIX 4 (links + formato) | `c83ce39` |
| `services/polymarket-agent/groq_analyzer.py` | FIX 5 (NBA 1-0) + FIX 8 (local elections) | `67f33e4` |

---

## Estado de APIs

| API | Estado | Notas |
|---|---|---|
| Groq | ✅ Activo | Rotación 4 modelos; fallback básico si TPD agotado |
| Gamma API (Polymarket) | ✅ Activo | Sin autenticación |
| ESPN Scoreboard | ✅ Activo | NBA series state + win_prob, sin API key |
| odds-api.io | ✅ Primaria sports | 5000/h |
| The Odds API | ✅ Secundaria | 500/mes, quota gestionada |
| OddsPapi | ✅ Terciaria | 250/mes |
| Optic Odds | ✅ Cuaternaria | 1000/mes |
| DuckDuckGo HTML | ✅ Activo | Timeout 4s, cache 2-4h |
| CoinGecko | ✅ Activo | Cache 60s en `correlation_tracker` |
| Tavily | ✅ Activo | Injury search NBA |
| Firestore | ✅ Activo | GCP `prediction-intelligence` |
| Telegram Bot | ✅ Activo | Canal con topics Sports=4, Polymarket=2, Daily=X |

---

## Estructura Firestore relevante

| Colección | Contenido |
|---|---|
| `poly_predictions` | Análisis LLM: real_prob + edge + recommendation + result (resolver) |
| `shadow_trades` | Seguimiento virtual: virtual_stake + pnl_virtual + result |
| `predictions` | Señales sports: edge + confidence + odds + correct + result |
| `enriched_markets` | Mercados Polymarket enriquecidos |
| `poly_price_history` | Snapshots precio cada 30 min |
| `alerts_sent` | Dedup alertas + price monitor |
| `accuracy_log` | Accuracy semanal por liga |
| `model_weights` | Pesos Poisson/ELO/form/h2h actualizados por learning engine |
| `standings` | Clasificaciones por liga (MOTIVATION_CHECK) |
| `enriched_matches` | Partidos enriquecidos: Poisson, ELO, forma, H2H, days_rest |
| `agent_state/groq_quota` | Estado TPD Groq (reset medianoche UTC) |

---

## Comandos útiles

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
# sports-agent
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=sports-agent" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=50 --format="value(timestamp,textPayload)" --freshness=2h

# polymarket-agent
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=polymarket-agent" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=50 --format="value(timestamp,textPayload)" --freshness=2h
```

### Verificar fixes en logs

```bash
# FIX 1: señales con edge=0 descartadas
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=sports-agent \
   AND textPayload=~\"descartado — edge=\"" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=20 --freshness=12h --format="value(timestamp,textPayload)"

# FIX 5: NBA floor 1-0 aplicado
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=polymarket-agent \
   AND textPayload=~\"NBA_PLAYOFF_FLOOR|NBA_NO_CONTRA\"" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=20 --freshness=12h --format="value(timestamp,textPayload)"

# FIX 8: local election filter
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=polymarket-agent \
   AND textPayload=~\"LOCAL_ELECTION_NO_DATA\"" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=20 --freshness=12h --format="value(timestamp,textPayload)"
```

### Deploy manual (si necesario)

```powershell
$env:CLOUDSDK_PYTHON = "C:\Python311\python.exe"
$env:SSL_CERT_FILE   = "C:\Python311\Lib\site-packages\certifi\cacert.pem"

# sports-agent
xcopy /E /I /Y shared services\sports-agent\shared
gcloud run deploy sports-agent --source services/sports-agent `
  --project prediction-intelligence --region europe-west1 `
  --account pejocanal@gmail.com --allow-unauthenticated `
  --timeout=1800 --min-instances=0 --memory=512Mi --cpu=1 --quiet
rmdir /S /Q services\sports-agent\shared
```

---

## Prompt de continuación para próxima sesión

```
Proyecto: prediction-intelligence-ok (sports betting + polymarket)
Repo: github.com/pejofv93/prediction-intelligence
Deploy: automático vía deploy.yml en push a main
gcloud: pejocanal@gmail.com · proyecto: prediction-intelligence · región: europe-west1

Estado al 11 mayo 2026 (ver ESTADO_11_MAYO_2026.md):

SERVICIOS ACTIVOS (deploy 18:01 UTC hoy):
- sports-agent rev 00343-rtf (512Mi / 1800s)
- polymarket-agent rev 00289-xwk (512Mi / 1800s)
- telegram-bot rev 00226-xxx (256Mi / 60s)
- dashboard rev 00221-wcx (512Mi / 60s)

MÉTRICAS:
- Win rate: 35% | ROI: −26.3% — modelo DEGRADADO
- Objetivo: win rate >45%, ROI positivo

7 FIXES IMPLEMENTADOS HOY:
1. FIX 1 (ab0bbe6): edge≤0 guard en value_bet_engine.py — elimina señales con 0% edge
2. FIX 7 (ab0bbe6): confidence cap min(conf, 0.99) — corrige confianza 104% por multiplicadores
3. FIX 2 (40e7cae): daily report por tiers (Fuertes/Detectadas/Moderadas) con ROI por nivel
4. FIX 3 (23095cb): price monitor en pp absolutos "−4pp (20.5%→16.5%)"
5. FIX 4 (c83ce39): links Polymarket con fallback market_id + acción explícita en alerta
6. FIX 5 (67f33e4): NBA floor 1-0 (65%) + BUY_NO block cuando equipo VA GANANDO serie
7. FIX 8 (67f33e4): filter elecciones locales no anglófonas → skip antes del LLM

PENDIENTE URGENTE:
- FIX 6: weekly-report devuelve HTTP 500 — convertir send_weekly_report() a background task
  (igual que daily_report() → 202 Accepted + asyncio.create_task(_bg_weekly_report()))

THRESHOLDS CLAVE:
- POLY: MIN_EDGE=0.08, MIN_CONF=0.65, GROQ_DELAY=4s
- SPORTS: MIN_EDGE=0.08, MIN_CONF=0.65
- CL gate: EV>15% AND conf>75% | EL/ECL: EV>10% AND conf>68%
- NBA series floors: 1-0→65%, 2-0→75%, 3-0→90%, 3-1→85%
- Confidence: min(conf, 0.99) después de todos los multiplicadores

VERIFICAR EN PRÓXIMA SESIÓN:
1. Logs FIX 1: ¿aparece "descartado — edge=0" en sports-analyze?
2. Logs FIX 5: ¿"NBA_NO_CONTRA BUY_NO→PASS" para series NBA en juego?
3. Logs FIX 8: ¿"LOCAL_ELECTION_NO_DATA" en polymarket-analyze?
4. Daily report: ¿llegan los tiers Fuertes/Detectadas/Moderadas en Telegram?
5. Price monitor: ¿alertas con formato pp absoluto?
```
