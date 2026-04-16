# PREDICTION INTELLIGENCE SYSTEM — CLAUDE CODE SPEC

> Lee este archivo completo antes de escribir cualquier línea de código.
> Este es el contrato de construcción. No inventes nada que no esté aquí.

---

## REGLAS NO NEGOCIABLES

1. **Idioma:** código en inglés, comentarios y logs en español
2. **Modelo IA:** siempre `llama-3.3-70b-versatile` via Groq API — nunca OpenAI, nunca Anthropic
3. **Web Search:** siempre Tavily API para búsquedas en tiempo real
4. **Python:** 3.12 con type hints en todas las funciones
5. **Sin ML externo:** no sklearn, no tensorflow, no modelos externos — solo pesos en Firestore
6. **Firestore es la única fuente de verdad:** todos los servicios leen y escriben Firestore
7. **No inventes colecciones ni campos** que no estén en los schemas de este documento
8. **No añadas dependencias** que no estén en los requirements.txt definidos aquí
9. **Thresholds fijos:** sports edge > 0.08 + confianza > 0.65 | poly edge > 0.12 + confianza > 0.65
10. **Si algo no está especificado aquí, pregunta antes de implementar**
11. **Makefile: usar TABS, no espacios.** Un Makefile con espacios falla con `missing separator`.
    Cada línea de comando dentro de un target DEBE empezar con un carácter TAB real (\t), nunca con espacios.

### MANEJO DE ERRORES — REGLAS GLOBALES
Aplica a TODOS los módulos sin excepción:
- **API externa falla (4xx/5xx/timeout):** loggear error con contexto, devolver None o lista vacía. NUNCA crashear el pipeline.
- **Firestore falla:** reintentar 3 veces con backoff exponencial (1s, 2s, 4s). Si persiste → loggear y continuar.
- **Rate limit (429):** esperar `Retry-After` header segundos. Si no hay header → esperar 60s. Loggear.
- **JSON inválido de LLM:** los LLMs frecuentemente envuelven JSON en ```json ... ```.
  Estrategia de extracción en orden:
  1. `json.loads(response)` directo
  2. Si falla: `re.search(r'\{.*\}', response, re.DOTALL)` y `json.loads(match.group())`
  3. Si falla: reintentar la llamada 1 vez con instrucción explícita "responde SOLO JSON, sin texto adicional"
  4. Si falla: descartar análisis y loggear. NUNCA crashear.
- **Datos insuficientes:** si un enriquecedor no tiene datos → usar valor neutral (0.5, "STABLE", etc.) y marcar `data_quality: "partial"` en el documento Firestore.
- **Nunca silenciar excepciones** con `except: pass`. Siempre loggear con `logging.error(exc_info=True)`.

---

## SCOPE V1

### Sports Agent V1
- **Multi-deporte**: futbol (Poisson+ELO completo), NBA/NFL/MLB/NHL/MMA (estadisticas + IA)
- **Multi-mercado**: resultado 1X2 con modelo propio; otros mercados (ambos marcan, corners, etc.) via Groq+noticias
- **Futbol**: football-data.org → estadisticas profundas + Poisson bivariado + ELO
- **Resto de deportes**: API-Sports (misma key FOOTBALL_RAPID_API_KEY) → stats + Groq analiza forma + noticias
- **Cuotas todos los deportes**: API-Sports odds endpoint
- Sin BallDontLie necesario — FOOTBALL_RAPID_API_KEY ya cubre NBA/NFL/MLB/MMA via API-Sports

### Polymarket Agent V1
- Analiza TODOS los tipos de mercado: deportes, politica, crypto, economia
- Monitoreo en TIEMPO REAL via WebSocket (no solo polling cada 2h)
- Deteccion de smart money via analisis on-chain de Polygon
- Correlacion con activos subyacentes (crypto via CoinGecko, etc.)

### Por que NO hay simulador de apuestas
En trading puedes comprimir 5 anos de datos en 5 minutos porque el precio historico ya existe.
En apuestas el resultado no existe hasta que se juega el partido — simular contra las propias
predicciones produce overfitting. Lo correcto es backtesting contra datos historicos reales.

### Modulo de backtesting (arranque en frio)

Anadir estos archivos al arbol:
- `services/sports-agent/backtester/__init__.py`
- `services/sports-agent/backtester/backtest.py`
- `services/polymarket-agent/backtester/__init__.py`
- `services/polymarket-agent/backtester/backtest_poly.py`

#### `backtester/backtest.py` — sports-agent
```python
async def run_backtest(seasons: int = 2) -> dict:
    # Corre el modelo contra partidos historicos de las ultimas N temporadas.
    # 1. Fetch historical matches de football-data.org
    # 2. Por cada partido: calcula prediccion con el modelo actual
    # 3. Compara con resultado real, ajusta pesos igual que run_daily_learning()
    # 4. Guarda pesos calibrados en model_weights doc current
    # Devuelve {accuracy, matches_processed, weights_final}
    # Trigger: POST /run-backtest (anadir a sports-agent/main.py)
    # Ejecutar UNA SOLA VEZ al inicializar el sistema. NO scheduler.
    # Tiempo estimado: 30-60 min por rate limit de football-data.org
```

#### `backtester/backtest_poly.py` — polymarket-agent
```python
async def run_poly_backtest(days_back: int = 90) -> dict:
    # Analiza mercados de Polymarket YA resueltos en los ultimos N dias.
    # Gamma API: GET /markets?closed=true&order=volume24hr&limit=100
    # Por cada mercado resuelto: calcula que habria predicho el modelo vs resultado real.
    # Guarda en Firestore coleccion poly_backtest_results.
    # Devuelve {accuracy, markets_analyzed, avg_edge_detected}
    # Trigger: POST /run-poly-backtest (anadir a polymarket-agent/main.py)
    # Ejecutar UNA SOLA VEZ al inicializar el sistema. NO scheduler.
```

#### Coleccion Firestore: `poly_backtest_results`
```python
{
    "run_date": datetime,
    "days_analyzed": int,
    "markets_total": int,
    "correct_direction": int,
    "accuracy": float,
    "avg_edge_detected": float,
    "created_at": datetime,
}
```

Anadir a sports-agent/main.py:
`POST /run-backtest  # 202 inmediato → background: backtester/backtest.py`

Anadir a polymarket-agent/main.py:
`POST /run-poly-backtest  # 202 inmediato → background: backtester/backtest_poly.py`

---

## SERVICIO: payments — V2 (NO construir en Sesiones 1-8)

### Concepto
Canal Telegram privado con acceso de pago via Stripe. El sistema envia alertas tanto al
TELEGRAM_CHAT_ID (dueño) como al TELEGRAM_CHANNEL_ID (canal de suscriptores).

### Flujo
```
Usuario solicita acceso → POST /subscribe → Stripe Checkout URL
→ paga → Stripe webhook → /stripe-webhook → activa en Firestore → bot invita al canal
Cancela o falla pago → Stripe webhook → desactiva → bot expulsa del canal
```

### Requirements
```
fastapi==0.115.0
uvicorn==0.30.0
stripe==10.0.0
google-cloud-firestore==2.19.0
httpx==0.27.0
python-dotenv==1.0.0
```

### Endpoints
```
POST /stripe-webhook     # eventos Stripe — protegido con Stripe signature, NO x-cloud-token
POST /subscribe          # Body: {"telegram_user_id": int, "email": str}
                         # Devuelve {"checkout_url": str}
GET  /status/{telegram_user_id}
GET  /health
```

### Coleccion Firestore: `subscriptions`
```python
{
    "telegram_user_id": int,
    "email": str,
    "stripe_customer_id": str,
    "stripe_subscription_id": str,
    "status": str,                 # "active" | "cancelled" | "past_due" | "trialing"
    "plan": str,                   # "monthly"
    "created_at": datetime,
    "current_period_end": datetime,
    "cancelled_at": datetime | None,
}
```

### stripe_handler.py
```python
STRIPE_EVENTS = [
    "checkout.session.completed",    # nuevo suscriptor: activar + invitar canal
    "invoice.payment_succeeded",     # renovacion: actualizar current_period_end
    "invoice.payment_failed",        # fallo: marcar past_due + avisar por DM
    "customer.subscription.deleted", # cancelacion: desactivar + expulsar canal
]

async def handle_webhook(payload: bytes, sig_header: str) -> None:
    # SIEMPRE verificar: stripe.WebhookSignature.verify_header(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    # Si falla verificacion → HTTP 400. NUNCA procesar sin verificar firma.
```

### access_manager.py
```python
async def invite_to_channel(telegram_user_id: int) -> bool:
    # Bot API: POST /createChatInviteLink con member_limit=1
    # Luego enviar link por DM via sendMessage al usuario
    # Devuelve True si exito

async def remove_from_channel(telegram_user_id: int) -> bool:
    # Bot API: POST /banChatMember + POST /unbanChatMember (ban+unban = expulsion sin bloqueo)

async def send_dm(telegram_user_id: int, message: str) -> None:
    # DM directo al usuario: bienvenida, aviso pago fallido, confirmacion cancelacion
```

### subscription_tracker.py
```python
async def create_subscription(telegram_user_id: int, email: str,
                               stripe_customer_id: str, stripe_sub_id: str) -> None:
async def update_subscription_status(stripe_sub_id: str, status: str,
                                      period_end: datetime | None = None) -> None:
async def cancel_subscription(stripe_sub_id: str) -> None:
async def get_subscription(telegram_user_id: int) -> dict | None:
async def is_active(telegram_user_id: int) -> bool:
```

### Modificacion en alert_manager.py
```python
# Enviar alertas al canal de suscriptores ademas del chat personal del dueno
TARGETS = [TELEGRAM_CHAT_ID]
if TELEGRAM_CHANNEL_ID:
    TARGETS.append(TELEGRAM_CHANNEL_ID)
for target in TARGETS:
    await send_message_to(target, text)
```

### Cloud Run payments
```
min-instances: 0  max-instances: 1  memory: 256Mi  cpu: 1  timeout: 60s
```

### Secrets adicionales necesarios
```
STRIPE_SECRET_KEY       sk_live_... (o sk_test_... para pruebas)
STRIPE_WEBHOOK_SECRET   whsec_... (del dashboard de Stripe)
STRIPE_PRICE_ID         price_... (ID del precio mensual creado en Stripe)
TELEGRAM_CHANNEL_ID     ID del canal privado de pago
PAYMENTS_URL            URL Cloud Run del servicio payments (tras primer deploy)
```

### Configuracion manual (fuera del codigo)
```
Stripe:
  1. Crear cuenta en stripe.com
  2. Crear producto y precio recurrente mensual (ej: 49 EUR/mes) → STRIPE_PRICE_ID
  3. Configurar webhook → URL: {PAYMENTS_URL}/stripe-webhook → STRIPE_WEBHOOK_SECRET
  4. Para tests locales: stripe listen --forward-to localhost:8080/stripe-webhook

Telegram:
  1. Crear canal privado en Telegram
  2. Anadir el bot como administrador con permiso "Invitar usuarios"
  3. Obtener TELEGRAM_CHANNEL_ID via @userinfobot o getChat API
```

---

## STACK Y CONFIGURACIÓN

```
Cloud:        GCP proyecto=prediction-intelligence región=europe-west1
Runtime:      Python 3.12
Web:          FastAPI (agents + dashboard backend)
Bot:          python-telegram-bot v20+ asyncio
DB:           Cloud Firestore (modo nativo)
Deploy:       Cloud Run
Scheduler:    GitHub Actions (cron workflows) → HTTP POST a Cloud Run
Secrets:      Env vars directas en Cloud Run (--set-env-vars) + GitHub Secrets — SIN Secret Manager
CI/CD:        GitHub Actions (deploy.yml) — SIN Cloud Build dedicado
IA:           Groq API (llama-3.3-70b-versatile) — gratuito
GitHub repo:  DEBE ser PUBLIC — repos privados tienen 2,000 min/mes y nuestros 8 workflows
              consumen ~1,234 min/mes. Repo público = minutos ilimitados.
Search:       Tavily API — 1,000 búsquedas/mes gratuitas
Budget:       Tavily se usa SOLO en top 10 mercados poly/día + dashboard. Nunca en bucles grandes.
```

---

## ESTRUCTURA DEL MONOREPO

