# Estado del Sistema — 7 mayo 2026

## Servicios Cloud Run activos

| Servicio          | Timeout | Memoria | CPU | Estado |
|-------------------|---------|---------|-----|--------|
| polymarket-agent  | 1800s   | 512Mi   | 1   | ✅ OK  |
| sports-agent      | 1800s   | 512Mi   | 1   | ✅ OK  |
| telegram-bot      | 60s     | 256Mi   | 1   | ✅ OK  |
| dashboard         | 60s     | 512Mi   | 1   | ✅ OK  |

Proyecto GCP: `prediction-intelligence` · Región: `europe-west1`

---

## Deploys del día

| Hora UTC | Commit | Descripción | Estado |
|----------|--------|-------------|--------|
| 09:54    | `52e1e4a` | NBA_UNDERDOG_EXTREME basketball_analyzer | ✅ 7m23s |
| 08:09    | `f5b7804` | tennis→sports + NBA series state floor  | ✅ 7m18s |
| 07:18    | `7a6b2fa` | polymarket-agent timeout 1800s + 512Mi  | ✅ 7m54s |

---

## Fixes implementados hoy (7 mayo 2026)

### 1. polymarket-agent timeout + OOM (commits `7a6b2fa`)
- **Causa 1**: `deploy.yml` tenía `--timeout=300` → analyze cortado a los 300s exactos con 504.
- **Causa 2**: `--memory=256Mi` → price-monitor cargaba todos los enriched_markets sin límite → OOM → exit code 56.
- **Fix**: `--timeout=1800`, `--memory=512Mi`, `.limit(150).stream(timeout=30.0)` en `price_tracker.py:346`.
- **Verificado**: analyze completó en 455s (7m35s) sin timeout.

### 2. Tenis categorizado como crypto (commit `f5b7804`)
- **Causa**: `categorize_market` itera el dict en orden; "crypto" va antes que "sports" → "Solana Sierra" (jugadora) activaba la keyword `solana`.
- **Fix**: pre-check con `_TENNIS_RE` en `groq_analyzer.py:categorize_market` — si el regex hace match → retorna `"sports"` inmediatamente.
- `_TENNIS_RE` ya incluía: `internazionali`, `roland garros`, `wimbledon`, `atp`, `wta`, `us open`, `australian open`, `madrid open`, etc.

### 3. NBA series state floor (commit `f5b7804`)
- **Causa**: floor solo se aplicaba cuando ESPN devolvía `win_prob > 85%` en juego EN DIRECTO. Entre partidos no hay predictor activo → `_nba_win_prob = None` → sin floor → LLM subestimaba al equipo líder (Knicks 70% vs mercado 92%).
- **Fix**: nueva función `_fetch_nba_series_state(team)` que lee `series.competitors[].wins` del ESPN scoreboard (disponible entre partidos). Floors por marcador:
  - 2-0 → prob_min = 75%
  - 3-0 → prob_min = 90%
  - 3-1 → prob_min = 85%
- BUY_NO bloqueado automáticamente si el equipo va ≥ 2-0.
- Cache 30 min (`_NBA_SERIES_STATE_CACHE`).

### 4. NBA_UNDERDOG_EXTREME en basketball_analyzer (commit `52e1e4a`)
- **Causa**: Lakers @ 8.50 generaba SEÑAL MODERADA con confianza 80% aunque OKC tiene 89.9% win_prob. El fix de confianza 60% no lo bloqueaba porque `off_edge=0.86` es dato real.
- **Fix**: en el loop moneyline de `basketball_analyzer.py`, después del seed filter:
  - `AWAY odds > 6.00` → descartar siempre.
  - `AWAY odds > 4.00 AND win_prob < 20%` → descartar.
- Log: `NBA_UNDERDOG_EXTREME: {equipo} descartado (odds=X.XX > 6.00)`.

---

## Fixes implementados ayer (6 mayo 2026)

