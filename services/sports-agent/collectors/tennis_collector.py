"""
Collector: Tennis API - ATP WTA ITF via RapidAPI.
host: tennis-api-atp-wta-itf.p.rapidapi.com
Recolecta: torneos activos, partidos próximos, rankings, forma por superficie, H2H.
Escribe en Firestore: upcoming_matches + team_stats (usando player_id como clave).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import FOOTBALL_RAPID_API_KEY
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_HOST = "tennis-api-atp-wta-itf.p.rapidapi.com"
_BASE = f"https://{_HOST}"
_HTTP_TIMEOUT = 20.0
_DELAY = 1.5

# Mapeo tipo de torneo → league code en Firestore (coincide con _ODDS_SPORT_MAP)
_TOURNAMENT_LEAGUE_MAP = {
    "French Open": "ATP_FRENCH_OPEN",
    "Roland Garros": "ATP_FRENCH_OPEN",
    "Roland-Garros": "ATP_FRENCH_OPEN",
    "Wimbledon": "ATP_WIMBLEDON",
    "US Open": "ATP_US_OPEN",
    "Australian Open": "ATP_AUS_OPEN",
    "Barcelona Open": "ATP_BARCELONA",
    "Munich": "ATP_MUNICH",
    "Stuttgart": "WTA_STUTTGART",
}

_SURFACE_KEYS = {
    "clay": "clay",
    "grass": "grass",
    "hard": "hard",
    "carpet": "hard",  # agrupado con hard
    "indoor hard": "hard",
    "outdoor hard": "hard",
}


async def _request(path: str, params: dict | None = None) -> dict | None:
    if not FOOTBALL_RAPID_API_KEY:
        logger.warning("tennis_collector: FOOTBALL_RAPID_API_KEY no configurada")
        return None

    await asyncio.sleep(_DELAY)
    headers = {"X-RapidAPI-Key": FOOTBALL_RAPID_API_KEY, "X-RapidAPI-Host": _HOST}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_BASE}{path}", headers=headers, params=params or {})
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            logger.warning("tennis_collector: rate limit — esperando %ds", wait)
            await asyncio.sleep(wait)
            return None
        if resp.status_code >= 400:
            logger.error("tennis_collector: %s → HTTP %d %.150s", path, resp.status_code, resp.text)
            return None
        return resp.json()
    except Exception:
        logger.error("tennis_collector: error en %s", path, exc_info=True)
        return None


def _get_league_code(tournament_name: str, tour: str = "atp") -> str:
    """Mapea nombre de torneo al código de liga interno."""
    for keyword, code in _TOURNAMENT_LEAGUE_MAP.items():
        if keyword.lower() in tournament_name.lower():
            wta_code = code.replace("ATP_", "WTA_")
            return wta_code if tour.lower() == "wta" else code
    # Genérico
    prefix = "WTA" if tour.lower() == "wta" else "ATP"
    slug = tournament_name.upper().replace(" ", "_")[:20]
    return f"{prefix}_{slug}"


async def get_active_tournaments() -> list[dict]:
    """Devuelve torneos ATP y WTA activos o próximos."""
    data = await _request("/tournaments")
    if data is None:
        return []
    results = data.get("results", data if isinstance(data, list) else [])
    if not isinstance(results, list):
        logger.warning("tennis_collector: formato inesperado en /tournaments: %s", type(results))
        return []
    logger.info("tennis_collector: %d torneos obtenidos", len(results))
    return results


async def get_tournament_fixtures(tournament_id: int | str) -> list[dict]:
    """Devuelve partidos de un torneo."""
    data = await _request(f"/fixtures/{tournament_id}")
    if data is None:
        data = await _request("/fixtures", params={"tournament_id": tournament_id})
    if data is None:
        return []
    return data.get("results", data if isinstance(data, list) else [])


async def get_player_rankings(tour: str = "atp", limit: int = 100) -> list[dict]:
    """Devuelve ranking ATP o WTA."""
    data = await _request(f"/rankings/{tour}")
    if data is None:
        data = await _request("/rankings", params={"type": tour})
    if data is None:
        return []
    results = data.get("results", data if isinstance(data, list) else [])
    return results[:limit]


async def get_player_stats(player_id: int | str) -> dict:
    """Devuelve estadísticas por superficie del jugador."""
    data = await _request(f"/player/{player_id}")
    if data is None:
        data = await _request(f"/players/{player_id}")
    if data is None:
        return {}
    return data.get("results", data) if isinstance(data, dict) else {}


async def get_h2h(player1_id: int | str, player2_id: int | str) -> dict:
    """Head-to-head entre dos jugadores."""
    data = await _request(f"/h2h/{player1_id}/{player2_id}")
    if data is None:
        data = await _request("/h2h", params={"p1": player1_id, "p2": player2_id})
    if data is None:
        return {}
    return data.get("results", data) if isinstance(data, dict) else {}


def _compute_form_score(recent_matches: list[dict], player_id: str) -> float:
    """form_score 0-100 basado en últimos 10 partidos (win=10, loss=0)."""
    wins = 0
    total = 0
    for m in recent_matches[:10]:
        winner_id = str(m.get("winner_id", m.get("winner", {}).get("id", "")))
        if winner_id:
            total += 1
            if winner_id == str(player_id):
                wins += 1
    if total == 0:
        return 50.0
    return round((wins / total) * 100, 1)


def _compute_surface_scores(stats: dict) -> dict:
    """Extrae win rates por superficie desde las stats del jugador."""
    surface_data = stats.get("statistics", stats.get("surfaces", {}))
    if not surface_data:
        return {}
    result = {}
    for surface_key, norm_key in _SURFACE_KEYS.items():
        s = surface_data.get(surface_key, {})
        if not s:
            continue
        wins = int(s.get("wins", s.get("win", 0)))
        losses = int(s.get("losses", s.get("loss", 0)))
        total = wins + losses
        if total > 0:
            result[f"win_rate_{norm_key}"] = round(wins / total, 4)
    return result


async def _save_player_stats(player_id: str, player_name: str, ranking: int,
                              recent_matches: list[dict], stats: dict) -> None:
    """Guarda estadísticas del jugador en Firestore team_stats."""
    form_score = _compute_form_score(recent_matches, player_id)
    surface_scores = _compute_surface_scores(stats)

    doc = {
        "player_id": player_id,
        "name": player_name,
        "ranking": ranking,
        "form_score": form_score,
        "raw_matches": recent_matches[:10],
        "streak": {"type": "win" if form_score >= 60 else "loss", "count": 0},
        "updated_at": datetime.now(timezone.utc),
        **surface_scores,
    }
    try:
        col("team_stats").document(str(player_id)).set(doc)
    except Exception:
        logger.error("tennis_collector: error guardando stats de %s", player_id, exc_info=True)


async def collect_tennis_matches(days: int = 7) -> list[dict]:
    """
    Pipeline completo:
    1. Obtiene torneos activos
    2. Por torneo, obtiene partidos próximos
    3. Por partido, guarda stats de ambos jugadores
    4. Devuelve lista de upcoming_matches normalizados
    """
    tournaments = await get_active_tournaments()
    if not tournaments:
        logger.warning("tennis_collector: sin torneos activos")
        return []

    # Rankings para asignar ranking a jugadores
    atp_rankings: dict[str, int] = {}
    wta_rankings: dict[str, int] = {}
    atp_data = await get_player_rankings("atp", 200)
    for entry in atp_data:
        pid = str(entry.get("player_id", entry.get("id", entry.get("player", {}).get("id", ""))))
        rank = int(entry.get("ranking", entry.get("rank", 999)))
        if pid:
            atp_rankings[pid] = rank

    wta_data = await get_player_rankings("wta", 200)
    for entry in wta_data:
        pid = str(entry.get("player_id", entry.get("id", entry.get("player", {}).get("id", ""))))
        rank = int(entry.get("ranking", entry.get("rank", 999)))
        if pid:
            wta_rankings[pid] = rank

    all_matches: list[dict] = []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    players_processed: set[str] = set()

    for t in tournaments[:20]:  # max 20 torneos para no agotar budget
        t_id = t.get("id", t.get("tournament_id", t.get("tournament", {}).get("id")))
        t_name = t.get("name", t.get("tournament", {}).get("name", "Unknown"))
        tour = t.get("type", t.get("tour", t.get("category", "atp"))).lower()
        if "wta" in str(tour).lower() or "women" in str(t_name).lower():
            tour = "wta"
        else:
            tour = "atp"

        if not t_id:
            continue

        league_code = _get_league_code(t_name, tour)
        fixtures = await get_tournament_fixtures(t_id)

        for f in fixtures:
            # Normalizar fecha
            date_raw = f.get("date", f.get("start_at", f.get("match_date", "")))
            try:
                match_date = datetime.fromisoformat(str(date_raw).replace("Z", "+00:00"))
            except Exception:
                match_date = now + timedelta(days=1)

            if match_date > cutoff:
                continue

            # Jugadores
            home_p = f.get("home_player", f.get("player1", f.get("home", {})))
            away_p = f.get("away_player", f.get("player2", f.get("away", {})))
            if isinstance(home_p, str):
                home_p = {"name": home_p}
            if isinstance(away_p, str):
                away_p = {"name": away_p}

            home_id = str(home_p.get("id", home_p.get("player_id", f.get("home_id", ""))))
            away_id = str(away_p.get("id", away_p.get("player_id", f.get("away_id", ""))))
            home_name = home_p.get("name", home_p.get("full_name", "Player 1"))
            away_name = away_p.get("name", away_p.get("full_name", "Player 2"))

            if not home_id or not away_id:
                continue

            match_id = str(f.get("id", f.get("fixture_id", f"{t_id}_{home_id}_{away_id}")))

            # H2H → h2h_advantage
            h2h_data = {}
            if home_id and away_id:
                h2h_data = await get_h2h(home_id, away_id)

            h2h_home_wins = int(h2h_data.get("home_wins", h2h_data.get("player1_wins", 0)))
            h2h_away_wins = int(h2h_data.get("away_wins", h2h_data.get("player2_wins", 0)))
            h2h_total = h2h_home_wins + h2h_away_wins
            h2h_advantage = round((h2h_home_wins - h2h_away_wins) / max(h2h_total, 1), 4)

            # Stats de jugadores (solo si no procesados ya)
            rankings_map = wta_rankings if tour == "wta" else atp_rankings
            for pid, pname in [(home_id, home_name), (away_id, away_name)]:
                if pid not in players_processed:
                    pstats = await get_player_stats(pid)
                    recent = pstats.get("recent_matches", pstats.get("matches", []))
                    ranking = rankings_map.get(pid, 999)
                    await _save_player_stats(pid, pname, ranking, recent, pstats)
                    players_processed.add(pid)

            all_matches.append({
                "match_id": f"tennis_{match_id}",
                "date": match_date.isoformat(),
                "home_team_id": int(home_id) if home_id.isdigit() else hash(home_id) % 1000000,
                "away_team_id": int(away_id) if away_id.isdigit() else hash(away_id) % 1000000,
                "home_team": home_name,
                "away_team": away_name,
                "home_team_name": home_name,
                "away_team_name": away_name,
                "goals_home": None,
                "goals_away": None,
                "league": league_code,
                "status": f.get("status", "SCHEDULED"),
                "sport": "tennis",
                "h2h_advantage": h2h_advantage,
                "surface": t.get("surface", f.get("surface", "hard")),
                "tournament": t_name,
                "source": "tennis_api",
            })

    logger.info("tennis_collector: %d partidos de tenis recolectados", len(all_matches))
    return all_matches
