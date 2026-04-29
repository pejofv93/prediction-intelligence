# Estado del proyecto — 29 de abril de 2026

## Resumen de la sesión

Sesión larga de corrección de bugs y ampliación de cobertura. Tres bloques principales:

1. **Sports Agent** — 10+ fixes: accuracy real medida por primera vez (16.7%), filtros de calidad (AWAY, underdog extremo, empate), pre-fetch global de odds, TTL dinámico en rate limit.
2. **Polymarket + Dashboard** — 4 fixes: timedelta, mercados expirados, volume_spike, índice Firestore. Dashboard con accuracy_by_league y reporte semanal completo.
3. **Polymarket — inteligencia completa** — segunda tanda (tarde): resolver two-pass, poly_learning_engine (72.7%), calibración Groq, race condition fix, dashboard poly weights, alertas Telegram enriquecidas.

---

## Commits del día

| Hash | Descripción |
|---|---|
| `a321cb8` | fix: añadir `timedelta` al import de datetime en groq_analyzer |
| `be9b1b7` | feat: versionar índice Firestore `prodalerts_sent` (market_id + sent_at) |
| `b4c321a` | fix: descartar mercados sin end_date en scanner (days_to_close bypass) |
| `bd90279` | fix: retry automático en sports-collect si runner no disponible |
| `71b1fbb` | fix: filtrar match_ids no enteros en get_match_result (football-data.org) |
| `61b30c6` | fix: reducir ventana volume_spike de 7d a 3d en price_tracker |
| `d61699e` | fix: baloncesto — key check explícito + activar Euroleague en collector |
| `ccca7e6` | feat: soporte ACB (116) y Euroleague (120) en basketball_collector |
| `135b807` | fix: guard _synthetic vs plain en check_result + backfill 30 docs |
| `19debda` | fix(odds-api): parse 429 TTL desde body + filtros AWAY anti-sesgo |
| `c17f8f2` | fix(resolver): lookup directo por ID para mercados fuera del top-100 |
| `fb8d61d` | feat(poly): poly_learning_engine + umbrales por dirección BUY_YES/BUY_NO |
| `961021c` | fix(poly): category propagation + calibración Groq (temp 0.5, anclas conf) |
| `8d6d1fc` | fix(poly): race condition alerts_sent → transacción Firestore atómica |
| `4154dff` | fix(accuracy_log): backfill category 18 shadow_trades + workflow W17/W18 |
| `d5e3531` | feat(dashboard): sección poly model weights + /api/poly-stats + category field |
| `0c4cc28` | feat(alerts): alertas Telegram enriquecidas (intensidad, categoría, cierre, volumen) |

---

## Fixes en detalle

### 1. Polymarket — timedelta NameError (groq_analyzer.py)
**Síntoma:** analyze #59: total=20 analizados=0 alertas=0 skip_err=19
**Causa:** `from datetime import datetime, timezone` omitía `timedelta`; línea 280 usaba `timedelta(hours=24)` → NameError en cada mercado
**Fix:** `from datetime import datetime, timedelta, timezone`
**Resultado:** analyze post-fix: analizados=6 alertas=2 skip_err=0

### 2. Polymarket — mercados expirados sin end_date (scanner.py)
**Síntoma:** "Will Bitcoin dip to $20,000 in April?" pasaba el filtro aunque había expirado
**Causa:** `if end_date and isinstance(end_date, datetime)` → si `end_date=None` el bloque se saltaba completo
**Fix:** `if not end_date or not isinstance(end_date, datetime): continue`
**Resultado:** mercados sin fecha parseada descartados en scan, no en analyze

### 3. Índice Firestore prodalerts_sent
**Síntoma:** `alert_engine.check_and_alert()`: "The query requires an index" — dedup fallaba silenciosamente
**Fix:**
- Creado `firestore.indexes.json` con índice compuesto `market_id ASC + sent_at ASC`
- Actualizado `firebase.json` para apuntar al archivo de índices
**Pendiente:** `firebase deploy --only firestore:indexes` para activarlo

