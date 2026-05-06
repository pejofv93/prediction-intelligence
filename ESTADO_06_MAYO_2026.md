# Estado del Sistema — 6 de mayo de 2026

## Servicios activos (Google Cloud Run — project: prediction-intelligence, region: europe-west1)

| Servicio | Revision actual | Timeout | Memoria | URL |
|---|---|---|---|---|
| polymarket-agent | 00279-pg5 | 1800s | 256Mi | https://polymarket-agent-327240737877.europe-west1.run.app |
| sports-agent | 00334-7jp | 1800s | 512Mi | https://sports-agent-327240737877.europe-west1.run.app |
| telegram-bot | 00225-zt2 | — | — | https://telegram-bot-327240737877.europe-west1.run.app |
| dashboard | 00212-hs5 | — | — | https://dashboard-cragcibmwq-ew.a.run.app |
| market-sentinel | 00005-9t2 | — | — | https://market-sentinel-cragcibmwq-ew.a.run.app |

> **IMPORTANTE:** Deploy siempre con gcloud, nunca Railway.
> El proyecto usa Google Cloud Run (prediction-intelligence), no Railway.

---

## Mejoras implementadas hoy (6 mayo 2026)

### polymarket-agent — 5 fixes en 5 commits

| Commit | Fix | Efecto |
|---|---|---|
| `2a221de` | TITLE_RACE_CHECK: cap 15% si >10pts detrás líder liga con <90d | Man City PL evitado |
| `2a221de` | MLB_NO_DATA_EXTREME: PASS si prob <10% o >90% en béisbol | Athletics 5% evitado |
| `2a221de` | Slug propagation: `enriched_markets` + fallback analyzer | Links Telegram correctos |
| `3daf2f0` | Validar `real_prob > 1.0` del LLM (divide /100 o descarta) | UFC edge=2.75 evitado |
| `9542b69` | Sports individual match context: tenis/UFC/MLB via DDG | Señales sin datos evitadas |

### polymarket-agent — fixes de sesión anterior que llegan a prod hoy

| Commit | Fix |
|---|---|
| `e0d776c` | curl `--max-time` 300→600s en workflow + timeouts individuales 5s |
| `25fde40` | CoinGecko via `get_crypto_price()` de `correlation_tracker` |
| `25fde40` | NEAR_TARGET_FLOOR: floor 60% + bloqueo señal contraria si target <10% lejos |
| `25fde40` | NBA_PLAYOFF_FLOOR: floor 75% + bloqueo BUY_NO si ESPN win_prob >85% |
| `6ff0f8e` | Price context injection para mercados "Will X reach $Y" |

### sports-agent — 1 fix

| Commit | Fix | Efecto |
|---|---|---|
| `24c0122` | `basketball_analyzer`: cap conf 60% cuando todas las señales son 0.50 (default) | Evitar 95% confianza sin datos reales |

---

## Detalle de los nuevos filtros (polymarket-agent)

### `_fetch_sports_odds_context()` — DDG search para partidos individuales

**Detección por slug o regex:**
- Tenis: slug `atp-`/`wta-` o keywords ATP/WTA/torneos
- UFC/MMA: slug `ufc-` o keywords weight class
- MLB: slug `mlb-` o keywords "mlb/baseball"

**Comportamiento:**
- DDG busca odds/rankings con timeout 5s, cache 2h
- Tenis/UFC sin datos DDG → `return None` (PASS, no señal)
- MLB sin datos → warning + filtro extreme prob existente
- Si encuentra datos → inyectar en user_prompt como ancla primaria
- Post-LLM: `edge > 0.40` + implied odds diverge >40% → PASS

### `real_prob` validation

```python
if real_prob > 1.0:
    real_prob = real_prob / 100.0  # LLM devolvió 75 en lugar de 0.75
    if real_prob > 1.0:
        return None  # Imposible incluso tras /100
edge = round(real_prob - price_yes, 4)  # Siempre recalcular (no confiar en LLM edge)
```

---

## Filtros post-LLM activos (polymarket-agent) — pipeline completo

