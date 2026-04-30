# Estado NEXUS — 30 Abril 2026

## Sesión de hoy — Polymarket Production Fixes

### Contexto
Pipeline Polymarket en producción desde hace días. Diagnóstico del ciclo 06:23 UTC
reveló: 0 alertas, skip_vol=15, sent=False en bot, umbrales aprendidos bloqueando
señales prematuras (solo 11 outcomes resueltos).

---

## Fixes desplegados (en orden)

### 1. Umbrales adaptativos (poly_learning_engine / alert_engine)
- **Problema**: umbrales aprendidos BUY_YES=0.133/BUY_NO=0.125 con solo 11 outcomes
- **Fix**: si `sample_size < 20` → umbrales base BUY_YES=0.08 / BUY_NO=0.07
- **Log**: `umbral_mode=base (11 outcomes < 20)`
- **Commit**: d55bdaf

### 2. Mercado zombi 1918792
- **Problema**: mercado expirado el 21 abril seguía en Firestore y entraba al analyze loop
- **Fix**: `_parse_market()` retorna None silencioso si `end_date < now - 24h`
- **Borrado manual**: `prodpoly_markets/1918792` eliminado via Python Firestore client
- **Commit**: d55bdaf

### 3. Tracebacks CLOB 404
- **Fix**: `fetch_market_orderbook()` degradado de `logger.error(exc_info=True)` a `logger.warning(str(e))`
- `orderbook_analyzer.py`: wrap explícito del CLOB fetch con try/except → warning
- **Commit**: d55bdaf

### 4. Dedup doble (bug crítico — 0 alertas llegaban a Telegram)
- **Problema**: `alert_engine` escribía `status=pending` en `alerts_sent` antes de llamar al bot;
  el bot encontraba ese doc y lo trataba como duplicado → `sent=False`
- **Fix**: bot filtra `.where("status", "==", "sent")` — ignora pendientes
- **Commit**: 2232037

### 5. Filtro rec=WATCH/PASS en alert_engine
- **Problema**: mercado 1895140 con `rec=WATCH` generaba alerta (solo debería BUY_YES/BUY_NO)
- **Fix**: guard `if rec not in ("BUY_YES", "BUY_NO"): return False`
- **Commit**: c61ff5b

### 6. Pre-filtro balanceo (skip_vol 15 → 0)
- **Problema**: de 748 enriched_markets, el top-5/categoría elegía mercados que luego
  `analyze_market()` descartaba por <24h o precio extremo → skip_vol=15/19
- **Fix 1**: `market_enricher.py` propaga `end_date` y `price_yes` al doc enriched
- **Fix 2**: `main.py` carga `poly_markets` como `_poly_cache` una vez antes del loop;
  pre-filtra `end_date<now+24h` y `price_yes<0.05/>0.95` antes del balanceo
- **Resultado**: skip_vol=0, ciclo 54s vs 90s anterior
- **Commits**: 2a430b7, 0a773ae

### 7. Guard contradicción en alert_engine
- **Lógica**: si hay alerta enviada con dirección opuesta en las últimas 6h → skip
- **Implementación**: consulta `alerts_sent` por `market_id + direction=opuesta + status=sent`
- El doc dedup ahora incluye campo `direction` para habilitar la consulta
- **Commit**: c103238

### 8. Reducir varianza Groq (temperature 0.5 → 0.35)
- **Problema**: mismo mercado → prob 81% un run, 63% siguiente (18pp varianza)
- **Fix**: `temperature: 0.5 → 0.35`
- SYSTEM_PROMPT: instrucción de consistencia — variación >15pp debe justificarse
- user_prompt: ancla `last_prob` leída de `poly_predictions` (<24h) con rango ±15pp
- **Commit**: c103238

### 9. DDG retry con backoff
- **Problema**: timeouts en béisbol/tenis → data_quality=partial innecesario
- **Fix**: 3 intentos con esperas 1s, 2s antes de cada reintento
- **Commit**: fcb7dc3

### 10. Link al mercado en alertas Telegram
- `scanner._parse_market()`: captura campo `slug` de Gamma API
- `groq_analyzer`: propaga `slug` al prediction dict
- `alert_manager._format_poly_alert()`: `🔗 Ver mercado: polymarket.com/event/{slug}`
- **Commit**: f62bd37

---

## Métricas comparadas

| Métrica | 06:23 UTC | 12:17 UTC |
|---|---|---|
| skip_vol | 15 | **0** |
| analizados | 4 | 5 |
| alertas enviadas | 0 | **3** |
| hit rate (alertas/analizados) | 0% | **60%** |
| duración ciclo | 91s | 54s |
| sent=True en bot | ❌ | ✅ |

---

## Estado APIs Sports

- `the_odds_api`: **cuota mensual agotada** — reset 1 mayo
- `oddspapi`: **cuota mensual agotada** — reset 1 mayo
- Señales sports saliendo solo por Poisson sintético hasta mañana

---

## Pendientes

1. **1 mayo**: verificar reset APIs sports (`the_odds_api` + `oddspapi`)
2. **Fin de semana**: activar player props si hay partidos NBA/MLB
3. **Slug en prod**: efectivo tras próximo `/run-scan` (slugs aún no en poly_markets actuales)
4. **sample_size**: acumular outcomes resueltos hacia 20 para activar umbrales aprendidos
5. **ARGONAUT**: vacuum SQLite + auditoría huérfanos (pendiente desde sesión anterior)