### 4. sports-collect.yml — retry si runner no disponible
**Síntoma:** workflow falla a las 4:14 AM con "runner not available" (15m35s = timeout de espera GitHub)
**Fix:** Job `retry` con `if: ${{ needs.trigger.result == 'failure' }}`. El job `trigger` pasa a `continue-on-error: true`

### 5. football-data.org — IDs con sufijo _totals
**Síntoma:** HTTP 400 masivos en `GET /matches/544528_totals`, `/matches/537154_totals`, etc.
**Causa:** `learning_engine` pasaba match_ids con sufijo `_totals` a `get_match_result()`
**Fix:** Guard `if not str(match_id).isdigit(): return None` al inicio de `get_match_result()`

### 6. Polymarket — volume_spike nunca True (price_tracker.py)
**Síntoma:** `volume_spike=False` en todos los mercados
**Causa:** ventana 7 días → con sistema reciente nunca hay 7 días de histórico
**Fix:** Ventana reducida a 3 días (`timedelta(days=3)`) + fallback `>$50k` para mercados con solo 1 snapshot

### 7. Basketball — key check + Euroleague
**Síntoma:** `collect.basketball_enhanced` fallaba silenciosamente si `FOOTBALL_RAPID_API_KEY` no estaba
**Fix:** Warning explícito al inicio de `collect_basketball_games()` si falta la key
**Extra:** `_LEAGUE_TO_SPORT_TYPE["euroleague"] = "basketball"` + `_SPORT_TO_LEAGUE["euroleague"] = "EUROLEAGUE"`

### 8. Basketball — ACB/Euroleague/discovery NCAA/EuroBasket
- Añadida `get_games_by_league(league_id)` en api_sports_client.py: `GET /games?date=&league=ID`
- Añadida `discover_leagues(search)`: loguea todos los resultados de `GET /leagues?search=`
- `basketball_collector.py`: `_LEAGUES_BY_ID = {"ACB": 116, "EUROLEAGUE": 120}`
- `_LEAGUES_TO_DISCOVER = ["NCAA", "EuroBasket"]` → ejecuta en cada collect

**Pendiente:** buscar en logs del próximo collect:
```
discover_leagues('NCAA'): id=XX name='...'
discover_leagues('EuroBasket'): id=YY name='...'
```
Luego descomentar en `_LEAGUES_BY_ID`.

### 9. Sports Agent — check_result() accuracy bug (135b807)
**Síntoma:** accuracy global reportada como 0% en todos los análisis
**Causa:** `check_result()` en `learning_engine` evaluaba predicciones `_synthetic` contra resultados reales como si fueran picks normales; las `_synthetic` nunca matcheaban porque tienen formato distinto → siempre `correct=False`
**Fix:** Guard explícito: si `match_id` termina en `_synthetic` → skip en check_result; `learning_engine` actualiza el flag `_synthetic` al resolverse el partido real
**Backfill:** 30 docs de `prodpredictions` actualizados → correct=True: 5, correct=False: 25
**Resultado:** accuracy real medida: **16.7%** (5/30)

### 10. Sports Agent — filtros de calidad de señales (value_bet_engine.py)

**Filtro underdog extremo (5b — umbral dinámico):**
- PD/SA/PL/BL1 → umbral 4.5 (ligas top, underdogs extremos raros y suelen perder)
- Resto → umbral 5.0
- Antes: umbral fijo 5.5 → pasaban demasiados underdogs de 5.x

**Filtro empate (5c):**
- Si `p_draw > 0.30` → descartar señal
- Contexto: partido muy abierto, las cuotas de 1X2 pierden valor predictivo

**Filtros AWAY anti-sesgo (5d) — diagnóstico: 12.5% acc AWAY vs 21.4% HOME:**
- F1 — zona muerta 2.5–3.5: `odds` en ese rango → 0% accuracy histórico → descartar
- F2 — PD/DED AWAY con odds > 2.5: 0% accuracy en ambas ligas → descartar
- F3 — gate final: solo pasa AWAY favorito (odds < 2.5) o underdog extremo (odds > 3.5 + conf > 0.85)

**Resultado simulado:** elimina los 9 picks AWAY confirmados erróneos del histórico, sube accuracy AWAY simulada a 28.6%

