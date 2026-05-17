# Estado del sistema — 17 mayo 2026

## Resumen ejecutivo

Sistema de predicción deportiva y Polymarket operativo en Google Cloud Run.
Tres servicios: `sports-agent`, `telegram-bot`, `polymarket-agent`.
Alertas en tiempo real vía Telegram (topic Sports y topic Polymarket).

---

## Servicios en producción

| Servicio | URL | Estado |
|---|---|---|
| sports-agent | Cloud Run · europe-west1 | ✅ Operativo |
| telegram-bot | Cloud Run · europe-west1 | ✅ Operativo |
| polymarket-agent | Cloud Run · europe-west1 | ✅ Operativo |

**Proyecto GCP:** `prediction-intelligence`
**Cuenta deploy:** `pejocanal@gmail.com`
**Region:** `europe-west1`

---

## Sports-agent

### Deportes y fuentes activas

| Deporte | Fuente | Estado | Notas |
|---|---|---|---|
| NBA | ESPN scoreboard API (sin key) | ✅ | Playoffs activos |
| Euroleague | feeds.incrowdsports.com (sin key) | ✅ | Final Four activo |
| ACB | TheSportsDB id=4408 (sin key) | ✅ | 1 partido próximo |
| Fútbol europeo | football-data.org (PL/PD/BL1/SA/FL1/CL/EL/ECL) | ⚠️ HTTP 400 | Ver bug conocido #1 |
| Tennis | odds-api.io (fallback desde RapidAPI muerto) | ✅ | 805 partidos |
| NFL / MLB / NHL | api-sports via RapidAPI | ❌ 403 | No suscrito |
| UFC | api-sports via RapidAPI | ❌ Desactivado | Endpoint muerto |

### Umbrales de señal

```
SPORTS_MIN_EDGE     = 8%   (fútbol)
BASKETBALL_MIN_EDGE = 4%   (NBA/Euroleague — mercados eficientes)
POLY_MIN_EDGE       = 8%
POLY_MIN_CONFIDENCE = 65%
```

### Ligas fútbol por umbral calibrado (backtest histórico)

| Liga | Umbral | ROI backtest |
|---|---|---|
| Premier League | 7.2% | +13.5% |
| La Liga | 8.0% | +7.4% |
| Bundesliga | 9.6% | -31.9% |
| Serie A | 9.6% | -18.9% |
| Ligue 1 | 9.6% | -24.6% |
| Champions/Europa/Conference | 8.0% | sin muestra suficiente |

### Estado NBA Playoffs (a 17/05/2026)

| Serie | Estado | Señal hoy |
|---|---|---|
| DET vs CLE (Game 7) | Empatada 3-3 — juegan hoy | Sin señal (DET ev=+1.7% < 4%) |
| OKC vs SAS | West Finals en curso | ✅ SAS away ev=+4.3% (enviada) |
| NYK vs ganador CLE/DET | East Finals — pendiente equipos | TBD vs TBD · sin team_stats |
| NYK vs PHI (76ers) | **TERMINADA** — Knicks 4-0 | Limpiada el 17/05 |

### Estado Euroleague Final Four

| Partido | Señal |
|---|---|
| Olympiacos vs Fenerbahce | ✅ Olympiacos home (deduplicada — ya enviada) |
| Valencia vs Real Madrid | ✅ Valencia home (deduplicada — ya enviada) |

### Cleanup de datos stale (añadido hoy)

- `_cleanup_stale_predictions()`: marca `result="expired"` en predictions con `match_date > 48h`
- `_cleanup_stale_odds_cache()`: borra `odds_cache` entries sin partido activo en `upcoming_matches`
- Se ejecuta automáticamente en cada `collect` (cada 6h)
- `POST /admin/cleanup-stale`: endpoint on-demand
- `cleanup-stale.yml`: workflow GitHub Actions manual

