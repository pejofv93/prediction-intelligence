# Estado del sistema — 27 mayo 2026

## Resumen ejecutivo

Sistema de predicción deportiva y Polymarket operativo en Google Cloud Run.
Cinco servicios activos: `sports-agent`, `telegram-bot`, `polymarket-agent`,
`dashboard`, `market-sentinel`.

Sesión de hoy: 3 fixes de calidad de señal + implementación de filtros dinámicos
que aprenden de sus propios bloqueos (sports-agent).

Roland Garros en curso (semana 1). NBA Conference Finals en curso.

---

## Servicios en producción

| Servicio | URL | Último deploy | Estado |
|---|---|---|---|
| sports-agent | https://sports-agent-327240737877.europe-west1.run.app | 2026-05-27 (rev 00427-8p8) | ✅ |
| polymarket-agent | https://polymarket-agent-cragcibmwq-ew.a.run.app | 2026-05-27 | ✅ |
| telegram-bot | https://telegram-bot-cragcibmwq-ew.a.run.app | 2026-05-24 | ✅ |
| dashboard | https://dashboard-cragcibmwq-ew.a.run.app | 2026-05-24 | ✅ |
| market-sentinel | https://market-sentinel-cragcibmwq-ew.a.run.app | 2026-04-25 | ✅ |

**Proyecto GCP:** `prediction-intelligence`
**Cuenta deploy:** `pejocanal@gmail.com`
**Región:** `europe-west1`

---

## Cambios de esta sesión

### FIX 1 — Señales BTC contradictorias (commit `4909fa7`)

**Problema:** En la misma sesión llegaron BTC $80k BUY_YES (subida +3.8%) y
BTC $72k BUY_YES (bajada -5.1%). El sistema apostó simultáneamente a subida y bajada
del mismo activo porque los guards existentes solo comparaban mismo `market_id`.

**Fix en `groq_analyzer.py`:**
- Nueva cache en memoria `_CRYPTO_DIRECTIONAL_SIGNALS: dict[str, tuple[str, float, float]]`
  — clave: asset (BTC/ETH/etc.), valor: (dirección, timestamp_monotónico, target_price)
- `_CRYPTO_DIRECTION_WINDOW = 21600.0` — ventana 6h en segundos monotónicos
- Guard `CRYPTO_DIRECTION_CONFLICT` antes del cleanup final: si ya existe señal activa
  para el asset en dirección contraria dentro de la ventana → `recommendation = "PASS"`
- Al confirmar BUY_YES, registra dirección en la cache para bloquear contradictorias futuras

### FIX 2 — Real Madrid vs Baskonia edge +52.8% falso (commit `dd68b50`)

**Problema:** Señal con `off_edge=0.00` AND `form=0.00` generaba confianza artificialmente
alta. El `SYNTHETIC_DEFAULT_CAP` existente solo cubría el caso `all(signals == 0.5)` (datos
neutros en 0.5) pero no el caso `all(signals == 0.0)` (datos extremos/inválidos).
Con `std([0, 0, 0]) = 0 → conf = 1.0`, la señal superaba el threshold de 0.65 y emitía
edge de más del 50%.

**Fix en `basketball_analyzer._build_ratings()`:**
```python
if off_sig < 0.001 and form_sig < 0.001:
    conf = min(conf, 0.60)
    logger.warning("SYNTHETIC_DEFAULT_CAP: datos inválidos → conf capped 0.60")
```
Con `conf=0.60 < SPORTS_MIN_CONFIDENCE=0.65` la señal queda bloqueada.

### FIX 3 — F1 BUY_NO sin datos reales (commit `4909fa7`)

**Problema:** Russell BUY_NO @ 50% y Antonelli BUY_NO @ 78% llegaron sin datos reales
de clasificación ni tiempos de práctica. `SPORTS_SINGLE_NO_BLOCK` bloqueaba BUY_NO en
partidos individuales (tenis, UFC, MLB, NHL, cricket) pero F1 no estaba incluido.

**Fix en `groq_analyzer.py`:**
- Nueva regex `_F1_RE` — detecta Formula 1, Grand Prix, pilotos conocidos
- Flag `_is_f1` derivado de slug + question
- F1 añadido a `_is_individual_match` → activa `SPORTS_SINGLE_NO_BLOCK`
- Branch específica de DDG: busca tiempos de clasificación, práctica y favoritos del GP
  antes de generar la señal

### FEAT — Filtros dinámicos que aprenden (commit `8746307`)

Sistema de aprendizaje semanal para los filtros de bloqueo de `value_bet_engine.py`.
Cada filtro ahora registra sus bloqueos en Firestore y el learning engine evalúa
semanalmente si estaban acertando o fallando.

**Flujo completo:**