### 11. Sports Agent — _TOP6_KEYWORDS extendido
Añadidas DED (Eredivisie) y BSA (Serie A Brasil) a `_TOP6_KEYWORDS` para que sus partidos reciban keyword bonus en el scoring de señales (antes solo PD/SA/PL/BL1/CL/FL1).

### 12. Sports Agent — FL1 + PPL activadas
Ligue 1 (`FL1`) y Primeira Liga (`PPL`) añadidas a las ligas activas en el collector.
**Resultado:** +4 señales vs sesiones anteriores (22 totales vs 18)

### 13. odds-api.io — pre-fetch global de odds
**Problema anterior:** N requests `/odds/multi` por liga × por analyze cycle → agotaba el límite de 100 req/h en minutos
**Nuevo diseño:**
- `_fetch_all_soccer_events()`: tras obtener los eventos, llama a `_prefetch_priority_odds()`
- `_prefetch_priority_odds()`: filtra eventos de ligas prioritarias, toma los 50 más próximos (`_MAX_ODDS_PREFETCH=50`), llama `_fetch_odds_batch()` — máximo 5 requests totales
- `_fetch_odds_map_for_events()`: si `_ODDS_MAP_PREFETCHED_AT is not None` y cache vigente → return sin HTTP
- Total por ciclo de analyze: 1 `/events` + ≤5 `/odds/multi` = **≤6 requests** (antes: potencialmente 50+)
- Fix bug: `_ODDS_MAP_CACHE` vacío era falsy → skip incorrecto; cambiado a `_ODDS_MAP_PREFETCHED_AT is not None`

### 14. odds-api.io — TTL dinámico desde body del 429
**Problema:** `_TTL_RATE_LIMIT = 3600s` hardcodeado → si el API reseteaba a los 29 min, el sistema esperaba 60 min
**Fix:**
- En 429 handler de `_fetch_all_soccer_events()`: parsea `"resets in X minutes and Y seconds"` con regex
- Calcula `ttl = X*60 + Y + 30` (30s buffer); fallback 3600s si no parsea
- Guarda `ttl_override` en la entrada de caché
- `_cache_ttl()` lee `ttl_override` cuando `rate_limited=True`
- Log: `odds-api.io: 429 rate limit — TTL=XXXs — body=...`

### 15. Dashboard — fixes UI
- `/api/predictions`: parámetro `limit` ignorado → fix; ahora filtra correctamente
- `accuracy_by_league` renderizada en la vista de predicciones (antes siempre vacía)

### 16. Telegram/Weekly report — mejoras
- Ventana temporal corregida: ahora usa la semana anterior (lunes–domingo) en vez de últimos 7 días desde `now`
- `accuracy_by_league` incluida en el reporte semanal
- Bankroll virtual incluido en el reporte
- `accuracy_log` también usa ventana de semana anterior

---

## Sesión tarde — Polymarket intelligence completa

### 17. Resolver two-pass — mercados fuera del top-100 (c17f8f2)
**Síntoma:** `resolved=0` durante semanas. El resolver solo consultaba los top-100 mercados por volumen de la Gamma API, pero los mercados alertados con poco volumen nunca aparecían ahí.
**Fix:** Dos pasadas en `polymarket_resolver.py`:
- Paso 1: top-100 cerrados por volumen (comportamiento anterior)
- Paso 2: para cada predicción alertada que no se resolvió en el paso 1, hace `GET /markets/{market_id}` directamente (máximo 20 llamadas)
- Guard: `if not market_id: return False` en `alert_engine` para evitar `market_id=None` en `alerts_sent`
**Resultado:** primera ejecución post-fix → `resolved=11`, `skipped_unresolved=5` (mercados aún abiertos)