```
1. CLOSING_SOON_BLEND    días<48h → blend 50% LLM + 50% precio mercado
2. BIAS_CORRECTION       calibración histórica por categoría (n>=5)
3. NEAR_TARGET_FLOOR     target <10% lejos → floor 60%, bloquear contraria
4. PRICE_MOVE_CAP        variación >100% en <1 año → cap 25%; >50% en <3m → cap 35%
5. CRYPTO_VALIDATE       validación específica crypto (magnitud + dirección)
6. MLB_NO_DATA_EXTREME   prob <10% o >90% en MLB → PASS
7. TITLE_RACE_CHECK      >10pts detrás del líder + <90d → cap 15%
8. NBA_PLAYOFF_FLOOR     ESPN win_prob >85% → floor 75% + bloquear BUY_NO
9. SPORTS_EDGE_SUSPICIOUS edge >40% sin datos DDG → PASS
10. SPORTS_ODDS_DIVERGE  implied odds difiere >40% del modelo → PASS
11. TEAM_ELIMINATED      equipo eliminado de torneo → descartado antes del LLM
12. TENNIS/UFC_NO_DATA   sin odds DDG → descartado antes del LLM
13. LOW_PRICE_GEO_FILTER edge <20% en geo/politics con price <15% → PASS
14. real_prob > 1.0       LLM devolvió porcentaje → divide /100 o descarta
```

---

## Estado de workflows (GitHub Actions)

| Workflow | Schedule | Función |
|---|---|---|
| `polymarket-analyze.yml` | `0 4,10,16,22 * * *` (4 veces/día) | enrich → analyze |
| `polymarket-scan.yml` | (schedule propio) | scan Gamma API → poly_markets |
| `polymarket-enrich.yml` | (schedule propio) | enrich standalone |
| `sports-analyze.yml` | (schedule propio) | analyze sports |
| `sports-collect.yml` | (schedule propio) | collect team stats |
| `sports-enrich.yml` | (schedule propio) | enrich sports matches |
| `daily-report.yml` | diario | reporte Telegram |
| `weekly-report.yml` | semanal | reporte semanal |
| `polymarket-learn.yml` | (schedule) | learning engine |
| `poly-price-monitor.yml` | (schedule) | monitor precios Polymarket |
| `poly-reset-cooldown.yml` | temporal | borra predictions últimas 13h |

**Último run exitoso polymarket-analyze:** `#25452037255` — 2026-05-06 18:03 UTC  
Total: 20 mercados → 19 analizados → 7 alertas → 298s

---

## Métricas actuales

### polymarket-agent (último run 18:03 UTC)
- Mercados enriquecidos: 114
- Mercados analizados: 19/20 (1 skip_vol)
- Alertas enviadas: 7
- Categorías: other×5, geopolitics×5, crypto×5, culture×1, politics×2, sports×1, economy×1
- Tiempo analyze: 298s (dentro de límite 1800s Cloud Run)

### sports-agent (último run 15:31 UTC)
- Señales generadas: 23 de 71 enriquecidos en 224.6s
- NBA activo: OKC Thunder vs Lakers, Knicks vs 76ers
- Arbitrage detector: lanzado en background

### Filtros activos hoy (logs)
- `NEAR_TARGET_FLOOR`/`NEAR_TARGET_NO_CONTRA`: firing en mercados BTC cerca del target
- `TITLE_RACE_CHECK`: disponible (DDG standings check)
- `TENNIS_NO_DATA`/`UFC_NO_DATA`: activo, descarta antes del LLM
- `real_prob > 1.0`: fix aplicado (UFC edge=2.75 era el caso de producción)

---

## Conocidos y pendientes

### Bugs conocidos (no bloqueantes)
- [ ] `--timeout` en `gcloud run deploy` no persiste: hay que ejecutar `gcloud run services update --timeout=1800` después de cada deploy de polymarket-agent
- [ ] Slug `polymarket-agent-00278-n72` creado por `gcloud run services update` (cada update crea revisión nueva) — no afecta funcionalidad
- [ ] WTA Eala vs Frech (2155920) obtuvo BUY_NO edge=0.18 antes del fix de tenis — no se repetirá
- [ ] MLB: detección por team name (sin keyword "mlb" en pregunta) cubierta ahora por slug `mlb-`

### Pendientes prioritarios
- [ ] **Verificar que `TENNIS_NO_DATA` y `UFC_NO_DATA` disparan** en el próximo run con mercados de tenis/UFC activos
- [ ] **Verificar slug fix** en alertas Telegram del próximo run — todos los links deben funcionar
- [ ] Cloud Run timeout workaround: añadir `gcloud run services update --timeout=1800` al script de deploy o CI
- [ ] `polymarket-analyze.yml` timeout del job: `timeout-minutes: 20` puede quedar corto si enrich tarda mucho (enrich tomó 164.7s + analyze 298s = 462s > 20min)
- [ ] NBA playoffs: verificar `NBA_PLAYOFF_FLOOR` con series activas (OKC vs LAL)
- [ ] Evaluar `TITLE_RACE_CHECK` con Man City vs Arsenal (Liga Premier — pocas jornadas restantes)

