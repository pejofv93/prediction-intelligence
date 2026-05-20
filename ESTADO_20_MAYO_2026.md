# Estado del sistema — 20 mayo 2026

## Resumen ejecutivo

Sistema de predicción deportiva y Polymarket operativo en Google Cloud Run.
Tres servicios: `sports-agent`, `telegram-bot`, `polymarket-agent`.
Hoy: sesión de fixes en polymarket y tenis. Roland Garros empieza el 26/05.
Estado de fuentes de odds para tenis: **todos los proveedores bloqueados temporalmente** (ver detalle).

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

## Polymarket-agent

### Fixes desplegados hoy

**Fix 1 — SPORTS_SINGLE_NO_BLOCK: cricket/IPL (commit `99fc0a8`)**
- IPL Kolkata vs Gujarat BUY_NO @ 84% pasaba el bloque porque cricket no estaba en la lista.
- `_CRICKET_RE`: detecta `ipl`, `t20`, `odi`, `cricket`, equipos IPL (KKR, CSK, MI, RCB…).
- `_is_cricket` incluido en `_is_individual_match` → BUY_NO bloqueado cuando `price_yes > 55%`.
- `categorize_market()` usa `_CRICKET_RE` como sports override (igual que `_TENNIS_RE`).
- Excepción: `price_yes > 75%` AND datos externos que contradigan al mercado.

**Fix 2 — mini-analyze umbral dinámico para movimientos grandes (commit `90ec7b4`)**
- ETH $2,000 +36pp no generaba señal porque $30k vol < umbral $200k.
- Movimiento ≥ 20pp indica información real independientemente del volumen.
- Nueva constante: `_VOL_THRESHOLD_BIG_MOVE = $25k` (para `abs(pct_change) ≥ 20%`).
- Log incluye `big_move=True/False` para diagnóstico.

### Floors y gates Polymarket (estado completo)

```
1.  NEAR_TARGET_FLOOR        — abs(pct_needed) < 10% → prob_min=60% + BUY_NO→PASS
2.  ALREADY_EXCEEDED         — current > target alcista → prob_min=90% + BUY_NO→PASS
3.  PRICE_MOVE_CAP           — abs(pct_needed) > 50% → real_prob=15%
4.  _validate_crypto_price   — caps históricos por asset/timeframe
5.  NEAR_TARGET_FINAL_GATE   — re-aplica BUY_NO block post crypto-validator
6.  SM_HIGH_PRICE            — smart_money + price>80% → BUY_NO→PASS
7.  HIGH_PRICE_VOL_FLOOR     — price>85% + vol>$30k → real_prob≥price-10% + PASS
8.  LOW_PRICE_VOL_CEIL       — price<15% + vol>$30k → real_prob≤price+10% + PASS
9.  NBA_SERIES_OVER          — ESPN tw≥4 → return None
10. NBA_PLAYOFF_FLOOR        — floors 3-0/3-1/2-0/1-0 + BUY_NO block si team leading
11. NBA_SERIES_SETTLED       — price≥0.92 en NBA series + BUY_NO → PASS
12. FINALIST_FINAL_GATE      — PSG/Arsenal BUY_NO bloqueado hasta resultado final
13. SPORTS_SINGLE_NO_BLOCK   — tenis/UFC/MLB/NHL/cricket: BUY_NO→PASS si price_yes>55%
14. SPORTS_BUY_NO_THRESHOLD  — min edge separado para BUY_NO en deportes (learning)
```

---

## Sports-agent

### Deportes y fuentes activas

| Deporte | Fuente | Estado | Notas |
|---|---|---|---|
| NBA Playoffs | ESPN scoreboard API (sin key) | ✅ | Finals en curso |
| Euroleague | feeds.incrowdsports.com (sin key) | ✅ | Final Four terminado |
| ACB | TheSportsDB id=4408 (sin key) | ✅ | Temporada acabando |
| Fútbol europeo | football-data.org (PL/PD/BL1/SA/FL1/CL/EL/ECL) | ⚠️ | HTTP 400 ventana >10 días |
| Tennis | odds-api.io (fallback RapidAPI) | ⚠️ | 1067 partidos en Firestore, 0 señales hoy |
| NFL / MLB / NHL | api-sports via RapidAPI | ❌ | 403 — no suscrito |

### Estado tenis — Roland Garros (20/05/2026)

Roland Garros empieza **26 mayo 2026**. Los 1067 partidos en Firestore son rondas de
clasificación con jugadores aún sin nombre definitivo (`R64P57`, `R64P58`, etc.) y
primeras rondas con jugadores reales. Hoy se han lanzado collect + analyze con resultados:

```
collect.tennis: 42 partidos guardados (The Odds API fallback — oddsapiio rate limited)
analyze: tenis → 0 señales, 1067 sin stats
```

**Estado de los tres proveedores de odds para tenis:**

| Proveedor | Endpoint | HTTP | Causa |
|---|---|---|---|
| `api-tennis.p.rapidapi.com` | `/tournaments`, `/atp/tournaments`… | 404 | Host incorrecto — endpoint no existe |
| `odds-api.io` `/v3/events` | `?sport=tennis` | 429 | Rate limit agotado (100 req/hora) |
| `odds-api.io` `/v3/odds` | `?sport=tennis&markets=h2h` | 429 | Idem — hammered durante analyze |
| The Odds API | `tennis_atp` | 404 | Endpoint no existe |
| The Odds API | `tennis_atp_french_open` | **401** | **Key expirada o plan sin acceso** |

