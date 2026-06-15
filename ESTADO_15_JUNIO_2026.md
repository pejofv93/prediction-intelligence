# Estado del sistema — 15 junio 2026

## Resumen ejecutivo

El Mundial 2026 lleva cuatro días en curso (empezó el 11 jun). El sistema está
generando señales para partidos WC con ELO FIFA real para 60+ selecciones.
Semanas NBA y ACB terminadas: Knicks campeones NBA, Valencia Basket campeón ACB.

Dos sesiones completas hoy. Mañana se desplegaron FORM_ELO_CONFLICT,
RISING_ODDS_BLOCK y PLAYOFF_SERIES_ADJUSTMENT. En la sesión de tarde se hizo
un análisis de estrategia de cuotas para temporada completa (agosto + octubre
simultáneos), se confirmó que The Odds API consume ~400 req/mes (dentro de 500)
y que odds-api.io puede cubrir baloncesto/tenis (pendiente verificar en oct).
Fix adicional: blocklist permanente para `basketball_spain_acb` que desperdiciaba
~1.440 req/mes en 404s repetidos. Total ~15 fixes en la sesión.

sports-agent en revisión `00465-p8x` (Cloud Run).

---

## Infraestructura

| Servicio          | Estado   | URL / Revisión                                                    |
|-------------------|----------|-------------------------------------------------------------------|
| sports-agent      | LIVE     | `sports-agent-327240737877.europe-west1.run.app` rev `00465-p8x` |
| polymarket-agent  | LIVE     | Cloud Run europe-west1                                            |
| telegram-bot      | LIVE     | Cloud Run europe-west1                                            |
| dashboard         | LIVE     | Cloud Run europe-west1                                            |
| Firestore         | LIVE     | proyecto `prediction-intelligence`                                |
| GitHub Actions    | LIVE     | 4 workflows de analyze (01:00 / 07:00 / 13:00 / 19:00 UTC)       |

---

## Fuentes de datos activas

| Fuente              | Estado      | Notas                                                          |
|---------------------|-------------|----------------------------------------------------------------|
| football-data.org   | OK          | Free tier, `days=7`, competición WC                            |
| odds-api.io         | OK          | Primaria fútbol — 75 IDs pre-fetch, 8 batches                  |
| The Odds API v4     | OK          | Secundaria fútbol + baloncesto + tenis (~400 req/mes en pico)  |
| Sofascore           | OK (parcial)| xG para clubs; 403 en Cloud Run para selecciones WC           |
| Sofascore WC        | OK          | fixture WC26 vía tournament/16/season/58210                    |
| odds-api.io BTTS    | OK          | Fix `ddf1eff` — lista → dict parseado correctamente            |
| AH/Spreads          | OK          | Fix `ddf1eff` — branch dict añadido                            |
| ELO FIFA WC26       | OK          | 64 selecciones base + 7 aliases en `_WC26_FIFA_ELO`            |

---

## Mercados alternativos — estado (confirmado en producción WC26)

### Mercados desbloqueados y operativos

| Mercado       | Fuente primaria   | Fallback             | Estado                                |
|---------------|-------------------|----------------------|---------------------------------------|
| h2h (1X2)     | odds-api.io       | The Odds API         | ✓ operativo todas las ligas           |
| Asian Handicap| odds-api.io       | The Odds API spreads | ✓ confirmado en WC26 (FIX6 + FIX10)  |
| Totals 2.5    | odds-api.io       | The Odds API → TOA → sintético | ✓ FIX12 — cadena de 3 fuentes |
| BTTS          | odds-api.io       | The Odds API btts    | ✓ FIX12 — btts en The Odds API       |
| Corners O/U   | OddsPapi fixtures | football-data.co.uk  | ✓ ECL+WC26 IDs añadidos (FIX11)      |
| Bookings O/U  | OddsPapi fixtures | stats FDCO           | ✓ mismo pipeline que corners          |

**Una línea por mercado** (la más favorable del bookmaker) pero **múltiples mercados
por partido** en una sola señal: un partido WC puede generar AH + BTTS + OU 2.5
como señales independientes. Confirmado en producción con la cadena de fallback.

### Basketball spreads (FIX10) — listo para octubre

El spread home_line se normaliza igual que fútbol (`key = pt if is_home else -pt`).
Activo desde el `097f4cd`. NBA preseason arranca en octubre — se activará automáticamente.

---

## Mundial 2026 — estado (15 junio)