**Cleanup ejecutado hoy:**
```
upcoming_deleted:    6
predictions_expired: 29   (incluidas señales NYK vs PHI)
odds_cache_deleted:  128  (incluidas cuotas NYK vs PHI)
```

---

## Telegram-bot

### Dedup de alertas

Mecanismo: `_claim_alert_slot(key, type)` — pre-escribe en Firestore **antes** de enviar.
- Document ID = sanitized key → idempotente
- Ventana de race condition: < 20ms
- Fail-open: si Firestore falla → envía (prefiere duplicado a silencio)
- TTL dedup: 24h para sports y polymarket

### Topics Telegram

| Topic | Thread ID | Contenido |
|---|---|---|
| Sports | 4 | Señales deportivas + cambios de cuota |
| Polymarket | 3 | Oportunidades Polymarket |
| Daily | 4 | Reporte diario + semanal |

### check_pending_odds_changes

- Lee `odds_cache` de Firestore (hasta 500 docs)
- Compara vs `predictions` con `result=None`
- Guardia defensiva añadida hoy: salta predicciones con `match_date > 48h`
- Umbral de alerta: cambio de cuota > 10%
- Formato señal accionable: `✅ SEÑAL POR CAMBIO DE CUOTA` cuando `new_edge ≥ 8%`

---

## Polymarket-agent

### Fixes desplegados hoy (commit dc78266)

**Bug 1 — Series NBA ya resueltas:**
- `NBA_SERIES_OVER`: si ESPN devuelve `team_wins ≥ 4` → `return None` (descartar mercado)
- `NBA_SERIES_SETTLED`: fallback cuando ESPN ya no devuelve la serie; si `price_yes ≥ 0.92` en mercado NBA series + `BUY_NO` → `PASS`

**Bug 2 — ETH near-target floor bypassed:**
- `_validate_crypto_price_prediction()` reseteaba `recommendation = "BUY_NO"` tras NEAR_TARGET_NO_CONTRA
- `NEAR_TARGET_FINAL_GATE`: re-aplica bloqueo BUY_NO→PASS después del crypto validator cuando `abs(pct_needed) < 10%`

**Bug 3 — HIGH_PRICE_VOL_FLOOR umbral:**
- Bajado de `$50,000` → `$30,000` en ambos floors (precio >85% y precio <15%)
- NVIDIA #1 @ 95% con vol $43,927 ahora activa el floor correctamente

### Floors y caps activos (orden de ejecución)

```
1. NEAR_TARGET_FLOOR        — abs(pct_needed) < 10% → prob_min=60% + BUY_NO→PASS
2. ALREADY_EXCEEDED         — current > target alcista → prob_min=90% + BUY_NO→PASS
3. PRICE_MOVE_CAP           — abs(pct_needed) > 50% → real_prob=15%
4. _validate_crypto_price   — caps históricos por asset/timeframe
5. NEAR_TARGET_FINAL_GATE   — re-aplica BUY_NO block post crypto-validator [NUEVO HOY]
6. SM_HIGH_PRICE            — smart_money + price>80% → BUY_NO→PASS
7. HIGH_PRICE_VOL_FLOOR     — price>85% + vol>$30k → real_prob≥price-10% + PASS
8. LOW_PRICE_VOL_CEIL       — price<15% + vol>$30k → real_prob≤price+10% + PASS
9. NBA_SERIES_OVER          — ESPN tw≥4 → return None [NUEVO HOY]
10. NBA_PLAYOFF_FLOOR       — floors 3-0/3-1/2-0/1-0 + BUY_NO block si team leading
11. NBA_SERIES_SETTLED      — price≥0.92 en NBA series + BUY_NO → PASS [NUEVO HOY]
```

---

## Workflows GitHub Actions

