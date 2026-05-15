# Estado del Sistema — 15 Mayo 2026

## Resumen ejecutivo

Sistema en producción en GCP Cloud Run. 4 servicios activos.
Sesión dedicada a tres análisis: preparación Roland Garros, diagnóstico WR señales
EV>20% y verificación de sequía de alertas. Se resolvieron 2 bugs críticos y se
implementó la primera calibración real del modelo ensemble.

Último deploy: `sports-agent-00380-59f` (16:10 UTC). Servicio sano.

---

## Arquitectura de servicios

| Servicio | Descripción | Timeout | Última revisión |
|---|---|---|---|
| `sports-agent` | Colecta, enriquecimiento y análisis multi-deporte | 1800s | `b48aca3` |
| `polymarket-agent` | Mercados de predicción Polymarket | 1200s | `70fc03a` |
| `telegram-bot` | Notificaciones, daily report y odds-check | — | `a9e48a2` |
| `dashboard` | Panel web (FastAPI + frontend) | — | `8c5d700` |

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

Para fútbol con Poisson real (`_is_real_poisson=True`), el pipeline usa la probabilidad
Poisson directamente, bypaseando el ensemble — el ensemble se usa solo para confidence.

### Baloncesto

| Liga | Fuente stats | Fuente odds | Estado |
|---|---|---|---|
| NBA | ESPN public API (sin key) | The Odds API `basketball_nba` | ⚠️ odds 401 plan free — excluida |
| Euroleague | incrowdsports.com oficial (sin key) | The Odds API `basketball_euroleague` | ✅ operativa |
| ACB | TheSportsDB eventslast por equipo (sin key) | The Odds API `basketball_spain_acb` | ✅ operativa |

Modelo: offensive/defensive ratings + ventaja local (NBA 3.2pts, Euroleague/ACB 2.8pts)
+ `scipy.stats.norm`. Mercados: `h2h`, `spread`, `totals`.

ACB/Euroleague se activaron en sesión 2026-05-14 (bug `continue` en `collect_basketball_team_stats`).
Fix: `_fetch_acb_team_last_games()` + `_fetch_euroleague_history()`.

### Tenis

Fuente primaria: odds-api.io.
Fallback: The Odds API (Roland Garros y torneos activos).
Ensemble propio en `tennis_analyzer.py`: form (30%) + superficie (30%) + ranking (25%) + H2H (15%).

**Bug crítico resuelto hoy** (`5818d35`): odds-api.io retornaba `status="pending"` para
eventos upcoming de tenis. Este status no estaba mapeado en `_STATUS_MAP` de
`firestore_writer.py`, por lo que se almacenaba en Firestore como string raw `"pending"`.
El analyze filtra por `status IN ["SCHEDULED","TIMED"]`, lo que excluía el 100% de los
596 partidos de tenis → 0 señales de tenis durante 7+ días.

Fix: añadido `"pending": "SCHEDULED"` (y `"pre"`, `"inplay"` preventivos) a `_STATUS_MAP`.

### Roland Garros — preparación para 19 mayo

- Mapeado en `tennis_collector.py`: `"roland" → ("ATP_FRENCH_OPEN", "clay", "Roland Garros")`
- Fallback key en `_FALLBACK_TENNIS_KEYS`: `("tennis_atp_french_open", "ATP_FRENCH_OPEN", ...)`
- `collect_tennis_matches(days=7)`: recoge eventos con `commence <= now + 7d`
- Partidos esperados en odds-api.io: 17-18 mayo (torneos aparecen 1-3 días antes)
- Con el fix de `status="pending"` en producción, la próxima colecta los grabará como
  `SCHEDULED` y el siguiente analyze los procesará correctamente
- **No requiere acción manual** — el sistema lo gestionará automáticamente

### Otros deportes (API-Sports, 100 req/día compartidos)

NFL, MLB, NHL, UFC. Presupuesto residual tras fútbol.

---

## Fuentes de odds y quotas

| Fuente | Quota | Uso | Estado |
|---|---|---|---|
| The Odds API | 500 req/mes | Secundaria (baloncesto, tenis fallback) | ⚠️ escasa, NBA excluida (401) |
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

Thresholds por liga calibrados por backtest (BL1/SA/FL1 subidos a 9.6% por ROI negativo).
Confianza histórica visible en alertas Telegram cuando hay ≥10 señales resueltas.

---

## Análisis de rendimiento — WR por bucket EV

