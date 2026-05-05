# Estado del Sistema — 5 de mayo de 2026

## Servicios activos (Google Cloud Run, europe-west1)

| Servicio | Revisión actual | URL |
|---|---|---|
| polymarket-agent | 00229-fjh | https://polymarket-agent-327240737877.europe-west1.run.app |
| sports-agent | (ver gcloud) | https://sports-agent-327240737877.europe-west1.run.app |
| telegram-bot | (ver gcloud) | https://telegram-bot-327240737877.europe-west1.run.app |

```
gcloud run revisions list --region europe-west1 --project prediction-intelligence
```

## Commits recientes (últimas 2 sesiones)

```
752980e fix(groq): cap low-price market signals (<15% → real_prob≤2.5x, geo/politics edge≥0.20)
25a597e fix(wallet-tracker): switch CLOB→data-api.polymarket.com (sin auth)
c135112 feat(analyze): arbitrage detector integrado al final de _bg_analyze
2279842 fix(whale-tracker): activar sin POLYMARKET_CLOB_KEY
f4f3670 feat(weekly-report): formato enriquecido BUY_YES/NO accuracy + sección MODELO
5545cc2 feat(arbitrage): detector en pipeline → predictions + alerta 💎 Telegram
a5c3265 feat(backtest): learner/backtest_engine.py — accuracy real por liga/mercado/edge/conf
a40e76d feat(alerts): check_pending_odds_changes — alerta si cuota cambia >10%
12e1935 fix(value-bet): filtro standings — sin relevancia en liga reduce confidence 20%
c068c83 fix(basketball): PLAYOFF_DISCOUNT 8% en totales NBA playoffs
b07881a fix(groq): reasoning consistente con recommendation + limpieza frases contradictorias
e9b32a4 docs: estado completo del sistema 4 mayo 2026
```

## Mejoras implementadas esta sesión (5 mayo 2026)

### BLOQUE A — Mercados resolución próxima
- `groq_analyzer.py`: CLOSING_SOON_SKIP log para <24h (log unificado)
- `groq_analyzer.py`: `_closing_soon` flag cuando `days_to_close < 2`
- `groq_analyzer.py`: blend `real_prob = LLM×0.5 + market_price×0.5` para mercados 24-48h
- `groq_analyzer.py`: `days_to_close` añadido al dict `prediction`
- `alert_manager.py`: badge "⚡ *CIERRA PRONTO* — precio actual muy relevante" si `days_left < 3`

### BLOQUE B — Clasificación volume spike
- `price_tracker.py`: nueva función `classify_volume_spike(market_id) → str`
- Clasifica en: SMART_MONEY | MANIPULATION | WASH_TRADING | ORGANIC
- SMART_MONEY: spike >2h + precio consistente en una dirección ≥70%
- MANIPULATION: spike <30min + precio regresa al nivel inicial
- WASH_TRADING: volumen alto + precio sin moverse (<1% variación)
- ORGANIC: resto de casos

### BLOQUE C — groq_ai en learning engine
- `learning_engine.py`: eliminado filtro `data_source == "statistical_model"` para `update_weights()`
- Todas las predicciones resueltas (incluidas groq_ai) ajustan ahora los pesos del ensemble
- Añadido `groq_predictions_count` al doc `model_weights/current`

### Fixes sesión 4-5 mayo
- `wallet_tracker.py`: migrado de CLOB (401) a `data-api.polymarket.com` — endpoint público
- `groq_analyzer.py`: cap mercados baja probabilidad (<15%) → real_prob ≤ price×2.5
- `groq_analyzer.py`: geopolítica/política con precio <15% requiere edge ≥ 0.20 para BUY

## Arquitectura de señales

```
GitHub Actions (cron)
  │
  ├── polymarket-analyze.yml → POST /run-analyze → polymarket-agent
  │     └── scan → enrich → groq_analyze → alert_engine → telegram-bot
  │
  ├── sports-analyze.yml → POST /run-collect + /run-analyze → sports-agent
  │     └── collect → enrich → value_bet → signal → arb_detector → telegram-bot
  │
  └── learning-engine.yml → POST /run-learning → sports-agent
        └── fetch_pending_results → check_result → evaluate → update_weights
```

## Colecciones Firestore en uso

| Colección | Propósito |
|---|---|
| `predictions` | Señales sports (result=None si pendiente) |
| `poly_predictions` | Señales Polymarket (alerted=False si no enviada) |
| `poly_markets` | Mercados Polymarket escaneados |
| `poly_price_history` | Snapshots precio/volumen por mercado |
| `poly_smart_wallets` | Wallets con win_rate > 65% |
| `enriched_markets` | Mercados enriched (TTL 90min) |
| `model_weights` | Pesos ensemble sports |
| `poly_model_weights` | Umbrales y calibración Polymarket |
| `accuracy_log` | Precisión por semana ISO |
| `alerts_sent` | Dedup de alertas (24h TTL lógico) |
| `arb_opportunities` | Arbitrajes detectados (TTL 2h) |
| `shadow_trades` | Bankroll virtual ($50 inicial) |
| `agent_state` | Estado cuota Groq, etc. |