1. **Instrumentación** (`value_bet_engine.py`):
   - `_get_filter_params()`: lee `model_weights/filter_performance` con caché 30min;
     usa defaults hardcoded si Firestore no responde
   - `_log_filter_block()`: escribe cada bloqueo en colección `filter_blocks` con
     filter_name, match_id, team_to_back, league, odds, confidence, blocked_at

2. **5 filtros parametrizados** (antes hardcoded, ahora dinámicos):

   | Filtro | Parámetro | Default |
   |---|---|---|
   | `HIGH_DRAW_PROB` | `threshold` | 0.30 |
   | `UNDERDOG_EXTREME` | `odds` por liga | PD/SA/PL=4.5, BL1/FL1=5.0 |
   | `AWAY_DEAD_ZONE` | `odds_min`, `odds_max` | 2.5–3.5 |
   | `AWAY_PD_FILTER` | `odds_threshold` | 2.5 |
   | `AWAY_GATE_CONF` | `conf_threshold` | 0.85 |

3. **Evaluación semanal** (`learning_engine.py`):
   - `evaluate_filter_performance()`: lee `filter_blocks` de las últimas 4 semanas,
     resuelve resultados reales con `check_result()` en paralelo, computa `win_rate`
     por filtro (mínimo 10 bloqueos + 5 con resultado para ajustar)
   - `win_rate > 45%` → **relajar** (el filtro bloqueaba buenas señales)
   - `win_rate < 30%` → **endurecer** (los bloqueos eran correctos)
   - Ajuste por pasos conservadores con bounds por parámetro:
     `HIGH_DRAW_PROB.threshold` ∈ [0.22, 0.40] · paso ±0.02
     `AWAY_GATE_CONF.conf_threshold` ∈ [0.70, 0.95] · paso ±0.03
   - `_maybe_evaluate_filters()`: gate que ejecuta la evaluación solo si han pasado ≥7 días
   - `run_daily_learning()` llama `_maybe_evaluate_filters()` como paso 8 final

4. **Nuevas colecciones Firestore:**
   - `filter_blocks` — log de bloqueos (1 doc por filter+match_id)
   - `model_weights/filter_performance` — params aprendidos + stats + last_evaluated

La primera evaluación real ocurrirá ~7 días después de que `filter_blocks` empiece
a acumular datos.

---

## Polymarket-agent — estado completo

### Floors y guards activos

```
 1. NEAR_TARGET_FLOOR        — abs(pct_needed) < 10% → prob_min=60% + BUY_NO→PASS
 2. ALREADY_EXCEEDED         — current > target alcista → prob_min=90% + BUY_NO→PASS
 3. PRICE_MOVE_CAP           — abs(pct_needed) > 50% → real_prob=15%
 4. _validate_crypto_price   — caps históricos por asset/timeframe
 5. NEAR_TARGET_FINAL_GATE   — re-aplica BUY_NO block post crypto-validator
 6. SM_HIGH_PRICE            — smart_money + price>80% → BUY_NO→PASS
 7. HIGH_PRICE_VOL_FLOOR     — price>85% + vol>$30k → real_prob≥price-10% + PASS
 8. LOW_PRICE_VOL_CEIL       — price<15% + vol>$30k → real_prob≤price+10% + PASS
 9. NBA_SERIES_OVER          — ESPN tw≥4 → return None
10. NBA_PLAYOFF_FLOOR        — floors 3-0/3-1/2-0/1-0 + BUY_NO block si team leading
11. NBA_SERIES_SETTLED       — price≥0.92 en NBA series + BUY_NO → PASS
12. FINALIST_FINAL_GATE      — BUY_NO bloqueado en finalistas confirmados hasta result
13. _KNOWN_FINALISTS         — eastern/western conference + nba finals con floor=0.35
14. SPORTS_SINGLE_NO_BLOCK   — tenis/UFC/MLB/NHL/cricket/F1: BUY_NO→PASS si price>55%
15. SPORTS_BUY_NO_THRESHOLD  — min edge separado para BUY_NO en deportes
16. IMPROVISED_CAP           — sin datos externos: real_prob ancla a price_yes ±cap
    geopolitics=±15%, sports=±20%, crypto=±20%, politics=±15%, economy=±15%, other=±20%
17. CRYPTO_DIRECTION_CONFLICT — BUY_YES bloqueado si mismo asset tiene señal activa
    en dirección contraria dentro de ventana 6h (nuevo hoy)
```

### Variables de entorno Cloud Run — polymarket-agent

