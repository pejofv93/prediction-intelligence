import os

GOOGLE_CLOUD_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]

# Variables opcionales segun el servicio — usar .get() para evitar KeyError al arrancar.
# Cada servicio solo recibe las vars que necesita en --set-env-vars.
# Si una var no esta presente → None. El servicio debe validar antes de usarla.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")        # solo telegram-bot
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")    # solo telegram-bot
TELEGRAM_SPORTS_THREAD_ID = int(os.environ.get("TELEGRAM_SPORTS_THREAD_ID", "4"))   # topic Sports
TELEGRAM_POLY_THREAD_ID   = int(os.environ.get("TELEGRAM_POLY_THREAD_ID",   "3"))   # topic Polymarket
TELEGRAM_DAILY_THREAD_ID  = int(os.environ.get("TELEGRAM_DAILY_THREAD_ID",  "4"))   # topic Daily Report
TELEGRAM_TRENDS_THREAD_ID = int(os.environ.get("TELEGRAM_TRENDS_THREAD_ID", "4"))   # topic Tendencias
TELEGRAM_BOT_URL = os.environ.get("TELEGRAM_BOT_URL")    # sports-agent + polymarket-agent
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")    # solo sports-agent
FOOTBALL_RAPID_API_KEY = os.environ.get("FOOTBALL_RAPID_API_KEY")  # solo sports-agent
# BALLDONTLIE_API_KEY no necesaria — usar FOOTBALL_RAPID_API_KEY para todos los deportes via API-Sports
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")            # polymarket-agent (opcional)
DASHBOARD_USER = os.environ.get("DASHBOARD_USER")        # solo dashboard
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS")        # solo dashboard
CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")  # token inter-servicios
ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")      # The Odds API — secundaria (500/mes)
ODDSPAPI_KEY   = os.environ.get("ODDSPAPI_KEY", "")      # OddsPapi — terciaria (250/mes)
ODDSAPIIO_KEY  = os.environ.get("ODDSAPIIO_KEY", "")     # odds-api.io — primaria (5000 req/h)
OPTIC_ODDS_KEY = os.environ.get("OPTIC_ODDS_KEY", "")    # Optic Odds — cuaternaria (1000/mes)
COLLECTION_PREFIX = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "")

# IA
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_MODEL_ROTATION = [
    "llama-3.3-70b-versatile",   # principal
    "llama3-70b-8192",           # fallback 1
    "gemma2-9b-it",              # fallback 2
    "llama-3.1-8b-instant",      # fallback 3
]
GROQ_FALLBACK_MODEL = GROQ_MODEL_ROTATION[1]  # backward compat
GROQ_BASE_URL = "https://api.groq.com/openai/v1"  # compatible con openai SDK

# Thresholds
SPORTS_MIN_EDGE = 0.08
SPORTS_MIN_CONFIDENCE = 0.65
SPORTS_ALERT_EDGE = 0.08

# Filtro de divergencia modelo-mercado (h2h/moneyline) — único valor para los 3 deportes.
# Divergencia = prob del modelo - prob implícita (1/odds). Cuando el modelo se separa del
# mercado más de este umbral, el edge está inflado (underdog sobreestimado tipo McDonald,
# Virtanen; o favorito sobreconfiado tipo Valencia ACB) y la señal pierde. Espejo de
# _BUY_YES_MIN_MP_YES en Polymarket. 0.10 conservador, no sobreajustado a la banda rentable.
# Alcance: SOLO h2h/moneyline. NO aplicar a spread/totales/hándicap (allí la divergencia
# mide otra cosa y sobre-filtraría edges legítimos).
SPORTS_MAX_DIVERGENCE = 0.10