```
prediction-intelligence/
├── services/
│   ├── sports-agent/
│   │   ├── main.py                    # FastAPI: /run-collect /run-enrich /run-analyze /run-learning
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   ├── collectors/
│   │   │   ├── __init__.py
│   │   │   ├── football_api.py        # football-data.org: futbol europeo (Poisson+ELO)
│   │   │   ├── api_sports_client.py   # API-Sports: NBA/NFL/MLB/MMA stats (misma key que futbol)
│   │   │   ├── multi_sport_analyzer.py # Groq+Tavily: analisis IA para deportes sin modelo propio
│   │   │   # ❌ understat_scraper.py ELIMINADO — understat.com carga datos via JavaScript,
│   │   │   # beautifulsoup4 solo lee HTML estático → devuelve siempre vacío.
│   │   │   # Selenium/Playwright funcionarían pero pesan 500MB+ (no caben en 512Mi Cloud Run).
│   │   │   # xG se aproxima con shots_on_target/shots_total de football-data.org (ver stats_processor.py)
│   │   │   ├── odds_movement.py       # movimiento de cuotas desde odds_cache (sin llamadas API)
│   │   │   ├── stats_processor.py     # procesado y enriquecimiento de datos crudos
│   │   │   └── firestore_writer.py
│   │   ├── enrichers/
│   │   │   ├── __init__.py
│   │   │   ├── poisson_model.py       # Poisson bivariado + corrección Dixon-Coles
│   │   │   ├── elo_rating.py          # ELO dinámico adaptado al fútbol
│   │   │   └── data_enricher.py       # orquesta enrichers → enriched_match completo
│   │   ├── analyzers/
│   │   │   ├── __init__.py
│   │   │   └── value_bet_engine.py    # recibe enriched_match → genera señal
│   │   ├── backtester/
│   │   │   ├── __init__.py
│   │   │   └── backtest.py            # backtesting historico — ejecutar UNA VEZ al arrancar
│   │   └── learner/
│   │       ├── __init__.py
│   │       └── learning_engine.py
│   ├── polymarket-agent/
│   │   ├── main.py                    # FastAPI: /run-scan /run-enrich /run-analyze
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   ├── scanner.py                 # fetch top 50 mercados por volumen
│   │   ├── price_tracker.py           # snapshots históricos + momentum + volume spike
│   │   ├── enrichers/
│   │   │   ├── __init__.py
│   │   │   ├── orderbook_analyzer.py  # ratio compradores/vendedores + detección smart money
│   │   │   ├── correlation_detector.py # correlaciones entre mercados relacionados
│   │   │   ├── news_sentiment.py      # Tavily: sentiment noticias 72h ponderado por fuente
│   │   │   └── market_enricher.py     # orquesta → enriched_market completo
│   │   ├── groq_analyzer.py           # recibe enriched_market → prob real + edge + reasoning
│   │   └── alert_engine.py
│   ├── telegram-bot/
│   │   ├── main.py                    # FastAPI app con /webhook (POST de Telegram) + /health
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   ├── handlers.py                # Comandos /start /sports /poly /stats /calc /help
│   │   └── alert_manager.py           # Recibe POST /send-alert desde sports-agent y poly-agent → push Telegram
│   └── dashboard/
│       ├── main.py                    # FastAPI app con Basic Auth + static files
│       ├── requirements.txt
│       ├── Dockerfile
│       ├── api/
│       │   ├── __init__.py
│       │   ├── predictions.py
│       │   ├── polymarket.py
│       │   ├── calculator.py
│       │   ├── odds_finder.py
│       │   └── tracker.py
│       └── frontend/                  # React + Vite, build → /static
│           ├── package.json
│           ├── vite.config.ts
│           └── src/
│               ├── App.tsx
│               ├── components/
│               │   ├── MatchedBetting.tsx
│               │   ├── SportSignals.tsx
│               │   ├── PolymarketCards.tsx
│               │   └── ModelStats.tsx
│               └── hooks/
│                   └── useApi.ts
├── shared/
│   ├── __init__.py
│   ├── firestore_client.py
│   ├── groq_client.py             # Groq (IA) + Tavily (web search)
│   ├── config.py
│   └── report_generator.py       # generate_weekly_report() — importado por telegram-bot
├── .gitignore                     # OBLIGATORIO — ver contenido abajo
├── .env.example                   # plantilla de variables (sin valores reales)
├── firestore.rules                # security rules — ver sección FIRESTORE SECURITY RULES
├── .firebaserc                    # config Firebase CLI: {"projects": {"default": "prediction-intelligence"}}
└── infra/
    └── setup.sh                   # provisiona GCP desde cero — orden: bot primero, luego agentes
    # cloudbuild/ ELIMINADO — CI/CD se gestiona via deploy.yml (GitHub Actions).
    # Tener ambos causaría doble deploy en cada push.
.github/
└── workflows/
    ├── deploy.yml                 # CI/CD: deploy todos los servicios on push to main (ver spec abajo)
    ├── sports-collect.yml         # Cron: cada 6h → POST /run-collect
    ├── sports-enrich.yml          # Cron: cada 6h+30m → POST /run-enrich
    ├── sports-analyze.yml         # Cron: 01:00, 07:00, 13:00, 19:00 UTC → POST /run-analyze
    ├── learning-engine.yml        # Cron: diario 02:00 → POST /run-learning
    ├── polymarket-scan.yml        # Cron: cada 2h → POST /run-scan
    ├── polymarket-enrich.yml      # Cron: cada 2h+30m → POST /run-enrich
    ├── polymarket-analyze.yml     # Cron: cada 6h → POST /run-analyze
    └── weekly-report.yml          # Cron: lunes 09:00 → POST /send-weekly-report
```

---

## VARIABLES DE ENTORNO / SECRETS

⚠️ NO usar Google Secret Manager — cuesta dinero por encima de 6 secrets activos (tenemos 14).
Estrategia 100% gratuita:
- **Cloud Run:** variables pasadas con `--set-env-vars` en el gcloud run deploy (ver Makefile)
- **GitHub Actions:** variables guardadas como Repository Secrets en GitHub (gratis ilimitado)
- **Local dev:** archivo `.env` en la raíz del repo (excluido en .gitignore)

| Variable                      | Dónde se configura                       |
|-------------------------------|------------------------------------------|
| `TELEGRAM_TOKEN`              | GitHub Secret + gcloud --set-env-vars    |
| `TELEGRAM_CHAT_ID`            | Chat ID destino de alertas               |
| `GROQ_API_KEY`                | Groq API key (IA gratuita)               |
| `TAVILY_API_KEY`              | Tavily API key — presupuesto: máx 30 búsquedas/día |
| `FOOTBALL_API_KEY`            | football-data.org API key                |
| `FOOTBALL_RAPID_API_KEY`      | API-Football/API-Sports key (odds todos deportes) |
# BALLDONTLIE_API_KEY NO necesaria — API-Sports cubre todos los deportes con FOOTBALL_RAPID_API_KEY
| `COINGECKO_API_KEY`           | CoinGecko API key — opcional, mejora rate limits |
| `GOOGLE_CLOUD_PROJECT`        | `prediction-intelligence`                |
| `DASHBOARD_USER`              | Usuario Basic Auth dashboard             |
| `DASHBOARD_PASS`              | Password Basic Auth dashboard            |
| `FIRESTORE_COLLECTION_PREFIX` | Prefijo colecciones (ej: `prod_`)        |
| `SPORTS_AGENT_URL`            | URL Cloud Run sports-agent (para GitHub Actions) |
| `DASHBOARD_URL`               | URL Cloud Run dashboard (informativo)            |
| `POLY_AGENT_URL`              | URL Cloud Run polymarket-agent (para GitHub Actions) |
| `TELEGRAM_BOT_URL`            | URL Cloud Run telegram-bot (para GitHub Actions) |
| `CLOUD_RUN_TOKEN`             | Token secreto para autenticar llamadas GitHub Actions → Cloud Run |
| `GCP_SA_KEY`                  | JSON de service account github-deployer (para deploy.yml CI/CD) |

---

## SHARED MODULES

### shared/config.py
```python
import os

GOOGLE_CLOUD_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]

# ⚠️ Variables opcionales según el servicio — usar .get() para evitar KeyError al arrancar.
# Cada servicio solo recibe las vars que necesita en --set-env-vars.
# Si una var no está presente → None. El servicio debe validar antes de usarla.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")        # solo telegram-bot
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")    # solo telegram-bot
TELEGRAM_BOT_URL = os.environ.get("TELEGRAM_BOT_URL")    # sports-agent + polymarket-agent
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")    # solo sports-agent
FOOTBALL_RAPID_API_KEY = os.environ.get("FOOTBALL_RAPID_API_KEY")  # solo sports-agent
# BALLDONTLIE_API_KEY no necesaria — usar FOOTBALL_RAPID_API_KEY para todos los deportes via API-Sports
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")            # polymarket-agent (opcional)
DASHBOARD_USER = os.environ.get("DASHBOARD_USER")        # solo dashboard
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS")        # solo dashboard
COLLECTION_PREFIX = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "")

# IA
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_FALLBACK_MODEL = "llama-3.1-70b-versatile"  # fallback si el modelo principal es deprecado
GROQ_BASE_URL = "https://api.groq.com/openai/v1"  # compatible con openai SDK

# Thresholds
SPORTS_MIN_EDGE = 0.08
SPORTS_MIN_CONFIDENCE = 0.65
SPORTS_ALERT_EDGE = 0.10
POLY_MIN_EDGE = 0.12
POLY_MIN_CONFIDENCE = 0.65

# Ligas de futbol — football-data.org (modelo Poisson+ELO completo)
SUPPORTED_FOOTBALL_LEAGUES = {
    "PL": 2021,    # Premier League
    "PD": 2014,    # La Liga
    "BL1": 2002,   # Bundesliga
    "SA": 2019,    # Serie A
    # CL: no disponible en free tier de football-data.org
}

# Deportes adicionales — API-Sports (misma key FOOTBALL_RAPID_API_KEY) + Groq (analisis IA)
# ⚠️ API-Sports = 100 req/dia compartidos entre futbol + todos los demas deportes
# Prioridad: futbol primero, resto de deportes con lo que quede del budget diario
SUPPORTED_SPORTS_APISPORTS = {
    "basketball": "nba",   # NBA — https://api-basketball.p.rapidapi.com
    "american-football": "nfl",  # NFL — https://api-american-football.p.rapidapi.com
    "baseball": "mlb",     # MLB
    "hockey": "nhl",       # NHL
    "mma": "ufc",          # UFC/MMA
}
# Para cada deporte: stats de forma reciente + H2H desde API-Sports
# Groq analiza esas stats + noticias Tavily para estimar probabilidades
# Ensemble: stats_score (0.60) + groq_estimate (0.40)

MIN_MATCHES_TO_FIT = 5  # futbol: free tier da 10 partidos; 5 es minimo para Poisson

LEARNING_RATE = 0.05
DEFAULT_WEIGHTS = {
    # Pesos para ensemble_probability — deben coincidir con las 4 señales del modelo
    "poisson": 0.40,      # modelo Poisson bivariado (mas robusto estadisticamente)
    "elo": 0.25,          # rating ELO dinamico
    "form": 0.20,         # forma reciente (ultimos 10 partidos)
    "h2h": 0.15,          # ventaja historica directa
}
```

### shared/firestore_client.py
> El módulo `shared/` se importa via `PYTHONPATH=/app` (seteado en el Dockerfile).
> El CI copia `shared/` dentro de cada servicio antes del build.
> Con `COPY . .` en el Dockerfile, `shared/` queda en `/app/shared/`.
> Con `PYTHONPATH=/app`, Python encuentra `import shared.config` correctamente.
> No se necesita setup.py ni pip install -e.

```python
from google.cloud import firestore
from shared.config import GOOGLE_CLOUD_PROJECT, COLLECTION_PREFIX

_client = None

def get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=GOOGLE_CLOUD_PROJECT)
    return _client

def col(name: str) -> firestore.CollectionReference:
    """Devuelve referencia a colección con prefijo."""
    return get_client().collection(f"{COLLECTION_PREFIX}{name}")
```

### shared/groq_client.py
```python
from shared.config import GROQ_API_KEY, TAVILY_API_KEY, GROQ_MODEL, GROQ_FALLBACK_MODEL, GROQ_BASE_URL

# ⚠️ NO importar openai ni tavily a nivel de modulo.
# Los imports dentro de las funciones evitan ModuleNotFoundError en servicios
# que tienen groq_client.py copiado pero no necesitan IA (ej: telegram-bot).
_groq = None
_tavily = None

def _get_groq():
    global _groq
    if _groq is None:
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY no configurada para este servicio")
        from openai import OpenAI  # import aqui, no a nivel de modulo
        _groq = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
    return _groq

def _get_tavily():
    global _tavily
    if _tavily is None:
        if not TAVILY_API_KEY:
            raise RuntimeError("TAVILY_API_KEY no configurada para este servicio")
        from tavily import TavilyClient  # import aqui, no a nivel de modulo
        _tavily = TavilyClient(api_key=TAVILY_API_KEY)
    return _tavily

def search_web(query: str, max_results: int = 5) -> str:
    """Busca en la web con Tavily. Devuelve resultados formateados como string."""
    results = _get_tavily().search(query=query, max_results=max_results)
    return "\n\n".join(
        f"[{r['title']}]\n{r['content']}" for r in results.get("results", [])
    )

# ⚠️ GROQ TPM LIMIT: free tier = 6,000 tokens/min para llama-3.3-70b.
# Un enriched_market = ~1,500 tokens. Máximo 4 llamadas seguidas antes de pausar.
# Usar GROQ_CALL_DELAY entre llamadas en batch. Ver constante abajo.
GROQ_CALL_DELAY = 4  # segundos entre llamadas en batch (conservador para no exceder TPM)

def analyze(system_prompt: str, user_prompt: str, web_search: bool = True) -> str:
    """
    Llama a Groq con contexto de búsqueda web opcional.
    Si web_search=True, primero busca con Tavily y añade resultados al contexto.
    Si GROQ_MODEL falla con 404/model_not_found → reintentar automáticamente con GROQ_FALLBACK_MODEL.
    Devuelve texto de respuesta.
    """
    if web_search:
        search_results = search_web(user_prompt[:200])
        enriched_prompt = f"""Resultados de búsqueda web actuales:
{search_results}

---
{user_prompt}"""
    else:
        enriched_prompt = user_prompt

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": enriched_prompt},
    ]

    # Intentar con modelo principal, fallback si el modelo fue deprecado
    for model in [GROQ_MODEL, GROQ_FALLBACK_MODEL]:
        try:
            response = _get_groq().chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=2048,
                temperature=0.3,
            )
            return response.choices[0].message.content
        except Exception as e:
            if "model_not_found" in str(e).lower() or "404" in str(e):
                # Loggear y probar con fallback
                continue
            raise  # otros errores propagar
    raise RuntimeError(f"Ambos modelos Groq fallaron: {GROQ_MODEL}, {GROQ_FALLBACK_MODEL}")
```

