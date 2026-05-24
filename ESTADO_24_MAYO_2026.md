# Estado del sistema — 24 mayo 2026

## Resumen ejecutivo

Sistema de predicción deportiva y Polymarket operativo en Google Cloud Run.
Cinco servicios activos: `sports-agent`, `telegram-bot`, `polymarket-agent`,
`dashboard`, `market-sentinel`.

Sesión de hoy: 7 fixes/features en polymarket-agent y telegram-bot.
Roland Garros comenzó el 26/05. NBA Conference Finals en curso (Knicks 1-0 Cavs).

---

## Servicios en producción

| Servicio | URL | Último deploy | Estado |
|---|---|---|---|
| polymarket-agent | https://polymarket-agent-cragcibmwq-ew.a.run.app | 2026-05-24 13:02 UTC | ✅ |
| telegram-bot | https://telegram-bot-cragcibmwq-ew.a.run.app | 2026-05-24 13:03 UTC | ✅ |
| sports-agent | https://sports-agent-cragcibmwq-ew.a.run.app | 2026-05-24 13:00 UTC | ✅ |
| dashboard | https://dashboard-cragcibmwq-ew.a.run.app | 2026-05-24 13:04 UTC | ✅ |
| market-sentinel | https://market-sentinel-cragcibmwq-ew.a.run.app | 2026-04-25 | ✅ |

**Proyecto GCP:** `prediction-intelligence`
**Cuenta deploy:** `pejocanal@gmail.com`
**Región:** `europe-west1`

---

## Polymarket-agent — cambios de esta sesión

### TEMA 2 — Knicks BUY_NO @ 78% bloqueado (commits `fd01ee8`, `d049905`)

**Causa raíz:**
- `_KNOWN_FINALISTS` no tenía entradas para "eastern conference" ni "western conference".
- `_fetch_nba_series_state` solo consultaba el scoreboard diario de ESPN — en días de
  descanso entre partidos no devolvía datos → `_nba_series_wins = None` → sin floor.
- El LLM estimaba 16% para Knicks → `edge = 0.16 - 0.78 = -0.62` → BUY_NO enviado.

**Fixes:**

*`_KNOWN_FINALISTS` — nuevas entradas con `floor=0.35`:*
```
"eastern conference"        → Knicks, Cavaliers
"eastern conference finals" → Knicks, Cavaliers
"western conference"        → Thunder, Spurs
"western conference finals" → Thunder, Spurs
```
Con `floor ≥ 0.30` y sin datos externos: `real_prob = price_yes` → `edge = 0` → PASS.

*`_fetch_nba_series_state` — fallback playoff endpoint:*
Refactorizado en helpers `_fetch_url` + `_parse_series`. Prueba primero el scoreboard
diario; si no hay partido hoy, hace fallback a `?seasontype=3` (playoff schedule completo)
que incluye próximos partidos aunque sea día de descanso.

*`_is_tennis` — detección por slug además de question:*
```python
_is_tennis = (
    _slug.startswith(("atp-", "wta-"))
    or bool(_TENNIS_RE.search(question))
    or bool(_TENNIS_RE.search(_slug.replace("-", " ")))  # nuevo
)
```
Cubre slugs como `altmaier-shelton-french-open-2026` donde "french open" está en el slug
pero no en la question literal. Antes: sin DDG search de rankings ATP. Ahora: detectado.

### TEMA 3 — Mini-analyze no generaba señales (commit `4a30faf`)

**Causa raíz:**
`polymarket-scan` corría cada 2h → 1 snapshot por mercado en la ventana de 2h que
`monitor_price_changes` evalúa. Con `len(snaps) < 2` → skip inmediato → movimientos
de $1M+ en Iran airspace y $300k en BTC $70k no disparaban mini-analyze.

**Fix 1 — `polymarket-scan.yml`:** cron `0 */2 * * *` → `*/30 * * * *`.
Con 4 snapshots/hora hay siempre ≥ 2 en la ventana incluso con un scan fallido.

**Fix 2 — fallback con 1 snapshot en `monitor_price_changes`:**
Cuando `len(snaps) == 1`, compara el snapshot contra `market.price_yes` de
`enriched_markets` (precio de la última enrichment, ≤ 4h). Si el delta supera
`_PRICE_MOVE_THRESHOLD` construye un `oldest` sintético y sigue el flujo normal
de alerta + mini-analyze.

### TEMA 1 — ODDS_API_KEY ausente en Cloud Run (acción directa)

`ODDS_API_KEY` **no estaba configurada** → todos los mercados MLB recibían
`data_quality=improvised` aunque el código de `_fetch_odds_api_context` es correcto.

**Acción:** key añadida directamente vía `gcloud run services update` en revisión `00370`.
Ahora `_fetch_odds_api_context("baseball_mlb", ...)` llama a The Odds API real.
Budget: 10 llamadas/día (500/mes), caché 2h por partido.