### 18. poly_learning_engine — umbrales adaptativos por dirección (fb8d61d)
**Qué hace:** analiza los 11 mercados resueltos, calcula accuracy por dirección y ajusta los umbrales de alerta individualmente para BUY_YES y BUY_NO.
**Resultados primera ejecución:**
- Accuracy global: **72.7%** (8/11 resueltos correctamente)
- BUY_YES: `min_edge=0.133`, `min_confidence=0.70`
- BUY_NO: `min_edge=0.125`, `min_confidence=0.63`
- Guardado en `prodpoly_model_weights/current` (version=1)
**Lógica:** si accuracy por dirección > 70% → el modelo es conservador y puede permitir edge ligeramente menor; si < 60% → sube umbral. Ajuste suavizado ±0.01 por ciclo.

### 19. Calibración Groq + category propagation (961021c)
**Problema 1 — temperatura:** `temperature=0.7` generaba distribuciones de confianza planas (todo 0.5–0.7). Cambiado a `temperature=0.5` → distribución más discriminada.
**Problema 2 — anclas de confianza:** Groq devolvía `confidence=0.9` para mercados con edge mínimo. Añadidas anclas explícitas en el prompt: si `edge < 0.05` → max conf 0.55; si `edge < 0.10` → max conf 0.70.
**Problema 3 — category no llegaba al Firestore:** `category` se calculaba en `scanner.py` pero no se pasaba al `enriched_market` que recibía `groq_analyzer`. Fix: `category` ahora se incluye en el dict que fluye por todo el pipeline.

### 20. Race condition en alerts_sent (8d6d1fc)
**Síntoma:** alertas duplicadas ocasionales cuando dos requests concurrentes del scheduler pasaban el check `edge >= threshold` simultáneamente.
**Causa:** flujo read-then-write no atómico — dos coroutines podían leer `alerts_sent` vacío al mismo tiempo, las dos decidir "no existe" y las dos enviar.
**Fix:** `check_and_alert` usa ahora una transacción Firestore `@transactional`:
- Dentro de la transacción: lee el doc `dedup_ref`, comprueba si existe y si `sent_at > cutoff_24h`
- Si no existe o ha pasado >24h con cambio de precio >5%: `transaction.set(dedup_ref, {status: "pending"})`
- Solo después del `transaction.set` exitoso se hace el POST a Telegram
- El perdedor de la transacción (contention) recibe `Aborted` y no envía
- Post-envío: `dedup_ref.update({status: "sent"})`; si falla el POST: `dedup_ref.delete()` para no bloquear reintentos

### 21. Backfill category en shadow_trades (4154dff)
**Problema:** 18 documentos en `prodshadow_trades` tenían `category=None` porque fueron creados antes de que `category` fluyera por el pipeline. Afectaba métricas de accuracy por categoría en el reporte semanal.
**Fix:** Script de backfill + workflow `backfill-category.yml` que corre una vez y se auto-desactiva. Todos los shadow_trades sin categoría se actualizan a la categoría del mercado correspondiente en `poly_predictions`.

### 22. Dashboard — sección Polymarket Model Weights (d5e3531)
**Qué se añadió:**
- `GET /api/poly-stats`: nuevo endpoint que lee `prodpoly_model_weights/current` → devuelve umbrales por dirección, accuracy global/BUY_YES/BUY_NO, sample_size y `updated_at`
- `ModelStats.tsx`: nueva sección `PolyModelSection` al fondo de la vista Stats con 3 tarjetas (accuracy global, BUY YES con edge/conf/acc, BUY NO con edge/conf/acc) y fecha de última actualización del modelo
- `api/polymarket.py`: `category` añadido al whitelist de `/api/poly` (se guardaba en Firestore pero se descartaba al serializar)
**Datos son dinámicos:** cuando `poly_learning_engine` ajuste los umbrales, el dashboard lo refleja en el siguiente refresh.