| Variable | Estado | Notas |
|---|---|---|
| `GROQ_API_KEY` | ✅ configurada | LLM principal |
| `TAVILY_API_KEY` | ✅ configurada | Búsquedas web |
| `CLOUD_RUN_TOKEN` | ✅ configurada | Auth inter-servicios |
| `TELEGRAM_BOT_URL` | ✅ configurada | Envío alertas |
| `ODDS_API_KEY` | ✅ configurada | The Odds API MLB/UFC/NHL |
| `COINGECKO_API_KEY` | ⚠️ vacía | Usa endpoint público — sin key pro |
| `FIRESTORE_COLLECTION_PREFIX` | `prod` | Colecciones de producción |

---

## Sports-agent — estado actual

### Deportes y fuentes activas

| Deporte | Fuente | Estado | Notas |
|---|---|---|---|
| NBA Playoffs | ESPN scoreboard API (sin key) | ✅ | Conference Finals en curso |
| Euroleague | feeds.incrowdsports.com (sin key) | ✅ | Temporada terminada |
| ACB | TheSportsDB id=4408 (sin key) | ✅ | Temporada terminada |
| Fútbol europeo | football-data.org | ⚠️ | HTTP 400 ventana >10 días |
| Tenis | odds-api.io (fallback The Odds API) | ⚠️ | Odds-only path activo |
| NFL / MLB / NHL | api-sports via RapidAPI | ❌ | 403 — no suscrito |

### Umbrales de señal

```
SPORTS_MIN_EDGE           = 8%   (fútbol)
SPORTS_MIN_CONFIDENCE     = 0.65 (todos)
BASKETBALL_MIN_EDGE       = 4%   (NBA/Euroleague)
TENNIS_ODDS_ONLY_MIN_EDGE = 3%   (sin stats RapidAPI)
POLY_MIN_EDGE             = 8%
POLY_MIN_CONFIDENCE       = 65%
```

---

## Workflows GitHub Actions

| Workflow | Schedule | Función |
|---|---|---|
| `sports-collect.yml` | cada 6h | Collect NBA/Euroleague/fútbol/tenis |
| `sports-analyze.yml` | manual / post-collect | Analyze + alertas |
| `learning-engine.yml` | 02:00 UTC diario | Ajusta pesos + evalúa filtros (nuevo: paso 8) |
| `polymarket-scan.yml` | cada 30 min | Scan + snapshots de precio |
| `polymarket-enrich.yml` | 4x/día (03:30/09:30/15:30/21:30) | Enrich enriched_markets |
| `polymarket-analyze.yml` | cada 2h | Analyze 40 mercados balanceados |
| `poly-price-monitor.yml` | cada 30 min | Monitor movimientos bruscos |
| `daily-report.yml` | 08:00 UTC | Reporte diario Telegram |
| `weekly-report.yml` | lunes 08:00 UTC | Reporte semanal |

---

## Bugs conocidos

| # | Bug | Servicio | Impacto | Pendiente |
|---|---|---|---|---|
| 1 | `football-data.org HTTP 400` — ventana >10 días | sports-agent | Sin fútbol europeo | Reducir `days=14` → `days=10` en `_collect_football` |
| 2 | `api-tennis.p.rapidapi.com` 404 en todos los endpoints | sports-agent | 0 stats de jugadores | Encontrar host correcto en RapidAPI dashboard |
| 3 | The Odds API 401 en `tennis_atp_french_open` | sports-agent | Sin odds Roland Garros via Odds API | Verificar plan en The Odds API dashboard |
| 4 | ETH/SOL ticker precios incorrectos | sports-agent (NEXUS) | Cosmético | Pendiente |
| 5 | CALÍOPE genera inglés en algunos modos (NEXUS) | nexus | Cosmético | Pendiente |

---

## Commits de esta sesión

```
8746307  feat(sports-agent): filtros dinámicos que aprenden de sus bloqueos
dd68b50  fix(sports): SYNTHETIC_DEFAULT_CAP para off_edge=0.00 AND form=0.00
4909fa7  fix(polymarket): señales BUY_YES contradictorias BTC + F1 en SPORTS_SINGLE_NO_BLOCK
c8482cf  fix(poly-websocket-keepalive): aumentar max-time curl 30s → 60s
```

---

## Próximas acciones recomendadas

1. **Verificar `filter_blocks`** — en ~7 días la colección tendrá datos suficientes
   para la primera evaluación automática. Monitorizar en Firestore console.
2. **football-data.org HTTP 400** — reducir ventana de 14 a 10 días en `_collect_football`
   para restaurar fútbol europeo (LaLiga, Premier, etc.)
3. **RapidAPI dashboard** — encontrar host correcto para API de tenis (stats de jugadores
   para señales con más datos que odds-only)
4. **The Odds API plan** — verificar si la key actual tiene acceso a tenis Grand Slam
   (`tennis_atp_french_open`)
5. **COINGECKO_API_KEY** — añadir key pro en Cloud Run si se supera el rate limit público
