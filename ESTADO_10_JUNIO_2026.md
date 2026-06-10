# Estado del sistema — 10 junio 2026

## Resumen ejecutivo

Sistema operativo en Cloud Run (europe-west1). Pipeline de análisis corriendo 4 veces/día
(01:00, 07:00, 13:00, 19:00 UTC). El Mundial 2026 empieza el 12 de junio — el sistema
está preparado para cubrir los partidos desde el primer día.

---

## Infraestructura

| Servicio        | Estado     | URL / Notas                                  |
|-----------------|------------|----------------------------------------------|
| sports-agent    | LIVE       | Cloud Run europe-west1, timeout=1800s         |
| polymarket-agent| LIVE       | Cloud Run europe-west1                        |
| telegram-bot    | LIVE       | Cloud Run europe-west1                        |
| dashboard       | LIVE       | Cloud Run europe-west1                        |
| Firestore       | LIVE       | proyecto prediction-intelligence              |
| GitHub Actions  | LIVE       | deploy en push a main, 4 workflows de analyze |

---

## Fuentes de datos activas

| Fuente                  | Estado      | Notas                                                    |
|-------------------------|-------------|----------------------------------------------------------|
| football-data.org       | OK          | Free tier, max 10 días por request, `days=7` en collect  |
| odds-api.io             | OK          | Primaria para fútbol — 75 IDs pre-fetch / 8 batches      |
| The Odds API v4         | OK          | Secundaria fútbol + tenis + baloncesto                   |
| Sofascore               | OK (local)  | 403 en Cloud Run para WC26; OK para xG en otras ligas    |
| Tennis API              | OK          | 1060 partidos colectados                                 |

---

## Ligas activas en enriched_matches (10 junio 2026)

```
['ATP', 'ATP_MADRID', 'ATP_ROME', 'NBA', 'WC', 'WTA']
```

Las ligas europeas (PL, PD, BL1, SA, FL1, CL) están en off-season. Volverán en agosto.

---

## Mundial 2026 — estado

| Check                                   | Estado |
|-----------------------------------------|--------|
| WC en `enriched_matches`                | ✓      |
| WC en `_POISSON_EXEMPT_LEAGUES`         | ✓      |
| WC en `_FOOTBALL_LEAGUES` (odds-api.io) | ✓      |
| WC en `_LEAGUE_KEYWORDS` (odds-api.io)  | ✓      |
| WC en `_PRIORITY_LEAGUES_FOR_ODDS`      | ✓      |
| WC en `_ODDS_SPORT_MAP` (The Odds API)  | ✓      |
| odds-api.io encuentra 10 eventos WC     | ✓      |
| Mercados BTTS/OU/AH abiertos            | PENDIENTE — bookmakers los abren el día del partido |

**Partidos WC detectados (próximos 7 días):**
Netherlands vs Japan, Brazil vs Morocco, France vs Senegal, Belgium vs Egypt,
Mexico vs South Africa, Ivory Coast vs Ecuador, Iran vs New Zealand, Sweden vs Tunisia
— todos el 12 junio 2026, kickoff desde las 15:00 UTC.

**Primera vez que aparecerán señales BTTS/OU/AH:** analyze de las 13:00 UTC del 12 junio,
cuando Bet365/Unibet abran los mercados de props para los partidos del día.

---

## Señales activas (último analyze — 20:43 UTC)

```
analyze: 1 señales generadas de 651 enriquecidos en 109.1s
```

Solo 1 señal porque:
- Ligas europeas en off-season → no hay partidos de PL/PD/BL1/SA/FL1
- WC Group Stage: BTTS/OU/AH no abiertos aún por bookmakers (48h antes)
- WC h2h: SYNTHETIC_DEFAULT_CAP conf=0.60 (sin stats históricos de selecciones)
  → gate de confianza no supera umbral mínimo

---

## Fixes desplegados en esta sesión (10 junio 2026)

### `a90de04` — feat(corners): stats reales Sofascore

**Problema:** `_calculate_corners_prob()` usaba xG como proxy para corners.
**Fix:**
- `sofascore_football.py`: extrae `cornerKicks` de `fetch_event_statistics()` para los
  últimos partidos de cada equipo. Cap 8 llamadas/equipo (5 con xG + 5 corners-only).
- `data_enricher.py`: propaga `home_avg_corners_for`, `home_avg_corners_against`,
  `away_avg_corners_for`, `away_avg_corners_against`.
- `value_bet_engine.py`: usa stats reales cuando disponibles; fallback a proxy xG.

### `5dd2a5e` — fix(football): 3 bugs que bloqueaban señales en off-season/WC

**Bug 1 — days=14 HTTP 400:**
`_collect_football()` llamaba `get_upcoming_matches(days=14)` → football-data.org free tier
rechaza rangos >10 días. Fix: `days=7`.

**Bug 2 — WC no en _POISSON_EXEMPT_LEAGUES:**
Partidos con `league="WC"` bloqueados por el Poisson Guard (sin datos de liga doméstica).
Fix: añadir `"WC"` junto a `"WC26"`.

