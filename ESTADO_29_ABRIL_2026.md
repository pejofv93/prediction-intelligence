# Estado del proyecto — 29 de abril de 2026

## Resumen de la sesión

Sesión de corrección de bugs y ampliación de cobertura. 8 commits sobre Polymarket y sports-agent.

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
- URL directa para crear en consola disponible en logs del analyze #59  
**Pendiente:** `firebase deploy --only firestore:indexes` para activarlo en Firestore

### 4. sports-collect.yml — retry si runner no disponible
**Síntoma:** workflow falla a las 4:14 AM con "runner not available" (15m35s = timeout de espera GitHub)  
**Fix:** Job `retry` que se lanza con `if: ${{ needs.trigger.result == 'failure' }}`. El job `trigger` pasa a `continue-on-error: true`

### 5. football-data.org — IDs con sufijo _totals
**Síntoma:** HTTP 400 masivos en `GET /matches/544528_totals`, `/matches/537154_totals`, etc.  
**Causa:** `learning_engine` pasaba match_ids con sufijo `_totals` a `get_match_result()` — la API solo acepta enteros  
**Fix:** Guard `if not str(match_id).isdigit(): return None` al inicio de `get_match_result()`

### 6. Polymarket — volume_spike nunca True (price_tracker.py)
**Síntoma:** `volume_spike=False` en todos los mercados desde el inicio del sistema  
**Causa:** ventana de 7 días → con sistema reciente nunca hay 7 días de histórico para la mayoría de mercados  
**Fix:** Reducir ventana a 3 días (`timedelta(days=3)`)

### 7. Basketball — key check + Euroleague
**Síntoma:** `collect.basketball_enhanced` fallaba silenciosamente si `FOOTBALL_RAPID_API_KEY` no estaba configurada  
**Fix:** Warning explícito al inicio de `collect_basketball_games()` si falta la key  
**Extra:** `_LEAGUE_TO_SPORT_TYPE["euroleague"] = "basketball"` en api_sports_client.py + `_SPORT_TO_LEAGUE["euroleague"] = "EUROLEAGUE"`

### 8. Basketball — ACB/Euroleague/discovery NCAA/EuroBasket
**Qué se hizo:**
- Añadida `get_games_by_league(league_id)` en api_sports_client.py: `GET /games?date=&league=ID`
- Añadida `discover_leagues(search)`: `GET /leagues?search=` que loguea todos los resultados
- `basketball_collector.py` reescrito con `_LEAGUES_BY_ID = {"ACB": 116, "EUROLEAGUE": 120}`
- `_LEAGUES_TO_DISCOVER = ["NCAA", "EuroBasket"]` → se ejecuta en cada collect, loguea los IDs
- Log por liga: N partidos obtenidos / offseason o liga inactiva

**Pendiente:** buscar en logs del próximo collect:
```
discover_leagues('NCAA'): id=XX name='...'
discover_leagues('EuroBasket'): id=YY name='...'
```
Luego descomentar en `_LEAGUES_BY_ID`.

---

## Estado de APIs (2026-04-29)

| API | Estado | Notas |
|---|---|---|
| **odds-api.io** | ✅ Activa | 100 req/hora. Bookmakers param correcto. Señales POISSON_SYNTHETIC mientras The Odds API y OddsPapi a 0. |
| **The Odds API** | ⏳ 0 créditos | Reset ~1 mayo 2026 |
| **OddsPapi** | ⏳ 0 créditos | Reset ~1 mayo. Añadir `ODDSPAPI_KEY` a GitHub Secrets al renovar. |
| **football-data.org** | ✅ Activa | IDs `_totals` filtrados. |
| **api-basketball** | ✅ Activo | NBA + ACB(116) + Euroleague(120). NCAA/EuroBasket en discovery. |
| **API-Football** | ✅ Activa | 100 req/día. Fallback BTTS/AH. |
| **Tennis API** | ✅ Activa | tennisapi1.p.rapidapi.com |
| **AllSports** | 🚫 Desactivado | 404 |
| **MMA** | 🚫 Desactivado | 404 |

---

## Métricas del día

- **Sports analyze:** 22 señales de 109 enriquecidos en 182s (+4 vs ayer por FL1/PPL)
- **Polymarket analyze:** analizados=6 / alertas=2 / skip_err=0 (era 19 antes del fix)
- **Deployments:** sports-agent-00195-vr9 / polymarket-agent-00172-z2q

---

## Pendientes para 1 mayo (reset cuotas)

1. Renovar The Odds API (500 req/mes) + OddsPapi (250 req/mes)
2. Añadir `ODDSPAPI_KEY` en GitHub Secrets
3. Lanzar analyze y buscar "con odds" > 0 en logs → confirmar edge real de bookmaker
4. `firebase deploy --only firestore:indexes` → activar índice `prodalerts_sent`
5. Confirmar IDs NCAA/EuroBasket desde logs del collect → descomentar en `_LEAGUES_BY_ID`