### Floors y gates Polymarket — estado completo

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
12. FINALIST_FINAL_GATE      — PSG/Arsenal BUY_NO bloqueado hasta resultado final
13. _KNOWN_FINALISTS         — eastern/western conference + nba finals con floor=0.35
14. SPORTS_SINGLE_NO_BLOCK   — tenis/UFC/MLB/NHL/cricket: BUY_NO→PASS si price_yes>55%
15. SPORTS_BUY_NO_THRESHOLD  — min edge separado para BUY_NO en deportes (learning)
16. IMPROVISED_CAP           — sin datos externos: real_prob ancla a price_yes ±cap/categoría
    geopolitics=±15%, sports=±20%, crypto=±20%, politics=±15%, economy=±15%, other=±20%
```

### Variables de entorno Cloud Run — polymarket-agent

| Variable | Estado | Notas |
|---|---|---|
| `GROQ_API_KEY` | ✅ configurada | LLM principal |
| `TAVILY_API_KEY` | ✅ configurada | Búsquedas web |
| `CLOUD_RUN_TOKEN` | ✅ configurada | Auth inter-servicios |
| `TELEGRAM_BOT_URL` | ✅ configurada | Envío alertas |
| `ODDS_API_KEY` | ✅ añadida hoy | The Odds API MLB/UFC/NHL |
| `COINGECKO_API_KEY` | ⚠️ vacía | Sin key pro — usa endpoint público |
| `FIRESTORE_COLLECTION_PREFIX` | `prod` | Colecciones de producción |

---

## Telegram-bot — cambios de esta sesión

### Fecha/hora partido en timezone Madrid (commit `713137d`)

Nueva función `_format_match_date_madrid(match_date)` en `alert_manager.py`.
Convierte `match_date` (datetime o ISO string UTC) a `Europe/Madrid` vía `zoneinfo`
(DST automático, ahora CEST = UTC+2) y formatea:

| Cuándo | Formato |
|---|---|
| Partido hoy | `📅 Hoy 24/05 a las 21:00 Madrid` |
| Partido mañana | `📅 Mañana 25/05 a las 02:30 Madrid` |
| Pasado mañana+ | `📅 26/05 a las 19:00 Madrid` |
| `match_date = None` | línea omitida — sin romper alertas sin fecha |

Usada en `_format_alert_unified` (todas las alertas de `send_sports_alert`) y en
`_format_sports_alert` (función legacy, también corregida).

**Ejemplo de alerta tras el fix:**
```
✅ SEÑAL DETECTADA | ⚽ Champions League
Real Madrid vs PSG
📅 Mañana 25/05 a las 21:00 Madrid
Mercado: 1X2 | Selección: *Real Madrid*
Cuota: *2.10* | Edge: *+12.3%* | Confianza: *74%*
```

---

## Sports-agent — estado actual

### Deportes y fuentes activas

| Deporte | Fuente | Estado | Notas |
|---|---|---|---|
| NBA Playoffs | ESPN scoreboard API (sin key) | ✅ | Conference Finals en curso |
| Euroleague | feeds.incrowdsports.com (sin key) | ✅ | Final Four terminado |
| ACB | TheSportsDB id=4408 (sin key) | ✅ | Temporada acabando |
| Fútbol europeo | football-data.org | ⚠️ | HTTP 400 ventana >10 días |
| Tenis | odds-api.io (fallback The Odds API) | ⚠️ | Odds-only path activo |
| NFL / MLB / NHL | api-sports via RapidAPI | ❌ | 403 — no suscrito |

### Estado tenis — Roland Garros

Roland Garros comenzó el **26 mayo 2026**. El sistema tiene 1067 partidos en Firestore
(rondas de clasificación + primeras rondas). El flujo odds-only genera señales cuando
hay partido con cuota disponible en `odds-api.io`:

```
collect: /v3/events (1 req) + /v3/odds (1 req) → odds embebidas en Firestore
analyze: lee home_odds/away_odds del doc → 0 HTTP extra → sin rate limit
```

### Umbrales de señal

```
SPORTS_MIN_EDGE           = 8%   (fútbol)
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
| `polymarket-scan.yml` | **cada 30 min** (antes: 2h) | Scan + snapshots de precio |
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
| 4 | ETH/SOL ticker precios incorrectos | sports-agent | Cosmético | Pendiente |
| 5 | CALÍOPE genera inglés en algunos modos (NEXUS) | nexus | Cosmético | Pendiente |

---

## Commits de esta sesión

```
713137d  feat(telegram-bot): fecha/hora partido en timezone Madrid en alertas sports
d049905  fix(groq_analyzer): detectar tenis en slug además de en question
4a30faf  fix(price_tracker): mini-analyze no generaba señales — scan 30min + fallback 1 snapshot
fd01ee8  fix(groq_analyzer): Knicks BUY_NO — eastern/western conference floor + ESPN playoff fallback
66e301e  fix(price_tracker): ordenar enriched_markets por volume_24h DESC + limit 150→200
```

---

## Próximas acciones recomendadas

1. **Verificar primer scan cada 30 min** — en ~30 min debe aparecer en logs
   `monitor_price_changes: evaluando 200 mercados` con snapshots actualizados
2. **RapidAPI dashboard** — encontrar host correcto para API de tenis (stats de jugadores)
3. **football-data.org HTTP 400** — reducir ventana de 14 a 10 días en `_collect_football`
4. **The Odds API plan** — verificar si key `0c42d51a...` tiene acceso a tenis Grand Slam
5. **COINGECKO_API_KEY** — añadir key pro en Cloud Run si se supera el rate limit público