### shared/report_generator.py
```python
def generate_weekly_report(week_stats: dict, weights_before: dict, weights_after: dict) -> str:
    """
    Genera string Markdown formateado para Telegram.
    week_stats: {week, predictions_total, predictions_correct, accuracy, accuracy_by_league,
                 best_match, best_edge, best_result, worst_match, worst_edge, worst_error,
                 poly_total, poly_alerts, poly_avg_edge, prev_week_accuracy}
    Ver formato exacto en sección TELEGRAM — FORMATO MENSAJES.
    Esta funcion esta en shared/ para que telegram-bot la pueda importar sin dependencias cruzadas.
    """
```
> El telegram-bot /send-weekly-report construye week_stats con ESTAS queries Firestore:
> 1. accuracy_log donde week == current_week → predictions_total, predictions_correct, accuracy, prev_week_accuracy
> 2. model_weights doc 'current' → weights_before (weights_start del accuracy_log), weights_after
> 3. predictions donde created_at >= semana_actual → encontrar best (max edge, correct=True) y worst (correct=False, min confidence)
> 4. poly_predictions donde analyzed_at >= semana_actual → contar total y alertas (alerted=True), calcular avg edge
> 5. Construir week_stats dict y llamar shared.report_generator.generate_weekly_report(week_stats, weights_before, weights_after)
> 6. Si no hay datos (semana sin predicciones) → enviar mensaje resumido sin estadísticas detalladas
> 7. Envía el resultado a TELEGRAM_CHAT_ID via Bot API

---

## FIRESTORE — SCHEMAS EXACTOS

### Colección: `predictions`
```python
{
    "match_id": str,           # ID único del partido
    "home_team": str,
    "away_team": str,
    "sport": str,              # "football" | "nba" | "nfl" | "mlb" | "nhl" | "mma"
    "league": str,             # "PL","PD","BL1","SA" para futbol; "NBA","NFL",etc. para otros
    "data_source": str,        # "statistical_model" (Poisson+ELO) | "groq_ai" (otros deportes)
    "match_date": datetime,
    "team_to_back": str,
    "bookmaker": str,
    "odds": float,             # cuota decimal
    "calculated_prob": float,  # 0.0–1.0
    "edge": float,             # calculated_prob - (1/odds)
    "confidence": float,       # 0.0–1.0
    "kelly_fraction": float,   # fracción Kelly recomendada
    "factors": {
        # FUTBOL (data_source="statistical_model"):
        # "poisson": float, "elo": float, "form": float, "h2h": float
        # OTROS DEPORTES (data_source="groq_ai"):
        # "stats_score": float,   # form+h2h normalizados (0-1)
        # "groq_estimate": float  # probabilidad estimada por Groq
        #
        # El learning engine usa ERROR_TO_WEIGHT segun data_source:
        # - statistical_model: ajusta pesos {poisson, elo, form, h2h}
        # - groq_ai: no ajusta pesos estadisticos (no hay modelo que ajustar)
    },
    "weights_version": int,
    "created_at": datetime,
    "result": str | None,      # "HOME_WIN" | "AWAY_WIN" | "DRAW" | None
    "correct": bool | None,
    "error_type": str | None,  # ver tipos en learning_engine.py
}
```

### Colección: `model_weights` — doc ID: `current`
```python
{
    "version": int,
    "updated": datetime,
    "weights": {
        # Pesos del ensemble — deben coincidir con DEFAULT_WEIGHTS en config.py
        "poisson": float,
        "elo": float,
        "form": float,
        "h2h": float,
    },
    "accuracy_by_league": {
        "PL": float, "PD": float, "BL1": float, "SA": float,
        # CL eliminada — no disponible en free tier de football-data.org
    },
    "blacklisted_leagues": list[str],
    "min_edge_threshold": float,
    "min_confidence": float,
    "total_predictions": int,
    "correct_predictions": int,
}
```

### Colección: `upcoming_matches`
```python
{
    "match_id": str,
    "home_team": str,
    "away_team": str,
    "home_team_id": int,
    "away_team_id": int,
    "league": str,
    "match_date": datetime,
    "status": str,             # "SCHEDULED" | "LIVE" | "FINISHED"
    "collected_at": datetime,
}
```

### Colección: `team_stats`
```python
{
    "team_id": int,
    "team_name": str,
    "league": str,
    "last_10": list[str],      # ["W","W","D","L","W", ...]
    "form_score": float,       # 0–100
    "home_stats": {"played": int, "won": int, "drawn": int, "lost": int, "goals_for": int, "goals_against": int},
    "away_stats": {"played": int, "won": int, "drawn": int, "lost": int, "goals_for": int, "goals_against": int},
    "streak": {"type": str, "count": int},  # type: "win" | "loss" | "draw"
    "xg_per_game": float,      # proxy de xG calculado por stats_processor.calculate_xg_proxy()
    # REQUERIDO para Poisson — raw match data con goles por partido:
    "raw_matches": list[dict], # [{match_id, date, home_team_id, away_team_id,
                               #   goals_home, goals_away, was_home: bool}]
                               # ultimos 10 partidos — usado por fit_attack_defense()
    "updated_at": datetime,
}
```

### Colección: `h2h_data`
```python
{
    "pair_key": str,           # f"{team1_id}_{team2_id}" (menor primero)
    "team1_id": int,
    "team2_id": int,
    "matches": list[dict],     # últimos partidos disponibles (máx 10 con free tier)
    "team1_wins": int,
    "team2_wins": int,
    "draws": int,
    "h2h_advantage": float,   # -1.0 a 1.0 (positivo = ventaja team1)
    "updated_at": datetime,
}
```

### Colección: `poly_markets`
```python
{
    "market_id": str,
    "condition_id": str,       # ⚠️ requerido para CLOB orderbook — mapear desde "conditionId" en Gamma API
    "question": str,
    "end_date": datetime,
    "volume_24h": float,
    "price_yes": float,        # 0.0–1.0
    "price_no": float,
    "active": bool,
    "updated_at": datetime,
}
```

### Colección: `poly_price_history`
```python
{
    "market_id": str,
    "timestamp": datetime,
    "price_yes": float,
    "price_no": float,
    "volume_24h": float,
}
```
> ⚠️ Esta colección crece 600 documentos/día. Sin limpieza supera 1GB en ~2 años (free tier).
> Limpieza implementada en polymarket-agent groq_analyzer.run_maintenance() — se ejecuta al final de /run-analyze.
> Borra en batch: poly_price_history con timestamp < now-30d + enriched_markets con enriched_at < now-7d.

### Colección: `poly_predictions`
```python
{
    "market_id": str,
    "question": str,
    "market_price_yes": float,
    "real_prob": float,
    "edge": float,
    "confidence": float,
    "trend": str,              # "RISING" | "FALLING" | "STABLE"
    "recommendation": str,     # "BUY_YES" | "BUY_NO" | "PASS" | "WATCH"
    "key_factors": list[str],  # factores clave del análisis (del groq_analyzer)
    "reasoning": str,
    "volume_spike": bool,
    "smart_money_detected": bool,  # copiado de enriched_market.smart_money.is_smart_money
    "analyzed_at": datetime,
    "alerted": bool,
}
```

### Colección: `bets` (tracker personal)
```python
{
    "bet_type": str,           # "qualifying" | "free_bet_snr" | "free_bet_sr"
    "event": str,
    "back_stake": float,
    "back_odds": float,
    "lay_odds": float,
    "commission": float,
    "lay_stake": float,
    "profit_back": float,
    "profit_lay": float,
    "rating": float,
    "status": str,             # "pendiente" | "ganado_back" | "ganado_lay" | "cancelado"
    "pnl": float,              # ganancia/pérdida realizada al cerrar
    "created_at": datetime,
    "updated_at": datetime,
}
```

### Colección: `alerts_sent` (deduplicación)
```python
{
    "alert_key": str,          # f"{match_id or market_id}_{round(edge, 2)}" — redondear a 2 decimales
    "sent_at": datetime,
    "type": str,               # "sports" | "polymarket" | "weekly_report"
}
```

### Colección: `accuracy_log`
```python
{
    "week": str,               # "2025-W14"
    "predictions_total": int,
    "predictions_correct": int,
    "accuracy": float,
    "prev_week_accuracy": float | None,  # para calcular delta en el reporte semanal
    "accuracy_by_league": dict,
    "weights_start": dict,
    "weights_end": dict,
    "created_at": datetime,
}
```

### Colección: `enriched_matches` (sports-agent)
```python
{
    "match_id": str,
    "sport": str,              # "football" | "nba" | "nfl" | "mlb" | "nhl" | "mma"
    "home_team_id": int,
    "away_team_id": int,
    # Campos SOLO FUTBOL (None para otros deportes):
    "poisson_home_win": float | None,
    "poisson_draw": float | None,
    "poisson_away_win": float | None,
    "home_xg": float | None,
    "away_xg": float | None,
    "elo_home_win_prob": float | None,
    "home_elo": float | None,
    "away_elo": float | None,
    # Campos TODOS LOS DEPORTES:
    "home_form_score": float,
    "away_form_score": float,
    "h2h_advantage": float,
    "home_streak": {"type": str, "count": int},
    "away_streak": {"type": str, "count": int},
    "odds_opening": {"home": float, "draw": float, "away": float},
    "odds_current": {"home": float, "draw": float, "away": float},
    "odds_movement": float,    # variación cuota home desde apertura
    "data_quality": str,       # "full" | "partial" (partial si algún enricher no tuvo datos)
    "enriched_at": datetime,
}
```

### Colección: `team_elo`
```python
{
    "team_id": int,
    "team_name": str,
    "elo": float,              # ELO actual
    "elo_history": list[dict], # [{date, elo, opponent_id, result}] últimas 10 entradas
    "updated_at": datetime,
}
```

### Colección: `enriched_markets` (polymarket-agent)
```python
{
    "market_id": str,
    "price_momentum": str,
    "volume_spike": bool,
    "smart_money": {"is_smart_money": bool, "hours_before_news": float | None},
    "orderbook": {"buy_pressure": float, "spread": float, "depth": float, "imbalance_signal": str},
    "correlations": list[dict],
    "arbitrage": {"detected": bool, "inefficiency": float, "direction": str},
    "news_sentiment": {"score": float, "count": int, "headlines": list[str], "trend": str},
    "data_quality": str,       # "full" | "partial" (partial si algún enricher no tuvo datos)
    "enriched_at": datetime,
}
```

### Colección: `realtime_events` (TTL 24h — datos WebSocket)
```python
{
    "market_id": str,
    "condition_id": str,
    "event_type": str,      # "book" | "price_change" | "last_trade_price"
    "timestamp": datetime,
    "best_bid": float | None,
    "best_ask": float | None,
    "trade_price": float | None,
    "trade_size": float | None,
    "buy_pressure": float | None,  # calculado de book snapshot
    "is_large_trade": bool,
}
```

### Colección: `wallet_profiles` (cache de analisis on-chain)
```python
{
    "wallet_address": str,   # doc ID
    "age_hours": float,      # horas desde la primera transaccion
    "tx_count": int,
    "is_fresh": bool,        # age < 48h AND tx_count < 10
    "total_pnl_usd": float | None,  # si disponible via subgraph
    "profile_type": str,     # "fresh" | "whale" | "bot_suspect" | "regular"
    "last_analyzed": datetime,
}
```

### Colección: `tavily_budget`
```python
{
    "date": str,               # "2025-04-12" — doc ID es la fecha
    "calls_today": int,
    "limit": int,              # 30
    "updated_at": datetime,
}
```

### Colección: `odds_cache`
```python
{
    "fixture_id": str,          # doc ID
    "home_odds": float,         # cuota actual
    "draw_odds": float,
    "away_odds": float,
    "opening_home_odds": float, # cuota en el PRIMER fetch — no actualizar en refrescos
    "opening_draw_odds": float,
    "opening_away_odds": float,
    "bookmaker": str,
    "first_fetched_at": datetime, # timestamp del primer fetch
    "fetched_at": datetime,       # timestamp del fetch más reciente (TTL: 4h)
}
# Lógica: al primer fetch guardar opening_* y fetched_at.
# En refrescos posteriores: actualizar home_odds/draw/away y fetched_at, NO opening_*.
# odds_movement = (home_odds - opening_home_odds) / opening_home_odds
```

# poly_orderbook_snapshots ELIMINADA — ningún módulo escribe en ella.
# Los datos del orderbook se incluyen directamente en enriched_markets.orderbook
# sin persistirlos por separado (los datos de orderbook son efímeros, no históricos).

---

## SERVICIO: sports-agent

### Requirements
```
fastapi==0.115.0
uvicorn==0.30.0
google-cloud-firestore==2.19.0
httpx==0.27.0
scipy==1.13.0
numpy==1.26.0
python-dotenv==1.0.0
# balldontlie SDK no necesario — API-Sports usa httpx igual que football_api.py
openai==1.51.0            # para multi_sport_analyzer.py (Groq via shared/groq_client.py)
tavily-python==0.5.0      # para multi_sport_analyzer.py (noticias via shared/groq_client.py)
```

### main.py — endpoints
```python
# ⚠️ ENDPOINTS ASYNC — todos devuelven 202 Accepted inmediatamente.
# El trabajo real se ejecuta en background (asyncio.create_task).
# Motivo: el curl de GitHub Actions usa --max-time 30 (solo espera confirmación 202).
# /run-collect puede tardar hasta 15min en background — Cloud Run lo permite con timeout=900s.
# Con 202 inmediato el job de GitHub Actions siempre es ✅.

POST /run-collect   # 202 inmediato → background: collectors/ (futbol + multideporte) → Firestore
POST /run-enrich    # 202 inmediato → background: enrichers/ → Firestore
POST /run-analyze   # 202 inmediato → background: value_bet_engine → Firestore
POST /run-learning  # 202 inmediato → background: learning_engine → Firestore
POST /run-backtest  # 202 inmediato → background: backtester/backtest.py
                    # Solo llamar UNA VEZ al iniciar. Calibra pesos con datos historicos.
GET  /health        # {"status": "ok"}
GET  /status        # {"last_collect": ISO, "last_enrich": ISO, "last_analyze": ISO}
```

### Flujo de datos sports-agent
```
/run-collect → football_api (futbol) + api_sports_client (NBA/NFL/MLB/MMA, misma key) + odds
              → Firestore: upcoming_matches, team_stats, h2h_data (todos los deportes)
/run-enrich  → data_enricher (futbol: Poisson+ELO | resto: stats+Groq)             → Firestore enriched_matches
/run-analyze → value_bet_engine (lee enriched_matches + cuotas actuales)           → Firestore predictions
/run-learning→ learning_engine (lee predictions + results)                         → Firestore model_weights
```

