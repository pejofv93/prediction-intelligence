# Estado del sistema — 15 junio 2026

## Resumen ejecutivo

El Mundial 2026 lleva cuatro días en curso (empezó el 11 jun). El sistema está
generando señales para partidos WC con ELO FIFA real para 60+ selecciones.
Semanas NBA y ACB terminadas: Knicks campeones NBA, Valencia Basket campeón ACB.
Tres fixes de calidad desplegados hoy: FORM_ELO_CONFLICT, RISING_ODDS_BLOCK y
PLAYOFF_SERIES_ADJUSTMENT. sports-agent en revisión `00454-pc2` (Cloud Run).

---

## Infraestructura

| Servicio          | Estado   | URL / Revisión                                                    |
|-------------------|----------|-------------------------------------------------------------------|
| sports-agent      | LIVE     | `sports-agent-327240737877.europe-west1.run.app` rev `00454-pc2` |
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
| The Odds API v4     | OK          | Secundaria fútbol + baloncesto + tenis                         |
| Sofascore           | OK (parcial)| xG para clubs; 403 en Cloud Run para selecciones WC           |
| Sofascore WC        | OK          | fixture WC26 vía tournament/16/season/58210                    |
| odds-api.io BTTS    | OK          | Fix `ddf1eff` — lista → dict parseado correctamente            |
| AH/Spreads          | OK          | Fix `ddf1eff` — branch dict añadido                            |
| ELO FIFA WC26       | OK          | 64 selecciones base + 7 aliases en `_WC26_FIFA_ELO`            |

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

### Señales esperadas durante fase de grupos

```
Collect → football-data.org /matches?competitions=WC → save_upcoming_matches
Enrich  → data_enricher: ELO real (no DEFAULT) + form de Sofascore
Analyze → pre-fetch: WC 5-10 eventos con BTTS/totals/spreads
        → generate_signal: ELO domina ensemble
        → BTTS Yes/No + OU 2.5 + AH -0.5/+0.5
        → FORM_ELO_CONFLICT activo: protege contra selecciones con form inflada
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
- Bug conocido: `basketball_spain_acb` no existe en The Odds API (HTTP 404).
  Fix anterior `8baf6d1` lo eliminó del `_SPORT_KEY_MAP`.
- ACB sin odds de terceros → señales ACB solo posibles con proveedor de pago (Pinnacle/Betfair).

---

## Fixes desplegados hoy (15 junio 2026)

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
- **Dirección:** `pct_change_6h > 0.20 AND direction != team_to_back_dir`
  (positivo = cuota subió; direction = hacia quién fue el dinero = opuesto al que queremos apostar)
- **Cuota bajando (SMART_MONEY):** nunca bloqueada — es señal positiva.

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

**ESPN collector** (`api_sports_client.py`): ahora extrae `competition.series`
y persiste `series_wins_home`, `series_wins_away`, `series_title`, `playoff=True`
en Firestore. Listo para NBA 2026-27 y Euroleague 2026-27.

**`4af5b29` — `scripts/deploy_sports.sh`:** script de deploy equivalente al de
polymarket — copia `shared/` al contexto antes del build, limpia al salir.

---

## Historial de fixes desde el estado anterior (10 junio)

| Commit    | Descripción                                                            |
|-----------|------------------------------------------------------------------------|
| `b6ffe00` | ELO FIFA junio 2026 para 63 selecciones WC en `sofascore_wc.py`       |
| `54534f0` | 3 bugs que bloqueaban señales WC26                                     |
| `72c5f5a` | Excluir WC/WC26 del SYNTHETIC_DEFAULT_CAP                              |
| `96e6d8c` | Conectar ELO WC26 con enricher vía `_resolve_elo()`                   |
| `10b61e0` | Ampliar odds cap a 6.00 para señales WC/WC26                           |
| `d8abea1` | Aliases de nombre para selecciones WC26                                |
| `d62a3f2` | Añadir Ghana, Sweden, Norway, Cape Verde, alias Curacao al dict ELO    |
| `5e8b4aa` | FORM_ELO_CONFLICT + RISING_ODDS_BLOCK                                  |
| `8e115d4` | PLAYOFF_SERIES_ADJUSTMENT (basketball)                                 |
| `4af5b29` | `scripts/deploy_sports.sh`                                             |

---

## Leaderboard de señales (acumulado hasta hoy)

*(Basado en datos de Firestore `predictions` — solo señales con `result != null`)*

| Liga        | Señales | Correctas | Accuracy |
|-------------|---------|-----------|----------|
| PL          | —       | —         | —        |
| PD          | —       | —         | —        |
| WC26/WC     | en curso| —         | —        |
| NBA playoffs| —       | —         | 0% (3/3 fallidas Joventut → fix aplicado) |
| Tenis ATP   | —       | —         | —        |

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

## Bugs conocidos (no bloqueantes)

| Bug                                                       | Impacto | Prioridad |
|-----------------------------------------------------------|---------|-----------|
| Sofascore 403 en Cloud Run para selecciones WC            | Medio   | Media     |
| ACB sin odds de mercado (The Odds API 404)                | Bajo    | Baja      |
| ETH/SOL ticker precios incorrectos (NEXUS)                | Bajo    | Baja      |
| CALÍOPE genera inglés en algunos modos (NEXUS)            | Bajo    | Baja      |
| Señales WC conf fluctúa: sin stats de club para selecciones | Medio | Media     |

---

## Próximos pasos (orden prioridad)

1. **Monitor señales WC (inmediato)** — verificar en logs del analyze 19:00 UTC:
   - `FORM_ELO_CONFLICT` activa para partidos Scotland/upset potencial
   - `RISING_ODDS_BLOCK` activa cuando cuota suba >20%
   - `elo_home_win_prob` calculado con ELO real (≠ DEFAULT 1500)

2. **Xtra markets WC** — confirmar BTTS/OU/AH en `all_markets` para partidos WC.
   Apparecen 48h antes del partido. Buscar `EXTRA_MARKETS_CHECK` en logs.

3. **Fase eliminatoria WC (desde 3 julio)** — KO rounds sin empate →
   probar si AWAY_GATE_CONF y HIGH_DRAW_PROB se comportan correctamente en KO.

4. **Euroleague 2026-27 (oct)** — PLAYOFF_SERIES_ADJUSTMENT ya listo.
   Verificar que ESPN API devuelve `competition.series` para Euroleague playoffs.

5. **NBA 2026-27 (oct)** — series_wins_home/away se persisten en Firestore automáticamente.
   Sin acción requerida.

6. **Ligas europeas (ago)** — PL, PD, BL1, SA, FL1 regresan. Sin cambios pendientes.

7. **Sofascore xG selecciones** — añadir EURO 2024/Copa América como fuente
   para selecciones que no tienen historial en football-data.org.

8. **NEXUS RECON** — añadir `YOUTUBE_API_KEY` en Railway Dashboard.

9. **NEXUS HELIOS v3** — añadir saldo en fal.ai → activar avatar IA.

10. **RAPID/TikTok** — subir cookies tiktok-uploader para publicación automática.