**Bug 3 — WC no en odds-api.io:**
`_FOOTBALL_LEAGUES` y `_LEAGUE_KEYWORDS` no tenían `"WC"` (solo `"WC26"`).
Fix: añadir `"WC"` a ambos con keywords FIFA World Cup.

### `ddf1eff` — fix(odds-apiio): BTTS/Spreads + WC pre-fetch

**Bug 1 — BTTS nunca parseado:**
`_parse_market("btts", [{"yes":"1.72","no":"2.00"}], ...)` — branch solo manejaba `dict`.
odds-api.io envía `odds` como lista → BTTS siempre vacío → nunca señales BTTS.
Fix: añadir `elif isinstance(mkt_data, list)` en branch `btts`.

**Bug 2 — AH/Spreads nunca parseado:**
Raw odds `[{"home":"1.90","away":"1.90","handicap":"-0.5"}]` tiene clave `"home"` →
se "unwrapea" a dict. Branch `spreads` solo manejaba list → dict llega como dict → vacío.
Fix: añadir `elif isinstance(mkt_data, dict)` en branch `spreads`.

**Bug 3 — WC no en pre-fetch:**
`_PRIORITY_LEAGUES_FOR_ODDS` no tenía `"WC"` ni `"WC26"` → los eventos WC nunca
entraban al pre-fetch de `/odds/multi` → `all_markets` vacío en el analyzer.
Fix: añadir `"WC"` y `"WC26"` al frozenset. Aumentar cap 50→75 (11 ligas × 5 = 55 IDs).

---

## Comportamiento esperado del sistema desde el 12 junio

```
Collect (prev. noche):
  → football-data.org /matches?dateFrom=...&dateTo=...&competitions=...WC → WC Group Stage
  → save_upcoming_matches: N partidos WC guardados

Enrich:
  → data_enricher.py: WC matches → enriched_matches con ELO sintético
  → sofascore_football.py: 403 en Cloud Run → sin xG/corners para selecciones

Analyze (13:00 UTC cada día del WC):
  → pre-fetch odds: WC → 5 eventos en _ODDS_MAP_CACHE con BTTS/totals/spreads
  → generate_signal: EXTRA_MARKETS_CHECK → _generate_oddsapiio_extra_signals
  → señales BTTS Yes/No + OU 2.5 Over/Under + AH -0.5/+0.5
  → [NUEVA] señal corners OU si bookmaker ofrece el mercado
```

---

## Bugs conocidos (no críticos)

| Bug                                             | Impacto  | Prioridad |
|-------------------------------------------------|----------|-----------|
| Sofascore 403 en Cloud Run para WC26/selecciones| Bajo     | Media     |
| conf=0.60 cap para WC (sin stats de selecciones)| Medio    | Media     |
| ETH/SOL ticker precios incorrectos (NEXUS)      | Bajo     | Baja      |
| CALÍOPE genera inglés en algunos modos (NEXUS)  | Bajo     | Baja      |

---

## Variables de entorno Railway/Cloud Run

| Variable                  | Servicio       | Estado      |
|---------------------------|----------------|-------------|
| GROQ_API_KEY              | polymarket     | Configurada |
| TELEGRAM_BOT_TOKEN        | telegram-bot   | Configurada |
| TELEGRAM_CHAT_ID          | telegram-bot   | Configurada |
| FOOTBALL_API_KEY          | sports-agent   | Configurada |
| ODDS_API_KEY              | sports-agent   | Configurada |
| ODDSAPIIO_KEY             | sports-agent   | Configurada |
| ODDSPAPI_KEY              | sports-agent   | Configurada |
| CLOUD_RUN_TOKEN           | todos          | Configurada |
| YOUTUBE_TOKEN_B64         | NEXUS/Railway  | Configurada |
| CRON_SECRET               | NEXUS          | Pendiente   |
| YOUTUBE_API_KEY           | NEXUS RECON    | Pendiente   |
| FAL_KEY                   | NEXUS HELIOS   | Pendiente   |

---

## Próximos pasos (orden de prioridad)

1. **12 junio 07:00 UTC** — verificar `MARKETS_PARSED: X vs Y → ['btts', 'totals', ...]`
   en logs del analyze del día. Confirma que el fix BTTS/Spreads funciona en producción.

2. **WC conf cap** — estudiar si bajar `SYNTHETIC_DEFAULT_CAP` para ligas internacionales
   o usar ELO puro sin cap cuando no hay stats históricos.

3. **Sofascore selecciones** — añadir fuente alternativa de xG/form para selecciones
   (EURO 2024, Copa América 2024 ya están en TOURNAMENTS como fuente histórica).

4. **RAPID/TikTok** — subir cookies tiktok-uploader.

5. **RECON** — añadir `YOUTUBE_API_KEY` en Railway Dashboard.

6. **HELIOS v3** — añadir saldo en fal.ai.