### Mejoras futuras (backlog)
- [ ] Parser de odds americanos más robusto en `_parse_implied_prob()` (probar con +150/-200 en snippets DDG)
- [ ] Cache compartida entre instancias Cloud Run (actualmente en-memoria, se pierde en cold start)
- [ ] Añadir `SPORTS_DATA_CACHE` a Firestore para persistir entre instancias
- [ ] Moneyline odds directo de The Odds API para partidos de tenis (actualmente solo DDG)
- [ ] Dashboard: mostrar filtros aplicados en cada predicción (NEAR_TARGET, TITLE_RACE, etc.)

---

## Comandos útiles

### Deploy
```bash
# polymarket-agent
xcopy /E /I /Y shared services\polymarket-agent\shared
gcloud run deploy polymarket-agent --source services/polymarket-agent \
  --project prediction-intelligence --region europe-west1 \
  --account pejocanal@gmail.com --allow-unauthenticated \
  --timeout=1800 --min-instances=0 --memory=256Mi --cpu=1 --quiet
rmdir /S /Q services\polymarket-agent\shared
# FIX: el timeout se resetea a 300s tras deploy → actualizar:
gcloud run services update polymarket-agent --timeout=1800 \
  --project prediction-intelligence --region europe-west1 --account pejocanal@gmail.com --quiet

# sports-agent
xcopy /E /I /Y shared services\sports-agent\shared
gcloud run deploy sports-agent --source services/sports-agent \
  --project prediction-intelligence --region europe-west1 \
  --account pejocanal@gmail.com --allow-unauthenticated \
  --timeout=1800 --min-instances=0 --memory=512Mi --cpu=1 --quiet
rmdir /S /Q services\sports-agent\shared
```

### Trigger manual workflows
```bash
gh workflow run polymarket-analyze.yml --ref main
gh workflow run sports-analyze.yml --ref main
gh workflow run sports-collect.yml --ref main
gh workflow run daily-report.yml --ref main
```

### Ver logs en tiempo real
```bash
# polymarket-agent
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=polymarket-agent" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=50 --format="value(timestamp,textPayload)" --freshness=1h

# sports-agent
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=sports-agent" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=50 --format="value(timestamp,textPayload)" --freshness=1h
```

### Ver señales generadas (últimas alertas)
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=polymarket-agent AND textPayload=~\"alerta enviada\"" \
  --project=prediction-intelligence --account=pejocanal@gmail.com \
  --limit=20 --format="value(timestamp,textPayload)" --freshness=6h
```

### Verificar revisiones activas
```bash
gcloud run services list --project=prediction-intelligence \
  --account=pejocanal@gmail.com --region=europe-west1 \
  --format="table(metadata.name,status.latestReadyRevisionName)"
```

---

## Estado de APIs

| API | Estado | Notas |
|---|---|---|
| Groq (LLM) | Activo | Rotación de modelos, fallback básico si cuota agotada |
| Gamma API (Polymarket) | Activo | Sin autenticación, sin rate limit conocido |
| DuckDuckGo HTML | Activo | Timeout 4s, cache 2-4h; ocasionales timeouts |
| CoinGecko | Activo | Via `correlation_tracker.get_crypto_price()`, cache 60s |
| The Odds API | Activo (sports-agent) | Quota gestionada, race condition fix aplicado |
| ESPN Scoreboard | Activo | Sin API key, NBA predictor (game-level) |
| Firestore | Activo | Collections: poly_markets, enriched_markets, poly_predictions, alerts_sent, team_stats |
| Telegram Bot | Activo | Canal operativo, alertas Polymarket y sports |

---

## Prompt de continuación para próxima sesión

```
Contexto: Sistema de predicciones deportivas y Polymarket en Google Cloud Run
Proyecto: prediction-intelligence (europe-west1) — cuenta: pejocanal@gmail.com
Deploy: gcloud run deploy, NUNCA railway up

Estado a 6 mayo 2026 (ver ESTADO_06_MAYO_2026.md):
- polymarket-agent rev 00279-pg5 (timeout 1800s — verificar tras cada deploy)
- sports-agent rev 00334-7jp
- 5 fixes polymarket hoy: real_prob validation, sports match context DDG, slug propagation,
  title race cap, MLB extreme filter
- 1 fix sports hoy: basketball confidence cap 60% cuando señales default

Verificar en próxima sesión:
1. ¿`TENNIS_NO_DATA`/`UFC_NO_DATA` disparan en run con mercados activos de tenis/UFC?
2. ¿Links Telegram tienen slug correcto tras el fix de slug propagation?
3. Timeout polymarket-agent: ¿sigue en 1800s o volvió a 300s?
   Fix: gcloud run services update polymarket-agent --timeout=1800 --project prediction-intelligence --region europe-west1 --account pejocanal@gmail.com --quiet
4. workflow polymarket-analyze timeout-minutes: 20 → puede quedar corto (enrich+analyze ~462s = 7.7min, OK)
```
