"""
Fuente UEFA (CL / EL / ECL) vía allsportsapi2 en RapidAPI — espejo de Sofascore.

Por qué esta fuente: football-data.org en plan free devuelve CL con 0 partidos (las rondas
previas y el playoff no entran), EL da 403 y ECL 404. Sofascore directo da 403 desde Cloud
Run. allsportsapi2 sirve los mismos tournament ids con fixtures, resultados CON marcador e
histórico por club, y es el ÚNICO host de RapidAPI al que la clave está suscrita.

Rutas verificadas 2026-08-19 (las de allsports_client.py, /football/..., están muertas):
    /api/tournament/{tid}/seasons
    /api/tournament/{tid}/season/{sid}/matches/{next|last}/{page}
    /api/team/{team_id}/matches/previous/{page}

Cuota: 100 requests/día para toda la clave (cabecera X-RateLimit-Requests-Remaining).
Una página son 30 eventos como máximo, con hasNextPage para saber si hay más.
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx

from shared.config import FOOTBALL_RAPID_API_KEY

logger = logging.getLogger(__name__)

_HOST = "allsportsapi2.p.rapidapi.com"
_BASE = f"https://{_HOST}"
_TIMEOUT = 25.0
_DELAY = 0.4          # cortesía entre llamadas
_PAGE_SIZE = 30       # tamaño real de página observado

# tournament ids (los mismos de Sofascore)
UEFA_TOURNAMENTS: dict[str, int] = {"CL": 7, "EL": 679, "ECL": 17015}

# Caché en proceso del descubrimiento de temporada + TTL del doc en Firestore
_SEASON_CACHE: dict[str, int] = {}
_SEASON_TTL_DAYS = 7
_SEASON_DOC = "uefa_seasons"


def current_season_label(now: datetime | None = None) -> str:
    """
    Etiqueta de campaña europea tal y como la nombra la API: "26/27".
    De agosto a diciembre la campaña es año/año+1; de enero a julio, año-1/año.
    """
    now = now or datetime.now(timezone.utc)
    start = now.year if now.month >= 8 else now.year - 1
    return f"{start % 100:02d}/{(start + 1) % 100:02d}"


async def _request(path: str) -> dict | None:
    """GET contra allsportsapi2 con control de cuota diaria."""
    if not FOOTBALL_RAPID_API_KEY:
        logger.warning("allsports_uefa: FOOTBALL_RAPID_API_KEY no configurada")
        return None

    try:
        from shared.api_quota_manager import QuotaManager
        quota = QuotaManager()
        if not quota.can_call("allsports"):
            logger.warning("allsports_uefa: cuota diaria agotada — %s no se pide", path)
            return None
    except Exception:
        quota = None

    await asyncio.sleep(_DELAY)
    headers = {"x-rapidapi-key": FOOTBALL_RAPID_API_KEY, "x-rapidapi-host": _HOST}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{_BASE}{path}", headers=headers)
    except Exception:
        logger.error("allsports_uefa: error de red en %s", path, exc_info=True)
        return None

    if quota is not None:
        try:
            quota.track_call("allsports", r.headers.get("X-RateLimit-Requests-Remaining"))
        except Exception:
            pass

    if r.status_code == 429:
        logger.warning("allsports_uefa: 429 en %s — cuota diaria agotada", path)
        return None
    if r.status_code == 204:
        # standings/total en temporada recién empezada; no es un error
        return {}
    if r.status_code >= 400:
        logger.error("allsports_uefa: %s → HTTP %d %.120s", path, r.status_code, r.text)
        return None
    try:
        return r.json()
    except Exception:
        logger.error("allsports_uefa: respuesta no-JSON en %s", path, exc_info=True)
        return None


async def discover_season(league: str, force: bool = False) -> int | None:
    """
    season_id de la campaña en curso, descubierto por /seasons — nunca hardcodeado.

    Regla: se busca la temporada cuyo `year` coincide con la campaña derivada de la fecha
    ("26/27"); si no aparece, se usa la primera de la lista (la API las devuelve de más
    reciente a más antigua). El resultado se cachea en Firestore api_meta/uefa_seasons con
    TTL de 7 días, así que el cambio de campaña se resuelve solo — que es exactamente el
    fallo que nos dejó sin datos en el cambio de temporada doméstico.
    """
    if not force and league in _SEASON_CACHE:
        return _SEASON_CACHE[league]

    cached = _read_season_cache()
    entry = cached.get(league) if not force else None
    if entry and _cache_fresh(entry.get("discovered_at")):
        _SEASON_CACHE[league] = int(entry["season_id"])
        return _SEASON_CACHE[league]

    tid = UEFA_TOURNAMENTS.get(league)
    if not tid:
        logger.error("allsports_uefa: liga desconocida %s", league)
        return None

    data = await _request(f"/api/tournament/{tid}/seasons")
    seasons = (data or {}).get("seasons", [])
    if not seasons:
        logger.error("allsports_uefa: /seasons vacío para %s (tid=%d)", league, tid)
        return None

    label = current_season_label()
    ordered = [s for s in seasons if str(s.get("year")) == label] + list(seasons)
    for cand in ordered[:3]:
        sid = cand.get("id")
        if not sid:
            continue
        # Validación: una temporada válida tiene partidos (próximos o jugados).
        probe = await _request(f"/api/tournament/{tid}/season/{sid}/matches/next/0")
        if not (probe or {}).get("events"):
            probe = await _request(f"/api/tournament/{tid}/season/{sid}/matches/last/0")
        if (probe or {}).get("events"):
            _SEASON_CACHE[league] = int(sid)
            _write_season_cache(league, int(sid), str(cand.get("year", "")))
            logger.info(
                "allsports_uefa: %s temporada %s → season_id=%d (descubierta)",
                league, cand.get("year"), sid,
            )
            return int(sid)
        logger.warning(
            "allsports_uefa: %s temporada %s (id=%s) sin partidos — probando la siguiente",
            league, cand.get("year"), sid,
        )
    return None


def _cache_fresh(discovered_at) -> bool:
    if not discovered_at:
        return False
    try:
        dt = (datetime.fromisoformat(str(discovered_at).replace("Z", "+00:00"))
              if isinstance(discovered_at, str) else discovered_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days < _SEASON_TTL_DAYS
    except Exception:
        return False


def _read_season_cache() -> dict:
    try:
        from shared.firestore_client import col
        doc = col("api_meta").document(_SEASON_DOC).get()
        return (doc.to_dict() or {}) if doc.exists else {}
    except Exception:
        logger.debug("allsports_uefa: sin caché de temporadas", exc_info=True)
        return {}


def _write_season_cache(league: str, season_id: int, label: str) -> None:
    try:
        from shared.firestore_client import col
        col("api_meta").document(_SEASON_DOC).set(
            {league: {"season_id": season_id, "label": label,
                      "discovered_at": datetime.now(timezone.utc)}},
            merge=True,
        )
    except Exception:
        logger.warning("allsports_uefa: no se pudo cachear la temporada de %s", league, exc_info=True)


def parse_event(raw: dict, league: str) -> dict | None:
    """
    Evento de allsportsapi2 → dict del schema de save_upcoming_matches.

    Los ids de equipo son los de Sofascore: NO se usan tal cual como team_id, los resuelve
    team_identity.resolve() contra los equipos que ya existen. Aquí se dejan crudos en
    home_source_id / away_source_id.
    """
    try:
        ht, at = raw.get("homeTeam") or {}, raw.get("awayTeam") or {}
        ev_id = raw.get("id")
        if not ev_id or not ht.get("id") or not at.get("id"):
            return None

        ts = raw.get("startTimestamp")
        match_date = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                      if ts else "")
        status_type = (raw.get("status") or {}).get("type", "notstarted")
        gh = (raw.get("homeScore") or {}).get("current")
        ga = (raw.get("awayScore") or {}).get("current")

        return {
            "match_id": f"{league}_SF_{ev_id}",
            "home_source_id": int(ht["id"]),
            "away_source_id": int(at["id"]),
            "home_team": ht.get("name", ""),
            "away_team": at.get("name", ""),
            "goals_home": gh,
            "goals_away": ga,
            "league": league,
            "sport": "football",
            "source": "allsports_uefa",
            "match_date": match_date,
            "date": (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""),
            "round": (raw.get("roundInfo") or {}).get("name", ""),
            "status": "FINISHED" if status_type == "finished" else "SCHEDULED",
        }
    except Exception:
        logger.error("allsports_uefa: error parseando evento", exc_info=True)
        return None


async def fetch_tournament_matches(
    league: str, direction: str = "next", max_pages: int = 2
) -> list[dict]:
    """
    Partidos de una competición. direction='next' (programados) | 'last' (jugados con marcador).
    Pagina mientras hasNextPage y no se pase de max_pages — una página son 30 eventos.
    """
    sid = await discover_season(league)
    tid = UEFA_TOURNAMENTS.get(league)
    if not sid or not tid:
        return []

    out: list[dict] = []
    for page in range(max_pages):
        data = await _request(f"/api/tournament/{tid}/season/{sid}/matches/{direction}/{page}")
        events = (data or {}).get("events", [])
        for e in events:
            parsed = parse_event(e, league)
            if parsed:
                out.append(parsed)
        if not (data or {}).get("hasNextPage") or len(events) < _PAGE_SIZE:
            break
    logger.info("allsports_uefa: %s /%s → %d partidos", league, direction, len(out))
    return out


async def fetch_team_history(source_team_id: int, max_pages: int = 1) -> list[dict]:
    """
    Histórico de un club (todas las competiciones, con marcador). Una página = 30 partidos,
    que en agosto cubre desde marzo: liga doméstica, copa y previas europeas.

    Devuelve el mismo schema que parse_event, con league="" cuando el partido no es de UEFA
    (el nombre del torneo llega en `tournament`, útil para diagnóstico).
    """
    out: list[dict] = []
    for page in range(max_pages):
        data = await _request(f"/api/team/{source_team_id}/matches/previous/{page}")
        events = (data or {}).get("events", [])
        for e in events:
            tourn = ((e.get("tournament") or {}).get("uniqueTournament") or {})
            code = next(
                (k for k, tid in UEFA_TOURNAMENTS.items() if tid == tourn.get("id")), ""
            )
            parsed = parse_event(e, code or "OTHER")
            if parsed:
                parsed["tournament"] = tourn.get("name", "")
                out.append(parsed)
        if not (data or {}).get("hasNextPage") or len(events) < _PAGE_SIZE:
            break
    return out


async def collect_uefa_clubs(leagues: list[str] | None = None) -> dict[int, str]:
    """
    Censo de clubes con partido en las competiciones UEFA (próximos + jugados).
    Devuelve {sofascore_team_id: nombre}. Coste: 2 requests por competición y página.
    """
    leagues = leagues or list(UEFA_TOURNAMENTS)
    clubs: dict[int, str] = {}
    for lg in leagues:
        for direction in ("next", "last"):
            for m in await fetch_tournament_matches(lg, direction):
                clubs[m["home_source_id"]] = m["home_team"]
                clubs[m["away_source_id"]] = m["away_team"]
    logger.info("allsports_uefa: %d clubes distintos en %s", len(clubs), leagues)
    return clubs