El WC empezó el 11 de junio. La fase de grupos dura hasta el 2 de julio (48 partidos).

### ELO FIFA inicializado

`init_wc26_national_elos()` escribe ELO basado en el ranking FIFA de junio 2026.
Es idempotente: conserva el ELO real si el equipo ya tiene historial de partidos.

| Confederación | Plazas | Rango ELO en dict | Ejemplo                        |
|---------------|--------|-------------------|--------------------------------|
| UEFA          | 16     | 1452 – 1875       | Spain 1875, Scotland 1503      |
| CONMEBOL      | 6+     | 1358 – 1877       | Argentina 1877, Bolivia 1358   |
| CONCACAF      | 6+3    | 1340 – 1687       | Mexico 1687, Haiti 1350        |
| AFC           | 8      | 1280 – 1662       | Japan 1662, Indonesia 1280     |
| CAF           | 9      | 1370 – 1755       | Morocco 1755, Cape Verde 1382  |
| OFC           | 1      | 1345              | New Zealand 1345               |

**Selecciones con alias añadidos (para normalizar nombres de The Odds API):**
South Korea, Cote d'Ivoire, Congo DR, United States, Curacao (sin ç),
Czech Republic, Bosnia & Herzegovina.

### Conexión ELO ↔ enricher

Fix `96e6d8c`: `_resolve_elo()` en `data_enricher.py` intenta `sf_XXXX` primero
(ID Sofascore) y, si devuelve DEFAULT_ELO, busca `wc_{team_name}` usando el
nombre del partido. Recalcula `elo_home_win_prob` con `expected_score()`.

### Checks de integración WC

| Check                                     | Estado |
|-------------------------------------------|--------|
| WC en `_POISSON_EXEMPT_LEAGUES`           | ✓      |
| WC en `_FOOTBALL_LEAGUES` (odds-api.io)   | ✓      |
| WC en `_LEAGUE_KEYWORDS` (odds-api.io)    | ✓      |
| WC en `_PRIORITY_LEAGUES_FOR_ODDS`        | ✓      |
| WC en `_ODDS_SPORT_MAP` (The Odds API)    | ✓      |
| ELO FIFA inicializado en Firestore        | ✓      |
| ELO conectado al enricher vía `_resolve_elo` | ✓   |
| SYNTHETIC_DEFAULT_CAP excluido para WC/WC26 | ✓    |
| Odds cap ampliado a 6.00 para WC/WC26    | ✓      |
| BTTS/OU/AH en pre-fetch WC                | ✓      |
| BTTS en WC26 con bookmaker real           | ⚠️ brecha — solo disponible vía odds-api.io cuando el mercado abre |

### Señales esperadas durante fase de grupos

```
Collect → football-data.org /matches?competitions=WC → save_upcoming_matches
Enrich  → data_enricher: ELO real (no DEFAULT) + form de Sofascore
Analyze → pre-fetch: WC 5-10 eventos con BTTS/totals/spreads
        → generate_signal: ELO domina ensemble
        → BTTS Yes/No + OU 2.5 + AH -0.5/+0.5
        → FORM_ELO_CONFLICT activo: protege contra selecciones con form inflada
        → RISING_ODDS_BLOCK activo: bloquea edge ilusorio por cuota en alza
```

---

## Temporadas finalizadas

### NBA 2025-26 — Knicks campeones

Los New York Knicks ganaron el campeonato NBA 2025-26.
- Señales de playoffs: `PLAYOFF_SERIES_ADJUSTMENT` implementado hoy para próxima temporada.
- Bug histórico: Joventut (3 señales fallidas en final ACB) y SAS vs OKC motivaron el fix.
- Lección: el modelo de temporada regular subestima al dominador de la serie.

### ACB 2025-26 — Valencia Basket campeón

Valencia Basket ganó la liga ACB (Valencia 3-0 frente a Joventut en la final).
- `basketball_spain_acb` no existe en The Odds API (HTTP 404 permanente).
- Fix `3552547`: blocklist `_THE_ODDS_API_NO_COVERAGE` — ACB nunca vuelve a intentarse.
- ACB sin odds de terceros → señales solo posibles con Betfair API (pendiente, no urgente).

---

## Estrategia de cuotas — temporada completa (agosto + octubre)

### Análisis realizado en sesión 15 junio

Con fútbol EU (agosto) + baloncesto (octubre) activos simultáneamente:

| API             | Límite free | Consumo estimado pico | Margen    | Acción requerida |
|-----------------|-------------|----------------------|-----------|-----------------|
| odds-api.io     | 72k/mes (100/h) | ~1.100 req/mes    | ×65       | Ninguna — primaria confirmed |
| The Odds API    | 500/mes     | ~360-450 req/mes     | OK        | Ninguna — multi-mercado ya implementado |
| OddsPapi        | 250/mes     | ~240 req/mes         | Ajustado  | Ninguna — solo fixtures calls |
| AllSports       | 100/día     | <50/día              | OK        | Ninguna |

**Conclusiones:**
- `_get_league_events` ya pide `markets=h2h,spreads,totals,btts` en **un solo request** por sport_key.
- TTL 24h + Firestore persistence → 1 llamada/sport_key/día máximo.
- Fix `3552547`: blocklist ACB ahorra ~1.440 req/mes desperdiciados en 404s.
- Fix `3552547`: TTL 404 desconocidos 30 min → 4h (reducción ×8 reintentos innecesarios).

### Verificaciones pendientes (fuera de temporada)

| Fecha      | Acción                                                         | Consecuencia si positivo                    |
|------------|----------------------------------------------------------------|---------------------------------------------|
| 30 junio   | Llamar `/api/oddsapiio-coverage` — ¿Wimbledon en feed tenis?  | The Odds API eliminable para tenis          |
| 1 octubre  | Ídem — ¿NBA/Euroleague en feed basket con odds Bet365/Unibet? | The Odds API eliminable para baloncesto     |

**Betfair Exchange API** identificada como opción de respaldo: API REST pública gratuita,
sin cuota mensual, cobertura fútbol/basket/tenis. No urgente mientras odds-api.io cubra.

---

## Fixes desplegados hoy — sesión de mañana

### `5e8b4aa` — fix(value-bet): FORM_ELO_CONFLICT + RISING_ODDS_BLOCK

**FIX 1 — FORM_ELO_CONFLICT** (`ensemble_probability` en `value_bet_engine.py`)

Cuando la señal de form y la de ELO difieren más de 0.40, el peso de form
se reduce al 30% de su valor original. ELO domina el ensemble.

- **Problema real:** Escocia ganó a Haití (ELO 1350) e infló form a 0.80. Su ELO es
  1503 → contra Marruecos (ELO 1755) la señal de form tiraba hacia apostar a Escocia.
- **Threshold:** `_FORM_ELO_CONFLICT_THRESHOLD = 0.40`
- **Efecto:** form=0.80 / elo=0.29 → diff=0.51 → peso form ×0.30. ELO y Poisson deciden.

**FIX 2 — RISING_ODDS_BLOCK** (`generate_signal` en `value_bet_engine.py`)

Si la cuota del equipo apostado sube más del 20% en las últimas 6h o 24h,
la señal se bloquea (`return []`).

- **Problema real:** Scotland +28.8% en cuota → edge calculado +40.8% (aritmético, ilusorio).
  El bookmaker sube cuotas cuando tiene info negativa (lesión, alineación débil, etc.).
- **Threshold:** `_RISING_ODDS_THRESHOLD = 0.20`
- **Matiz:** cuota bajando (SMART_MONEY) nunca bloqueada — es señal positiva.

### `8e115d4` — feat(basketball): PLAYOFF_SERIES_ADJUSTMENT

Nuevo bloque en `generate_basketball_signals` que ajusta `p_home_win` según
el marcador actual de la serie de playoffs (NBA, Euroleague, ACB).

| Estado de serie | Ajuste p_home_win |
|-----------------|-------------------|
| 2-0 líder       | ±6pp              |
| 1-0             | sin ajuste        |
| ≥3 de ventaja   | ±4pp/victoria, cap ±16pp |

**`_parse_series_state()`:** extrae datos desde `series_wins_home/away` (directo)
o parsea `series_title` con regex (`"Boston leads 3-1"`, `"Series tied 2-2"`).

---

## Fixes desplegados — sesión de tarde (15 junio)

### `3552547` — fix(quota): blocklist 404 ACB + diagnóstico odds-api.io

**FIX — Blocklist permanente sport_keys sin cobertura** (`value_bet_engine.py`)

`basketball_spain_acb` devolvía 404 permanente en The Odds API pero se cacheaba
solo 30 min → reintento cada 30 min → **~1.440 req/mes desperdiciados** capaces de
agotar la cuota mensual de 500 req por sí solos.