| Bucket EV | Win Rate | Señales | Fuente predominante |
|---|---|---|---|
| EV < 0% | ~20% | mayoría descartadas | — |
| EV 0-8% | ~28% | — | ensemble + poisson |
| EV 8-12% | **33%** | base | ensemble + poisson_totals |
| EV 12-20% | ~30% | moderadas | ensemble |
| **EV >20%** | **17%** | pocas pero recurrentes | ensemble (form inflado) |

### Causa raíz del 17% WR en EV >20%

El modelo ensemble usaba `form_score / 100` como señal de probabilidad directa.
Ejemplo: equipo con 80% de victorias recientes → signal_form = 0.80.

Problema: la forma reciente refleja victorias contra rivales de distinto nivel. El
bookmaker ajusta por calidad del oponente; el modelo no. Resultado: EV inflado contra
rivales fuertes donde el bookie cotiza correctamente y el modelo sobreestima.

Con ELO excluido (ambos equipos a DEFAULT_ELO=1500 por falta de historial suficiente)
y renormalización del ensemble, form ocupaba ~27% del peso efectivo — el doble de su
peso nominal (0.20), suficiente para generar señales EV>20% artificiales.

---

## Modelo ensemble — calibración (fix de esta sesión)

**Commit `b48aca3` — desplegado en `sports-agent-00380-59f`**

### Cambio 1: Form shrinkage factor 0.6

```python
# Antes:
signals = {"poisson": poisson_home_s, "form": float(home_form) / 100.0}

# Después:
_FORM_SHRINK = 0.6
form_home_cal = 0.5 + _FORM_SHRINK * (float(home_form) / 100.0 - 0.5)
signals = {"poisson": poisson_home_s, "form": form_home_cal}
```

Tabla de conversión:

| form_score raw | Señal antigua | Señal nueva |
|---|---|---|
| 100% | 1.00 | 0.80 |
| 80% | 0.80 | **0.68** |
| 65% | 0.65 | 0.59 |
| 50% | 0.50 | 0.50 (neutro) |
| 35% | 0.35 | 0.41 |
| 20% | 0.20 | **0.32** |
| 0% | 0.00 | 0.20 |

### Cambio 2: DEFAULT_WEIGHTS rebalanceados

```python
# Antes:
DEFAULT_WEIGHTS = {"poisson": 0.40, "elo": 0.25, "form": 0.20, "h2h": 0.15}

# Después:
DEFAULT_WEIGHTS = {"poisson": 0.50, "elo": 0.25, "form": 0.10, "h2h": 0.15}
```

### Impacto combinado

| Escenario | Contribución form antes | Contribución form después | Reducción |
|---|---|---|---|
| form=80%, ELO excluido | 0.216 | 0.088 | **-59%** |
| form=80%, ELO incluido | 0.139 | 0.052 | -63% |

El modelo ahora depende principalmente de Poisson (señal más robusta estadísticamente)
y usa la forma solo como ajuste leve, no como determinante. El efecto esperado en producción
es una reducción del volumen de señales EV>20% y un aumento del WR en ese bucket.

**Nota**: Para fútbol con datos Poisson reales (`_is_real_poisson=True`), la probabilidad
ya bypaseaba el ensemble. Los cambios afectan principalmente a: baloncesto, tenis,
y la confidence de todas las señales.

---

## Sequía de alertas — diagnóstico (15 mayo 22:38)

### Sports-agent

Última alerta Telegram: 14 mayo 11:38.
Causa: ventana de dedup de 48h en `telegram-bot/alert_manager.py`.

```python
cutoff_48h = datetime.now(timezone.utc) - timedelta(hours=48)
# Señales del mismo match_id + market + selection bloqueadas si sent_at >= cutoff
```

Cada run de analyze generaba 10-12 señales, de las cuales ~9 eran `POISSON_SYNTHETIC`
(sin odds externas) — estas nunca alertan por diseño. Las 1-2 con odds reales estaban
en dedup desde el 14 mayo. Ventana liberada: **16 mayo ~11:38**.

Workflows en schedule (verificado 15 mayo): 09:48, 15:17, 20:17 — todos completaron.

### Polymarket-agent

Última alerta analyze: 15 mayo 13:54.
Analyze de 17:35: `total=35, analyzed=27, alerts=0, skip_dedup=6` (SKIP_DEDUP_7D/24H).
Price tracker: 1 alerta enviada a las 18:04 (HTTP 200 confirmado).
Odds-check (sistema nuevo `a9e48a2`): 11 alertas de cambio de cuota a las 20:22.

Ambos servicios: `/health` → `{"status":"ok"}`. Sin errores de deploy.

---

## Polymarket

40+ tickers activos: crypto, acciones US (Alpha Vantage primaria para WTI/Gold/Silver),
ETF, elecciones. Precios reales desde Alpha Vantage + yfinance.

