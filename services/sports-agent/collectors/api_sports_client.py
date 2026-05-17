"""
Collector: API-Sports multi-deporte via RapidAPI.
Usa FOOTBALL_RAPID_API_KEY — la misma key que football_api.py.
Deportes: NBA, NFL, MLB, NHL, MMA.
Rate limit: 100 req/DIA total compartido con cuotas de futbol.
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx

from shared.config import FOOTBALL_RAPID_API_KEY, SUPPORTED_SPORTS_APISPORTS

logger = logging.getLogger(__name__)

# Base URLs por deporte (todas requieren X-RapidAPI-Key y X-RapidAPI-Host)
API_SPORTS_HOSTS = {
    "basketball": "api-basketball.p.rapidapi.com",  # corregido: basketball-arbitrage era 404
    "american-football": "api-american-football.p.rapidapi.com",
    "baseball": "api-baseball.p.rapidapi.com",
    "hockey": "api-hockey.p.rapidapi.com",
    # "mma": "api-mma.p.rapidapi.com",  # desactivado: API eliminada → 404
}

# Deportes desactivados por endpoint muerto — get_games_today retorna [] sin HTTP
_DISABLED_SPORTS = {"mma", "ufc"}  # api-mma.p.rapidapi.com/fights → 404

API_SPORTS_DELAY = 2.0  # segundos entre requests — conservador dado el limite diario

# Mapeo deporte abreviado → sport_type key (para buscar en API_SPORTS_HOSTS)
_LEAGUE_TO_SPORT_TYPE = {v: k for k, v in SUPPORTED_SPORTS_APISPORTS.items()}
# {"nba": "basketball", "nfl": "american-football", "mlb": "baseball", "nhl": "hockey", "ufc": "mma"}
# Euroleague usa el mismo host que NBA (api-basketball.p.rapidapi.com)
_LEAGUE_TO_SPORT_TYPE["euroleague"] = "basketball"

# Mapeo sport abreviado → nombre de liga en Firestore
_SPORT_TO_LEAGUE = {
    "nba": "NBA",
    "nfl": "NFL",
    "mlb": "MLB",
    "nhl": "NHL",
    "mma": "MMA",
    "ufc": "MMA",
    "euroleague": "EUROLEAGUE",
}


def _get_host(sport: str) -> str:
    """Devuelve el host RapidAPI para un deporte dado (ej: 'nba' → 'api-basketball...')."""
    sport_type = _LEAGUE_TO_SPORT_TYPE.get(sport)
    if not sport_type:
        raise ValueError(f"Deporte no soportado: {sport}")
    host = API_SPORTS_HOSTS.get(sport_type)
    if not host:
        raise ValueError(f"Host no encontrado para sport_type: {sport_type}")
    return host


async def _request(host: str, path: str, params: dict | None = None) -> dict | None:
    """
    Llamada autenticada a API-Sports via RapidAPI con rate limiting y manejo de errores.
    Devuelve JSON parseado o None si falla.
    """
    if not FOOTBALL_RAPID_API_KEY:
        raise RuntimeError("FOOTBALL_RAPID_API_KEY no configurada para este servicio")

    await asyncio.sleep(API_SPORTS_DELAY)

    url = f"https://{host}{path}"
    headers = {
        "X-RapidAPI-Key": FOOTBALL_RAPID_API_KEY,
        "X-RapidAPI-Host": host,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers, params=params)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                logger.warning("Rate limit API-Sports — esperando %ds", wait)
                await asyncio.sleep(wait)
                resp = await client.get(url, headers=headers, params=params)

            if resp.status_code >= 400:
                logger.error(
                    "API-Sports %s%s → HTTP %d: %.200s",
                    host, path, resp.status_code, resp.text,
                )
                return None

            return resp.json()

    except httpx.TimeoutException:
        logger.error("Timeout en API-Sports %s%s", host, path, exc_info=True)
        return None
    except Exception:
        logger.error("Error en API-Sports %s%s", host, path, exc_info=True)
        return None


def _parse_game(item: dict, sport: str) -> dict:
    """
    Normaliza un objeto game de API-Sports al formato interno estándar.
    La estructura varía ligeramente por deporte; intentamos cubrir los casos principales.
    """
    teams = item.get("teams", {})
    # API-Basketball usa "visitors", otros usan "away"
    home = teams.get("home", {})
    away = teams.get("visitors", teams.get("away", {}))

    scores = item.get("scores", item.get("score", {}))
    home_score_obj = scores.get("home", {})
    away_score_obj = scores.get("visitors", scores.get("away", {}))

    # Puntos/goles según deporte
    home_pts = (
        home_score_obj.get("points")
        or home_score_obj.get("score")
        or home_score_obj.get("total")
    )
    away_pts = (
        away_score_obj.get("points")
        or away_score_obj.get("score")
        or away_score_obj.get("total")
    )

    status_obj = item.get("status", {})
    status_str = status_obj.get("long", status_obj.get("short", "SCHEDULED"))

    return {
        "match_id": str(item.get("id", "")),
        "date": item.get("date", item.get("date_start", "")),
        "home_team_id": home.get("id"),
        "away_team_id": away.get("id"),
        "home_team_name": home.get("name", ""),
        "away_team_name": away.get("name", ""),
        "goals_home": home_pts,
        "goals_away": away_pts,
        "league": _SPORT_TO_LEAGUE.get(sport, sport.upper()),
        "status": status_str,
        "sport": sport,
        # Shots no disponibles en API-Sports de forma estándar
        "shots_home": None,
        "shots_away": None,
        "shots_on_target_home": None,
        "shots_on_target_away": None,
    }


async def get_games_today(sport: str) -> list[dict]:
    """
    sport: "nba" | "nfl" | "mlb" | "nhl".
    Devuelve partidos del dia con scores si disponibles.
    """
    if sport in _DISABLED_SPORTS:
        logger.info("api_sports_client: colector %s desactivado — endpoint muerto", sport)
        return []
    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_games_today: %s", e)
        return []

    today = datetime.now(timezone.utc).date().isoformat()

    # API-MMA/UFC usa endpoint diferente
    if sport in ("mma", "ufc"):
        data = await _request(host, "/fights", {"date": today})
    else:
        data = await _request(host, "/games", {"date": today})

    if not data:
        return []

    games = []
    for item in data.get("response", []):
        try:
            games.append(_parse_game(item, sport))
        except Exception:
            logger.error("Error parseando game de API-Sports (%s)", sport, exc_info=True)

    logger.info("get_games_today(%s): %d partidos hoy", sport, len(games))
    return games


_BASKETBALL_HOST = "api-basketball.p.rapidapi.com"

# NBA league_id en api-basketball.p.rapidapi.com (documentación oficial API-Sports)
NBA_LEAGUE_ID = 12


def _current_basketball_season() -> str:
    """Temporada activa de baloncesto, ej. '2025-2026'. Cambia el 1-Oct de cada año."""
    now = datetime.now(timezone.utc)
    start = now.year if now.month >= 10 else now.year - 1
    return f"{start}-{start + 1}"


async def get_nba_games_espn(date_str: str | None = None) -> list[dict]:
    """
    NBA games via ESPN public scoreboard API — sin clave, completamente gratuito.
    Fallback primario cuando api-basketball.p.rapidapi.com devuelve 403 (suscripción no activada).
    URL: https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
    """
    today = date_str or datetime.now(timezone.utc).date().isoformat()
    espn_date = today.replace("-", "")  # YYYYMMDD
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"dates": espn_date})
        if resp.status_code != 200:
            logger.warning("ESPN NBA: HTTP %d para fecha %s", resp.status_code, today)
            return []
        data = resp.json()
        games: list[dict] = []
        for event in data.get("events", []):
            for comp in event.get("competitions", []):
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                state = comp.get("status", {}).get("type", {}).get("state", "pre")
                if state == "post":
                    continue  # partido ya terminado
                status = "LIVE" if state == "in" else "SCHEDULED"
                home_team = home.get("team", {})
                away_team = away.get("team", {})
                # Seed durante playoffs (ESPN lo expone como curveSeed o curRank en el competidor)
                def _extract_seed(c: dict) -> int | None:
                    raw = c.get("curveSeed") or c.get("curRank") or c.get("seed")
                    try:
                        return int(raw) if raw is not None else None
                    except (TypeError, ValueError):
                        return None
                # ESPN embeds Caesars odds in competition.odds — extraer para fallback en basketball_analyzer
                espn_odds = None
                for _o in comp.get("odds", []):
                    _home_ml = (_o.get("homeTeamOdds") or {}).get("moneyLine")
                    _away_ml = (_o.get("awayTeamOdds") or {}).get("moneyLine")
                    if _home_ml and _away_ml:
                        espn_odds = {
                            "home_ml": int(_home_ml),
                            "away_ml": int(_away_ml),
                            "spread": _o.get("details"),       # e.g. "-4.5" or "Cavaliers -4"
                            "total": _o.get("overUnder"),      # e.g. 218.5
                        }
                        break
                games.append({
                    "match_id": str(event.get("id") or comp.get("id") or ""),
                    "date": comp.get("date", event.get("date", "")),
                    "home_team_id": int(home_team.get("id", 0)) or None,
                    "away_team_id": int(away_team.get("id", 0)) or None,
                    "home_team_name": home_team.get("displayName", home_team.get("name", "")),
                    "away_team_name": away_team.get("displayName", away_team.get("name", "")),
                    "home_seed": _extract_seed(home),
                    "away_seed": _extract_seed(away),
                    "goals_home": None,
                    "goals_away": None,
                    "league": "NBA",
                    "status": status,
                    "sport": "nba",
                    "source": "espn",
                    "espn_odds": espn_odds,
                })
        logger.info("ESPN NBA: %d partidos para %s", len(games), today)
        return games
    except Exception:
        logger.error("ESPN NBA: error fetch", exc_info=True)
        return []


async def get_nba_team_stats_espn(espn_team_id: int) -> list[dict]:
    """
    Últimos partidos del equipo NBA desde el schedule ESPN (gratuito, sin clave).
    URL: /apis/site/v2/sports/basketball/nba/teams/{id}/schedule
    Devuelve raw_matches en formato basketball_collector:
      [{goals_home, goals_away, home_team_id, was_home, match_date}]
    Solo partidos completados (ambas puntuaciones > 0).
    """
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{espn_team_id}/schedule"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning("ESPN NBA schedule team %d: HTTP %d", espn_team_id, resp.status_code)
            return []
        data = resp.json()
    except Exception:
        logger.error("ESPN NBA schedule team %d: error fetch", espn_team_id, exc_info=True)
        return []

    matches: list[dict] = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        home_score_raw = home.get("score", {})
        away_score_raw = away.get("score", {})
        goals_home = float(home_score_raw.get("value", 0)) if isinstance(home_score_raw, dict) else 0.0
        goals_away = float(away_score_raw.get("value", 0)) if isinstance(away_score_raw, dict) else 0.0

        if goals_home <= 0 or goals_away <= 0:
            continue  # partido futuro o en curso

        home_tid = int(home.get("team", {}).get("id", 0)) or None
        matches.append({
            "goals_home": goals_home,
            "goals_away": goals_away,
            "home_team_id": home_tid,
            "was_home": home_tid == espn_team_id,
            "match_date": event.get("date", "")[:10],
        })

    logger.info("ESPN NBA schedule team %d: %d partidos completados", espn_team_id, len(matches))
    return matches


async def get_games_by_league(league_id: int, date_str: str | None = None) -> list[dict]:
    """
    GET /games?date=&league=ID&season=YYYY-YYYY via api-basketball.p.rapidapi.com.
    Requiere season además de date+league — sin él la API devuelve [].
    NOTA: Requiere suscripción activa a api-basketball en RapidAPI.
          Si devuelve 403, usar get_nba_games_espn() para NBA.
    """
    today = date_str or datetime.now(timezone.utc).date().isoformat()
    season = _current_basketball_season()
    data = await _request(_BASKETBALL_HOST, "/games", {
        "date": today, "league": league_id, "season": season,
    })
    if not data:
        return []

    games = []
    for item in data.get("response", []):
        try:
            games.append(_parse_game(item, "basketball"))
        except Exception:
            logger.error("Error parseando game league_id=%d", league_id, exc_info=True)

    logger.info("get_games_by_league(%d): %d partidos hoy", league_id, len(games))
    return games


async def discover_leagues(search: str) -> list[dict]:
    """
    GET /leagues?search=term — descubre league_ids en api-basketball.
    Solo para diagnóstico: loguea los resultados, no persiste nada.
    """
    data = await _request(_BASKETBALL_HOST, "/leagues", {"search": search})
    if not data:
        return []
    results = []
    for item in data.get("response", []):
        league = item.get("league", {})
        country = item.get("country", {})
        entry = {
            "id": league.get("id"),
            "name": league.get("name"),
            "type": league.get("type"),
            "country": country.get("name"),
        }
        results.append(entry)
        logger.info("discover_leagues(%r): id=%s name=%r country=%r type=%r",
                    search, entry["id"], entry["name"], entry["country"], entry["type"])
    return results


async def get_team_stats_bdl(
    sport: str, team_id: int, last_n: int = 10
) -> list[dict]:
    """
    Ultimos N partidos del equipo para calcular forma.
    Devuelve lista de partidos normalizados (mismo formato que football_api).
    """
    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_team_stats_bdl: %s", e)
        return []

    # Algunos endpoints de API-Sports usan "last", otros usan paginacion
    if sport in ("mma", "ufc"):
        # MMA/UFC no tiene endpoint de team stats estándar
        logger.info("get_team_stats_bdl: MMA no tiene stats de equipo — omitiendo")
        return []

    data = await _request(host, "/games", {"team": team_id, "last": last_n})
    if not data:
        return []

    matches = []
    for item in data.get("response", []):
        try:
            parsed = _parse_game(item, sport)
            # Solo incluir partidos terminados con resultado
            if parsed["goals_home"] is not None and parsed["goals_away"] is not None:
                matches.append(parsed)
        except Exception:
            logger.error("Error parseando team stats (%s, %d)", sport, team_id, exc_info=True)

    logger.info("get_team_stats_bdl(%s, %d): %d partidos", sport, team_id, len(matches))
    return matches


async def get_injuries(sport: str) -> list[dict]:
    """
    Solo NBA y NFL tienen endpoint de lesiones en API-Sports.
    Devuelve lista de jugadores lesionados actualmente.
    """
    if sport not in ("nba", "nfl"):
        return []

    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_injuries: %s", e)
        return []

    data = await _request(host, "/injuries")
    if not data:
        return []

    injuries = []
    for item in data.get("response", []):
        try:
            player = item.get("player", {})
            team = item.get("team", {})
            injuries.append({
                "player_id": player.get("id"),
                "player_name": player.get("name", ""),
                "team_id": team.get("id"),
                "team_name": team.get("name", ""),
                "status": item.get("status", ""),
                "reason": item.get("reason", ""),
                "sport": sport,
            })
        except Exception:
            logger.error("Error parseando injury (%s)", sport, exc_info=True)

    logger.info("get_injuries(%s): %d lesionados", sport, len(injuries))
    return injuries


async def get_odds_bdl(sport: str, game_id: int) -> dict | None:
    """
    Cuotas en tiempo real si disponibles (API-Sports, solo algunas ligas).
    Devuelve {moneyline_home, moneyline_away, spread, total} o None.
    """
    try:
        host = _get_host(sport)
    except ValueError as e:
        logger.error("get_odds_bdl: %s", e)
        return None

    data = await _request(host, "/odds", {"game": game_id})
    if not data:
        return None

    response = data.get("response", [])
    if not response:
        return None

    try:
        bookmaker = response[0].get("bookmakers", [{}])[0]
        bets = bookmaker.get("bets", [])

        result: dict = {}
        for bet in bets:
            bet_name = bet.get("name", "").lower()
            values = {v["value"]: v.get("odd") for v in bet.get("values", [])}

            if "money line" in bet_name or "moneyline" in bet_name:
                result["moneyline_home"] = values.get("Home")
                result["moneyline_away"] = values.get("Away")
            elif "spread" in bet_name or "handicap" in bet_name:
                result["spread"] = values
            elif "total" in bet_name or "over/under" in bet_name:
                result["total"] = values

        return result if result else None

    except (IndexError, KeyError, TypeError):
        logger.warning("get_odds_bdl(%s, %d): formato de odds inesperado", sport, game_id)
        return None