| Commit | Fix |
|--------|-----|
| `24c0122` | basketball_analyzer: cap confidence 60% cuando todas las señales son default (0.50) |
| `9542b69` | groq_analyzer: sports individual match context — tenis/UFC/MLB vía DDG |
| `3daf2f0` | groq_analyzer: validar real_prob > 1.0 del LLM — dividir /100 o descartar |
| `2a221de` | analyzer: title race cap, MLB extreme filter, slug propagation |
| `e0d776c` | analyze: timeout curl 300→600s + timeouts individuales 5s por llamada externa |

---

## Schedulers activos

| Workflow | Cron (UTC) | Horario España (CEST) | Último run hoy | Estado |
|----------|------------|----------------------|----------------|--------|
| sports-collect | `0 */6 * * *` | 02:00, 08:00, 14:00, 20:00 | 08:23 (20m12s) | ✅ |
| sports-enrich | `30 */6 * * *` | 02:30, 08:30, 14:30, 20:30 | 09:08 (5m10s) | ✅ |
| sports-analyze | `0 1,7,13,19 * * *` | 03:00, 09:00, 15:00, 21:00 | 09:34 (4m3s / 227s) | ✅ |
| polymarket-scan | `0 */2 * * *` | cada 2h | 06:41 (13s) | ✅ |
| polymarket-enrich | `30 3,9,15,21 * * *` | 05:30, 11:30, 17:30, 23:30 | 06:22 (2m21s) | ✅ |
| polymarket-analyze | `0 4,10,16,22 * * *` | 06:00, 12:00, 18:00, 00:00 | 07:35 manual (9m38s) | ✅ |
| poly-price-monitor | `*/30 * * * *` | cada 30 min | 09:10 (1m) | ✅ |
| poly-news-trigger | `*/30 * * * *` | cada 30 min | 07:49 (7s) | ✅ |
| polymarket-resolve | `0 3 * * *` | 05:00 | 05:55 (1m22s) | ✅ |
| polymarket-learn | `30 3 * * *` | 05:30 | 06:21 (15s) | ✅ |
| learning-engine | `0 2 * * *` | 04:00 | 05:24 (55s) | ✅ |
| daily-report | `0 7 * * *` | 09:00 | 09:28 (14s) | ✅ |

Nota: GitHub Actions cron puede tener 30-60 min de delay en horas de alta carga.

---

## Fallos del día y resolución

| Hora UTC | Workflow | Error | Fix |
|----------|----------|-------|-----|
| 02:44 | poly-price-monitor | exit code 56 (OOM 256Mi) | memory→512Mi + stream limit |
| 06:28 | polymarket-analyze | HTTP 504 (timeout 300s) | timeout→1800s |

---

## Métricas del último ciclo completo

**polymarket-analyze** (07:35 UTC, 455s):
- Enrich: 115 mercados en 111s
- Analyze: 30 total · 27 analizados · 2 alertas · 3 skip_vol · 0 errores

**sports-analyze** (09:34 UTC, 227s):
- Completado OK con ping cada 30s

---

## Thresholds y configuración

### Polymarket
| Parámetro | Valor |
|-----------|-------|
| POLY_MIN_EDGE | 0.08 |
| POLY_MIN_CONFIDENCE | 0.65 |
| GROQ_CALL_DELAY | 4s |
| Ciclo max mercados | 40 |
| Cooldown alto volumen (>$100k) | 4h |
| Cooldown medio (>$10k) | 6h |
| Cooldown bajo | 12h |
| Price move threshold | 8% en <1h |
| Dedup price alerts | 2h |
| Stream limit enriched_markets | 150 docs |
| Freshness guard analyze | 90 min |

### Sports
| Parámetro | Valor |
|-----------|-------|
| SPORTS_MIN_EDGE | 0.08 |
| SPORTS_MIN_CONFIDENCE | 0.65 |
| PL min_edge | 0.072 |
| BL1/SA/FL1 min_edge | 0.096 |
| NBA AWAY odds máximo | 6.00 |
| NBA AWAY odds+prob gate | >4.00 + <20% win_prob |