### odds_movement.py — nota crítica
```
⚠️ NO llama a API-Football directamente — tenemos solo 100 req/día y se usan para cuotas actuales.
Lee el movimiento directamente del documento odds_cache (UN doc por fixture_id):
  odds_cache.opening_home_odds  → cuota de apertura (guardada en el primer fetch, NO se sobreescribe)
  odds_cache.home_odds          → cuota actual (actualizada en cada refresh)
  movement = (home_odds - opening_home_odds) / opening_home_odds

Si opening_home_odds == home_odds (primer fetch, no hay movimiento aún) → movement = 0.0
CERO llamadas adicionales a la API.
```

### xG proxy — implementado en stats_processor.py
```
❌ understat.com DESCARTADO: carga datos via JavaScript, beautifulsoup4 no puede leerlo.
   Selenium/Playwright funcionarían pero son 500MB+ de dependencias — no caben en Cloud Run 512Mi.

✅ ALTERNATIVA: calcular xG aproximado desde datos de football-data.org:
   xg_proxy = shots_on_target / shots_total × goals_scored  (si shots disponibles)
   xg_proxy = goals_scored / matches_played                 (si no hay shots)

   Guardar en team_stats como "xg_per_game": float
   Precisión ~70% vs xG real — suficiente para el modelo Poisson.
   Implementar en stats_processor.calculate_xg_proxy(team_matches) → float
```

### collectors/api_sports_client.py — firmas exactas
```python
# Usa FOOTBALL_RAPID_API_KEY — la misma key que ya tienes para futbol
# Base URLs por deporte (todas requieren X-RapidAPI-Key y X-RapidAPI-Host):
API_SPORTS_HOSTS = {
    "basketball": "api-basketball.p.rapidapi.com",
    "american-football": "api-american-football.p.rapidapi.com",
    "baseball": "api-baseball.p.rapidapi.com",
    "hockey": "api-hockey.p.rapidapi.com",
    "mma": "api-mma.p.rapidapi.com",
}

# URLs por deporte — ver API_SPORTS_HOSTS dict arriba
# Sin SDK adicional — usa httpx como football_api.py

# Rate limit API-Sports free tier: 100 req/DIA total (compartido con futbol)
# Usar API_SPORTS_DELAY entre llamadas para no exceder el limite diario
API_SPORTS_DELAY = 2.0  # segundos entre requests — conservador dado el limite diario

async def get_games_today(sport: str) -> list[dict]:
    # sport: "nba" | "nfl" | "mlb" | "nhl" | "mma"
    # Devuelve partidos del dia con scores si disponibles

async def get_team_stats_bdl(sport: str, team_id: int, last_n: int = 10) -> dict:
    # Ultimos N partidos del equipo: pts, reb, ast (NBA) o yds, td (NFL) etc.
    # Calcula: form_score, home_away_split, streak

async def get_injuries(sport: str) -> list[dict]:
    # Solo NBA y NFL tienen endpoint de lesiones en API-Sports
    # Devuelve lista de jugadores lesionados actualmente

async def get_odds_bdl(sport: str, game_id: int) -> dict | None:
    # Cuotas en tiempo real si disponibles (API-Sports, solo algunas ligas)
    # Si no disponible → fallback a API-Sports: GET https://api-sports.io/odds
    # Devuelve {moneyline_home, moneyline_away, spread, total} o None
```

### collectors/multi_sport_analyzer.py — firmas exactas
```python
# Para deportes donde tenemos estadisticas de API-Sports pero NO modelo Poisson
# Usa Groq + Tavily para estimar probabilidades

async def analyze_non_football_game(game: dict, home_stats: dict, away_stats: dict) -> dict:
    # 1. Busca noticias recientes con Tavily: lesiones, forma, contexto
    # 2. Llama Groq con stats + noticias → estima probabilidades
    # System prompt: "Eres un experto en {sport}. Dados estos stats y noticias,
    #   estima la probabilidad de victoria local. Responde SOLO JSON:
    #   {home_win_prob: float, confidence: float, key_factors: list[str]}"
    # Devuelve {home_win_prob, confidence, key_factors, data_source: "groq_ai"}
    # data_source distingue predicciones con modelo propio vs estimacion IA
```

### collectors/firestore_writer.py — firmas exactas
```python
# OBLIGATORIO: save_team_stats debe guardar raw_matches para que Poisson funcione

async def save_upcoming_matches(matches: list[dict]) -> None:
    # Guarda lista de upcoming_matches en Firestore. Doc ID = match_id.

async def save_team_stats(team_id: int, raw_api_matches: list[dict]) -> None:
    # Procesa raw_api_matches y guarda en team_stats.
    # Calcula: last_10, form_score, home_stats, away_stats, streak, xg_per_game
    # IMPRESCINDIBLE: raw_matches = [{match_id, date, home_team_id, away_team_id,
    #                                 goals_home, goals_away, was_home}]

async def save_h2h(team1_id: int, team2_id: int, h2h_matches: list[dict]) -> None:
    # Guarda h2h_data. pair_key = f"{min(t1,t2)}_{max(t1,t2)}"
    # h2h_advantage desde perspectiva del equipo con menor ID (= team1 canonico)
```

### collectors/football_api.py — firmas exactas
```python
from shared.config import FOOTBALL_API_KEY, FOOTBALL_RAPID_API_KEY

BASE_URL = "https://api.football-data.org/v4"
RATE_LIMIT_DELAY = 6.5  # segundos entre requests (10 req/min = 1/6s, con margen)
# Implementar: await asyncio.sleep(RATE_LIMIT_DELAY) entre cada llamada HTTP.

# NO definir HEADERS a nivel de modulo — FOOTBALL_API_KEY puede ser None en el momento
# en que se importa el modulo. Construir el header dentro de cada funcion async:
# headers = {"X-Auth-Token": FOOTBALL_API_KEY}
# Si FOOTBALL_API_KEY is None → raise RuntimeError antes de hacer la request.

async def get_upcoming_matches(days: int = 7) -> list[dict]:
    """GET /matches?dateFrom=today&dateTo=today+days. Filtra por SUPPORTED_LEAGUES."""

async def get_team_stats(team_id: int, last_n: int = 10) -> dict:
    """GET /teams/{team_id}/matches?status=FINISHED&limit={last_n}"""

async def get_h2h(team1_id: int, team2_id: int) -> list[dict]:
    """GET /teams/{team1_id}/matches?status=FINISHED&limit=10. Filtra vs team2_id.
    Free tier: max 10 partidos. Parametro years eliminado — no hay suficientes datos."""

async def get_standings(league_id: int) -> list[dict]:
    """GET /competitions/{league_id}/standings"""

async def get_match_result(match_id: str) -> dict | None:
    """GET /matches/{match_id}. Devuelve resultado si FINISHED, None si no."""
```

### collectors/stats_processor.py — firmas exactas
```python
def calculate_form_score(results: list[str]) -> float:
    """
    results: lista de "W","D","L" más reciente primero.
    Ponderación decreciente: posición 0 vale 1.0, posición N vale 1/(N+1).
    W=3pts, D=1pt, L=0pts. Normaliza a 0–100.
    """

def calculate_home_away_split(matches: list[dict], team_id: int) -> tuple[dict, dict]:
    """Separa stats de local vs visitante. Devuelve (home_stats, away_stats)."""

def detect_streak(results: list[str]) -> dict:
    """
    results: más reciente primero.
    Devuelve {"type": "win"|"loss"|"draw", "count": N}
    donde N es la longitud de la racha actual desde el partido más reciente.
    """

def calculate_h2h_advantage(h2h_matches: list[dict], team_id: int) -> float:
    """
    Retorna float en [-1.0, 1.0].
    1.0 = equipo ganó todos. -1.0 = equipo perdió todos. 0.0 = equilibrio.
    """
```

### enrichers/poisson_model.py — firmas exactas
```python
import numpy as np
from scipy.stats import poisson
from scipy.optimize import minimize

# PARÁMETROS DE ARRANQUE (cold start)
# El modelo necesita datos históricos para ajustarse. El día 1 Firestore está vacío.
# Estrategia: precarga de parámetros medios por liga al inicializar.
# Estos valores son medias empíricas de las 5 ligas. Se sobreescriben con datos reales
# en cuanto haya suficientes partidos (mínimo 10 por equipo).
COLD_START_PARAMS = {
    "home_advantage": 0.25,    # ventaja media de jugar en casa
    "default_attack": 1.2,     # goles esperados de ataque medio
    "default_defense": 1.0,    # goles esperados contra defensa media
}
# MIN_MATCHES_TO_FIT importado desde shared.config (= 5)
# No redefinir aquí — usar el valor centralizado

def fit_attack_defense(matches: list[dict]) -> dict:
    """
    Ajusta parámetros de ataque y defensa por equipo usando máxima verosimilitud.
    matches: últimos partidos disponibles (max 10 con free tier) con goals_home, goals_away.
    Si un equipo tiene < MIN_MATCHES_TO_FIT partidos → usa COLD_START_PARAMS.
    Devuelve {team_id: {"attack": float, "defense": float}} + home_advantage global.
    """

def dixon_coles_correction(lambda_home: float, mu_away: float, rho: float = -0.13) -> np.ndarray:
    """
    Corrección Dixon-Coles para scores bajos (0-0, 1-0, 0-1, 1-1).
    rho=-0.13 es el valor estándar empírico.
    Devuelve matriz de corrección 2x2.
    """

def predict_match_probs(home_id: int, away_id: int, team_params: dict) -> dict:
    """
    Calcula distribución bivariada de marcadores hasta 8 goles por equipo.
    Aplica corrección Dixon-Coles.
    Devuelve:
    {
      "home_win": float,   # suma probabilidades donde home_goals > away_goals
      "draw": float,
      "away_win": float,
      "home_xg": float,    # goles esperados local
      "away_xg": float,    # goles esperados visitante
      "score_matrix": list # matriz 9x9 de probabilidades por marcador
    }
    """
```

### enrichers/elo_rating.py — firmas exactas
```python
K_FACTOR = 32        # sensibilidad del sistema ELO a resultados
HOME_ADVANTAGE = 100 # puntos ELO extra para equipo local
DEFAULT_ELO = 1500

def expected_score(elo_a: float, elo_b: float) -> float:
    """Probabilidad esperada de victoria de A contra B según ELO."""

def update_elo(elo_winner: float, elo_loser: float, score: float) -> tuple[float, float]:
    """
    score: 1.0=victoria, 0.5=empate, 0.0=derrota del 'winner'.
    Devuelve (nuevo_elo_a, nuevo_elo_b).
    """

def get_team_elo(team_id: int) -> float:
    """Lee ELO actual de Firestore colección team_elo. Si no existe, devuelve DEFAULT_ELO."""

async def update_all_elos(finished_matches: list[dict]) -> None:
    """
    Procesa partidos terminados en orden cronológico.
    Actualiza Firestore team_elo por cada equipo.
    """

def elo_win_probability(home_id: int, away_id: int) -> float:
    """
    Devuelve prob de victoria local incluyendo HOME_ADVANTAGE.
    Resultado en [0.0, 1.0].
    """
```

### enrichers/data_enricher.py — firmas exactas
```python
async def enrich_match(match: dict) -> dict:
    """
    Orquesta todos los enrichers. Flujo Poisson obligatorio:
    1. Recoger partidos de home_team y away_team de Firestore team_stats
    2. Llamar poisson_model.fit_attack_defense(all_matches) → team_params dict
    3. Llamar poisson_model.predict_match_probs(home_id, away_id, team_params) → probs
    Los team_params de fit_attack_defense se pasan directamente a predict_match_probs
    dentro de esta función — NO se persisten en Firestore (son temporales).
    4. Leer h2h_advantage de Firestore h2h_data donde pair_key == f"{min(home_id,away_id)}_{max(home_id,away_id)}"
       Si home_id > away_id la ventaja almacenada es relativa al away (menor ID).
       En ese caso INVERTIR: h2h_for_home = -stored_h2h_advantage
       Si home_id < away_id usar h2h_advantage tal como esta.
    Input: documento de upcoming_matches.
    Output: enriched_match con todos los campos para value_bet_engine:
    {
      ...match,                        # campos originales
      "poisson_home_win": float,
      "poisson_draw": float,
      "poisson_away_win": float,
      "home_xg": float,
      "away_xg": float,
      "elo_home_win_prob": float,
      "home_elo": float,
      "away_elo": float,
      "home_form_score": float,        # 0-100 de stats_processor
      "away_form_score": float,
      "h2h_advantage": float,          # -1.0 a 1.0
      "home_streak": dict,             # {type, count}
      "away_streak": dict,
      "odds_opening": dict,            # cuotas de apertura
      "odds_current": dict,            # cuotas actuales
      "odds_movement": float,          # variación cuota home desde apertura
      "data_quality": str,              # "full" | "partial"
      "enriched_at": datetime,
    }
    Guarda en Firestore colección enriched_matches.
    """

async def run_enrichment() -> int:
    """
    Identifica partidos sin enriquecer:
    1. Lee todos los upcoming_matches con status == "SCHEDULED"
    2. Para cada match_id, busca si existe doc en enriched_matches
    3. Si NO existe en enriched_matches → llama enrich_match()
    4. Si existe pero enriched_at < now - 6h → re-enriquece (cuotas pueden haber cambiado)
    Devuelve número de partidos enriquecidos en esta ejecución.
    """
```

