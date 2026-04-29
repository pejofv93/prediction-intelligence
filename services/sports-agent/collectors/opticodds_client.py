"""
collectors/opticodds_client.py

Cliente para optic-odds.io — fallback cuaternario de cuotas.
Base URL: https://api.opticodds.com/api/v3
Auth: header X-Api-Key
Free tier: 1000 req/mes

Interface público:
  get_league_odds(league: str) → list[dict]
    Devuelve eventos normalizados al formato The Odds API:
    [{id, home_team, away_team, competition, bookmakers: [{key, markets: [{key, outcomes: [...]}]}]}]
    Mismo formato que odds_apiio_client — reutiliza _parse_the_odds_event() en value_bet_engine.

NOTA: los league slugs de optic-odds deben verificarse tras el registro en opticodds.io.
Llamar GET /api/v3/sports para ver la lista exacta.
Los valores en _LEAGUE_MAP son estimaciones basadas en convenciones de la API.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import OPTIC_ODDS_KEY
from shared.api_quota_manager import quota

logger = logging.getLogger(__name__)

_BASE = "https://api.opticodds.com/api/v3"
_HTTP_TIMEOUT = 15.0

# Caché en memoria: {league: {"events": list, "error": bool, "cached_at": datetime}}
_CACHE: dict[str, dict] = {}
_TTL_OK  = timedelta(hours=4)
_TTL_ERR = timedelta(minutes=10)


# Mapeo liga interna → (sport_slug, optic_league_slug)
# Verificar slugs exactos tras registro en opticodds.io con: GET /api/v3/sports
_LEAGUE_MAP: dict[str, tuple[str, str]] = {
    "PL":   ("soccer", "EPL"),
    "ELC":  ("soccer", "Championship"),
    "PD":   ("soccer", "LaLiga"),
    "SD":   ("soccer", "LaLiga2"),
    "BL1":  ("soccer", "Bundesliga"),
    "BL2":  ("soccer", "Bundesliga2"),
    "SA":   ("soccer", "SerieA"),
    "SB":   ("soccer", "SerieB"),
    "FL1":  ("soccer", "Ligue1"),
    "FL2":  ("soccer", "Ligue2"),
    "CL":   ("soccer", "UCL"),
    "EL":   ("soccer", "UEL"),
    "ECL":  ("soccer", "UECL"),
    "PPL":  ("soccer", "PrimeiraLiga"),
    "DED":  ("soccer", "Eredivisie"),
    "TU1":  ("soccer", "SuperLig"),
    "BSA":  ("soccer", "Brasileirao"),
    "ARG":  ("soccer", "PrimeraDivision"),
    "CLI":  ("soccer", "CopaLibertadores"),
    "NBA":  ("basketball", "NBA"),
    "EUROLEAGUE": ("basketball", "Euroleague"),
    "ACB":  ("basketball", "ACB"),
}


def _cache_hit(entry: dict | None, now: datetime) -> bool:
    if not entry:
        return False
    ttl = _TTL_ERR if entry.get("error") else _TTL_OK
    return (now - entry["cached_at"]) < ttl


async def _get(path: str, params: dict | None = None) -> dict | list | None:
    """GET autenticado a optic-odds. Auth: X-Api-Key header."""
    if not OPTIC_ODDS_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}{path}",
                headers={"X-Api-Key": OPTIC_ODDS_KEY},
                params=params or {},
            )
        if resp.status_code == 401:
            logger.warning("opticodds: clave inválida (401) — verificar OPTIC_ODDS_KEY")
            return None
        if resp.status_code == 429:
            logger.warning("opticodds: rate limit (429)")
            return None
        if resp.status_code != 200:
            logger.warning("opticodds: HTTP %d para %s", resp.status_code, path)
            return None
        return resp.json()
    except Exception:
        logger.error("opticodds: error en %s", path, exc_info=True)
        return None


def _extract_events(raw: dict | list | None) -> list[dict]:
    """Extrae la lista de eventos de la respuesta (acepta dict con 'data' o lista directa)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return raw.get("data", raw.get("events", raw.get("fixtures", [])))


def _to_float(v) -> float | None:
    try:
        f = float(v)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


