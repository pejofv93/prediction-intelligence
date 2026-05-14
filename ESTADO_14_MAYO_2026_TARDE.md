# Estado del Sistema — 14 Mayo 2026 (tarde)

## Resumen ejecutivo

Sistema en producción en GCP Cloud Run. 4 servicios activos.
17 commits pendientes de push al inicio de la sesión.
Investigación y fix del bug que impedía señales en ACB y Euroleague.

---

## Arquitectura de servicios

| Servicio | Descripción | Timeout |
|---|---|---|
| `sports-agent` | Colecta, enriquecimiento y análisis multi-deporte | 1800s |
| `polymarket-agent` | Mercados de predicción Polymarket | 1200s |
| `telegram-bot` | Notificaciones y daily report | — |
| `dashboard` | Panel web (FastAPI + frontend) | — |

Comunicación inter-servicios via `CLOUD_RUN_TOKEN` (header `x-cloud-token`).
Todos los `/run-*` devuelven 202 Accepted + streaming de progreso por SSE.

---

## Cobertura deportiva

### Fútbol — modelo Poisson + ELO completo

| Liga | Código | Umbral edge | ROI backtest |
|---|---|---|---|
| Premier League | PL | 7.2% | +13.5% |
| La Liga | PD | 8.0% | +7.4% |
| Bundesliga | BL1 | 9.6% | -31.9% (umbral subido) |
| Serie A | SA | 9.6% | -18.9% (umbral subido) |
| Ligue 1 | FL1 | 9.6% | -24.6% (umbral subido) |
| Champions League | CL | 8.0% | sin backtest suficiente |
| Europa League | EL | 8.0% | — |
| Conference League | ECL | 8.0% | — |

Fuentes: football-data.org (Big 5 + Europa) · AllSportsApi (NL, WCQ, ARG, CSUD, CAM).

Mercados activos: `1X2`, `BTTS`, `O/U 2.5`, `corners O/U` (via odds-api.io), `bookings`.

### Baloncesto

| Liga | Fuente stats | Fuente odds | Estado |
|---|---|---|---|
| NBA | ESPN public API (sin key) | The Odds API `basketball_nba` | ⚠️ odds 401 plan free — excluida |
| Euroleague | incrowdsports.com oficial (sin key) | The Odds API `basketball_euroleague` | ✅ FIX HOY |
| ACB | TheSportsDB eventslast por equipo (sin key) | The Odds API `basketball_spain_acb` | ✅ FIX HOY |

Modelo: offensive/defensive ratings + ventaja local (NBA 3.2pts, Euroleague/ACB 2.8pts) + `scipy.stats.norm`.
Mercados: `h2h`, `spread`, `totals`.

### Tenis

Fuente primaria: odds-api.io.
Fallback: The Odds API (Roland Garros y torneos activos).
Ensemble: form (30%) + superficie (30%) + ranking (25%) + H2H (15%).

### Otros deportes (API-Sports, 100 req/día compartidos)

NFL, MLB, NHL, UFC. Presupuesto residual tras fútbol.

---

## Fuentes de odds y quotas

| Fuente | Quota | Uso | Estado |
|---|---|---|---|
| The Odds API | 500 req/mes | Secundaria | ⚠️ escasa, exclye NBA (401) |
| odds-api.io | — | Primaria corners + tenis | ✅ |
| OddsPapi | mensual | Corners + bookings | ✅ quota guard activo |
| Optic Odds | 1000 req/mes | Cuaternaria | ✅ |
| AllSportsApi | — | Fútbol selecciones + sudamérica | ✅ |

Quota manager: `shared/api_quota_manager.py` · guarda en Firestore · fail-closed si sin quota.

---

## Pipeline de señales — thresholds globales

```
SPORTS_MIN_EDGE        = 0.08   (8%)
SPORTS_MIN_CONFIDENCE  = 0.65   (65%)
SPORTS_ALERT_EDGE      = 0.08
POLY_MIN_EDGE          = 0.08
POLY_MIN_CONFIDENCE    = 0.65
```

Calibración confianza histórica: se muestra en alertas Telegram cuando hay ≥10 señales resueltas.

---

## Polymarket

40+ tickers activos: crypto, acciones US (Alpha Vantage como fuente primaria WTI/Gold/Silver),
ETF, elecciones. Precios reales desde Alpha Vantage + yfinance.

Anchor cap ±15% — evita predicciones extremas sin datos externos sólidos.
`data_quality` field en cada mercado para diagnóstico.

Sesgo YES corregido en SYSTEM_PROMPT (mercados eficientes por defecto).
`end_date=null` → smart handling (closing_soon 20/80 blend).
Smart money + volume extremo como señales pre-análisis.