### analyzers/value_bet_engine.py — firmas exactas
```python
def load_weights() -> dict:
    """Lee doc 'current' de Firestore model_weights. Si no existe, usa DEFAULT_WEIGHTS."""

# Necesita: import numpy as np  (numpy==1.26.0 ya en requirements)

def ensemble_probability(enriched_match: dict, weights: dict) -> dict:
    """
    Combina señales estadísticas con pesos del modelo.
    weights keys: "poisson", "elo", "form", "h2h" — deben coincidir con DEFAULT_WEIGHTS.

    signals:
      poisson = enriched_match["poisson_home_win"]  (o away_win si se analiza visitante)
      elo     = enriched_match["elo_home_win_prob"]
      form    = enriched_match["home_form_score"] / 100  (normalizado 0-1)
      h2h     = (enriched_match["h2h_advantage"] + 1) / 2  (de [-1,1] a [0,1])

    final_prob = sum(signal * weights[key] for key, signal in signals.items())
    confidence = max(0.0, 1 - np.std(list(signals.values())))  # 0.5-1.0 para probs

    Devuelve {
      "prob": float,
      "confidence": float,
      "signals": {"poisson": float, "elo": float, "form": float, "h2h": float}
    }
    """

async def fetch_bookmaker_odds(match_id: str) -> dict | None:
    """
    Cache-first: verificar odds_cache en Firestore antes de llamar a la API.
    1. Buscar doc en odds_cache donde fixture_id == match_id
    2. Si existe Y fetched_at > now - 4h → devolver del cache (SIN llamar API)
    3. Si no existe o expirado → llamar API-Football GET /odds?fixture={match_id}
    4. Guardar resultado en odds_cache: {fixture_id, home_odds, draw_odds, away_odds,
       opening_home_odds (solo si es la primera vez), bookmaker, fetched_at}
    5. Devolver {bookmaker, home_odds, draw_odds, away_odds, opening_home_odds}
    Devuelve None si API falla o no hay odds.
    """

def calculate_edge(prob_calculated: float, decimal_odds: float) -> float:
    """edge = prob_calculated - (1 / decimal_odds)"""

def kelly_criterion(edge: float, decimal_odds: float) -> float:
    """
    Kelly fraction = edge / (decimal_odds - 1)
    Si edge <= 0 devuelve 0.0 (nunca apostar con edge negativo o cero).
    Clampea resultado entre 0.0 y 0.25 (max 25% del bankroll).
    """

async def generate_signal(enriched_match: dict) -> dict | None:
    """
    1. load_weights()
    2. ensemble_probability(enriched_match, weights)
    3. fetch_bookmaker_odds()
    4. calculate_edge()
    5. Si edge > SPORTS_MIN_EDGE AND confidence > SPORTS_MIN_CONFIDENCE:
       - kelly_criterion()
       - Guarda en Firestore predictions
       - Si edge > SPORTS_ALERT_EDGE: POST a {TELEGRAM_BOT_URL}/send-alert con x-cloud-token
         Body: {"type": "sports", "data": prediction_dict}
         Si falla el POST al bot → loggear y continuar (no bloquear el pipeline)
       - Devuelve el documento
    6. Si no cumple thresholds: devuelve None
    """
```

### learner/learning_engine.py — firmas exactas
```python
# Error types mapeados a los 4 signals del ensemble (poisson, elo, form, h2h)
# Usado por evaluate_prediction() para identificar qué signal causó el error
# y por update_weights() para reducir su peso
ERROR_TYPES = [
    "poisson_overweighted",    # el modelo Poisson sobreestimó la probabilidad
    "elo_misleading",          # el rating ELO no reflejaba el estado real del equipo
    "form_misleading",         # la forma reciente era engañosa (lesiones, rotaciones)
    "h2h_irrelevant",          # el historial directo no era relevante para este partido
    "odds_inefficiency",       # la cuota era trampa (bookmaker tenia informacion privilegiada)
]

async def fetch_pending_results() -> list[dict]:
    """
    Query Firestore predictions donde:
    - result == None
    - match_date < now - 24h
    Devuelve lista de predicciones pendientes de evaluar.
    """

async def check_result(match_id: str) -> str | None:
    """
    Llama football_api.get_match_result(match_id).
    Devuelve "HOME_WIN" | "AWAY_WIN" | "DRAW" | None.
    """

def evaluate_prediction(prediction: dict, actual_result: str) -> dict:
    """
    Determina si la predicción fue correcta.
    Clasifica el error_type si fue incorrecta usando los factores del documento.
    Devuelve {"correct": bool, "error_type": str | None}
    """

# Mapa de error_type → clave de weights para saber qué peso reducir
# Solo aplica para deportes con modelo estadistico (data_source='statistical_model')
# Para groq_ai sports (NBA/NFL/etc): no se ajustan pesos, solo se registra el error
ERROR_TO_WEIGHT = {
    "poisson_overweighted": "poisson",
    "elo_misleading":       "elo",
    "form_misleading":      "form",
    "h2h_irrelevant":       "h2h",
    "odds_inefficiency":    None,  # no reduce ningún weight específico
}

def update_weights(
    error_type: str | None,
    top_factor: str,   # clave de weights del signal más determinante: "poisson"|"elo"|"form"|"h2h"
    current_weights: dict,
    correct: bool
) -> dict:
    """
    Si fallo:  weights[ERROR_TO_WEIGHT[error_type]] *= (1 - LEARNING_RATE)
    Si acierto: weights[top_factor] *= (1 + LEARNING_RATE * 0.6)
    Si error_type == None o "odds_inefficiency" → no cambiar pesos (no hay factor culpable claro)
    Normaliza para que sumen 1.0.
    Clampea cada peso entre 0.05 y 0.60.
    Devuelve nuevos pesos.
    """

def calculate_accuracy(predictions: list[dict]) -> float:
    """Devuelve tasa de acierto (0.0–1.0) de la lista dada."""

async def run_daily_learning() -> None:
    """
    1. fetch_pending_results()
    2. Por cada predicción: check_result() → evaluate_prediction() → update_weights()
    3. Llamar elo_rating.update_all_elos(finished_matches) con los partidos verificados
       (OBLIGATORIO — sin esto los ELOs se quedan en DEFAULT_ELO=1500 para siempre)
    4. Actualiza doc 'current' en model_weights con nuevos pesos + nueva versión
    5. Calcula accuracy de la semana actual y compara con accuracy_log de la semana anterior
       para calcular delta (usado en el reporte semanal)
    6. Guarda/actualiza entrada en accuracy_log para la semana actual con:
       {week, predictions_total, predictions_correct, accuracy, accuracy_by_league,
        weights_start, weights_end, prev_week_accuracy (para calcular delta), created_at}
    7. Actualiza prediction en Firestore con result, correct, error_type
    """

# generate_weekly_report() MOVIDA a shared/report_generator.py
# No implementar aquí — importar desde shared en el telegram-bot directamente.
# Razon: telegram-bot no puede importar desde services/sports-agent/ (son servicios distintos).
```

---

## SERVICIO: polymarket-agent

### Requirements
```
fastapi==0.115.0
uvicorn==0.30.0
google-cloud-firestore==2.19.0
httpx==0.27.0
openai==1.51.0
tavily-python==0.5.0
numpy==1.26.0
python-dotenv==1.0.0
websockets==12.0          # WebSocket CLOB en tiempo real
web3==6.20.0              # leer transacciones on-chain Polygon para smart money detection
```

### Flujo de datos polymarket-agent
```
/run-scan          → 202 → scanner + price_tracker → poly_markets + poly_price_history
/run-enrich        → 202 → enrichers + realtime smart_money_analysis → enriched_markets
/run-analyze       → 202 → groq_analyzer + maintenance → poly_predictions
/run-poly-backtest → 202 → backtester/backtest_poly.py → poly_backtest_results (UNA VEZ)
/run-websocket     → 202 → inicia asyncio.create_task(websocket_loop) — loop infinito
                     Monitorea top 20 mercados: orderbook, trades, precios en tiempo real
                     Guarda en Firestore realtime_events (TTL 24h)
⚠️ LIMITACION CLOUD RUN: con min-instances=0 la instancia duerme si no hay trafico.
   El WebSocket se pierde cuando la instancia muere. Recomendacion:
   polymarket-scan.yml (cada 2h) mantiene la instancia viva y reinicia el WS si muere.
   Para señales criticas el sistema usa TAMBIEN REST polling como respaldo.
```
# Todos los endpoints devuelven 202 Accepted inmediatamente (ver nota en sports-agent).

### realtime/websocket_manager.py — firmas exactas
```python
WS_CLOB = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_RTDS  = "wss://ws-live-data.polymarket.com"

async def start_monitoring(top_n_markets: int = 20) -> None:
    # 1. Lee top N mercados de Firestore poly_markets (por volume_24h)
    # 2. Extrae condition_ids
    # 3. Conecta WebSocket y suscribe: {"assets_ids": [...], "type": "market"}
    # 4. Por cada evento recibido:
    #    - "book" → analyze_orderbook_snapshot() → guarda realtime_events
    #    - "price_change" → guarda realtime_events + comprueba anomalias
    #    - "last_trade_price" → detecta large trades → smart_money_detector si > umbral
    # 5. Reconexion automatica con backoff exponencial (1s, 2s, 4s, 8s...)
    # 6. Loop infinito — ejecutar con asyncio.create_task() desde main.py

async def send_ping(ws) -> None:
    # Envia ping cada 30s para mantener la conexion viva
    # Si no se recibe pong → reconectar
```

### realtime/orderflow_analyzer.py — firmas exactas
```python
def analyze_orderbook_snapshot(book_event: dict) -> dict:
    # Calcula: buy_pressure, spread, depth, imbalance_signal
    # buy_pressure = sum(bid_sizes) / (sum(bid_sizes) + sum(ask_sizes))
    # imbalance: "STRONG_BUY">0.70, "BUY">0.60, "NEUTRAL", "SELL"<0.40, "STRONG_SELL"<0.30

def detect_price_velocity(price_history: list[dict], window_minutes: int = 5) -> dict:
    # velocity = (current_price - price_N_ago) / price_N_ago
    # Devuelve {velocity, trend: "ACCELERATING"|"DECELERATING"|"STABLE"}

def detect_large_trade(trade_event: dict, market_avg_trade_usd: float) -> bool:
    # True si trade_size * price > 5x el tamano medio del mercado
```

### realtime/correlation_tracker.py — firmas exactas
```python
COINGECKO_URL = "https://api.coingecko.com/api/v3"
CRYPTO_KEYWORDS = {"bitcoin": "bitcoin", "btc": "bitcoin", "ethereum": "ethereum",
                   "eth": "ethereum", "solana": "solana", "sol": "solana"}

async def get_crypto_price(coin_id: str) -> float | None:
    # GET /simple/price?ids={coin_id}&vs_currencies=usd
    # Cache en memoria 60s para no spammear CoinGecko

def detect_market_leads_asset(
    market_price_now: float, market_price_1h_ago: float,
    asset_price_now: float, asset_price_1h_ago: float
) -> dict:
    # Si el mercado sube y el activo NO ha subido todavia → "market_leads" → posible smart money
    # Devuelve {divergence: bool, direction: "market_leads"|"asset_leads"|"aligned", magnitude: float}

async def enrich_with_correlation(poly_prediction: dict) -> dict:
    # Si el mercado tiene keyword crypto → obtiene precio de CoinGecko y calcula divergencia
    # Anade "asset_correlation" al poly_prediction
```

### realtime/smart_money_detector.py — firmas exactas
```python
POLYGON_RPC = "https://polygon-rpc.com"
POLYGON_RPC_BACKUP = "https://rpc.ankr.com/polygon"

async def profile_wallet(wallet_address: str) -> dict:
    # 1. Busca en Firestore wallet_profiles (cache 24h)
    # 2. Si no hay cache: consulta Polygon RPC con web3.py
    #    ⚠️ web3.py es SINCRONO — usar en contexto async:
    #    loop = asyncio.get_event_loop()
    #    result = await loop.run_in_executor(None, w3.eth.get_transaction_count, address)
    #    - w3.eth.get_transaction_count(address) → total txs
    #    - Estima antiguedad por bloque de primera tx (via Polygonscan API si disponible)
    # 3. Classifica: "fresh" | "whale" | "bot_suspect" | "regular"
    # 4. Guarda en wallet_profiles con TTL 24h

def score_suspicion(wallet_profile: dict, trade: dict, market: dict) -> dict:
    # Calcula un score de 0-100 de actividad sospechosa basado en:
    #   - wallet_profile.is_fresh + trade size grande → +40pts
    #   - niche market (bajo volumen) + gran posicion → +30pts
    #   - patron de timing uniforme (bot) → +20pts
    #   - historial negativo de win rate → -20pts
    # Devuelve {suspicion_score: int, signals: list[str], verdict: "clean"|"suspicious"|"likely_insider"}

async def run_smart_money_analysis(market_id: str) -> dict:
    # 1. Obtiene ultimos 50 trades del mercado via CLOB REST
    #    GET https://clob.polymarket.com/trades?market={condition_id}&limit=50
    # 2. Por cada trade > $1,000: profile_wallet()
    # 3. score_suspicion() para cada wallet
    # 4. Si algun score > 70 → is_smart_money=True
    # 5. Detecta bots: trades de tamano exactamente igual O intervalos regulares
    # Devuelve {is_smart_money, bot_probability, suspicious_wallets, confidence, signals}
```

### scanner.py — firmas exactas
```python
GAMMA_API = "https://gamma-api.polymarket.com"

async def fetch_active_markets(limit: int = 50, min_volume: float = 10000) -> list[dict]:
    # GET /markets?active=true&order=volume24hr&limit={limit}
    # Filtra: volume_24h >= min_volume AND end_date > now + 2 days
    # ⚠️ Guardar campo "conditionId" de la respuesta como "condition_id" en Firestore
    # Sin condition_id el orderbook_analyzer no puede funcionar
    # Guarda en Firestore poly_markets

async def fetch_market_orderbook(condition_id: str) -> dict:
    # ⚠️ El orderbook NO está en gamma-api.polymarket.com
    # URL correcta: https://clob.polymarket.com/order-book/{condition_id}
    # condition_id viene del campo condition_id del mercado en Firestore poly_markets
    # (distinto del market_id — hay que guardarlo en el scanner)
    # Sin auth para lectura pública. Si falla → devuelve buy_ratio=0.5 (neutral, no crashear)
    # Devuelve {bids, asks, spread, buy_ratio}
    # buy_ratio = sum(bid sizes) / (sum(bid sizes) + sum(ask sizes))
```