Anchor cap ±15% — evita predicciones extremas sin datos externos sólidos.
`data_quality` field en cada mercado para diagnóstico.
Sesgo YES corregido en SYSTEM_PROMPT (mercados eficientes por defecto).
`end_date=null` → smart handling (closing_soon 20/80 blend).
Smart money + volume extremo como señales pre-análisis.
Backtest poly: `services/polymarket-agent/backtester/backtest_poly.py`.

NBA playoffs `X vs Y` (e.g., "Celtics vs Knicks") bloqueado incorrectamente como
equipo no encontrado — fix `70fc03a` en producción.

---

## Odds snapshot y odds-check

**Commits `7d932ee` y `a9e48a2`:**

- `save_odds_snapshot()` conectado en `run-analyze` de sports-agent: guarda cuotas
  en Firestore `odds_snapshots` tras cada analyze.
- Endpoint `/run-odds-check` en telegram-bot: llama `check_pending_odds_changes()`
  y detecta movimientos de cuota significativos en señales pendientes de resolución.
- Se dispara automáticamente después de cada analyze (POST desde sports-agent).

---

## Estado git

- Branch: `main`
- Commits de esta sesión:
  - `b48aca3` — fix(model): calibrar form signal para reducir EV inflado en señales >20%
  - `5818d35` — fix(tennis): mapear status 'pending' de odds-api.io a SCHEDULED
- Deploy activo: `sports-agent-00380-59f`
- Repositorio: limpio, sin cambios pendientes

### Commits recientes (resumen últimas 2 sesiones)

| Hash | Descripción |
|---|---|
| `b48aca3` | fix(model): calibrar form signal — WR EV>20% |
| `5818d35` | fix(tennis): status pending → SCHEDULED |
| `a9e48a2` | fix(telegram-bot): /run-odds-check wired |
| `7d932ee` | fix(sports-agent): odds snapshot + odds-check |
| `70fc03a` | fix(polymarket): UTF-8 + NBA 'X vs Y' desbloqueado |
| `e203ff9` | fix(sports-agent): UTF-8 nombres equipos Firestore |
| `8c5d700` | fix(dashboard): 5 bugs visuales y de datos |
| `38c88b0` | feat(telegram): advertencia edge alto >20% |
| `ef8cc68` | fix(polymarket): 10 bugs auditados + test suite 31 tests |
| `0358d18` | fix(basketball): ACB y Euroleague primera señal |

---

## Deuda técnica conocida

| Item | Severidad | Notas |
|---|---|---|
| ELO en default para mayoría de equipos | Media | Solo 30 días historial. Form shrinkage mitiga el problema mientras ELO madura. |
| The Odds API 401 para `basketball_nba` | Media | Plan free — excluida, no genera spam |
| ACB stats = 1 partido/equipo | Baja | TheSportsDB free tier. Funcional pero baja confianza. |
| Poisson sin ajuste por rival específico | Media | Usa promedios históricos. Solo afecta partidos sin `_is_real_poisson`. |
| Tennis TENNIS_WEIGHTS: form=0.30 sin calibrar | Baja | Pendiente aplicar shrinkage también al tennis ensemble |
| `RECON` placeholder | Baja | Requiere YOUTUBE_API_KEY separada |
| `VECTOR` pytrends | Baja | En requirements.txt, integración parcial |

---

## Próximas sesiones recomendadas

1. **Verificar WR post-calibración**: tras ~2 semanas de señales con el modelo nuevo,
   comparar WR por bucket EV. Esperado: WR EV>20% sube de 17% a >28%.

2. **Roland Garros**: verificar el 17-18 mayo que aparecen partidos en upcoming_matches
   con status SCHEDULED. Logs esperados: `tennis_collector: guardados N partidos ATP_FRENCH_OPEN`.

3. **Tennis TENNIS_WEIGHTS form shrinkage**: aplicar el mismo factor 0.6 al ensemble
   en `tennis_analyzer.py` para consistencia. Form en tenis es más informativo que en
   fútbol (misma superficie), pero aún puede inflarse sin ajuste por ranking del rival.

4. **ELO seeding**: cuando haya >60 días de historial en Firestore `team_elo`, revisar
   si los ELOs se han diferenciado o si hace falta seed externo (posiciones de liga).

5. **NBA odds**: evaluar migrar a ActionNetwork, Pinnacle scraping o esperar upgrade
   del plan The Odds API para recuperar `basketball_nba`.

6. **Backtest con modelo calibrado**: re-ejecutar backtest histórico con los nuevos
   pesos y shrinkage para proyectar mejora de ROI antes de que haya señales reales.