Backtest poly: `services/polymarket-agent/backtester/backtest_poly.py`.

---

## Aprendizaje y calibración

- `learning_engine.py` — resuelve predicciones pendientes, actualiza pesos del ensemble
- `backtest.py` — backtest histórico 2 temporadas, calibra umbrales por liga
- `backtest_engine.py` — motor de evaluación de señales históricas
- `elo_rating.py` — ratings ELO actualizados en cada colecta (wired en collect pipeline)
- Pesos ensemble: `poisson 0.40 + ...` — actualizable via daily learning
- Backtest ahora evalúa mercados `totals` y `btts` además de `1X2`
- Backtest usa ELO + tasas históricas reales en lugar de siempre-home como baseline

---

## Fix de la sesión — ACB y Euroleague sin señales

### Causa raíz

`collect_basketball_team_stats()` tenía un `continue` explícito para
`source in ("euroleague_incrowd", "thesportsdb")`:

```python
# Línea 195 original — BLOQUEABA toda colección de stats
if source in ("euroleague_incrowd", "thesportsdb"):
    continue
```

Sin `team_stats` en Firestore → `generate_basketball_signals()` devolvía `[]` en línea 361:
```python
if not home_stats.get("raw_matches") and not away_stats.get("raw_matches"):
    return []  # silencioso, sin log visible
```

### Archivos modificados

**`services/sports-agent/collectors/basketball_collector.py`**
- `_fetch_acb_team_last_games(team_id)` — nueva función per-team via TheSportsDB `eventslast.php?id={team_id}`. El endpoint de liga (`eventspastleague.php`) devuelve solo 1 partido global (free tier), insuficiente.
- `_fetch_euroleague_history()` — nueva función: reutiliza `feeds.incrowdsports.com` (mismo endpoint que upcoming) incluyendo los 298 partidos FINISHED filtrados antes. Pre-fetch único.
- `collect_basketball_team_stats()` — el `continue` reemplazado por lógica correcta. ACB: per-team. Euroleague: filtrado del pre-fetch.

**`services/sports-agent/analyzers/basketball_analyzer.py`**
- `_SPORT_KEY_MAP` — añadido `"ACB": "basketball_spain_acb"` (faltaba completamente)
- `_HOME_ADV` — añadido `"ACB": BASKETBALL_HOME_ADV_EURO` (2.8pts)
- Guard `generate_basketball_signals()` — añadido `league` al mensaje de debug para diagnóstico

### Qué esperar tras deploy

- **Euroleague**: form score y ratings reales desde ~15 partidos/equipo (temporada 2025).
- **ACB**: form score desde el último partido por equipo (TheSportsDB free tier).
  Logs esperados: `basketball_collector: team_stats(XXXXX) Baskonia form=X.X src=thesportsdb partidos=1`
- NBA: odds siguen excluidas (The Odds API devuelve 401 en plan free para `basketball_nba`).

---

## Telegram bot

- Daily report con tiers de señales reales (sin listar señales individualmente).
- Calibración histórica visible en alertas cuando hay ≥10 señales resueltas.
- Fix `FieldFilter` en `send_weekly_report` (era HTTP 500).
- Dedup fail-open + timezone-aware comparisons.

---

## Estado git

- Branch: `main`
- 17 commits locales pendientes de push al inicio de la sesión
- 2 archivos modificados en sesión tarde (basketball fix):
  - `services/sports-agent/analyzers/basketball_analyzer.py` (+6/-2)
  - `services/sports-agent/collectors/basketball_collector.py` (+126/-8)

---

## Deuda técnica conocida

| Item | Severidad | Notas |
|---|---|---|
| The Odds API 401 para `basketball_nba` | Media | Plan free — excluida, no genera spam |
| ACB stats = 1 partido/equipo | Baja | TheSportsDB free tier. Señales con poca historia pero funcionales |
| `RECON` (sports-agent) | Baja | Placeholder, requiere YOUTUBE_API_KEY separada |
| `VECTOR` pytrends | Baja | En requirements.txt, integración parcial |
| Corners odds ACB/Euroleague | Baja | No configurado en OddsPapi |

---

## Próximas sesiones recomendadas

1. Verificar en logs Cloud Run que ACB y Euroleague generan señales tras deploy.
2. Verificar que el backtest calibrado (ELO + tasas históricas) mejora ROI en BL1/SA/FL1.
3. Evaluar subir quota The Odds API o migrar NBA odds a fuente alternativa.
4. Revisar si `basketball_spain_acb` está disponible en el plan The Odds API actual.