### Groq
| Parámetro | Valor |
|-----------|-------|
| Modelo principal | llama-3.3-70b-versatile |
| Fallback 1 | llama3-70b-8192 |
| Fallback 2 | gemma2-9b-it |
| Fallback 3 | llama-3.1-8b-instant |

---

## APIs y estado de cuotas

| API | Límite | Uso | Notas |
|-----|--------|-----|-------|
| Groq | rate-limit dinámico | OK | delay 4s entre calls |
| The Odds API | 500/mes | activa | secundaria |
| OddsPapi | 250/mes | activa | terciaria |
| odds-api.io | 5000/h | primaria | sin problemas |
| Optic Odds | 1000/mes | cuaternaria | fallback |
| CoinGecko | pública | OK | polymarket enricher |
| Tavily | activa | OK | injury search NBA |
| ESPN API | sin key | OK | scoreboard NBA playoffs |
| Firestore | GCP | OK | colección `prediction-intelligence` |

---

## Estructura de colecciones Firestore

| Colección | Contenido |
|-----------|-----------|
| `poly_markets` | Mercados activos Polymarket (scanner) |
| `enriched_markets` | Mercados enriquecidos con noticias/sentimiento |
| `poly_predictions` | Análisis LLM + edge + recommendation |
| `poly_price_history` | Snapshots de precio cada 30min |
| `alerts_sent` | Dedup de alertas enviadas |
| `shadow_trades` | Seguimiento virtual de señales |
| `poly_model_weights` | Thresholds aprendidos (learning engine) |
| `team_stats` | Stats NBA/fútbol para basketball_analyzer/value_bet |
| `prod_signals` | Señales sports generadas |
| `prodmatch_results` | Resultados reales para backtest |
| `ab_swap_queue` | Cola A/B Polymarket (aletheia) |

---

## Filtros NBA activos en basketball_analyzer

1. **Divergencia extrema** (línea ~460): descarta si modelo difiere >2.5× del mercado.
2. **Seed filter** (línea ~474): descarta si seed del equipo es >3 peor que el rival.
3. **NBA_UNDERDOG_EXTREME** (línea ~481, NUEVO HOY):
   - AWAY odds > 6.00 → descartado siempre.
   - AWAY odds > 4.00 AND win_prob < 20% → descartado.
4. **Descuento elite** (línea ~496): rival seed ≤2 → −20% edge.
5. **Back-to-back** (línea ~383): −5% p_home o +5% si es el visitante.
6. **Confidence cap 60%** (sesión 2026-05-06): si todas las señales son default 0.50.
7. **Playoff discount totales** (línea ~525): exp_total × 0.92 en playoffs.

## Filtros NBA activos en groq_analyzer

1. **Series state floor** (NUEVO HOY): 2-0→75%, 3-0→90%, 3-1→85%.
2. **BUY_NO bloqueado** si equipo va ≥2-0 en serie.
3. **ESPN win_prob fallback**: si >85% en juego en directo → floor 75%.
4. **Freshness guard**: aborta analyze si enriched_markets >90 min de antigüedad.

---

## Comandos útiles

```bash
# Ver últimos runs
gh run list --limit=10

# Forzar analyze Polymarket
gh workflow run polymarket-analyze.yml --ref main

# Forzar analyze Sports
gh workflow run sports-analyze.yml --ref main

# Forzar collect Sports
gh workflow run sports-collect.yml --ref main

# Ver logs de un run
gh run view <RUN_ID> --log

# Ver logs del último analyze polymarket
gh run list --workflow="polymarket-analyze.yml" --limit=1
gh run view <RUN_ID> --log-failed

# Deploy manual (requiere gcloud auth)
# NOTA: gcloud auth falla por SSL en Windows local
# → El deploy se dispara automáticamente en cada push a main

# Forzar deploy via workflow
gh workflow run deploy.yml --ref main

# Resetear cuota de API en Polymarket
curl -X POST "$POLY_AGENT_URL/admin/reset-quota/oddspapi" \
  -H "x-cloud-token: $CLOUD_RUN_TOKEN"

# Ver predicciones recientes
curl "$POLY_AGENT_URL/recent-predictions?limit=10" \
  -H "x-cloud-token: $CLOUD_RUN_TOKEN"

# Health check
curl "$POLY_AGENT_URL/health"
curl "$SPORTS_AGENT_URL/health"
```