- `_THE_ODDS_API_NO_COVERAGE: frozenset[str]` — check O(1) antes del cache de memoria.
- Sport_keys en la lista: cero HTTP, cero Firestore, retorno inmediato `[]`.
- TTL 404 para sport_keys desconocidos: 30 min → 4h (reducción ×8 en reintentos).

**Endpoint diagnóstico** añadido a `main.py`:
- `GET /api/oddsapiio-coverage` (requiere X-Cloud-Token)
- Prueba slugs basketball + tennis en odds-api.io y reporta ligas/mercados encontrados.
- Diagnóstico ejecutado hoy: basketball OK (471 eventos, NBA off-season), tennis OK
  (932 eventos, Wimbledon arranca 30 jun). NBA/Wimbledon no verificables hasta que estén en temporada.

---

## Historial completo de fixes desde el estado anterior (10 junio)

| Commit    | FIX | Descripción                                                              |
|-----------|-----|--------------------------------------------------------------------------|
| `b6ffe00` | —   | ELO FIFA junio 2026 para 63 selecciones WC en `sofascore_wc.py`         |
| `54534f0` | —   | 3 bugs que bloqueaban señales WC26                                       |
| `72c5f5a` | —   | Excluir WC/WC26 del SYNTHETIC_DEFAULT_CAP                                |
| `96e6d8c` | —   | Conectar ELO WC26 con enricher vía `_resolve_elo()`                     |
| `10b61e0` | —   | Ampliar odds cap a 6.00 para señales WC/WC26                             |
| `d8abea1` | —   | Aliases de nombre para selecciones WC26                                  |
| `d62a3f2` | —   | Añadir Ghana, Sweden, Norway, Cape Verde, alias Curacao al dict ELO      |
| `47ec51d` | FIX9| Totals cadena de 3 fuentes (OddsPapi primario → The Odds API → sintético)|
| `097f4cd` | FIX10| Basketball spreads: normalizar home_line igual que football              |
| `683ac63` | FIX11| Corners/bookings: ECL=480 y WC26=77 añadidos a `_TOURNAMENT_IDS`       |
| `673cbd6` | FIX12| Totals T3.5→T2.5, TOA primario, BTTS vía The Odds API                   |
| `9e06eef` | FIX13| OddsPapi: eliminar llamadas wasted + guard 48h + header remaining        |
| `5e8b4aa` | —   | FORM_ELO_CONFLICT + RISING_ODDS_BLOCK                                    |
| `8e115d4` | —   | PLAYOFF_SERIES_ADJUSTMENT (basketball)                                   |
| `4af5b29` | —   | `scripts/deploy_sports.sh`                                               |
| `3552547` | —   | Blocklist ACB 404 + endpoint `/api/oddsapiio-coverage`                   |

*(Fixes adicionales mencionados en sesión: confidence cap mercados alt, gates WC26,
xG sintético desde ELO, bug recuento weekly report — integrados en los commits de la tabla.)*

---

## ROI y rendimiento — diagnóstico crítico

### Datos reales (sesión 15 junio)

- **ROI sports acumulado: -14.6%**
- Semanas con señales fuertes (edge alto): **21% win rate / -28% ROI**
- Problema identificado: las señales con edge mayor son las que más fallan.

### Hipótesis (pendiente investigar)

El edge calculado puede estar inflado por:
1. **Cuotas infladas por bookmakers** antes de un evento con info negativa
   (RISING_ODDS_BLOCK ataca esto parcialmente)
2. **Overfitting al ELO** en selecciones sin historial de club → ensemble sesgado
3. **Umbrales de edge demasiado bajos** para mercados alternativos binarios (BTTS, AH)
   que tienen distribuciones distintas al 1X2
4. **Sample size**: n muy pequeño en señales resueltas → ROI volátil

### Acción prioritaria

**Investigar de raíz por qué las señales de edge alto fallan** antes de agosto
(cuando vuelvan las ligas EU y el volumen aumente). Posibles enfoques:
- Separar ROI por mercado (h2h vs BTTS vs AH) y por liga
- Revisar si el edge calculado vs cuota real concuerda con la probabilidad implícita
- Comparar con el modelo sin filtros (raw Poisson) para ver si los filtros ayudan o dañan

---

## Leaderboard de señales (acumulado hasta hoy)

*(Basado en datos de Firestore `predictions` — solo señales con `result != null`)*