# Frontera favorito/underdog para el filtro de divergencia DIRECCIONAL.
# El guard de divergencia solo debe bloquear cuando el lado apostado es UNDERDOG
# (cuota >= esta frontera). En FAVORITOS claros (cuota < 1.80, implícita > ~0.556)
# una divergencia alta es esperable — el modelo solo está más convencido que el
# mercado — y NO indica el edge inflado de longshot que el filtro persigue.
# Sin este condicional el filtro era direction-blind: cortaba favoritos buenos
# (fútbol: WR cortados 53% ≈ WR conservados 56%) además de underdogs malos.
# Frontera 1.80 = punto P&L-óptimo en la muestra de fútbol (150 resueltas); la parte
# robusta es "recuperar favoritos claros <1.80, seguir cortando >=1.80", no el número.
SPORTS_DIVERGENCE_UNDERDOG_ODDS = 1.80

# Timing guard — descartar señales de partidos ya empezados o demasiado próximos.
# Si faltan menos de N minutos para el inicio (o ya empezó) → no emitir/enviar la señal.
# Configurable por env. 0 = solo descartar partidos ya empezados.
SIGNAL_MIN_MINUTES_BEFORE_KICKOFF = int(
    os.environ.get("SIGNAL_MIN_MINUTES_BEFORE_KICKOFF", "20")
)
# ── Matched betting / surebets (motor back/lay — services/sports-agent/matched) ──
# Comisión del exchange usada en el cálculo qualifying. Betfair ~2%.
MATCHED_LAY_COMMISSION = float(os.environ.get("MATCHED_LAY_COMMISSION", "0.02"))
# rating qualifying (%) >= umbral → SUREBET (beneficio garantizado sin bono).
MATCHED_SUREBET_MIN_RATING = float(os.environ.get("MATCHED_SUREBET_MIN_RATING", "1.0"))
# rating qualifying (%) en [COVERAGE_MIN, SUREBET) → COVERAGE (buen qualifying para bono).
# -2.0 = aceptar pérdidas de hasta 2% del stake (un qualifying "barato" para liberar un bono).
MATCHED_COVERAGE_MIN_RATING = float(os.environ.get("MATCHED_COVERAGE_MIN_RATING", "-2.0"))
# Filtro de lay irreal: descartar lay_odds por encima de esto (betfair_ex_eu usa 1000.0
# como sentinela de "sin lay disponible" para esa selección).
MATCHED_LAY_ODDS_MAX = float(os.environ.get("MATCHED_LAY_ODDS_MAX", "51.0"))
# Máx sport_keys por escaneo (backstop de coste: N × 2 créditos de The Odds API).
# 3 keys = 6 créditos/scan → ~31 scans/mes contra el tope 210 (diario todo el mes, con
# margen para runs manuales). La 4ª key era siempre una liga menor (Austria/Bélgica) con
# lay de Betfair casi inexistente → recortarla no pierde señal accionable.
MATCHED_MAX_KEYS_PER_SCAN = int(os.environ.get("MATCHED_MAX_KEYS_PER_SCAN", "3"))
# Staleness del lay (The Odds API no da liquidez/size, sí last_update por mercado):
#   lay más fresco que FRESH_SEC  → confianza "high"
#   entre FRESH y STALE           → confianza "medium"
#   más viejo que STALE_SEC       → DESCARTAR (lay probablemente desactualizado/no ejecutable)
# Si la API no da last_update → confianza "unknown" (se conserva, no se descarta).
MATCHED_LAY_FRESH_SEC = int(os.environ.get("MATCHED_LAY_FRESH_SEC", "300"))   # 5 min
MATCHED_LAY_STALE_SEC = int(os.environ.get("MATCHED_LAY_STALE_SEC", "900"))   # 15 min
# Umbrales de ALERTA a Telegram (canal General). Persistimos todo, pero solo alertamos:
#   surebets con rating >= SUREBET_MIN (beneficio garantizado real)
#   coberturas con peaje mejor que COVERAGE_MIN (pierdes poco → apta para bono)
# Solo se alertan señales con confianza en MATCHED_ALERT_CONFIDENCE (fiables).
MATCHED_ALERT_SUREBET_MIN_RATING = float(os.environ.get("MATCHED_ALERT_SUREBET_MIN_RATING", "1.0"))
MATCHED_ALERT_COVERAGE_MIN_RATING = float(os.environ.get("MATCHED_ALERT_COVERAGE_MIN_RATING", "-1.0"))
MATCHED_ALERT_CONFIDENCE = os.environ.get("MATCHED_ALERT_CONFIDENCE", "high,medium")
# No alertar cuotas altas: en cuotas grandes (longshots) la liquidez del exchange se
# desploma y el "surebet" es un espejismo (fresco pero no ejecutable a tamaño real).
# The Odds API no da size → usamos la cuota como proxy. La señal SÍ se persiste (visible
# en el dashboard), solo NO se alerta si back o lay superan este tope.
# 4.0 (Fase 3): por encima el lay de Betfair es casi siempre fino.
MATCHED_ALERT_MAX_ODDS = float(os.environ.get("MATCHED_ALERT_MAX_ODDS", "4.0"))
# Fase 3 — proxy de liquidez: si el lay está muy por encima del back, el mercado del
# exchange es fino o la cuota está stale → la cobertura no es ejecutable a tamaño real.
# Ratio lay_odds / back_odds máximo para alertar (un mercado líquido los tiene pegados).
MATCHED_MAX_BACK_LAY_RATIO = float(os.environ.get("MATCHED_MAX_BACK_LAY_RATIO", "1.15"))
# Stake base con el que se expresan los importes de la alerta (back stake).
# El motor calcula todo por back_stake=100; la alerta reescala a este valor.
MATCHED_ALERT_BASE_STAKE = float(os.environ.get("MATCHED_ALERT_BASE_STAKE", "10.0"))