### price_tracker.py — firmas exactas
```python
async def save_price_snapshot(market_id: str, price_yes: float, price_no: float, volume_24h: float) -> None:
    # Añade a Firestore poly_price_history

async def price_momentum(market_id: str) -> str:
    # Lee snapshots últimas 6h
    # Sube > 3% → "RISING" | Baja > 3% → "FALLING" | Else → "STABLE"

async def volume_spike(market_id: str) -> bool:
    # True si vol_24h_actual > 3 x media_7_días

async def smart_money_detection(market_id: str) -> dict:
    # Detecta smart money usando heurística de velocidad del spike:
    # ⚠️ No compara contra noticias en Firestore (no existe esa colección).
    # Heurística: si el volumen sube > 5x la media en < 1 hora → probable smart money
    # (un movimiento tan rápido suele preceder noticias públicas)
    # Algoritmo:
    #   1. Lee últimos snapshots de poly_price_history para este market_id (últimas 2h)
    #   2. Calcula tasa de crecimiento de volume_24h en esa ventana
    #   3. Si tasa > 5x en < 60 min → is_smart_money=True
    # Devuelve {"is_smart_money": bool, "hours_before_news": None}
    # (hours_before_news siempre None — no tenemos timestamps de noticias)
```

### enrichers/orderbook_analyzer.py — firmas exactas
```python
async def analyze_orderbook(market_id: str) -> dict:
    # 1. Obtener condition_id desde Firestore poly_markets donde market_id == market_id
    #    (NO usar market_id directamente en la URL del CLOB — son IDs distintos)
    # 2. Llamar scanner.fetch_market_orderbook(condition_id)
    # 3. Si condition_id no existe o el fetch falla → devolver buy_pressure=0.5, imbalance_signal="NEUTRAL"
    # Devuelve:
    #   buy_pressure: ratio compradores YES (0.0-1.0)
    #   spread: diferencia bid/ask
    #   depth: volumen total en libro
    #   imbalance_signal: "BULLISH" si buy_ratio > 0.65, "BEARISH" si < 0.35, "NEUTRAL" si no
```

### enrichers/correlation_detector.py — firmas exactas
```python
async def find_correlated_markets(market_id: str, all_markets: list[dict]) -> list[dict]:
    # Detecta mercados correlacionados por keywords
    # Ej: "Biden gana" <-> "Trump gana" son mutuamente excluyentes

def detect_arbitrage(market: dict, correlated: list[dict]) -> dict:
    # Probabilidad = market["price_yes"] y correlated[i]["price_yes"]
    # Si sum(price_yes de mercados mutuamente excluyentes) != 1.0 → ineficiencia
    # inefficiency = abs(1.0 - total_prob)
    # direction = "OVERPRICED" si total > 1, "UNDERPRICED" si total < 1
    # Devuelve {"detected": bool, "inefficiency": float, "direction": str}
    # Clave "detected" (no "arbitrage_detected") — coincidir con enriched_markets.arbitrage schema
```

### enrichers/news_sentiment.py — firmas exactas
```python
SOURCE_WEIGHTS = {
    "reuters.com": 1.0, "apnews.com": 1.0, "bbc.com": 0.9,
    "bloomberg.com": 0.9, "ft.com": 0.8, "default": 0.5,
}

async def fetch_news_sentiment(market_question: str) -> dict:
    # ⚠️ PRESUPUESTO TAVILY: máx 30 búsquedas/día total (free tier = 1,000/mes)
    # 1. Comprobar Firestore tavily_budget {date, calls_today, limit: 30}
    #    Si calls_today >= limit → devolver {"sentiment_score": 0.0, "news_count": 0,
    #    "top_headlines": [], "sentiment_trend": "NO_DATA"} sin llamar Tavily
    # 2. Solo se invoca para top 10 mercados por volumen del día, no para todos
    # 3. Tavily search: max_results=5 (no 10, para conservar presupuesto)
    # 4. Incrementar calls_today en Firestore tras cada llamada exitosa
    # 5. Ponderar resultados por SOURCE_WEIGHTS
    # Devuelve:
    #   sentiment_score: float (-1.0 a 1.0)  → 0.0 si NO_DATA
    #   news_count: int                       → 0 si NO_DATA
    #   top_headlines: list[str] (máx 3)      → [] si NO_DATA
    #   sentiment_trend: "IMPROVING" | "DETERIORATING" | "STABLE" | "NO_DATA"
```

### enrichers/market_enricher.py — firmas exactas
```python
async def enrich_market(market: dict) -> dict:
    # Orquesta todos los enrichers. Guarda en Firestore enriched_markets.
    # Output enriched_market incluye todos los campos de poly_markets más:
    #   price_momentum: str
    #   volume_spike: bool
    #   smart_money: {is_smart_money, hours_before_news}
    #   orderbook: {buy_pressure, spread, depth, imbalance_signal}
    #   correlations: list[dict]
    #   arbitrage: {detected, inefficiency, direction}
    #   news_sentiment: {score, count, headlines, trend}
    #   data_quality: str  # "full" | "partial"
    #   enriched_at: datetime
```

### groq_analyzer.py — firmas exactas
```python
SYSTEM_PROMPT = (
    "Eres un analista experto en mercados de predicción. "
    "Se te proporciona un mercado de Polymarket con datos estadísticos completos: "
    "historial de precios, order book, smart money, correlaciones y sentiment de noticias. "
    "Integra TODOS los datos. Responde SOLO en JSON: "
    '{"real_prob": float, "edge": float, "confidence": float, '
    '"trend": "RISING|FALLING|STABLE", "recommendation": "BUY_YES|BUY_NO|PASS|WATCH", '
    '"key_factors": list[str], "reasoning": string}'
)

async def analyze_market(enriched_market: dict) -> dict | None:
    # Solo analiza si: volume_24h > 5000 AND days_to_close > 2
    # NO usa web_search (news_sentiment ya viene del enricher)
    # Al llamar en batch: await asyncio.sleep(GROQ_CALL_DELAY) entre cada mercado
    # para no exceder 6,000 tokens/min del free tier de Groq
    # Al guardar en poly_predictions copiar desde enriched_market:
    #   poly_prediction["volume_spike"] = enriched_market["volume_spike"]
    #   poly_prediction["smart_money_detected"] = enriched_market["smart_money"]["is_smart_money"]
    # Guarda resultado en Firestore poly_predictions

async def run_maintenance() -> None:
    # Ejecutar al final de cada /run-analyze
    # 1. Borrar poly_price_history donde timestamp < now - 30 días (batch delete)
    # 2. Borrar enriched_markets donde enriched_at < now - 7 días
    # Usar batch writes de Firestore (máx 500 ops/batch) para no exceder límites
```

### alert_engine.py — firmas exactas
```python
async def check_and_alert(analysis: dict) -> bool:
    # Envía alerta Telegram si:
    #   edge > POLY_MIN_EDGE (0.12)
    #   confidence > POLY_MIN_CONFIDENCE (0.65)
    #   volume_spike == True OR smart_money.is_smart_money == True
    # Verifica en alerts_sent que no se haya enviado ya
    # ⚠️ NO usa on_snapshot — llama directamente POST {TELEGRAM_BOT_URL}/send-alert
    #   Body: {"type": "polymarket", "data": analysis}
    #   Header: x-cloud-token
    #   Si falla el POST → loggear y continuar (no bloquear el pipeline)
    # Devuelve True si envió alerta
```

---

## SERVICIO: telegram-bot

### Requirements
```
fastapi==0.115.0
uvicorn==0.30.0
python-telegram-bot==20.7
google-cloud-firestore==2.19.0
httpx==0.27.0
python-dotenv==1.0.0
# fastapi+uvicorn: necesarios para los endpoints /webhook /send-alert /health
# python-telegram-bot: necesario para enviar mensajes via Bot API
```

### main.py — FastAPI webhook
```python
# ⚠️ telegram-bot usa modo WEBHOOK, no polling.
# Polling requiere min-instances=1 (cuesta dinero). Webhook es event-driven: min-instances=0.
# Telegram envía HTTP POST a /webhook cuando hay un mensaje → la instancia se despierta.
# Los agentes llaman POST /send-alert cuando generan señales → bot despierta y envía.

POST /webhook             # recibe updates de Telegram → despacha a handlers.py
POST /send-alert          # recibe señal de sports-agent o polymarket-agent → envía alerta
                          # Body: {"type": "sports"|"polymarket", "data": {…}}
                          # Protegido con x-cloud-token
POST /send-weekly-report  # llamado por weekly-report.yml scheduler (lunes 08:00 UTC)
                          # Llama shared.report_generator.generate_weekly_report() y envía resultado
                          # Protegido con x-cloud-token
GET  /health              # {"status": "ok"}

# Configurar webhook tras deploy (solo una vez):
# curl -X POST "https://api.telegram.org/bot{TOKEN}/setWebhook?url={CLOUD_RUN_URL}/webhook"
# Ver target set-webhook en Makefile
```

### handlers.py — comandos
```
/start   → Mensaje de bienvenida + lista de comandos disponibles
/sports  → Lee predictions Firestore: edge > 0.08, result==None, top 3 por edge DESC
/poly    → Lee poly_predictions Firestore: alerted==True o edge>0.12, top 5 por edge DESC
/stats   → Lee accuracy_log (semana actual) + model_weights doc 'current'
/calc    → Uso: /calc <stake> <back_odds> <lay_odds> <comisión%>
           Calcula qualifying bet. Responde con lay_stake, liability, profit_back, profit_lay, rating.
/help    → Lista todos los comandos con descripción breve
```

### alert_manager.py
```python
# ⚠️ on_snapshot ELIMINADO — incompatible con min-instances=0.
# on_snapshot necesita conexión permanente. Con min=0 la instancia duerme
# y el listener muere silenciosamente. Las alertas nunca llegarían.
#
# NUEVO FLUJO: push desde los agentes al bot.
# sports-agent y polymarket-agent llaman POST /send-alert cuando generan señal.
# El bot solo se despierta cuando recibe esa llamada — compatible con min=0.
#
# El endpoint /send-alert está en main.py del telegram-bot (ver abajo).
# alert_manager.py contiene solo el formateador y el sender de Telegram.

async def send_sports_alert(prediction: dict) -> bool:
    """
    Formatea predicción deportiva y envía a TELEGRAM_CHAT_ID.
    Verifica en alerts_sent que no se haya enviado ya (deduplicación).
    Devuelve True si envió.
    """

async def send_poly_alert(analysis: dict) -> bool:
    """
    Formatea señal de Polymarket y envía a TELEGRAM_CHAT_ID.
    Verifica en alerts_sent. Devuelve True si envió.
    """

async def send_message(text: str) -> None:
    """Envía mensaje raw a TELEGRAM_CHAT_ID via Bot API."""
```

### FORMATO MENSAJES TELEGRAM

**Señal deportiva:**
```
⚽ SEÑAL DETECTADA

🏟 {home_team} vs {away_team}
🏆 {league} | 📅 {match_date}

✅ Apostar a: *{team_to_back}*
💰 Cuota: *{odds}*
📊 Edge: *+{edge:.1%}* | Confianza: *{confidence:.0%}*

Señales del modelo:
• Poisson: {poisson:.0%} | ELO: {elo:.0%}
• Forma: {form:.0%} | H2H: {h2h:.0%}

🧮 Kelly sugerido: {kelly_fraction:.1%} del bankroll

⚠️ Apuesta responsablemente. No es asesoramiento financiero.
```

**Señal Polymarket:**
```
🔮 OPORTUNIDAD POLYMARKET

❓ {question}

📈 Precio mercado YES: *{market_price_yes:.0%}*
🎯 Probabilidad estimada: *{real_prob:.0%}*
💎 Edge: *+{edge:.0%}* | Confianza: *{confidence:.0%}*

Recomendación: *{recommendation}*
{'🐋 SMART MONEY detectado' if volume_spike else ''}

💭 {reasoning}

⚠️ Apuesta responsablemente. No es asesoramiento financiero.
```

**Reporte semanal:**
```
📈 REPORTE SEMANAL — Semana {week}

🏟 Sports Agent:
  Predicciones: {total} | Correctas: {correct} | Accuracy: {accuracy:.1%}
  Variación vs semana anterior: {delta:+.1%}

⚙️ Ajustes de pesos aplicados:
  poisson: {w_before:.2f} → {w_after:.2f} {arrow}
  elo: ...
  form: ...
  h2h: ...

🏆 Mejor señal: {best_match} (edge {best_edge:+.1%}, {best_result})
❌ Peor señal: {worst_match} (edge {worst_edge:+.1%}, error: {worst_error})

🔮 Polymarket:
  Mercados analizados: {poly_total} | Alertas: {poly_alerts}
  Edge medio detectado: +{poly_avg_edge:.1%}
```

---

## SERVICIO: dashboard

### Requirements
```
fastapi==0.115.0
uvicorn==0.30.0
google-cloud-firestore==2.19.0
openai==1.51.0
tavily-python==0.5.0
python-dotenv==1.0.0
httpx==0.27.0
# anthropic eliminado — dashboard usa groq_client.py (openai SDK compatible con Groq)
```

### Basic Auth (middleware en main.py)
```python
# Protege todas las rutas excepto /health
# Credentials: DASHBOARD_USER / DASHBOARD_PASS desde env vars
# Usa fastapi.security.HTTPBasic
```

### API Endpoints — contratos exactos

**GET /api/predictions**
```json
// Response: lista últimas 20 predicciones ordenadas por created_at DESC
[{
  "match_id": "str",
  "home_team": "str",
  "away_team": "str",
  "league": "str",
  "match_date": "ISO",
  "team_to_back": "str",
  "odds": 2.4,
  "edge": 0.095,
  "confidence": 0.71,
  "factors": {"poisson": 0.72, "elo": 0.60, "form": 0.65, "h2h": 0.50},
  "result": null,
  "correct": null
}]
```

**GET /api/poly**
```json
// Response: top 20 poly_predictions ordenados por edge DESC
[{
  "market_id": "str",
  "question": "str",
  "market_price_yes": 0.45,
  "real_prob": 0.62,
  "edge": 0.17,
  "confidence": 0.78,
  "trend": "RISING",
  "recommendation": "BUY_YES",
  "volume_spike": true,
  "analyzed_at": "ISO"
}]
```