| Liga        | Señales | ROI estimado | Notas                                         |
|-------------|---------|--------------|-----------------------------------------------|
| WC26/WC     | en curso| —            | Off-season clubs; señales activas desde 11 jun|
| NBA playoffs| —       | —            | 0% accuracy (3/3 Joventut → fix aplicado)     |
| Total sports| —       | -14.6%       | Edge alto correlaciona negativamente con ROI  |

*(Las ligas europeas de club están en off-season desde mayo. Vuelven en agosto.)*

---

## Variables de entorno Cloud Run

| Variable                | Servicio       | Estado      |
|-------------------------|----------------|-------------|
| GROQ_API_KEY            | sports, poly   | Configurada |
| TELEGRAM_BOT_TOKEN      | telegram-bot   | Configurada |
| TELEGRAM_CHAT_ID        | telegram-bot   | Configurada |
| FOOTBALL_API_KEY        | sports-agent   | Configurada |
| ODDS_API_KEY            | sports-agent   | Configurada |
| ODDSAPIIO_KEY           | sports-agent   | Configurada |
| ODDSPAPI_KEY            | sports-agent   | Configurada |
| OPTIC_ODDS_KEY          | sports-agent   | Configurada |
| TAVILY_API_KEY          | sports-agent   | Configurada |
| CLOUD_RUN_TOKEN         | todos          | Configurada |
| YOUTUBE_TOKEN_B64       | NEXUS/Railway  | Configurada |
| CRON_SECRET             | NEXUS          | Pendiente   |
| YOUTUBE_API_KEY         | NEXUS RECON    | Pendiente   |
| FAL_KEY                 | NEXUS HELIOS   | Pendiente   |

---

## Bugs conocidos

| Bug                                                       | Impacto | Prioridad |
|-----------------------------------------------------------|---------|-----------|
| Sofascore 403 en Cloud Run para selecciones WC            | Medio   | Media     |
| BTTS WC26 — solo disponible si bookmaker lo abre en odds-api.io | Bajo | Baja |
| ETH/SOL ticker precios incorrectos (NEXUS)                | Bajo    | Baja      |
| CALÍOPE genera inglés en algunos modos (NEXUS)            | Bajo    | Baja      |
| Señales WC conf fluctúa: sin stats de club para selecciones | Medio | Media    |
| ROI sports -14.6% — causa raíz sin identificar            | Alto    | **Alta**  |

*(ACB 404 eliminado de bugs: fix `3552547` lo resuelve con blocklist permanente.)*

---

## Próximos pasos (orden prioridad)

1. **[URGENTE] Investigar ROI -14.6%** — separar por mercado/liga, revisar si edge
   calculado es fiable o inflado. Antes de agosto para no escalar el problema con más ligas.

2. **[INMEDIATO] Monitor señales WC** — verificar en logs del analyze 19:00 UTC:
   - `FORM_ELO_CONFLICT` activa para partidos con forma inflada
   - `RISING_ODDS_BLOCK` activa cuando cuota suba >20%
   - `MARKETS_PARSED` con btts/totals/spreads para partidos WC

3. **[30 junio] Verificar Wimbledon en odds-api.io** — llamar
   `/api/oddsapiio-coverage` y confirmar que el torneo aparece en el feed tenis
   con odds de Bet365/Unibet. Si positivo: The Odds API ya no necesita quota para tenis.

4. **[1 octubre] Verificar NBA/Euroleague en odds-api.io** — mismo endpoint.
   Si positivo: The Odds API puede pasar a emergency-only fallback.

5. **[Agosto] Auditar mercados alt en ligas EU** — cuando arranquen PL/PD/BL1/SA/FL1,
   verificar en señales reales que AH, BTTS y OU 2.5 llegan con cuotas correctas.

6. **[Fase eliminatoria WC — 3 julio]** — KO rounds sin empate → verificar que
   AWAY_GATE_CONF y HIGH_DRAW_PROB se comportan correctamente.

7. **[Octubre] Euroleague/NBA** — PLAYOFF_SERIES_ADJUSTMENT ya listo.
   Verificar que ESPN API devuelve `competition.series` para Euroleague playoffs.

8. **[No urgente] Betfair Exchange API** — integración como fuente de respaldo sin cuota
   mensual. Registro + SDK Python (`betfairlightweight`). Solo si odds-api.io falla
   en octubre para baloncesto.

9. **NEXUS RECON** — añadir `YOUTUBE_API_KEY` en Railway Dashboard.

10. **NEXUS HELIOS v3** — añadir saldo en fal.ai → activar avatar IA.