BASKETBALL_MIN_EDGE = 0.04   # NBA/EURO más eficientes que fútbol → umbral menor
POLY_MIN_EDGE = 0.08
POLY_MIN_CONFIDENCE = 0.65
POLY_MIN_VOLUME = 5_000   # volumen 24h mínimo para analizar — por debajo el spread es inejecutable

# Thresholds por liga — calibrados por backtest histórico (prodmatch_results)
# Ligas con ROI positivo en backtest → umbral más bajo (más señales)
# Ligas con ROI negativo → umbral más alto (sólo señales de alta convicción)
LEAGUE_MIN_EDGE: dict[str, float] = {
    "PL":  0.072,  # Premier League — ROI +13.5%: umbral bajado para capturar más edge
    "PD":  0.080,  # La Liga — ROI +7.4%: igual al global
    "BL1": 0.096,  # Bundesliga — ROI -31.9%: umbral subido, solo alta convicción
    "SA":  0.096,  # Serie A — ROI -18.9%: umbral subido
    "FL1": 0.096,  # Ligue 1 — ROI -24.6%: umbral subido
    "CL":  0.080,  # Champions League — sin backtest suficiente: global
    "EL":  0.080,  # Europa League
    "ECL": 0.080,  # Conference League
}

# Ligas de futbol — football-data.org (modelo Poisson+ELO completo)
SUPPORTED_FOOTBALL_LEAGUES = {
    "PL":  2021,   # Premier League
    "PD":  2014,   # La Liga
    "BL1": 2002,   # Bundesliga
    "SA":  2019,   # Serie A
    "FL1": 2015,   # Ligue 1
    "CL":  2001,   # UEFA Champions League
    "EL":  2146,   # UEFA Europa League
    "ECL": 2137,   # UEFA Conference League
    "EC":  2018,   # European Championship
    "WC":  2000,   # FIFA World Cup
}

# Deportes adicionales — API-Sports (misma key FOOTBALL_RAPID_API_KEY) + Groq (analisis IA)
# API-Sports = 100 req/dia compartidos entre futbol + todos los demas deportes
# Prioridad: futbol primero, resto de deportes con lo que quede del budget diario
SUPPORTED_SPORTS_APISPORTS = {
    "basketball": "nba",          # NBA — https://api-basketball.p.rapidapi.com
    "american-football": "nfl",   # NFL — https://api-american-football.p.rapidapi.com
    "baseball": "mlb",            # MLB
    "hockey": "nhl",              # NHL
    "mma": "ufc",                 # UFC/MMA
}
# Para cada deporte: stats de forma reciente + H2H desde API-Sports
# Groq analiza esas stats + noticias Tavily para estimar probabilidades
# Ensemble: stats_score (0.60) + groq_estimate (0.40)