**GET /api/stats**
```json
{
  "accuracy_global": 0.71,
  "accuracy_by_league": {"PL": 0.68, "PD": 0.74},
  "weights": {"poisson": 0.41, "elo": 0.25, "form": 0.20, "h2h": 0.14},
  "weights_history": [{"week": "2025-W13", "weights": {...}}, ...],
  "total_predictions": 47,
  "correct_predictions": 33
}
```

**POST /api/calc**
```json
// Request:
{"type": "qualifying|free_bet_snr|free_bet_sr", "back_stake": 10, "back_odds": 3.5, "lay_odds": 3.6, "commission": 0.05}
// Response:
{"lay_stake": 9.47, "liability": 24.62, "profit_back": 1.23, "profit_lay": -1.23, "rating": -12.3, "steps": ["Hacer back de €10 a 3.5 en la casa", "Hacer lay de €9.47 a 3.6 en el exchange", "Responsabilidad: €24.62"]}
```

**POST /api/find-odds**
```json
// ⚠️ LIMITACIÓN CONOCIDA: Un LLM buscando cuotas via Tavily devolverá resultados
// aproximados, posiblemente desactualizados. Las casas bloquean scrapers activamente.
// Usar SOLO como orientación. El usuario debe verificar cuotas reales en las casas.
// Añadir en la respuesta: "warning": "Cuotas orientativas. Verifica en la casa antes de apostar."
//
// Request: {"event": "Real Madrid vs Barcelona"}
// Backend: groq_client.analyze() con web_search=True (Tavily)
// Response:
{
  "event": "str",
  "odds": [{"bookmaker": "Bet365", "home": 2.1, "draw": 3.4, "away": 3.2}],
  "best_back": {"bookmaker": "str", "odds": 2.1},
  "best_lay": {"bookmaker": "str", "odds": 2.0},
  "warning": "Cuotas orientativas — verifica antes de apostar",
  "fetched_at": "ISO"
}
```

**POST /api/fetch-offers**
```json
// Backend: llama groq_client.analyze() con web_search=True para buscar ofertas vigentes en casas españolas
// Response:
[{"bookmaker": "Bet365", "bonus": "Bono bienvenida", "amount": 100, "type": "welcome", "requirement": "Depósito mínimo €10", "rating": 4, "status": "activo", "advice": "Usar para qualifying con Betfair Exchange"}]
```

**POST /api/save-bet**
```json
// Request: mismo schema que colección bets (sin id, created_at, updated_at)
// Response: {"id": "firestore_doc_id", "status": "saved"}
```

**GET /api/bets**
```json
// Response: lista apuestas ordenadas por created_at DESC
// Incluye pnl calculado según status
```

**PUT /api/bets/{id}**
```json
// Request: {"status": "ganado_back|ganado_lay|cancelado"}
// Calcula y actualiza pnl según status
// Response: documento actualizado
```

---

## FÓRMULAS CALCULADORA MATCHED BETTING

```python
def calc_qualifying(back_stake, back_odds, lay_odds, commission):
    lay_stake = (back_stake * back_odds) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission) - back_stake
    rating = ((profit_back + profit_lay) / 2 / back_stake) * 100
    return lay_stake, liability, profit_back, profit_lay, rating

def calc_free_bet_snr(back_stake, back_odds, lay_odds, commission):
    lay_stake = (back_stake * (back_odds - 1)) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission)
    rating = (profit_lay / back_stake) * 100
    return lay_stake, liability, profit_back, profit_lay, rating

def calc_free_bet_sr(back_stake, back_odds, lay_odds, commission):
    lay_stake = (back_stake * back_odds) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    profit_back = back_stake * back_odds - lay_stake * (lay_odds - 1)
    profit_lay = lay_stake * (1 - commission)
    rating = (profit_lay / back_stake) * 100
    return lay_stake, liability, profit_back, profit_lay, rating
```

> `commission` se pasa como decimal: Betfair 5% → 0.05

---

## CLOUD RUN — CONFIGURACIÓN

| Servicio          | min-instances | max-instances | Memory | CPU | Timeout |
|-------------------|--------------|---------------|--------|-----|---------|
| sports-agent      | 0            | 1             | 512Mi  | 1   | 900s    |
| polymarket-agent  | 0            | 1             | 256Mi  | 1   | 300s    |
| telegram-bot      | 0            | 1             | 256Mi  | 1   | 60s     |
| dashboard         | 0            | 1             | 512Mi  | 1   | 60s     |

> ✅ TODOS en min-instances=0 → 100% dentro del free tier de Cloud Run.
> Free tier: 180,000 vCPU-seg/mes. Con min=0 solo se consume cuando hay requests activas.
> min=1 en cualquier servicio = ~2.6M vCPU-seg/mes = FUERA del free tier = CUESTA DINERO.
>
> ⚠️ telegram-bot usa modo WEBHOOK (no polling). Telegram envía HTTP POST al bot cuando
> llega un mensaje → el servicio se despierta solo cuando hay actividad. Con polling
> necesitaría min=1 (siempre activo) lo que cuesta dinero.
> Configurar webhook en Telegram: POST https://api.telegram.org/bot{TOKEN}/setWebhook
> con url={CLOUD_RUN_URL}/webhook. Hacer esto en setup.sh tras el primer deploy.
>
> ⚠️ sports-agent /run-collect puede tardar hasta 15 min en background.
> Cloud Run timeout=900s para el proceso en background.
> El curl de GitHub Actions tiene --max-time 30 (solo espera el 202 inmediato).
> Cloud Run sigue procesando en background después de devolver 202.

---

## GITHUB ACTIONS — SCHEDULERS

Cada workflow hace un POST autenticado a Cloud Run.
Las URLs y el token se guardan como Repository Secrets en GitHub.

> ⚠️ Los endpoints de Cloud Run son URLs públicas por defecto. Sin autenticación,
> cualquiera puede dispararlos. Se protegen con un header secreto validado por el servicio.

Añadir en cada servicio FastAPI (main.py):
```python
from fastapi import Header, HTTPException
import os

CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")

def verify_token(x_cloud_token: str = Header(...)):
    if x_cloud_token != CLOUD_RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
```
Añadir `Depends(verify_token)` a todos los endpoints /run-* y /send-*.
NO añadir a /health ni /webhook — /health es monitoreo publico, /webhook lo llama Telegram directamente.

| Workflow file               | Cron (UTC)        | Secret URL            | Endpoint             |
|-----------------------------|-------------------|-----------------------|----------------------|
| `sports-collect.yml`        | `0 */6 * * *`     | `SPORTS_AGENT_URL`    | POST /run-collect    |
| `sports-enrich.yml`         | `30 */6 * * *`    | `SPORTS_AGENT_URL`    | POST /run-enrich     |
| `sports-analyze.yml`        | `0 1,7,13,19 * * *` | `SPORTS_AGENT_URL`  | POST /run-analyze    |
| `learning-engine.yml`       | `0 2 * * *`       | `SPORTS_AGENT_URL`    | POST /run-learning   |
| `polymarket-scan.yml`       | `0 */2 * * *`     | `POLY_AGENT_URL`      | POST /run-scan       |
| `polymarket-enrich.yml`     | `40 */2 * * *`    | `POLY_AGENT_URL`      | POST /run-enrich     |
| `polymarket-analyze.yml`    | `0 */6 * * *`     | `POLY_AGENT_URL`      | POST /run-analyze    |
| `weekly-report.yml`         | `0 8 * * 1`       | `TELEGRAM_BOT_URL`    | POST /send-weekly-report |

> ⚠️ RACE CONDITIONS: GitHub Actions no garantiza que collect termine antes de enrich.
> Los offsets son: collect→enrich = 30min, enrich→analyze = 30min (1h total de margen).
> Defensa adicional: cada endpoint /run-enrich y /run-analyze comprueba que los datos
> de la fase anterior son recientes (< 2h). Si no → loggear advertencia y salir sin error.
> Crons en UTC. `0 2 * * *` UTC = `03:00 Europe/Madrid` en invierno.

**Template de workflow (igual para todos, cambia URL y endpoint):**
```yaml
name: sports-collect
on:
  schedule:
    - cron: '0 */6 * * *'
  workflow_dispatch:        # permite lanzarlo manualmente también

jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Cloud Run
        run: |
          # Todos los endpoints devuelven 202 Accepted inmediatamente (async).
          # --max-time 30 es suficiente — solo espera la confirmación 202, no el trabajo.
          curl -X POST "${{ secrets.SPORTS_AGENT_URL }}/run-collect" \
            -H "Content-Type: application/json" \
            -H "x-cloud-token: ${{ secrets.CLOUD_RUN_TOKEN }}" \
            --max-time 30 \
            --retry 3 \
            --retry-delay 5 \
            --fail --silent --show-error
```

### deploy.yml — CI/CD completo
```yaml
name: deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Auth GCP
        uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}

      - name: Setup gcloud
        uses: google-github-actions/setup-gcloud@v2

      - name: Copy shared/ into each service
        run: |
          for svc in sports-agent polymarket-agent telegram-bot dashboard; do
            cp -r shared/ services/$svc/shared/
          done

      - name: Deploy all services
        env:
          PROJECT: prediction-intelligence
          REGION: europe-west1
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          TAVILY_API_KEY: ${{ secrets.TAVILY_API_KEY }}
          FOOTBALL_API_KEY: ${{ secrets.FOOTBALL_API_KEY }}
          FOOTBALL_RAPID_API_KEY: ${{ secrets.FOOTBALL_RAPID_API_KEY }}
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          TELEGRAM_BOT_URL: ${{ secrets.TELEGRAM_BOT_URL }}
          DASHBOARD_USER: ${{ secrets.DASHBOARD_USER }}
          DASHBOARD_PASS: ${{ secrets.DASHBOARD_PASS }}
          CLOUD_RUN_TOKEN: ${{ secrets.CLOUD_RUN_TOKEN }}
          FIRESTORE_COLLECTION_PREFIX: ${{ secrets.FIRESTORE_COLLECTION_PREFIX }}
          COINGECKO_API_KEY: ${{ secrets.COINGECKO_API_KEY }}
        run: |
          gcloud run deploy sports-agent \
            --source services/sports-agent --project $PROJECT --region $REGION \
            --allow-unauthenticated --timeout=900 --min-instances=0 --memory=512Mi --cpu=1 \
            --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,FOOTBALL_API_KEY=$FOOTBALL_API_KEY,FOOTBALL_RAPID_API_KEY=$FOOTBALL_RAPID_API_KEY,CLOUD_RUN_TOKEN=$CLOUD_RUN_TOKEN,TELEGRAM_BOT_URL=$TELEGRAM_BOT_URL,FIRESTORE_COLLECTION_PREFIX=$FIRESTORE_COLLECTION_PREFIX"

          gcloud run deploy polymarket-agent \
            --source services/polymarket-agent --project $PROJECT --region $REGION \
            --allow-unauthenticated --timeout=300 --min-instances=0 --memory=256Mi --cpu=1 \
            --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,GROQ_API_KEY=$GROQ_API_KEY,TAVILY_API_KEY=$TAVILY_API_KEY,COINGECKO_API_KEY=$COINGECKO_API_KEY,CLOUD_RUN_TOKEN=$CLOUD_RUN_TOKEN,TELEGRAM_BOT_URL=$TELEGRAM_BOT_URL,FIRESTORE_COLLECTION_PREFIX=$FIRESTORE_COLLECTION_PREFIX"

          gcloud run deploy telegram-bot \
            --source services/telegram-bot --project $PROJECT --region $REGION \
            --allow-unauthenticated --timeout=60 --min-instances=0 --memory=256Mi --cpu=1 \
            --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,TELEGRAM_TOKEN=$TELEGRAM_TOKEN,TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID,CLOUD_RUN_TOKEN=$CLOUD_RUN_TOKEN,FIRESTORE_COLLECTION_PREFIX=$FIRESTORE_COLLECTION_PREFIX"

          gcloud run deploy dashboard \
            --source services/dashboard --project $PROJECT --region $REGION \
            --allow-unauthenticated --timeout=60 --min-instances=0 --memory=512Mi --cpu=1 \
            --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,GROQ_API_KEY=$GROQ_API_KEY,TAVILY_API_KEY=$TAVILY_API_KEY,DASHBOARD_USER=$DASHBOARD_USER,DASHBOARD_PASS=$DASHBOARD_PASS,CLOUD_RUN_TOKEN=$CLOUD_RUN_TOKEN,FIRESTORE_COLLECTION_PREFIX=$FIRESTORE_COLLECTION_PREFIX" 

      - name: Deploy Firestore security rules
        run: |
          # npx firebase-tools evita instalar globalmente (mas rapido en CI)
          # Autenticacion: google-github-actions/auth@v2 ya configuro Application Default Credentials
          npx --yes firebase-tools deploy --only firestore:rules --project $PROJECT --non-interactive
        env:
          PROJECT: prediction-intelligence

      - name: Cleanup shared/ copies
        if: always()
        run: |
          for svc in sports-agent polymarket-agent telegram-bot dashboard; do
            rm -rf services/$svc/shared/
          done
```

> ⚠️ Crear service account `github-deployer` con estos 5 roles exactos (todos gratuitos):
> ```bash
> gcloud iam service-accounts create github-deployer \
>   --display-name="GitHub Actions Deployer" \
>   --project=prediction-intelligence
>
> for ROLE in roles/run.developer roles/iam.serviceAccountUser roles/storage.admin roles/cloudbuild.builds.editor roles/firebase.admin; do
>   gcloud projects add-iam-policy-binding prediction-intelligence \
>     --member="serviceAccount:github-deployer@prediction-intelligence.iam.gserviceaccount.com" \
>     --role=$ROLE
> done
>
> # Exportar JSON de credenciales → pegar en GitHub Secret GCP_SA_KEY:
> gcloud iam service-accounts keys create gcp-sa-key.json \
>   --iam-account=github-deployer@prediction-intelligence.iam.gserviceaccount.com
> cat gcp-sa-key.json   # copiar este contenido como valor del secret GCP_SA_KEY
> rm gcp-sa-key.json    # borrar inmediatamente — no commitear
> ```
> Los 5 roles son necesarios:
> - `run.developer` → deployar Cloud Run
> - `iam.serviceAccountUser` → actuar como service account de Cloud Run
> - `storage.admin` → Cloud Build necesita GCS para artefactos de build
> - `cloudbuild.builds.editor` → lanzar builds
> - `firebase.admin` → deployar Firestore security rules via firebase-tools

