"""
Categorizador de mercados Polymarket. Sin dependencias (solo re) para que lo usen
igual groq_analyzer y los scripts de mantenimiento (reetiquetado de shadow_trades).
"""
import re

# Versión del categorizador, guardada en poly_predictions y alerts_sent para separar en
# las revisiones lo etiquetado antes (sin campo = v1, por subcadenas) y después.
CATEGORIZER_VERSION = 2

_TENNIS_RE = re.compile(
    r'\b(atp|wta|internazionali|french open|italian open|roland garros|wimbledon|'
    r'us open|australian open|masters 1000|davis cup|indian wells|miami open|'
    r'monte.?carlo|madrid open|qualification|qualifier|tennis'
    # Torneos ITF femeninos ("W50 Heraklion: A vs B")
    r'|w(?:15|35|50|75|100))\b',
    re.I,
)
_CRICKET_RE = re.compile(
    r'\b('
    r'ipl|indian premier league|cricket|t20s?|t20is?|odis?|test match'
    # Abreviaturas IPL
    r'|csk|kkr|rcb|srh|pbks|lsg|gt '
    # Nombres completos equipos IPL — necesarios cuando el slug/question no usa abreviaturas
    r'|rajasthan royals|lucknow super giants|mumbai indians|chennai super kings'
    r'|royal challengers|sunrisers hyderabad|delhi capitals|gujarat titans'
    r'|punjab kings|kolkata knight riders'
    # Abreviaturas adicionales con posible confusión
    r'|rr(?=\s)|mi(?=\s)|dc(?=\s)'
    r')\b',
    re.I,
)

CATEGORY_KEYWORDS = {
    "crypto": ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "defi", "blockchain", "halving", "altcoin", "xrp", "bnb", "doge", "dogecoin", "stablecoin", "nft", "web3", "layer 2", "base chain", "token", "fdv", "airdrop", "tge", "memecoin", "megaeth", "pump.fun", "hyperliquid"],
    "politics": ["election", "president", "vote", "congress", "senate", "minister", "parliament", "poll", "referendum", "prime minister", "chancellor", "governor", "ballot", "trump", "biden", "harris", "democrat", "democratic", "republican", "nominee", "nomination", "primary", "runoff", "house seat", "mayor", "mayoral", "gubernatorial", "speaker", "impeach", "impeachment", "cabinet"],
    "economy": ["fed", "interest rate", "inflation", "cpi", "gdp", "recession", "unemployment", "federal reserve", "rate hike", "rate cut", "jerome powell", "tariff", "trade war", "crude oil", "wti", "brent", "oil price", "gold price", "s&p", "nasdaq", "dow jones"],
    "sports": ["world cup", "champions league", "nba", "wnba", "nfl", "mlb", "nhl", "mls", "ncaa", "ncaaf", "ncaab", "super bowl", "world series", "stanley cup", "final", "tournament", "championship", "league", "wimbledon", "olympic", "olympics", "football", "soccer", "formula 1", "f1", "motogp", "tennis", "golf", "pga", "lpga", "boxing", "ufc", "mma", "playoffs", "copa", "euro", "roland garros", "french open", "italian open", "us open", "australian open", "masters", "nascar", "basketball", "baseball", "hockey", "cricket", "rugby", "atp", "wta", "itf", "challenger", "fifa", "uefa", "premier league", "la liga", "bundesliga", "serie a", "ligue 1", "grand prix", "ballon d'or", "mvp", "cy young", "heisman", "relegated", "top scorer", "darts", "snooker", "cycling", "tour de france", "esports", "counter-strike", "valorant", "dota", "fight", "knockout", "submission"],
    "geopolitics": ["war", "ceasefire", "conflict", "nato", "military", "invasion", "sanctions", "treaty", "diplomacy", "nuclear", "iran", "hormuz", "strait", "ukraine", "russia", "china", "taiwan", "israel", "gaza", "hamas", "hezbollah", "korea", "missile", "drone", "coup", "regime", "peace deal", "cease fire", "truce", "embargo", "iranian", "israeli", "russian", "ukrainian", "chinese", "airspace", "blockade", "strike", "airstrike", "troops", "annex", "venezuela", "kharg", "invade"],
    "business": ["apple", "tesla", "microsoft", "amazon", "google", "alphabet", "meta", "nvidia", "openai", "anthropic", "earnings", "merger", "acquisition", "ipo", "layoffs", "market cap", "revenue", "profit", "ceo", "stock", "shares", "valuation", "startup", "funding", "unicorn"],
    "science": ["climate", "nasa", "space", "vaccine", "fda", "cancer", "quantum", "discovery", "mission", "ai model", "chatgpt", "llm", "gpt", "gemini", "claude", "spacex", "rocket", "satellite", "drug approval", "clinical trial"],
    "culture": ["oscar", "grammy", "emmy", "movie", "album", "singer", "actor", "celebrity", "taylor swift", "award", "box office", "netflix", "spotify", "billboard", "world record", "streaming", "awards", "vmas", "mtv", "golden globe", "tony"],
}

# Palabras completas (con plural opcional), no subcadenas: con `kw in q` "fed" casaba
# en "Federico", "ipo" en "Sharipov", "war" en "Warsaw"/"award", "harris" (tenista)
# en politics.
_CATEGORY_RES: dict[str, re.Pattern] = {
    _cat: re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(k) for k in sorted(_kws, key=len, reverse=True)) + r")(?:s|es)?(?!\w)",
        re.I,
    )
    for _cat, _kws in CATEGORY_KEYWORDS.items()
}

# Formatos de partido de Polymarket: "A vs. B", "Torneo: A vs B", "Will X win on
# 2026-06-22?", "end in a draw", O/U, handicaps, BTTS. En 9.881 preguntas reales
# todas las que llevan "vs" son deporte.
_SPORTS_MATCH_RE = re.compile(
    r"\bvs\.?\s|\bwin on \d{4}-\d{2}-\d{2}\b|\bend in a draw\b|\bo/u\s*\d"
    r"|\b(?:spread|set handicap|game handicap|map handicap)\s*:|both teams to score",
    re.I,
)


def categorize_market(question: str) -> str:
    """Categoriza un mercado Polymarket según su pregunta. Devuelve categoria o 'other'."""
    q_lower = (question or "").lower()
    # Sports override: partido/tenis/cricket antes del scan general (nombres de jugadores
    # como "Solana Sierra" o "Billy Harris" no deben caer en crypto/politics).
    if _SPORTS_MATCH_RE.search(q_lower) or _TENNIS_RE.search(q_lower) or _CRICKET_RE.search(q_lower):
        return "sports"
    for category, pattern in _CATEGORY_RES.items():
        if pattern.search(q_lower):
            return category
    return "other"
