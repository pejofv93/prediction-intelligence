import os

GOOGLE_CLOUD_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]

# Variables opcionales segun el servicio — usar .get() para evitar KeyError al arrancar.
# Cada servicio solo recibe las vars que necesita en --set-env-vars.
# Si una var no esta presente → None. El servicio debe validar antes de usarla.
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
CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")  # token inter-servicios
ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")      # The Odds API — secundaria (500/mes)
ODDSPAPI_KEY   = os.environ.get("ODDSPAPI_KEY", "")      # OddsPapi — terciaria (250/mes)
ODDSAPIIO_KEY  = os.environ.get("ODDSAPIIO_KEY", "")     # odds-api.io — primaria (5000 req/h)
COLLECTION_PREFIX = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "")

# IA
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_MODEL_ROTATION = [
    "llama-3.3-70b-versatile",   # principal
    "mixtral-8x7b-32768",        # fallback 1
    "gemma2-9b-it",              # fallback 2
    "llama-3.1-8b-instant",      # fallback 3
]
GROQ_FALLBACK_MODEL = GROQ_MODEL_ROTATION[1]  # backward compat
GROQ_BASE_URL = "https://api.groq.com/openai/v1"  # compatible con openai SDK

# Thresholds
SPORTS_MIN_EDGE = 0.08
SPORTS_MIN_CONFIDENCE = 0.65
SPORTS_ALERT_EDGE = 0.08
POLY_MIN_EDGE = 0.08
POLY_MIN_CONFIDENCE = 0.65

# Ligas de futbol — football-data.org (modelo Poisson+ELO completo)
SUPPORTED_FOOTBALL_LEAGUES = {
    "PL":  2021,   # Premier League
    "PD":  2014,   # La Liga
    "BL1": 2002,   # Bundesliga
    "SA":  2019,   # Serie A
    "FL1": 2015,   # Ligue 1
    "PPL": 2017,   # Primeira Liga Portugal
    # Tier gratuito adicional
    "CL":  2001,   # UEFA Champions League
    "DED": 2003,   # Eredivisie
    "EC":  2018,   # European Championship
    "WC":  2000,   # FIFA World Cup
    "ELC": 2016,   # Championship
    "BSA": 2013,   # Brasileirao Serie A
    "CLI": 2152,   # Copa Libertadores
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
    "DED": 2003,   # Eredivisie
    "EC":  2018,   # European Championship
    "WC":  2000,   # FIFA World Cup
    "ELC": 2016,   # Championship
    "BSA": 2013,   # Brasileirao Serie A
    "CLI": 2152,   # Copa Libertadores
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

LEARNING_RATE = 0.05
DEFAULT_WEIGHTS = {
    # Pesos para ensemble_probability — deben coincidir con las 4 senales del modelo
    "poisson": 0.40,      # modelo Poisson bivariado (mas robusto estadisticamente)
    "elo": 0.25,          # rating ELO dinamico
    "form": 0.20,         # forma reciente (ultimos 10 partidos)
    "h2h": 0.15,          # ventaja historica directa
}
