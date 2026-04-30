"""
Fallback de odds via API-Football (RapidAPI) — reutiliza FOOTBALL_RAPID_API_KEY.

Activo cuando ODDSPAPI_KEY y ODDS_API_KEY están agotadas o vacías.
Cuota: 100 req/día del plan free de api-football-v1.p.rapidapi.com,
       independiente de la cuota api-basketball/api-american-football (API-Sports).

Mercados soportados (bet IDs verificados en API-Football v3):
  5  — Both Teams To Score (BTTS Yes/No)
  6  — Double Chance (1X / X2 / 12)
  15 — Goals Over/Under (múltiples líneas: 1.5, 2.5, 3.5, 4.5)
  4  — Asian Handicap (-1.5 / -1.0 / -0.5 / +0.5 / +1.0 / +1.5)

Corners/tarjetas: API-Football free no garantiza estos mercados;
los cálculos Poisson de corners_bookings.py no los necesitan — usan FDCO stats.

Budget plan (100 req/día):
  Fixtures por liga/día: 1 req × N ligas activas (cache TTL 12h)
  Odds por partido:      1 req × M partidos (cache TTL 1h)
  Estimación típica:     5 fixtures + 8-12 odds = ~17 req/día
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from shared.config import FOOTBALL_RAPID_API_KEY
from shared.api_quota_manager import quota

logger = logging.getLogger(__name__)
logger.info("apifootball_odds: módulo cargado — FOOTBALL_RAPID_API_KEY presente: %s", bool(FOOTBALL_RAPID_API_KEY))

_HOST    = "api-football-v1.p.rapidapi.com"
_BASE    = f"https://{_HOST}"
_TIMEOUT = 20.0

# Cache de fixtures: {cache_key: (fetched_at, [fixture_dicts])}
_FIXTURES_CACHE: dict[str, tuple[datetime, list]] = {}
_FIXTURES_TTL   = timedelta(hours=12)

# Cache de odds: {fixture_id: (fetched_at, parsed_odds_dict)}
_ODDS_CACHE: dict[int, tuple[datetime, dict]] = {}
_ODDS_TTL   = timedelta(hours=24)

# Mapeo código de liga interno → API-Football league_id (season 2025)
_LEAGUE_IDS: dict[str, int] = {
    # ── Fútbol masculino Europa ───────────────────────────────────────────────
    "PL":  39,    # Premier League
    "PD":  140,   # La Liga
    "BL1": 78,    # Bundesliga
    "SA":  135,   # Serie A
    "FL1": 61,    # Ligue 1
    "CL":  2,     # Champions League
    "EL":  3,     # Europa League
    "ECL": 848,   # Europa Conference League
    "TU1": 203,   # Süper Lig

    # ── Fútbol masculino internacional (selecciones) ──────────────────────────
    "WC":   1,    # FIFA World Cup (cualquier edición)
    "NL":   5,    # UEFA Nations League
    "CAM":  9,    # Copa América — ⚠️ verify (agent reported 17; 9 es más común)
    "INTL": 10,   # International Friendlies
    "EC":   4,    # UEFA Euro (cualquier edición)
    "WCQ":  32,   # WC 2026 Qualifiers UEFA (Europa)
    "WCQ_CONMEBOL":  31,   # WC 2026 Qualifiers CONMEBOL
    "WCQ_CONCACAF":  30,   # WC 2026 Qualifiers CONCACAF — ⚠️ verify
    "WCQ_AFC":       33,   # WC 2026 Qualifiers AFC (Asia)
    "WCQ_CAF":       29,   # WC 2026 Qualifiers CAF (África) — ⚠️ verify

    # ── Fútbol masculino Sudamérica ───────────────────────────────────────────
    "ARG":  128,  # Primera División Argentina
    "CSUD": 11,   # Copa Sudamericana

    # ── Fútbol femenino (sin colector activo — IDs listos para cuando se implemente)
    "W_WWC":      8,    # FIFA Women's World Cup — ⚠️ verify (alt: 20)
    "W_WEURO":    50,   # UEFA Women's Euro — ⚠️ verify
    "W_WNATIONS": 956,  # UEFA Women's Nations League — ⚠️ verify
    "W_WCL":      545,  # UEFA Women's Champions League — ⚠️ verify
    "W_WSL":      253,  # Women's Super League (England) — ⚠️ verify
    "W_NWSL":     264,  # NWSL (USA) — ⚠️ verify
    "W_FRAUEN_BL": 57,  # Frauen-Bundesliga (Germany) — ⚠️ verify
    "W_D1F":      519,  # D1 Féminine (France) — ⚠️ verify
    "W_LIGA_F":   750,  # Liga F (Spain) — ⚠️ verify
    # basketball usa api-basketball.p.rapidapi.com, no API-Football
}

# Bet IDs relevantes
_BET_BTTS     = 5
_BET_DC       = 6
_BET_GOALS_OU = 15
_BET_AH       = 4


async def _request(path: str, params: dict) -> dict | None:
    """Llamada autenticada a API-Football con manejo de cuota y errores."""
    if not FOOTBALL_RAPID_API_KEY:
        return None

    if not quota.can_call("apifootball"):
        logger.warning("apifootball_odds: cuota diaria agotada")
        return None

    headers = {
        "X-RapidAPI-Key":  FOOTBALL_RAPID_API_KEY,
        "X-RapidAPI-Host": _HOST,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{_BASE}{path}", headers=headers, params=params)

        if resp.status_code == 429:
            logger.warning("apifootball_odds: rate limit 429")
            return None
        if resp.status_code != 200:
            logger.warning("apifootball_odds: HTTP %d %s", resp.status_code, path)
            return None

        quota.track_call("apifootball")
        return resp.json()

    except Exception:
        logger.error("apifootball_odds: error en %s", path, exc_info=True)
        return None


async def fetch_league_fixtures(league: str, match_date: date) -> list[dict]:
    """
    GET /v3/fixtures?date=DATE&league=LEAGUE_ID&season=YEAR
    Devuelve lista de fixtures normalizados {fixture_id, home, away, date}.
    Cache TTL 12h — una sola llamada por liga/día.
    """
    league_id = _LEAGUE_IDS.get(league)
    if not league_id:
        return []

    cache_key = f"{league}_{match_date}"
    now = datetime.now(timezone.utc)
    cached = _FIXTURES_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _FIXTURES_TTL:
        return cached[1]

    season = match_date.year if match_date.month >= 7 else match_date.year - 1
    data = await _request("/v3/fixtures", {
        "date":   match_date.isoformat(),
        "league": league_id,
        "season": season,
    })
    if not data:
        return []

    fixtures = []
    for item in data.get("response", []):
        try:
            fix  = item.get("fixture", {})
            teams = item.get("teams", {})
            fixtures.append({
                "fixture_id": fix.get("id"),
                "home":       teams.get("home", {}).get("name", ""),
                "away":       teams.get("away", {}).get("name", ""),
                "date":       fix.get("date", ""),
            })
        except Exception:
            continue

    _FIXTURES_CACHE[cache_key] = (now, fixtures)
    logger.info("apifootball_odds: %d fixtures cargados para %s %s", len(fixtures), league, match_date)
    return fixtures


def find_fixture_id(fixtures: list[dict], home_team: str, away_team: str) -> int | None:
    """Fuzzy match por nombre de equipo sin acentos."""
    import re
    import unicodedata

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", s.lower())

    h, a = _norm(home_team), _norm(away_team)
    for fix in fixtures:
        fh = _norm(fix.get("home", ""))
        fa = _norm(fix.get("away", ""))
        if (h in fh or fh in h) and (a in fa or fa in a):
            return fix.get("fixture_id")
    return None


async def fetch_odds(fixture_id: int) -> dict | None:
    """
    GET /v3/odds?fixture=ID
    Devuelve dict normalizado con secciones btts, goals_ou, double_chance, asian_handicap.
    Cache TTL 1h.
    """
    now = datetime.now(timezone.utc)
    cached = _ODDS_CACHE.get(fixture_id)
    if cached and (now - cached[0]) < _ODDS_TTL:
        return cached[1]

    data = await _request("/v3/odds", {"fixture": fixture_id})
    if not data:
        return None

    responses = data.get("response", [])
    if not responses:
        return None

    # Agregar bets de todos los bookmakers disponibles; preferimos bet365 (id=6) si existe
    bets_by_id: dict[int, list[dict]] = {}
    for r in responses:
        for bkm in r.get("bookmakers", []):
            bkm_id   = bkm.get("id", 0)
            bkm_name = bkm.get("name", "")
            for bet in bkm.get("bets", []):
                bid = bet.get("id")
                if bid not in bets_by_id:
                    bets_by_id[bid] = []
                bets_by_id[bid].append({
                    "bookmaker":    bkm_name,
                    "bookmaker_id": bkm_id,
                    "values":       bet.get("values", []),
                })

    parsed = _parse_all_bets(bets_by_id)
    if parsed:
        _ODDS_CACHE[fixture_id] = (now, parsed)
        logger.info("apifootball_odds: odds parseadas para fixture %d", fixture_id)
    return parsed or None


def _pick_bookmaker(entries: list[dict]) -> dict:
    """Prefiere bet365 (id=6); fallback al primero disponible."""
    for e in entries:
        if e.get("bookmaker_id") == 6:
            return e
    return entries[0] if entries else {}


def _parse_all_bets(bets_by_id: dict[int, list[dict]]) -> dict:
    """Normaliza todos los bet IDs relevantes a un dict de secciones."""
    result: dict = {}

    # ── BTTS ──────────────────────────────────────────────────────────────────
    if _BET_BTTS in bets_by_id:
        entry = _pick_bookmaker(bets_by_id[_BET_BTTS])
        yes_odds = no_odds = None
        for v in entry.get("values", []):
            try:
                if v.get("value", "").lower() == "yes":
                    yes_odds = float(v["odd"])
                elif v.get("value", "").lower() == "no":
                    no_odds = float(v["odd"])
            except (KeyError, ValueError):
                continue
        if yes_odds and no_odds:
            result["btts"] = {
                "yes_odds":  yes_odds,
                "no_odds":   no_odds,
                "bookmaker": entry.get("bookmaker", "api-football"),
            }

    # ── GOALS OVER/UNDER (múltiples líneas) ──────────────────────────────────
    if _BET_GOALS_OU in bets_by_id:
        entry  = _pick_bookmaker(bets_by_id[_BET_GOALS_OU])
        lines: dict[float, dict] = {}
        for v in entry.get("values", []):
            try:
                raw = v.get("value", "")  # "Over 2.5" / "Under 2.5"
                parts = raw.split()
                if len(parts) == 2 and parts[0].lower() in ("over", "under"):
                    direction = parts[0].lower()
                    line      = float(parts[1])
                    price     = float(v["odd"])
                    if line not in lines:
                        lines[line] = {"bookmaker": entry.get("bookmaker", "api-football")}
                    lines[line][f"{direction}_odds"] = price
            except (KeyError, ValueError):
                continue
        if lines:
            result["goals_ou"] = lines   # {2.5: {over_odds, under_odds, bookmaker}, ...}

    # ── DOUBLE CHANCE ─────────────────────────────────────────────────────────
    if _BET_DC in bets_by_id:
        entry = _pick_bookmaker(bets_by_id[_BET_DC])
        dc: dict = {"bookmaker": entry.get("bookmaker", "api-football")}
        for v in entry.get("values", []):
            try:
                raw   = v.get("value", "")
                price = float(v["odd"])
                if raw == "Home/Draw":
                    dc["1X"] = price
                elif raw == "Draw/Away":
                    dc["X2"] = price
                elif raw in ("Home/Away", "Win Either Half"):
                    dc["12"] = price
            except (KeyError, ValueError):
                continue
        if any(k in dc for k in ("1X", "X2", "12")):
            result["double_chance"] = dc

    # ── ASIAN HANDICAP ────────────────────────────────────────────────────────
    if _BET_AH in bets_by_id:
        entry = _pick_bookmaker(bets_by_id[_BET_AH])
        ah_lines = []
        line_map: dict[str, dict] = {}
        for v in entry.get("values", []):
            try:
                # Formato: "Home -1.5" / "Away +1.5"
                raw   = v.get("value", "")
                price = float(v["odd"])
                parts = raw.rsplit(" ", 1)
                if len(parts) == 2:
                    side, line_str = parts
                    line_f = float(line_str)
                    key    = str(line_f)
                    if key not in line_map:
                        line_map[key] = {"bookmaker": entry.get("bookmaker", "api-football")}
                    if side.lower() == "home":
                        line_map[key]["home_line"]  = line_f
                        line_map[key]["home_odds"]  = price
                    else:
                        line_map[key]["away_line"]  = -line_f
                        line_map[key]["away_odds"]  = price
            except (KeyError, ValueError):
                continue
        ah_lines = [v for v in line_map.values()
                    if "home_odds" in v and "away_odds" in v]
        if ah_lines:
            result["asian_handicap"] = ah_lines

    return result


async def get_match_odds(
    home_team: str,
    away_team: str,
    league: str,
    match_date: date,
) -> dict | None:
    """
    Punto de entrada principal. Busca el fixture en API-Football y devuelve odds parseadas.
    Devuelve None si la liga no está mapeada, el fixture no se encuentra, o la cuota está agotada.
    """
    fixtures = await fetch_league_fixtures(league, match_date)
    if not fixtures:
        return None

    fixture_id = find_fixture_id(fixtures, home_team, away_team)
    if not fixture_id:
        logger.debug("apifootball_odds: fixture no encontrado (%s vs %s %s)", home_team, away_team, league)
        return None

    return await fetch_odds(fixture_id)


def parse_btts(odds_data: dict) -> dict | None:
    """Compatible con parse_btts_event de football_markets.py."""
    return odds_data.get("btts")


def parse_goals_ou(odds_data: dict, line: float = 2.5) -> dict | None:
    """Extrae over/under para una línea específica. Compatible con _parse_totals_event."""
    lines = odds_data.get("goals_ou", {})
    # Busca la línea exacta o la más cercana dentro de ±0.1
    best = min(lines.keys(), key=lambda k: abs(float(k) - line), default=None)
    if best is None or abs(float(best) - line) > 0.1:
        return None
    entry = lines[best]
    if "over_odds" in entry and "under_odds" in entry:
        return {"over_odds": entry["over_odds"], "under_odds": entry["under_odds"],
                "bookmaker": entry.get("bookmaker", "api-football"), "line": float(best)}
    return None


def parse_double_chance(odds_data: dict) -> dict | None:
    """Compatible con parse_double_chance_event de football_markets.py."""
    return odds_data.get("double_chance")


def parse_asian_handicap(odds_data: dict) -> list[dict]:
    """Compatible con _parse_oddspapi_ah de football_markets.py."""
    return odds_data.get("asian_handicap", [])