---

## APIS EXTERNAS — ENDPOINTS EXACTOS

### football-data.org (SOLO FUTBOL — modelo estadistico completo)
```
Base: https://api.football-data.org/v4
Auth: X-Auth-Token: FOOTBALL_API_KEY
Rate: 10 req/min
Deportes: Solo futbol europeo (4 ligas)
Uso: estadisticas profundas → modelo Poisson + ELO
```

### API-Sports multi-deporte (MISMO KEY que futbol — FOOTBALL_RAPID_API_KEY)
```
Base: ver API_SPORTS_HOSTS dict (por deporte)
Auth: X-RapidAPI-Key: FOOTBALL_RAPID_API_KEY
Rate: free tier generoso (no documentado exactamente, ~60 req/min)
Deportes: NBA, NFL, MLB, NHL, EPL, WNBA, MMA, Champions League, La Liga, Serie A, Bundesliga, Ligue 1
Endpoints clave:
  GET /{sport}/v1/games?dates[]={date}          → partidos del dia
  GET /{sport}/v1/games/{id}/stats              → stats por jugador/equipo
  GET /{sport}/v1/standings?season={year}       → clasificacion
  GET /{sport}/v1/injuries                      → lesiones (NBA/NFL)
  GET /{sport}/v2/odds                          → cuotas en tiempo real (2025+)
Sin SDK adicional — httpx igual que football_api.py
```

### API-Sports / API-Football (MULTI-DEPORTE — cuotas y stats adicionales)
```
Base: https://api-sports.io (o por deporte: v3.football.api-sports.io, etc.)
Auth: x-apisports-key: FOOTBALL_RAPID_API_KEY
Rate: 100 req/dia (free tier) — budget compartido con futbol, usar con cache agresivo
Deportes: futbol, NBA, NFL, baseball, handball, hockey, MMA, volleyball, rugby
Uso: estadisticas + odds para todos los deportes no-futbol
```

### CoinGecko API (CRYPTO — correlacion para mercados Polymarket)
```
Base: https://api.coingecko.com/api/v3
Auth: sin auth (free) o x-cg-demo-api-key: COINGECKO_API_KEY (mejora rate limit)
Rate: 10-50 req/min sin auth, mas con key gratuita
Uso: precio spot en tiempo real de BTC/ETH/etc. para correlacionar con mercados Polymarket
GET /simple/price?ids=bitcoin&vs_currencies=usd   → precio actual
GET /coins/{id}/market_chart?vs_currency=usd&days=1&interval=hourly → historico reciente
```

### Polygon RPC (ON-CHAIN — wallet tracking Polymarket)
```
URL publica gratuita: https://polygon-rpc.com
Uso: leer transacciones on-chain de Polymarket para tracking de wallets
Sin API key requerida (aunque puede tener rate limits en picos)
Alternativa gratuita: https://rpc.ankr.com/polygon
```

### Goldsky Subgraph (POLYMARKET — datos historicos on-chain)
```
Subgraph de posiciones: https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/positions/prod/gn
Sin auth, GraphQL queries
Uso: historial completo de posiciones por wallet, PnL on-chain
```

> ⚠️ LIMITACIONES DEL FREE TIER — afectan directamente al diseño:
> 1. `GET /teams/{id}/matches` devuelve máximo **10 partidos**, no 38.
>    El Poisson model usa los últimos 10 partidos, no 38. Ajustar MIN_MATCHES_TO_FIT = 5.
> 2. **No hay endpoint H2H directo.** Implementar H2H manual:
>    Obtener matches del equipo local, filtrar por oponente en los resultados. Máximo 10.
> 3. **Champions League (CL) NO está incluida en el free tier.**
>    Eliminar CL de SUPPORTED_LEAGUES. Solo: PL, PD, BL1, SA.
> 4. La llamada `GET /matches/{id}` para resultados SÍ funciona en free tier.

```
GET /matches?dateFrom={YYYY-MM-DD}&dateTo={YYYY-MM-DD}&competitions={PL,PD,BL1,SA}
GET /teams/{id}/matches?status=FINISHED&limit=10
GET /competitions/{id}/standings
GET /matches/{id}
```

### API-Football (RapidAPI)
```
Base: https://api-football-v1.p.rapidapi.com/v3
Auth: X-RapidAPI-Key + X-RapidAPI-Host headers
Rate: 100 req/día (free tier) — LÍMITE CRÍTICO
```

> ⚠️ Con 4 runs/día y ~10 partidos/run = ~40 llamadas de odds/día. Cerca del límite.
> Estrategia obligatoria: cachear en Firestore colección `odds_cache` (ver schema).
> TTL del caché: 4 horas. Antes de llamar la API comprobar fetched_at > now - 4h.
> Al primer fetch: guardar opening_home/draw/away_odds además de home/draw/away_odds.
> En refrescos: actualizar solo home/draw/away_odds y fetched_at. NO sobreescribir opening_*.

```
GET /odds?fixture={fixture_id}
```

### Polymarket Gamma
```
Base: https://gamma-api.polymarket.com
Sin auth

GET /markets?active=true&order=volume24hr&limit=50
```

---

## DOCKERFILES

> ⚠️ Regla global: el CI copia `shared/` dentro de cada servicio antes de buildear.
> `cp -r shared/ services/{service-name}/shared/`
> El `PYTHONPATH=/app` garantiza que `from shared.config import ...` funcione en Cloud Run.

### Dockerfile — sports-agent, polymarket-agent, telegram-bot (Python puro)
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# shared/ ya está dentro del servicio (copiado por CI/Makefile antes del build)
# COPY . . lo incluye — NO añadir COPY shared/ explícito (sería redundante y confuso)
COPY . .
ENV PYTHONPATH=/app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Dockerfile — dashboard (multi-stage: Node.js + Python)
```dockerfile
# ⚠️ El dashboard tiene frontend React+Vite. Necesita Node.js para compilar.
# Sin multi-stage el build falla — no hay Node en python:3.12-slim.

# Stage 1: compilar React
FROM node:20-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build
# Resultado en /frontend/dist

# Stage 2: servidor Python con el build estático
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# shared/ ya está dentro del servicio (copiado por CI antes del build)
COPY . .
COPY --from=frontend-builder /frontend/dist /app/static
ENV PYTHONPATH=/app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## MAKEFILE

```makefile
PROJECT=prediction-intelligence
REGION=europe-west1

# ⚠️ OBLIGATORIO: copiar shared/ en cada servicio antes de deployar.
# gcloud run deploy --source usa el directorio como build context.
# Sin este paso el build falla porque shared/ no está dentro del servicio.

# ENV_VARS: exportar antes de deployar, ej: export TELEGRAM_TOKEN=xxx
# O crear un archivo .env y ejecutar: export $(cat .env | xargs) antes del make

deploy-sports:
	cp -r shared/ services/sports-agent/shared/
	gcloud run deploy sports-agent 		--source services/sports-agent 		--project $(PROJECT) --region $(REGION) 		--allow-unauthenticated 		--timeout=900 		--min-instances=0 		--memory=512Mi --cpu=1 		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),FOOTBALL_API_KEY=$(FOOTBALL_API_KEY),FOOTBALL_RAPID_API_KEY=$(FOOTBALL_RAPID_API_KEY),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),TELEGRAM_BOT_URL=$(TELEGRAM_BOT_URL),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/sports-agent/shared/

deploy-poly:
	cp -r shared/ services/polymarket-agent/shared/
	gcloud run deploy polymarket-agent 		--source services/polymarket-agent 		--project $(PROJECT) --region $(REGION) 		--allow-unauthenticated 		--timeout=300 		--min-instances=0 		--memory=256Mi --cpu=1 		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),GROQ_API_KEY=$(GROQ_API_KEY),TAVILY_API_KEY=$(TAVILY_API_KEY),COINGECKO_API_KEY=$(COINGECKO_API_KEY),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),TELEGRAM_BOT_URL=$(TELEGRAM_BOT_URL),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/polymarket-agent/shared/

deploy-bot:
	cp -r shared/ services/telegram-bot/shared/
	gcloud run deploy telegram-bot 		--source services/telegram-bot 		--project $(PROJECT) --region $(REGION) 		--allow-unauthenticated 		--timeout=60 		--min-instances=0 		--memory=256Mi --cpu=1 		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),TELEGRAM_TOKEN=$(TELEGRAM_TOKEN),TELEGRAM_CHAT_ID=$(TELEGRAM_CHAT_ID),GROQ_API_KEY=$(GROQ_API_KEY),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/telegram-bot/shared/

deploy-dashboard:
	cp -r shared/ services/dashboard/shared/
	gcloud run deploy dashboard 		--source services/dashboard 		--project $(PROJECT) --region $(REGION) 		--allow-unauthenticated 		--timeout=60 		--min-instances=0 		--memory=512Mi --cpu=1 		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),GROQ_API_KEY=$(GROQ_API_KEY),TAVILY_API_KEY=$(TAVILY_API_KEY),DASHBOARD_USER=$(DASHBOARD_USER),DASHBOARD_PASS=$(DASHBOARD_PASS),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/dashboard/shared/

# Tras deploy-bot, configurar webhook de Telegram:
set-webhook:
	curl -X POST "https://api.telegram.org/bot$(TELEGRAM_TOKEN)/setWebhook" 		-d "url=$(shell gcloud run services describe telegram-bot --project=$(PROJECT) --region=$(REGION) --format='value(status.url)')/webhook" 

# ⚠️ PRIMER DEPLOY: ejecutar en este orden para evitar chicken-and-egg con TELEGRAM_BOT_URL:
# 1. make deploy-bot
# 2. Obtener URL: gcloud run services describe telegram-bot --format='value(status.url)' --region=europe-west1
# 3. Exportar: export TELEGRAM_BOT_URL=<url-del-paso-2>
# 4. Añadir TELEGRAM_BOT_URL como GitHub Secret
# 5. make deploy-sports deploy-poly deploy-dashboard
# 6. make set-webhook
#
# Redeploys posteriores (deploy-all) ya funcionan porque TELEGRAM_BOT_URL está en los secrets.
deploy-all: deploy-bot deploy-sports deploy-poly deploy-dashboard

build-frontend:
	cd services/dashboard/frontend && npm install && npm run build
```

---

## FIRESTORE SECURITY RULES

> ⚠️ Firestore recién creado abre en modo test: CUALQUIERA que conozca el project ID
> puede leer y escribir todos los datos. Inaceptable para producción.
> Crear el archivo `firestore.rules` en la raíz del repo con este contenido:

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {

    // Solo permite acceso desde las service accounts de Cloud Run
    // y desde el SDK autenticado con Application Default Credentials (ADC).
    // Bloquea todo acceso externo no autenticado.
    match /{document=**} {
      allow read, write: if request.auth != null;
    }
  }
}
```

Desplegar las reglas con Firebase CLI (gratis):
```bash
# Instalar Firebase CLI (una sola vez):
npm install -g firebase-tools  # o: npx --yes firebase-tools (evita instalación global)

# Inicializar en la raíz del proyecto (una sola vez):
firebase login
firebase init firestore   # seleccionar proyecto prediction-intelligence
                          # cuando pregunte por rules file → escribir: firestore.rules

# Deployar rules:
firebase deploy --only firestore:rules --project prediction-intelligence
```

> ⚠️ `gcloud firestore deploy` NO existe — ese comando es incorrecto.
> El correcto es `firebase deploy --only firestore:rules`.
> Añadir este comando al setup.sh y al deploy.yml.

Los servicios en Cloud Run usan ADC automáticamente — no necesitan credenciales explícitas
siempre que el service account tenga rol `roles/datastore.user`.
```bash
# Conceder acceso Firestore al Compute Engine default service account (usado por Cloud Run por defecto).
# El email sigue el patron: PROJECT_NUMBER-compute@developer.gserviceaccount.com
# ⚠️ El filtro 'displayName:Default compute' es poco fiable. Usar el patron directamente:
PROJECT_NUMBER=$(gcloud projects describe prediction-intelligence --format='value(projectNumber)')
gcloud projects add-iam-policy-binding prediction-intelligence \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/datastore.user"
```

---

## .gitignore

```gitignore
# Copias temporales de shared/ generadas por el Makefile durante deploy
services/*/shared/

# Entorno Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/

# Variables de entorno — NUNCA commitear valores reales
.env

# Frontend
services/dashboard/frontend/node_modules/
services/dashboard/frontend/dist/

# GCP
.gcloud/
gcloud-service-key.json
```

---

## ESTADO ACTUAL DEL REPO

```
[ ] Sesión 1: Scaffolding — shared/ + esqueletos 4 servicios + Dockerfiles + Makefile
[ ] Sesión 2: sports-agent collectors/ — football_api + odds_movement + stats_processor (con xG proxy)
[ ] Sesión 3: sports-agent enrichers/ — poisson_model + elo_rating + data_enricher
[ ] Sesión 4: sports-agent analyzers/ + learner/ — value_bet_engine + learning_engine
[ ] Sesión 5: polymarket-agent completo — scanner + price_tracker + todos los enrichers + groq_analyzer + alert_engine
[ ] Sesión 6: telegram-bot completo — handlers + alert_manager
[ ] Sesión 7: dashboard completo — FastAPI API + React frontend 4 secciones
[ ] Sesión 8: infra/ completo — cloudbuild + GitHub Actions + setup.sh
```

> Actualiza este bloque al completar cada sesión.
