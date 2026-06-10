"""
Cliente compartido para la API pública de Sofascore (sin key, sin auth).

Endpoints verificados:
  /unique-tournament/{tid}/season/{sid}/events/{last|next}/{page}
  /unique-tournament/{tid}/season/{sid}/standings/total
  /team/{team_id}/events/last/{page}
  /event/{event_id}/statistics

IDs de torneos confirmados:
  ACB               id=264   season_2526=80922
  Roland Garros ATP id=2480  season_2026=85951  season_2025=61364
  Roland Garros WTA id=2577  season_2026=85953  season_2025=61366
  Champions League  id=7     season_2425=61644  season_2526=76953
  La Liga           id=8     season_2425=61643  season_2526=77559
  Premier League    id=17    season_2425=61627  season_2526=76986
  Bundesliga        id=35    season_2425=61643  (verificar)
"""
import asyncio
import logging
import random

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.sofascore.com/api/v1"
_TIMEOUT = 15

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]


def _browser_headers(ua: str) -> dict:
    is_chrome = "Chrome" in ua
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.sofascore.com",
        "Referer": "https://www.sofascore.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        **(
            {
                "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not=A?Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
            if is_chrome
            else {}
        ),
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


# Torneos y temporadas conocidas {año_fin_o_clave: season_id}
TOURNAMENTS: dict[str, dict] = {
    # Baloncesto
    "acb":              {"id": 264,  "sport": "basketball", "surface": None,   "seasons": {2026: 80922}},
    # Tenis
    "roland_garros_atp":{"id": 2480, "sport": "tennis",     "surface": "clay", "seasons": {2026: 85951, 2025: 61364}},
    "roland_garros_wta":{"id": 2577, "sport": "tennis",     "surface": "clay", "seasons": {2026: 85953, 2025: 61366}},
    # Fútbol masculino — Europa
    "champions_league": {"id": 7,    "sport": "football",   "surface": None,   "league_code": "CL",  "seasons": {2425: 61644, 2526: 76953}},
    "europa_league":    {"id": 679,  "sport": "football",   "surface": None,   "league_code": "EL",  "seasons": {2425: 61645, 2526: 76984}},
    "conference_league":{"id": 17015,"sport": "football",   "surface": None,   "league_code": "ECL", "seasons": {2425: 61648, 2526: 76960}},
    "premier_league":   {"id": 17,   "sport": "football",   "surface": None,   "league_code": "PL",  "seasons": {2425: 61627, 2526: 76986}},
    "laliga":           {"id": 8,    "sport": "football",   "surface": None,   "league_code": "PD",  "seasons": {2425: 61643, 2526: 77559}},
    "bundesliga":       {"id": 35,   "sport": "football",   "surface": None,   "league_code": "BL1", "seasons": {2425: 63516, 2526: 77333}},
    "serie_a":          {"id": 23,   "sport": "football",   "surface": None,   "league_code": "SA",  "seasons": {2425: 63515, 2526: 76457}},
    "ligue_1":          {"id": 34,   "sport": "football",   "surface": None,   "league_code": "FL1", "seasons": {2425: 61736, 2526: 77356}},
    # Fútbol femenino
    "wsl":              {"id": 1044, "sport": "football",   "surface": None,   "league_code": "WSL", "seasons": {2425: 64370, 2526: 79227}},
    "liga_f":           {"id": 1127, "sport": "football",   "surface": None,   "league_code": "LIGA_F", "seasons": {2425: 65687, 2526: 77723}},
    "women_cl":         {"id": 696,  "sport": "football",   "surface": None,   "league_code": "WCL", "seasons": {2425: 63572, 2526: 77328}},
    # Fútbol internacional
    "brasileirao":      {"id": 325,  "sport": "football",   "surface": None,   "league_code": "BSA",  "seasons": {2025: 72034, 2026: 87678}},
    "copa_america":     {"id": 133,  "sport": "football",   "surface": None,   "league_code": "CA",   "seasons": {2024: 57114}},
    # Mundial 2026 y clasificatorias
    "world_cup_2026":   {"id": 16,   "sport": "football",   "surface": None,   "league_code": "WC26", "seasons": {2026: 58210}},
    "wcq_uefa":         {"id": 11,   "sport": "football",   "surface": None,   "league_code": "WCQ_UEFA",     "seasons": {2026: 69427}},
    "wcq_conmebol":     {"id": 295,  "sport": "football",   "surface": None,   "league_code": "WCQ_CONMEBOL", "seasons": {2025: 53820}},
    "wcq_caf":          {"id": 13,   "sport": "football",   "surface": None,   "league_code": "WCQ_CAF",      "seasons": {2025: 56249}},
    "wcq_afc":          {"id": 308,  "sport": "football",   "surface": None,   "league_code": "WCQ_AFC",      "seasons": {2025: 53508}},
    "wcq_concacaf":     {"id": 14,   "sport": "football",   "surface": None,   "league_code": "WCQ_CONCACAF", "seasons": {2026: 58146}},
    # Torneos recientes con xG para selecciones (fuente de xG para el WC)
    "euro_2024":        {"id": 1,    "sport": "football",   "surface": None,   "league_code": "EURO24",  "seasons": {2024: 56953}},
    "copa_america_2024":{"id": 133,  "sport": "football",   "surface": None,   "league_code": "CA24",    "seasons": {2024: 57114}},
}