### 23. Alertas Telegram Polymarket — formato enriquecido (0c4cc28)
**Problema:** la alerta llegaba sin contexto suficiente para evaluar la señal rápidamente.
**Antes:**
```
🔮 OPORTUNIDAD POLYMARKET
❓ NBA Playoffs: Timberwolves vs Nuggets
📈 Precio YES: 57% | Prob: 65% | Edge: +8% | Conf: 80%
Recomendación: BUY_YES
```
**Después:**
```
🔮 OPORTUNIDAD POLYMARKET — 🟡 SEÑAL DETECTADA
🏀 sports
❓ NBA Playoffs: Timberwolves vs Nuggets
💰 Precio YES: 57% → IA: 65% (+8% edge)
🎯 Confianza: 80% | Recomendación: BUY_YES
⏰ Cierra en 3d (02/05) | 💧 Vol 24h: $42,500
💭 [reasoning]
⚠️ Apuesta responsablemente.
```
**Cambios en los archivos:**
- `groq_analyzer.py`: `end_date_iso` y `volume_24h` añadidos al prediction dict y a Firestore
- `alert_manager.py`: badge de intensidad (🔴 >15% / 🟡 >10% / 🟢 resto), emoji de categoría, días hasta cierre calculados en runtime, volumen formateado ($K/$M), smart money rebautizado 🧠
- `dashboard/api/polymarket.py`: `end_date_iso` y `volume_24h` añadidos al whitelist para que el frontend los reciba también

---

## Estado de APIs (2026-04-29 14:00)

| API | Estado | Notas |
|---|---|---|
| **odds-api.io** | ⏳ Rate limit agotado | Reset ~14:30. Pre-fetch activo: ≤6 req/ciclo. TTL dinámico desde body 429. |
| **The Odds API** | ⏳ 0 créditos | Reset ~1 mayo 2026 |
| **OddsPapi** | ⏳ 0 créditos | Reset ~1 mayo. Añadir `ODDSPAPI_KEY` a GitHub Secrets al renovar. |
| **football-data.org** | ✅ Activa | IDs `_totals` filtrados. |
| **api-basketball** | ✅ Activo | NBA + ACB(116) + Euroleague(120). NCAA/EuroBasket en discovery. |
| **API-Football** | ✅ Activa | 100 req/día. Fallback BTTS/AH. |
| **Tennis API** | ✅ Activa | tennisapi1.p.rapidapi.com |
| **AllSports** | 🚫 Desactivado | 404 |
| **MMA** | 🚫 Desactivado | 404 |

**Nota selecciones:** NL (Nations League) y WCQ (World Cup Qualifiers) no disponibles en free tier de ninguna API activa.

---

## Métricas del día

- **Sports analyze:** 22 señales de 109 enriquecidos en 182s (+4 vs ayer por FL1/PPL)
- **Sports accuracy (backfill 30 docs):** 16.7% global (5/30) — primera medición real
- **Polymarket analyze:** analizados=6 / alertas=2 / skip_err=0 (era 19 antes del fix)
- **Polymarket resolver:** resolved=11, skipped_unresolved=5 (era 0 durante semanas)
- **Polymarket accuracy:** 72.7% (8/11) — primera medición real desde que arrancó el sistema
- **Poly model v1:** BUY_YES edge≥13.3% conf≥70% | BUY_NO edge≥12.5% conf≥63%
- **Commits del día:** 15 total (7 mañana / 8 tarde)
- **Deployments:** todos los servicios desplegados (polymarket-agent, telegram-bot, dashboard)

---

## Pendientes

### Inmediatos
1. Verificar en logs que `poly_learning_engine` corre correctamente en el próximo ciclo del scheduler
2. Confirmar que las próximas alertas incluyen los nuevos campos (categoria, días cierre, volumen)
3. Verificar pre-fetch global en `/odds/multi` tras reset de rate limit de odds-api.io

### 1 mayo (reset cuotas)
4. Renovar The Odds API (500 req/mes) + OddsPapi (250 req/mes)
5. Añadir `ODDSPAPI_KEY` en GitHub Secrets
6. Lanzar analyze y buscar "con odds" > 0 en logs → confirmar edge real de bookmaker
7. Verificar corners, BTTS y fútbol femenino disponibles con nuevos créditos

### Pendientes estructurales
8. `firebase deploy --only firestore:indexes` → activar índice `prodalerts_sent`
9. Confirmar IDs NCAA/EuroBasket desde logs del collect → descomentar en `_LEAGUES_BY_ID`
10. Acumular más resultados resueltos → accuracy estadísticamente significativa (objetivo: 50+ picks)
11. Link directo al mercado en alertas Telegram — requiere capturar `slug` del Gamma API en `scanner.py`