MIN_MATCHES_TO_FIT = 3  # minimo real para Poisson con datos escasos; 5 descartaba demasiados equipos

# football-data.org: competiciones gratuitas adicionales (IDs oficiales)
SUPPORTED_FOOTBALL_LEAGUES_EXTRA = {
    "CL":  2001,   # UEFA Champions League
    "EL":  2146,   # UEFA Europa League
    "ECL": 2137,   # UEFA Conference League
    "EC":  2018,   # European Championship
    "WC":  2000,   # FIFA World Cup
}

# AllSportsApi — fútbol de selecciones y sudamérica
# host: allsportsapi2.p.rapidapi.com
ALLSPORTS_FOOTBALL_LEAGUES = {
    "NL":   1014,  # UEFA Nations League
    "WCQ":  1182,  # WC 2026 Qualifiers Europe
    "ARG":   307,  # Liga Argentina
    "CSUD":   11,  # Copa Sudamericana
    "CAM":     9,  # Copa America
}
# Mapeo código interno → nombre en Firestore league field
ALLSPORTS_LEAGUE_NAMES = {
    "NL":   "NL",
    "WCQ":  "WCQ",
    "ARG":  "ARG",
    "CSUD": "CSUD",
    "CAM":  "CAM",
}

# Baloncesto
BASKETBALL_HOME_ADV_NBA  = 3.2   # pts de ventaja local histórica NBA
BASKETBALL_HOME_ADV_EURO = 2.8   # pts de ventaja local Euroleague
BASKETBALL_SPREAD_SIGMA  = 12.0  # desviación estándar del margen (distribución normal)

# Tenis — pesos del ensemble
TENNIS_WEIGHTS = {"form": 0.30, "surface": 0.30, "ranking": 0.25, "h2h": 0.15}

# ── Feed de tendencias (Telegram, tema aparte) ──────────────────────────────────
# Solo estadística/hit-rate, SIN cuotas ni EV. Aislado de predictions/shadow_trades/
# accuracy_log/model_weights — colección propia trend_signals, ver analyzers/trend_finder.py.
# Series (goles/BTTS/hándicap): ventana de partidos propios del equipo, evidencia fuerte.
TREND_SERIES_WINDOW = 10          # últimos N partidos considerados
TREND_SERIES_MIN_SAMPLE = 8       # mínimo de partidos válidos en la ventana
TREND_SERIES_MIN_HIT_RATE = 0.70  # umbral de hit-rate para emitir el patrón
# Rolling (córners/tarjetas, football-data.co.uk): solo promedio, no serie partido a
# partido → evidencia más débil. Umbral por desviación sobre la media de la liga.
TREND_ROLLING_MIN_SAMPLE = 8
TREND_ROLLING_MIN_RATIO = 1.30    # promedio del equipo >= 1.3x la media de la liga
# Volumen por jornada: mensaje agrupado por PARTIDO (todos los mercados que
# aplican van en el mismo mensaje). Tope duro de partidos tras rankear por la
# SUMA de "fuerza de patrón" de sus mercados, máx por equipo para no repetir el
# mismo equipo en varios partidos (p.ej. liga + copa la misma semana).
# Renombrado 2026-09-15 (antes TREND_MAX_SIGNALS_PER_RUN/_PER_TEAM, cuando el
# tope era por señal suelta, no por partido).
TREND_MAX_FIXTURES_PER_RUN = 10
TREND_MAX_FIXTURES_PER_TEAM = 2
# Reparto de mercados: descartado el round-robin (tenía sentido cuando cada
# mensaje era 1 señal suelta; con el mensaje agrupado por partido la variedad
# de mercados ya sale sola dentro de cada mensaje).