| Workflow | Schedule | Función |
|---|---|---|
| `sports-collect.yml` | cada 6h | Collect NBA/Euroleague/fútbol/tenis |
| `sports-analyze.yml` | manual / post-collect | Analyze + envío alertas |
| `sports-enrich.yml` | manual | Enrich enriched_matches |
| `polymarket-scan.yml` | periódico | Scan mercados Polymarket |
| `polymarket-analyze.yml` | periódico | Analyze mercados |
| `daily-report.yml` | 08:00 UTC | Reporte diario Telegram |
| `weekly-report.yml` | lunes 08:00 UTC | Reporte semanal |
| `cleanup-stale.yml` | manual | Limpiar predictions/odds_cache stale [NUEVO HOY] |
| `deploy.yml` | push a main | Rebuild + deploy Cloud Run |

---

## Colecciones Firestore activas

| Colección | Contenido | TTL/Limpieza |
|---|---|---|
| `upcoming_matches` | Partidos SCHEDULED/TIMED | Auto-borrado > 48h en collect |
| `enriched_matches` | Partidos con Poisson/ELO | — |
| `team_stats` | Stats + form_score por equipo | TTL 6h (cache check) |
| `h2h_data` | Historial enfrentamientos | TTL 6h (cache check) |
| `predictions` | Señales generadas | `result="expired"` en collect si > 48h |
| `odds_cache` | Cuotas opening/current | Borrado si fixture no en upcoming |
| `alerts_sent` | Dedup de alertas | TTL 24h implícito por _claim_alert_slot |
| `poly_predictions` | Análisis Polymarket | — |
| `model_weights` | Pesos del ensemble | Actualizado por production-backtest |
| `accuracy_log` | Accuracy semanal | Acumulativo |

---

## Bugs conocidos

| # | Bug | Servicio | Impacto | Pendiente |
|---|---|---|---|---|
| 1 | `football-data.org HTTP 400` — `dateTo-dateFrom > 10 days` | sports-agent | Sin partidos de fútbol europeo | Reducir ventana de 14 a 10 días en `get_upcoming_matches` |
| 2 | Tennis signals analizadas (730) pero sin señales entregadas | sports-agent | 0 alertas de tenis | Revisar umbrales `tennis_analyzer` |
| 3 | NYK vs CLE/DET: `TBD vs TBD` — sin team_stats hasta sortear | sports-agent | Normal hasta confirmar equipos East Finals | Ninguno |
| 4 | NHL/MLB/NFL `HTTP 403` RapidAPI | sports-agent | Sin cobertura de béisbol/hockey/americano | Requiere suscripción o endpoint alternativo |

---

## Últimos 10 commits

```
dc78266  fix(polymarket): 3 señales incorrectas — series resueltas + near-target + vol floor
f20969e  fix(playoffs): limpiar señales stale de series terminadas (NYK vs PHI)
dcfb0fa  fix(telegram-bot): dedup race condition + señal accionable por cuota
02f1886  fix(basketball): BASKETBALL_MIN_EDGE=4% + form_score propagation collect
050cafe  fix(basketball): form_score=0 bug + ratings diagnostics
9eefe29  fix(sports): espn_odds/seeds no se guardaban en Firestore
4737635  fix(sports): basketball_analyzer usa odds-api.io
b0874d7  fix(sports): NBA/ACB sin señales — bypass hardcoded + odds ESPN fallback
7e4c548  fix(polymarket): NBA series floor no se activaba sin "nba" en texto
5a66af6  fix(polymarket): no-external-data → ancla ±15%
```

---

## Próximas acciones recomendadas

1. **Fix football-data.org HTTP 400** — cambiar `days=14` → `days=10` en `_collect_football`
2. **Revisar tennis_analyzer** — 730 partidos analizados, 0 señales entregadas (umbrales o edge muy bajo)
3. **Verificar señales East Finals** una vez CLE/DET Game 7 termine y se formen los matchups
4. **Suscripción RapidAPI MLB/NHL** si se quiere cobertura béisbol/hockey en Playoffs