---

## Problema conocido: gcloud auth SSL en Windows

gcloud local falla con `SSLCertVerificationError` al autenticar contra `oauth2.googleapis.com`.
- Workaround aplicado: `gcloud config set core/custom_ca_certs_file` → certifi bundle.
- No resuelto del todo: el deploy manual desde Windows no funciona.
- **Solución operativa**: todos los deploys se hacen vía push a main → GitHub Actions CI/CD.
- El workflow `deploy.yml` copia `shared/` en cada servicio antes de hacer `gcloud run deploy`.

---

## Telegram

- Canal privado con topics:
  - Topic 3: Polymarket
  - Topic 4: Sports / Daily Report
- Alertas enviadas por: `telegram-bot` service vía `/send-alert`
- polymarket-agent y sports-agent llaman a `$TELEGRAM_BOT_URL/send-alert`

---

## Pendientes

### Alta prioridad
- [ ] **polymarket-analyze 10:00 UTC**: no apareció en el historial (GitHub delay). Monitorear el de las 16:00.
- [ ] **Verificar NBA series state en producción**: primer partido real con Knicks en playoffs para confirmar que el floor 2-0→75% aplica correctamente.
- [ ] **Verificar tenis override**: comprobar que "Internazionali BNL d'Italia: Solana Sierra" se categoriza como `sports` en el próximo enrich.

### Media prioridad
- [ ] **gcloud auth SSL**: resolver el problema de certificados SSL en Windows para poder hacer deploys locales de emergencia.
- [ ] **polymarket-analyze schedule 10:00 UTC**: el cron de las 04:00 UTC llegó con 2h28min de delay (se ejecutó a las 06:28). Evaluar si vale la pena cambiar a workflow_dispatch únicamente o añadir retry.
- [ ] **sports-analyze**: verificar que el filtro NBA_UNDERDOG_EXTREME bloquea Lakers @ OKC antes del siguiente partido.
- [ ] **Backtest NBA**: correr backtest de basketball_analyzer con los nuevos filtros para validar impacto en accuracy.

### Baja prioridad
- [ ] **weekly-report**: workflow existe pero no tiene schedule activo visible — verificar si está configurado.
- [ ] **Optic Odds** (cuaternaria): verificar cuota mensual antes de fin de mes.
- [ ] **poly-reset-cooldown**: workflow temporal — verificar si sigue siendo necesario o se puede eliminar.

---

## Prompt de continuación

```
Proyecto: prediction-intelligence-ok (sports betting + polymarket)
Repo: github.com/pejofv93/prediction-intelligence
Deploy: Cloud Run via push a main → GitHub Actions deploy.yml
gcloud: pejocanal@gmail.com · proyecto: prediction-intelligence · región: europe-west1
NOTA: gcloud auth falla por SSL en Windows — deploys solo via GitHub Actions

Estado al 7 mayo 2026:
- 4 servicios activos: polymarket-agent (1800s/512Mi), sports-agent (1800s/512Mi),
  telegram-bot (60s/256Mi), dashboard (60s/512Mi)
- Fixes de hoy: timeout polymarket 300→1800s, memory 256→512Mi,
  tenis override categorize_market, NBA series state floor,
  NBA_UNDERDOG_EXTREME basketball_analyzer (AWAY odds>6.00 descartado)
- Thresholds: POLY_MIN_EDGE=0.08, SPORTS_MIN_EDGE=0.08, conf=0.65
- Schedulers: polymarket-analyze 04/10/16/22 UTC, sports-analyze 01/07/13/19 UTC,
  poly-price-monitor cada 30min, sports-collect cada 6h
- Último analyze OK: 07:35 UTC — 27/30 mercados, 2 alertas, 455s
- Pendiente: verificar NBA series floor en producción, verificar tenis override

Ver ESTADO_07_MAYO_2026.md para estado completo.
```