def _normalise_event(item: dict) -> dict | None:
    """
    Convierte un evento de optic-odds al formato normalizado de The Odds API.
    Formato salida: {id, home_team, away_team, competition, bookmakers:[{key, markets:[...]}]}
    """
    # Nombres de equipo — optic-odds puede usar distintas claves
    home_obj = item.get("home_competitor") or item.get("home_team") or {}
    away_obj = item.get("away_competitor") or item.get("away_team") or {}

    if isinstance(home_obj, dict):
        home = home_obj.get("name") or home_obj.get("full_name") or ""
    else:
        home = str(home_obj)

    if isinstance(away_obj, dict):
        away = away_obj.get("name") or away_obj.get("full_name") or ""
    else:
        away = str(away_obj)

    if not home or not away:
        return None

    ev_id = str(item.get("id") or item.get("event_id") or "")
    competition = ""
    comp_obj = item.get("league") or item.get("competition") or {}
    if isinstance(comp_obj, dict):
        competition = comp_obj.get("name") or comp_obj.get("slug") or ""
    elif isinstance(comp_obj, str):
        competition = comp_obj

    bookmakers_out = []

    # Optic-odds puede usar "sportsbooks" o "bookmakers"
    raw_books = item.get("sportsbooks") or item.get("bookmakers") or []
    for book in raw_books:
        bk_key = (book.get("id") or book.get("key") or book.get("name") or "unknown").lower()
        markets_out = []

        raw_odds = book.get("odds") or book.get("markets") or {}

        # Formato dict: {"money_line": {"home": 2.1, "draw": 3.2, "away": 3.5}, "totals": [...]}
        if isinstance(raw_odds, dict):
            for mkt_name, mkt_data in raw_odds.items():
                outcomes = _parse_market(mkt_name, mkt_data, home)
                if outcomes:
                    markets_out.append({"key": _normalise_mkt(mkt_name), "outcomes": outcomes})

        # Formato lista: [{"key": "h2h", "outcomes": [...]}]
        elif isinstance(raw_odds, list):
            for mkt in raw_odds:
                mkt_key = mkt.get("key") or mkt.get("name") or mkt.get("type") or ""
                outcomes = _parse_market(mkt_key, mkt.get("outcomes", mkt.get("values", [])), home)
                if outcomes:
                    markets_out.append({"key": _normalise_mkt(mkt_key), "outcomes": outcomes})

        if markets_out:
            bookmakers_out.append({"key": bk_key, "markets": markets_out})

    return {
        "id": ev_id,
        "home_team": home,
        "away_team": away,
        "competition": competition,
        "bookmakers": bookmakers_out,
    }


def _normalise_mkt(raw: str) -> str:
    raw = raw.lower().strip()
    mapping = {
        "money_line": "h2h", "moneyline": "h2h", "1x2": "h2h", "match_winner": "h2h",
        "asian_handicap": "spreads", "handicap": "spreads",
        "over_under": "totals", "total_goals": "totals",
        "btts": "btts", "both_teams_to_score": "btts",
    }
    return mapping.get(raw, raw)


def _parse_market(mkt_key: str, mkt_data, home_team: str) -> list[dict]:
    outcomes: list[dict] = []
    norm = _normalise_mkt(mkt_key)

    if norm == "h2h":
        if isinstance(mkt_data, dict):
            h = _to_float(mkt_data.get("home") or mkt_data.get("1"))
            d = _to_float(mkt_data.get("draw") or mkt_data.get("x"))
            a = _to_float(mkt_data.get("away") or mkt_data.get("2"))
            if h:
                outcomes.append({"name": home_team, "price": h})
            if d:
                outcomes.append({"name": "Draw", "price": d})
            if a:
                outcomes.append({"name": "Away", "price": a})
        elif isinstance(mkt_data, list):
            for o in mkt_data:
                name = o.get("name") or o.get("team") or o.get("label") or ""
                price = _to_float(o.get("price") or o.get("odds") or o.get("value"))
                if price:
                    outcomes.append({"name": name, "price": price})

    elif norm == "totals":
        if isinstance(mkt_data, list):
            for o in mkt_data:
                line = _to_float(o.get("line") or o.get("total"))
                over = _to_float(o.get("over") or o.get("over_price"))
                under = _to_float(o.get("under") or o.get("under_price"))
                if over and under:
                    outcomes.append({"name": "Over", "price": over, "point": line or 2.5})
                    outcomes.append({"name": "Under", "price": under, "point": line or 2.5})

    elif norm == "btts":
        if isinstance(mkt_data, dict):
            yes = _to_float(mkt_data.get("yes") or mkt_data.get("Yes"))
            no = _to_float(mkt_data.get("no") or mkt_data.get("No"))
            if yes:
                outcomes.append({"name": "Yes", "price": yes})
            if no:
                outcomes.append({"name": "No", "price": no})

    return outcomes


async def get_league_odds(league: str) -> list[dict]:
    """
    Devuelve lista de eventos con cuotas normalizados al formato The Odds API.
    Caché en memoria TTL 4h (OK) / 10min (error).
    Registra consumo de cuota en QuotaManager como "opticodds".
    """
    if not OPTIC_ODDS_KEY:
        return []

    now = datetime.now(timezone.utc)
    entry = _CACHE.get(league)
    if _cache_hit(entry, now):
        return entry["events"]

    if not quota.can_call_monthly("opticodds"):
        logger.warning("opticodds: cuota mensual agotada — saltando liga %s", league)
        return []

    mapping = _LEAGUE_MAP.get(league)
    if not mapping:
        logger.debug("opticodds: liga %s no en _LEAGUE_MAP — ignorada", league)
        return []

    sport_slug, league_slug = mapping

    # GET /fixtures/odds devuelve fixtures + odds en un solo request
    raw = await _get("/fixtures/odds", {
        "sport": sport_slug,
        "league": league_slug,
        "status": "unresolved",
    })

    events_raw = _extract_events(raw)

    if not events_raw:
        logger.info("opticodds: %s/%s → 0 eventos", sport_slug, league_slug)
        _CACHE[league] = {"events": [], "error": True, "cached_at": now}
        return []

    quota.track_monthly("opticodds")

    normalised = [e for e in (_normalise_event(item) for item in events_raw)
                  if e and e.get("bookmakers")]

    logger.info("opticodds: %s → %d eventos con odds (de %d raw)",
                league, len(normalised), len(events_raw))

    _CACHE[league] = {"events": normalised, "error": False, "cached_at": now}
    return normalised