**Acción pendiente:** verificar suscripción ODDS_API_KEY en dashboard de The Odds API.
El 401 en `tennis_atp_french_open` es nuevo (antes devolvía 404) — sugiere key válida pero
plan insuficiente para endpoints de Grand Slam específicos.

### Fixes tenis desplegados hoy

**Fix 1 — Odds-only path cuando RapidAPI sin stats (commit `995cdf3`)**

Mientras `api-tennis.p.rapidapi.com` devuelve 404, el analyzer genera señales básicas
usando cuotas de mercado como proxy de probabilidad:

- `_fetch_tennis_odds_oddsapiio()`: fetch bulk h2h odds desde `odds-api.io/v3/odds`. Caché 2h.
- `_fetch_tennis_odds()`: fallback automático a oddsapiio cuando The Odds API falla.
- `_get_best_h2h_odds()`: best + avg de cuotas a través de todos los bookmakers.
- `_prob_from_market_odds()`: fair-prob margin-stripped + **regresión 80/20 hacia la media**.
  Justificación: el mercado sobreestima favoritos en tenis (documentado en Grand Slams clay).
  Edge positivo aparece para underdogs de favoritos muy fuertes (< ~1.35 odds, underdog ~3.50+).
- Umbral relajado para señales sin stats: `min_edge=3%`, `min_confidence=55%`.
- `data_source="odds_only"` en Firestore para distinguir de señales con stats reales.

**Fix 2 — Rate-limit storm eliminado (commits `620edd8`, `7af33eb`)**

Causa raíz: el analyze llamaba `/v3/odds` una vez por partido (855+ requests) en vez de
una sola. La caché no se populaba porque 429/404/401 no se cacheaban.

- `_fetch_tennis_odds_oddsapiio()`: cachea 429/error como `[]` con TTL 15 min → para el storm.
- `_fetch_tennis_odds()`: cachea cualquier fallo (404/401) en `_LEAGUE_ODDS_CACHE` 15 min.
  Impide requests repetidas por cada partido del mismo torneo (`tennis_atp_french_open`).
- `tennis_collector._fetch_oddsapiio_h2h_odds()`: fetch bulk **1 sola request** durante collect.
  Resultado embebido como `home_odds`/`away_odds`/`odds_bookmaker` en cada match doc Firestore.
- Odds-only path: **prioridad a odds embebidas del doc** (0 HTTP extra durante analyze).

**Flujo resultante cuando odds-api.io funciona:**
```
collect: /v3/events (1 req) + /v3/odds (1 req) → odds embebidas en Firestore
analyze: lee home_odds/away_odds de Firestore → 0 HTTP → señales sin rate limit
```

### Umbrales de señal

```
SPORTS_MIN_EDGE          = 8%   (fútbol)
BASKETBALL_MIN_EDGE      = 4%   (NBA/Euroleague — mercados eficientes)
TENNIS_ODDS_ONLY_MIN_EDGE = 3%  (sin stats RapidAPI — modelo regresión 80/20)
POLY_MIN_EDGE            = 8%
POLY_MIN_CONFIDENCE      = 65%
```

---

## Bugs conocidos

| # | Bug | Servicio | Impacto | Pendiente |
|---|---|---|---|---|
| 1 | `football-data.org HTTP 400` — ventana >10 días | sports-agent | Sin fútbol europeo | Reducir `days=14` → `days=10` en `_collect_football` |
| 2 | `api-tennis.p.rapidapi.com` 404 en todos los endpoints | sports-agent | 0 stats de jugadores | Encontrar host correcto en RapidAPI dashboard |
| 3 | The Odds API key **401** en `tennis_atp_french_open` | sports-agent | Sin odds Roland Garros | Verificar suscripción/plan en The Odds API dashboard |
| 4 | `odds-api.io` rate limit 429 tras múltiples runs | sports-agent | Sin odds temporalmente | Esperar reset 1h; embedding resuelve el problema a largo plazo |
| 5 | NHL/MLB/NFL HTTP 403 RapidAPI | sports-agent | Sin cobertura | Requiere suscripción |
| 6 | ETH/SOL ticker precios incorrectos (sports ticker) | sports-agent | Cosmético | Pendiente |

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
| `cleanup-stale.yml` | manual | Limpiar predictions/odds_cache stale |

---

## Commits de esta sesión

```
7af33eb  fix(tennis): cachear fallos en _LEAGUE_ODDS_CACHE 15min
620edd8  fix(tennis): eliminar rate-limit storm en odds-api.io durante analyze
995cdf3  feat(tennis): odds-only path cuando RapidAPI sin stats
90ec7b4  fix(polymarket): mini-analyze umbral $25k cuando movimiento >=20pp
99fc0a8  fix(polymarket): SPORTS_SINGLE_NO_BLOCK — añadir cricket/IPL al bloque
```

---

## Próximas acciones recomendadas

1. **The Odds API dashboard** — verificar si key `0c42d51a...` tiene acceso a `tennis_atp_french_open`
   (401 Unauthorized, no 404 — endpoint existe pero no autorizado)
2. **RapidAPI dashboard** — encontrar host correcto para API de tenis (activo con la misma key)
3. **football-data.org HTTP 400** — reducir ventana de 14 a 10 días en `_collect_football`
4. **Roland Garros 26/05** — con embed de odds funcionando, el primer día del torneo debería
   generar señales odds-only para los favoritos de cuota < 1.35
5. **Esperar reset odds-api.io** (~1h desde última run) y relanzar `sports-collect` + `sports-analyze`
   para confirmar que el embedding de odds funciona correctamente