## APIs y claves

| API | Estado | Notas |
|---|---|---|
| Groq (llama-3.3-70b) | ✅ Activa | Rotación de modelos si TPD agotado |
| The Odds API | ✅ Activa | Cache 1h, guard quota exhausted |
| football-data.org | ✅ Activa | Standings + partidos |
| data-api.polymarket.com | ✅ Público | Sin auth — /trades?market={conditionId} |
| Telegram Bot API | ✅ Activa | TELEGRAM_BOT_TOKEN en Cloud Run |
| Firestore | ✅ Activa | GCP service account |

## Variables de entorno (Cloud Run secrets)

```
GROQ_API_KEY, THE_ODDS_API_KEY, FOOTBALL_DATA_API_KEY,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
CLOUD_RUN_TOKEN (auth interna entre servicios),
SPORTS_AGENT_URL, POLYMARKET_AGENT_URL, TELEGRAM_BOT_URL
```

## Comandos útiles

```bash
# Ver logs de un servicio
gcloud logging read 'resource.labels.service_name="polymarket-agent"' \
  --project=prediction-intelligence --limit=50 --format="value(textPayload)"

# Disparar workflows manualmente
gh workflow run polymarket-enrich.yml --ref main
gh workflow run polymarket-analyze.yml --ref main
gh workflow run sports-analyze.yml --ref main

# Deploy manual de un servicio
xcopy /E /I /Y shared services\polymarket-agent\shared
cd services\polymarket-agent
gcloud run deploy polymarket-agent --source . --region europe-west1 --project prediction-intelligence
# Limpiar después:
Remove-Item -Recurse -Force services\polymarket-agent\shared

# Ver revisiones activas
gcloud run revisions list --region europe-west1 --project prediction-intelligence --limit 5
```

## Pendientes priorizados

### Alta prioridad
- [ ] **Exponer `classify_volume_spike`** en el enricher para que el campo `volume_spike_type` llegue a la señal y a Telegram
- [ ] **Conectar `check_pending_odds_changes`** — actualmente implementado pero no hay scheduler que lo llame con las cuotas actuales
- [ ] **`SPORTS_AGENT_URL` como GitHub secret** — necesario para que `weekly-report.yml` llame al backtest

### Media prioridad
- [ ] **`run-production-backtest` endpoint** en sports-agent — requiere deploy sports-agent con el nuevo código del backtest engine
- [ ] **Dedup multi-día en alerts_sent** — actualmente solo filtra 24h; mercados de semanas pueden re-alertar con edge diferente
- [ ] **`POLYMARKET_CLOB_KEY`** — no se usa actualmente (data-api no requiere auth), pero útil para futuros endpoints

### Baja prioridad
- [ ] Dashboard: mostrar `volume_spike_type` en tarjetas de mercado
- [ ] Retroalimentación poly: `poly_learning_engine` usa outcomes resueltos para ajustar umbrales

## Workflow de deploy completo (3 servicios)

```powershell
# sports-agent
xcopy /E /I /Y shared services\sports-agent\shared
cd services\sports-agent
gcloud run deploy sports-agent --source . --region europe-west1 --project prediction-intelligence
cd ..\..
Remove-Item -Recurse -Force services\sports-agent\shared

# polymarket-agent
xcopy /E /I /Y shared services\polymarket-agent\shared
cd services\polymarket-agent
gcloud run deploy polymarket-agent --source . --region europe-west1 --project prediction-intelligence
cd ..\..
Remove-Item -Recurse -Force services\polymarket-agent\shared

# telegram-bot
xcopy /E /I /Y shared services\telegram-bot\shared
cd services\telegram-bot
gcloud run deploy telegram-bot --source . --region europe-west1 --project prediction-intelligence
cd ..\..
Remove-Item -Recurse -Force services\telegram-bot\shared
```

## Prompt de continuación (próxima sesión)

Sistema de predicción deportiva + Polymarket desplegado en Google Cloud Run (proyecto `prediction-intelligence`, región `europe-west1`). Tres servicios: `sports-agent`, `polymarket-agent`, `telegram-bot`. Código en `C:\Users\Usuario\prediction-intelligence-ok\`. Deploy siempre con `xcopy /E /I /Y shared services\{servicio}\shared` → `gcloud run deploy {servicio} --source . --region europe-west1 --project prediction-intelligence` → limpiar shared.

Estado al 5 mayo 2026: sistema operativo, señales enviando a Telegram, learning engine ajustando pesos. Pendiente: exponer `classify_volume_spike` en enricher, conectar scheduler para `check_pending_odds_changes`, deploy del backtest engine al sports-agent.