# Mercados "model" (salida directa de Poisson/ELO ya en enriched_matches — NO
# hit-rate histórico ni promedio, es la probabilidad de un solo modelo para
# ESE partido). Tercer tipo de evidencia junto a series/rolling, marcado aparte
# en el mensaje y graduado aparte en trend_accuracy_log.
TREND_MODEL_DOUBLE_CHANCE_MIN = 0.75  # doble oportunidad: prob combinada mínima
TREND_MODEL_DNB_MIN = 0.65            # draw no bet: prob mínima tras renormalizar sin empate
# Total exacto / margen de victoria: no hay un "umbral de probabilidad" limpio
# (la salida más probable de una Poisson rara vez supera el 30% en solitario) —
# en su lugar exigimos que el resultado modal domine claramente al segundo.
TREND_MODEL_MODAL_RATIO_MIN = 1.5

# ── Auto-calibración por mercado (analyzers/trend_calibration.py) ──────────────
# Cada uno de los 14 mercados del feed acumula su propio hit-rate real en
# trend_accuracy_log. Con >= TREND_CALIBRATION_MIN_SAMPLE graduadas (excluyendo
# "void"), su umbral de emisión deja de ser el fijo genérico de arriba y se
# calibra con su propio historial — ver docstring de trend_calibration.py.
# v1 SOLO APRIETA (nunca afloja): con lo que se emite hoy solo hay datos
# graduados de la franja que ya pasó el umbral fijo — bajar el umbral exigiría
# datos de la franja que nunca se generó (sesgo de selección). Aflojar con
# sondas controladas queda para una fase futura con más historial acumulado.
TREND_CALIBRATION_MIN_SAMPLE = 30      # graduadas mínimas para calibrar (antes TREND_PERCENTILE_MIN_SAMPLE)
TREND_CALIBRATION_WINDOW = 200         # solo las N graduadas más recientes — la forma de los equipos cambia de temporada
TREND_CALIBRATION_MIN_TAIL_SAMPLE = 10 # mínimo de señales en un corte para no calibrar con ruido de muestra pequeña
TREND_TARGET_HIT_RATE = 0.70           # objetivo único de precisión para los 14 mercados

# Umbral fijo de cada mercado — mismo valor que ya usa trend_finder.py hoy,
# aquí como mapa para poder compararlo contra el umbral calibrado por mercado.
TREND_MARKET_FIXED_THRESHOLD = {
    "team_goals_over": TREND_SERIES_MIN_HIT_RATE,
    "btts": TREND_SERIES_MIN_HIT_RATE,
    "handicap": TREND_SERIES_MIN_HIT_RATE,
    "corners": TREND_ROLLING_MIN_RATIO,
    "cards": TREND_ROLLING_MIN_RATIO,
    "red_cards": TREND_ROLLING_MIN_RATIO,
    "shots": TREND_ROLLING_MIN_RATIO,
    "shots_on_target": TREND_ROLLING_MIN_RATIO,
    "fouls": TREND_ROLLING_MIN_RATIO,
    "ht_goals": TREND_ROLLING_MIN_RATIO,
    "double_chance": TREND_MODEL_DOUBLE_CHANCE_MIN,
    "dnb": TREND_MODEL_DNB_MIN,
    "exact_total": TREND_MODEL_MODAL_RATIO_MIN,
    "win_margin": TREND_MODEL_MODAL_RATIO_MIN,
}

LEARNING_RATE = 0.05
DEFAULT_WEIGHTS = {
    # Pesos para ensemble_probability — deben coincidir con las 4 senales del modelo
    "poisson": 0.40,      # modelo Poisson bivariado (mas robusto estadisticamente)
    "elo": 0.25,          # rating ELO dinamico
    "form": 0.20,         # forma reciente (ultimos 10 partidos)
    "h2h": 0.15,          # ventaja historica directa
}