# Mapeo rápido league_code → clave en TOURNAMENTS (para lookup inverso)
LEAGUE_CODE_TO_TOURNAMENT: dict[str, str] = {
    v["league_code"]: k
    for k, v in TOURNAMENTS.items()
    if "league_code" in v
}


async def _get(url: str, retries: int = 3) -> dict | None:
    ua = random.choice(_USER_AGENTS)
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(
                headers=_browser_headers(ua),
                timeout=_TIMEOUT,
                follow_redirects=True,
            ) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (403, 429):
                    wait = 3 + attempt * 5
                    logger.warning(
                        "sofascore %d en %s (intento %d/%d) — retry en %ds",
                        r.status_code, url[-60:], attempt + 1, retries, wait,
                    )
                    if attempt < retries - 1:
                        ua = random.choice(_USER_AGENTS)
                        await asyncio.sleep(wait)
                    continue
                logger.debug("sofascore._get(%s): HTTP %d", url[-60:], r.status_code)
                return None
        except Exception as e:
            logger.debug("sofascore._get(%s): %s", url[-60:], e)
            if attempt < retries - 1:
                await asyncio.sleep(2 + attempt * 2)
    return None


async def fetch_tournament_events(
    tournament_id: int,
    season_id: int,
    page: int = 0,
    direction: str = "last",
) -> list[dict]:
    """Una página de eventos de un torneo. direction='last' → terminados, 'next' → próximos."""
    url = f"{_BASE}/unique-tournament/{tournament_id}/season/{season_id}/events/{direction}/{page}"
    data = await _get(url)
    return (data or {}).get("events", [])


async def fetch_tournament_standings(tournament_id: int, season_id: int) -> list[dict]:
    """Tabla de clasificación de un torneo. Devuelve rows del primer grupo."""
    url = f"{_BASE}/unique-tournament/{tournament_id}/season/{season_id}/standings/total"
    data = await _get(url)
    standings = (data or {}).get("standings", [])
    return standings[0].get("rows", []) if standings else []


async def fetch_team_events(sf_team_id: int, page: int = 0) -> list[dict]:
    """Últimos ~20 eventos de un equipo (football/basketball). 404 para tenistas."""
    url = f"{_BASE}/team/{sf_team_id}/events/last/{page}"
    data = await _get(url)
    return (data or {}).get("events", [])


async def fetch_event_statistics(event_id: int) -> dict:
    """Estadísticas de un partido concreto (xG, shots, aces, etc.)."""
    url = f"{_BASE}/event/{event_id}/statistics"
    data = await _get(url)
    return data or {}


def normalize_name(name: str) -> str:
    """Normaliza nombre de jugador/equipo para comparación: minúsculas, sin acentos."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()
