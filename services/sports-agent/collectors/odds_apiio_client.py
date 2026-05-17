"""
collectors/odds_apiio_client.py

Cliente para odds-api.io — fuente primaria de cuotas.
Base URL: https://api.odds-api.io/v3
Auth: ?apiKey=KEY (query param)
Rate limit: 100 req/hora (tier gratuito — confirmado por 429 body)
Monthly limit: no declarado — tracked en QuotaManager como "oddsapiio"

Flujo fútbol (2 pasos, 1 request para TODAS las ligas):
  1. GET /events?sport=soccer         → TODOS los eventos de fútbol (1 request)
     filtrado local por liga (keywords) — elimina N requests por liga
  2. GET /odds/multi?eventIds={ids}  → odds para los eventos filtrados

Flujo otros deportes (baloncesto, tenis): GET /events?sport={slug} por deporte.

El cliente normaliza la respuesta al formato de The Odds API para que
_parse_the_odds_event() funcione sin cambios.

Sport slugs: se descubren via GET /sports (no requiere auth) y se
             cachean en memoria. Mappeo por nombre de competición.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import ODDSAPIIO_KEY
from shared.api_quota_manager import quota

logger = logging.getLogger(__name__)

_BASE = "https://api.odds-api.io/v3"
_HTTP_TIMEOUT = 15.0

# Caché de sports disponibles: {slug: {name, category, ...}}
_SPORTS_CACHE: dict[str, dict] = {}
_SPORTS_CACHED_AT: datetime | None = None
_SPORTS_TTL = timedelta(hours=24)
_SPORTS_LOCK = asyncio.Lock()  # evita que el pre-fetch llame /sports N veces en paralelo

# Caché de eventos por deporte.
# Estructura: {sport_slug: {"events": list, "error": bool, "cached_at": datetime}}
#   error=True  → respuesta fue 429/400/vacía por fallo de API → TTL corto (_TTL_ERR)
#   error=False → respuesta real (aunque sea 0 eventos legítimos) → TTL largo (_TTL_OK)
_SPORT_EVENTS_CACHE: dict[str, dict] = {}
_SPORT_EVENTS_LOCK = asyncio.Lock()

# Caché de eventos normalizados por liga.
# Misma estructura: {league_code: {"events": list, "error": bool, "cached_at": datetime}}
_EVENT_CACHE: dict[str, dict] = {}

# TTLs diferenciados
_TTL_OK         = timedelta(hours=24)     # respuesta real con eventos
_TTL_ERR        = timedelta(seconds=60)   # error genérico (400, sin slug, etc.)
_TTL_RATE_LIMIT = timedelta(seconds=3600) # 429 — esperar reset completo de 1h

# Clave especial en _EVENT_CACHE para el bloque global de todos los eventos de fútbol.
# Un solo request /events?sport=soccer cubre PL, BL1, SA, CLI, etc. simultáneamente.
_SOCCER_ALL_KEY  = "__soccer_all__"
_SOCCER_ALL_LOCK = asyncio.Lock()

# Mapeo liga interna → palabras clave del nombre de competición en odds-api.io.
# Múltiples variantes por liga porque odds-api.io puede cambiar slugs entre versiones.
# Se compara contra _build_comp_string(ev) que agrega slug+name+competition+tournament+…
_LEAGUE_KEYWORDS: dict[str, list[str]] = {
    "PL":   ["england-premier-league", "premier-league", "premier league",
             "english premier", "epl"],
    "PD":   ["spain-primera-division", "spain-laliga", "laliga", "la-liga",
             "la liga", "primera division", "primera-division"],
    "BL1":  ["germany-bundesliga", "bundesliga", "1. bundesliga", "1.bundesliga",
             "german bundesliga"],
    "SA":   ["italy-serie-a", "serie-a", "serie a", "italian serie"],
    "FL1":  ["france-ligue-1", "ligue-1", "ligue 1", "french ligue",
             "ligue 1 uber"],
    "CL":   ["uefa-champions-league", "champions-league", "champions league",
             "ucl", "uefa cl"],
    "EL":   ["uefa-europa-league", "europa-league", "europa league", "uel"],
    "ECL":  ["conference-league", "europa-conference", "uecl"],
    "TU1":  ["turkey-super-lig", "super-lig", "super lig", "turkish super"],
    "CLI":  ["conmebol-libertadores", "copa-libertadores", "libertadores",
             "copa libertadores"],
    "BSA":  ["brazil-campeonato", "campeonato-brasileiro", "brasileiro",
             "serie-a-brasileirao"],
    "ARG":  ["argentina-primera-division", "primera-division-argentina",
             "liga profesional"],
    "NBA":  ["nba", "national-basketball", "usa-nba", "usa - nba"],
    "ACB":  ["spain-acb", "liga-acb", "liga acb", "acb", "spain acb", "spain - acb",
             "liga endesa", "endesa"],
    "EUROLEAGUE": ["euroleague", "euro-league", "turkish airlines euroleague",
                   "international-euroleague", "international - euroleague"],
    "ATP_FRENCH_OPEN": ["roland-garros", "french-open", "french open"],
    "ATP_WIMBLEDON":   ["wimbledon"],
    "ATP_US_OPEN":     ["us-open", "us open"],
    "ATP_AUS_OPEN":    ["australian-open", "australian open"],
    "ATP_MADRID":      ["madrid-open", "mutua-madrid", "madrid open"],
    "ATP_ROME":        ["internazionali", "rome", "italian open"],
    "ATP_BARCELONA":   ["barcelona-open", "conde-de-godo", "barcelona open"],
}

# Sport category → odds-api.io top-level sport slug (descubierto via /sports)
# Fallbacks si /sports falla o no está en caché todavía.
_SPORT_FALLBACK_SLUGS: dict[str, list[str]] = {
    "football": ["soccer", "football", "soccer_football"],
    "basketball": ["basketball", "nba"],
    "tennis": ["tennis"],
}

# Ligas que son fútbol / baloncesto / tenis (para decidir qué sport slug buscar)
_FOOTBALL_LEAGUES = {"PL","PD","BL1","SA","FL1",
                     "CL","EL","ECL","TU1","ARG","CLI","BSA","CSUD","CAM"}
_BASKETBALL_LEAGUES = {"NBA","EUROLEAGUE","ACB"}
_TENNIS_LEAGUES = {"ATP_FRENCH_OPEN","ATP_WIMBLEDON","ATP_US_OPEN","ATP_AUS_OPEN",
                   "ATP_MADRID","ATP_ROME","ATP_BARCELONA",
                   "WTA_FRENCH_OPEN","WTA_WIMBLEDON","WTA_US_OPEN","WTA_AUS_OPEN"}


# ── Internals ──────────────────────────────────────────────────────────────────

async def _get(path: str, params: dict | None = None) -> dict | list | None:
    """Llamada GET autenticada a odds-api.io. Auth: ?apiKey=KEY (query param)."""
    if not ODDSAPIIO_KEY:
        return None
    try:
        merged = {**(params or {}), "apiKey": ODDSAPIIO_KEY}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}{path}",
                params=merged,
            )
        if resp.status_code == 429:
            logger.warning("odds-api.io: rate limit 429")
            return None
        if resp.status_code == 401:
            logger.warning("odds-api.io: clave inválida (401) — verificar ODDSAPIIO_KEY")
            return None
        if resp.status_code != 200:
            logger.warning("odds-api.io: HTTP %d para %s", resp.status_code, path)
            return None
        return resp.json()
    except Exception:
        logger.error("odds-api.io: error en %s", path, exc_info=True)
        return None


async def discover_sports() -> dict[str, dict]:
    """
    GET /sports — lista de todos los deportes disponibles.
    No requiere auth. Cacheado 24h en memoria con lock para evitar llamadas paralelas.
    Devuelve {slug: sport_dict}.
    """
    global _SPORTS_CACHE, _SPORTS_CACHED_AT
    now = datetime.now(timezone.utc)
    if _SPORTS_CACHED_AT and (now - _SPORTS_CACHED_AT) < _SPORTS_TTL and _SPORTS_CACHE:
        return _SPORTS_CACHE
    async with _SPORTS_LOCK:
        # Re-check dentro del lock
        if _SPORTS_CACHED_AT and (now - _SPORTS_CACHED_AT) < _SPORTS_TTL and _SPORTS_CACHE:
            return _SPORTS_CACHE
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(f"{_BASE}/sports")
            if resp.status_code != 200:
                logger.warning("odds-api.io: /sports HTTP %d", resp.status_code)
                return _SPORTS_CACHE
            data = resp.json()
            # La respuesta puede ser lista o dict {data: [...]}
            sports = data if isinstance(data, list) else data.get("data", data.get("sports", []))
            result: dict[str, dict] = {}
            for s in sports:
                slug = s.get("slug") or s.get("key") or s.get("id") or ""
                if slug:
                    result[slug.lower()] = s
            _SPORTS_CACHE = result
            _SPORTS_CACHED_AT = now
            logger.info("odds-api.io: %d sports descubiertos", len(result))
            return result
        except Exception:
            logger.error("odds-api.io: error obteniendo /sports", exc_info=True)
            return _SPORTS_CACHE


def _league_to_category(league: str) -> str:
    if league in _FOOTBALL_LEAGUES:
        return "football"
    if league in _BASKETBALL_LEAGUES:
        return "basketball"
    if league in _TENNIS_LEAGUES:
        return "tennis"
    return "football"


async def _find_sport_slug(category: str) -> str | None:
    """Busca el slug de odds-api.io que corresponde a la categoría."""
    sports = await discover_sports()
    keywords = {"football": ["soccer", "football"], "basketball": ["basketball"],
                "tennis": ["tennis"]}.get(category, [category])
    for slug, sport in sports.items():
        name = (sport.get("name") or sport.get("title") or "").lower()
        for kw in keywords:
            if kw in slug or kw in name:
                return slug
    # Fallback: intentar los slugs predeterminados directamente
    for slug in _SPORT_FALLBACK_SLUGS.get(category, [category]):
        return slug
    return None


# Slugs a probar en orden para fútbol — "soccer" suele ser el slug real en odds-api.io
_FOOTBALL_SLUG_CANDIDATES = ["soccer", "football", "soccer_football"]

# Casas de apuestas para /odds/multi — plan free de odds-api.io permite max 2.
# Bookmakers permitidos en el plan actual: Bet365, Unibet.
# Enviar más de 2 devuelve 403 "Access denied. You're allowed max 2 bookmakers."
_DEFAULT_BOOKMAKERS = "Bet365,Unibet"

# Caché global de odds por eventId: {event_id: odds_item}
# Poblado una sola vez por _prefetch_priority_odds() en _fetch_all_soccer_events().
# _ODDS_MAP_PREFETCHED_AT != None significa "pre-fetch intentado" (aunque cache vacío).
# Esto evita que _fetch_odds_map_for_events() reintente HTTP por cada liga.
_ODDS_MAP_CACHE: dict[str, dict] = {}
_ODDS_MAP_CACHED_AT: datetime | None = None
_ODDS_MAP_PREFETCHED_AT: datetime | None = None   # None = nunca intentado
_ODDS_MAP_LOCK = asyncio.Lock()

# Ligas prioritarias para pre-fetch de odds. CLI/BSA/ARG quedan fuera:
# sus bookmakers (1xbet, etc.) no cubren CONMEBOL sistemáticamente
# y se sirven con Poisson sintético cuando faltan odds reales.
_PRIORITY_LEAGUES_FOR_ODDS: frozenset[str] = frozenset({
    "PD", "SA", "BL1", "PL", "FL1", "CL", "EL", "ECL", "TU1",
})
_MAX_ODDS_PREFETCH: int = 50   # 50 IDs / 10 por batch = 5 requests /odds/multi


def _cache_ttl(entry: dict) -> timedelta:
    """Devuelve el TTL aplicable a una entrada de caché.
    rate_limited=True → ttl_override si presente, si no _TTL_RATE_LIMIT (3600s)
    error=True        → _TTL_ERR (60s)         — reintento rápido
    sin error         → _TTL_OK  (4h)           — resultado real
    """
    if entry.get("rate_limited"):
        override = entry.get("ttl_override")
        if override:
            return timedelta(seconds=override)
        return _TTL_RATE_LIMIT
    return _TTL_ERR if entry.get("error") else _TTL_OK


def _cache_hit(entry: dict | None, now: datetime) -> bool:
    """True si la entrada existe y no ha expirado según su TTL."""
    if not entry:
        return False
    age = now - entry["cached_at"]
    return age < _cache_ttl(entry)


async def _get_raw(path: str, params: dict | None = None) -> tuple[int, str, dict | list | None]:
    """
    Versión diagnóstica de _get() que devuelve (status_code, body_text, parsed_json|None).
    Siempre loguea status + primeros 500 chars del body para diagnóstico.
    """
    if not ODDSAPIIO_KEY:
        return 0, "NO_KEY", None
    try:
        merged = {**(params or {}), "apiKey": ODDSAPIIO_KEY}
        url = f"{_BASE}{path}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=merged)
        body = resp.text[:500]
        logger.info(
            "odds-api.io DIAG: GET %s params=%s → status=%d body=%s",
            path, {k: v for k, v in merged.items() if k != "apiKey"}, resp.status_code, body,
        )
        if resp.status_code != 200:
            return resp.status_code, resp.text, None
        try:
            return resp.status_code, resp.text, resp.json()
        except Exception:
            logger.warning("odds-api.io DIAG: body no es JSON válido")
            return resp.status_code, resp.text, None
    except Exception:
        logger.error("odds-api.io DIAG: error en %s", path, exc_info=True)
        return 0, "EXCEPTION", None


async def _fetch_events(sport_slug: str) -> list[dict]:
    """
    GET /events?sport={slug} → lista de eventos.
    Cachea por sport_slug con lock. TTL diferenciado:
      error=True  (429/400/sin respuesta) → 60s
      error=False (respuesta real)        → 4h
    """
    now = datetime.now(timezone.utc)

    # Outer check: respeta TTL según flag error (no loguear — hit silencioso)
    entry = _SPORT_EVENTS_CACHE.get(sport_slug)
    if _cache_hit(entry, now):
        return entry["events"]

    # DIAG_FETCH: solo cuando vamos a hacer HTTP real
    logger.info("DIAG_FETCH: llamando /events sport=%s", sport_slug)

    async with _SPORT_EVENTS_LOCK:
        # Inner check (otra corutina puede haber poblado el caché mientras esperábamos)
        entry = _SPORT_EVENTS_CACHE.get(sport_slug)
        if _cache_hit(entry, now):
            return entry["events"]

        from_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_dt   = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

        candidates = [sport_slug]
        if sport_slug not in _FOOTBALL_SLUG_CANDIDATES:
            candidates += _FOOTBALL_SLUG_CANDIDATES
        else:
            candidates += [s for s in _FOOTBALL_SLUG_CANDIDATES if s != sport_slug]

        best_raw: list[dict] = []
        winning_slug: str = sport_slug

        def _set_error(slugs: list[str]) -> None:
            """Cachea error=True (TTL 60s) para todos los slugs dados."""
            err_entry = {"events": [], "error": True, "cached_at": now}
            for s in slugs:
                _SPORT_EVENTS_CACHE[s] = err_entry

        for candidate in candidates:
            # Intento 1: sin filtro temporal
            status, body, data = await _get_raw("/events", {"sport": candidate})

            if status == 429:
                logger.warning("odds-api.io: 429 slug=%s body=%s — todos los slugs cacheados 60s",
                               candidate, body[:200])
                _set_error(list(set([candidate] + _FOOTBALL_SLUG_CANDIDATES)))
                return []

            if status == 401:
                logger.warning("odds-api.io: 401 — ODDSAPIIO_KEY inválida body=%s", body[:200])
                return []

            if status not in (200, 0) and data is None:
                logger.warning("odds-api.io: HTTP %d slug=%s body=%s", status, candidate, body[:200])
                _set_error([candidate])
                continue

            if data is not None:
                raw = data if isinstance(data, list) else data.get("data", data.get("events", []))
                if isinstance(raw, list) and raw:
                    statuses: dict[str, int] = {}
                    for ev in raw:
                        s = ev.get("status", "MISSING")
                        statuses[s] = statuses.get(s, 0) + 1
                    logger.info("odds-api.io DIAG slug=%s: %d eventos status_counts=%s sample=%s",
                                candidate, len(raw), statuses, str(raw[0])[:200])
                    best_raw = raw
                    winning_slug = candidate
                    break
                else:
                    logger.info("odds-api.io DIAG slug=%s: 0 eventos (sin filtro temporal)", candidate)

            # Intento 2: con ventana temporal
            status2, body2, data2 = await _get_raw(
                "/events", {"sport": candidate, "commenceTimeFrom": from_dt, "commenceTimeTo": to_dt}
            )
            if data2 is not None:
                raw2 = data2 if isinstance(data2, list) else data2.get("data", data2.get("events", []))
                if isinstance(raw2, list) and raw2:
                    statuses2: dict[str, int] = {}
                    for ev in raw2:
                        s = ev.get("status", "MISSING")
                        statuses2[s] = statuses2.get(s, 0) + 1
                    logger.info("odds-api.io DIAG slug=%s +timeRange: %d eventos status_counts=%s",
                                candidate, len(raw2), statuses2)
                    best_raw = raw2
                    winning_slug = candidate
                    break
                else:
                    logger.info("odds-api.io DIAG slug=%s +timeRange: 0 eventos", candidate)

        if not best_raw:
            logger.warning("odds-api.io: 0 eventos en todos los candidatos=%s — error TTL 60s", candidates)
            _set_error([sport_slug])
            return []

        # Filtrar pending — si 0 pending, pasar todos (para que get_league_odds filtre por liga)
        pending = [e for e in best_raw if e.get("status") == "pending"]
        if not pending:
            logger.warning("odds-api.io: slug=%s %d eventos pero 0 pending — pasando todos", winning_slug, len(best_raw))
            pending = best_raw

        logger.info("odds-api.io: slug=%s → %d pending de %d totales", winning_slug, len(pending), len(best_raw))

        ok_entry = {"events": pending, "error": False, "cached_at": now}
        _SPORT_EVENTS_CACHE[winning_slug] = ok_entry
        if winning_slug != sport_slug:
            _SPORT_EVENTS_CACHE[sport_slug] = ok_entry

        return pending


async def _fetch_odds_batch(event_ids: list[str]) -> list[dict]:
    """
    GET /odds/multi?eventIds={ids}&bookmakers={_DEFAULT_BOOKMAKERS}
    Máximo 10 IDs por llamada. El param 'bookmakers' es obligatorio (error "Missing bookmakers" sin él).
    En la primera llamada usa _get_raw para loguear el body si hay error.
    """
    if not event_ids:
        return []
    all_results = []
    _first_call = True
    for i in range(0, len(event_ids), 10):
        batch = event_ids[i:i+10]
        params = {
            "eventIds": ",".join(batch),
            "bookmakers": _DEFAULT_BOOKMAKERS,
            "markets": "h2h,btts,total_goals,asian_handicap,corners,correct_score,1st_half_goals",
        }
        if _first_call:
            status, body, data = await _get_raw("/odds/multi", params)
            _first_call = False
            if status != 200:
                logger.warning("odds-api.io: /odds/multi HTTP %d body=%s", status, body[:300])
                if status == 429:
                    break  # rate limit — no seguir con más batches
                continue
        else:
            data = await _get("/odds/multi", params)
        if data is None:
            continue
        items = data if isinstance(data, list) else data.get("data", data.get("odds", []))
        if isinstance(items, list):
            all_results.extend(items)
        quota.track_monthly("oddsapiio")
    return all_results


async def _fetch_odds_map_for_events(event_ids: list[str]) -> dict[str, dict]:
    """
    Devuelve {event_id: odds_item} para los IDs pedidos.
    Si _prefetch_priority_odds() ya corrió (PREFETCHED_AT != None y dentro de TTL),
    retorna del caché sin ningún HTTP — evita N×batches por liga que agotan 100 req/h.
    Fallback HTTP solo si el pre-fetch nunca ocurrió (instancia fría sin soccer events).
    """
    global _ODDS_MAP_CACHED_AT
    now = datetime.now(timezone.utc)

    async with _ODDS_MAP_LOCK:
        # Pre-fetch ya corrió y su caché sigue vigente → solo lookups, cero HTTP
        if (_ODDS_MAP_PREFETCHED_AT is not None
                and (now - _ODDS_MAP_PREFETCHED_AT) < _TTL_OK):
            return {eid: _ODDS_MAP_CACHE[eid] for eid in event_ids if eid in _ODDS_MAP_CACHE}

        # Fallback: instancia que no pasó por _fetch_all_soccer_events (no debería ocurrir)
        if _ODDS_MAP_CACHED_AT and (now - _ODDS_MAP_CACHED_AT) < _TTL_OK:
            return {eid: _ODDS_MAP_CACHE[eid] for eid in event_ids if eid in _ODDS_MAP_CACHE}

        items = await _fetch_odds_batch(event_ids)
        for item in items:
            eid = str(item.get("eventId") or item.get("id") or "")
            if eid:
                _ODDS_MAP_CACHE[eid] = item
        _ODDS_MAP_CACHED_AT = now
        return {eid: _ODDS_MAP_CACHE[eid] for eid in event_ids if eid in _ODDS_MAP_CACHE}


def _normalise_event(raw_event: dict, odds_item: dict | None) -> dict | None:
    """
    Convierte un evento + odds de odds-api.io al formato de The Odds API:
    {id, home_team, away_team, bookmakers: [{key, markets: [{key, outcomes:[{name, price}]}]}]}
    """
    # Extraer nombres de equipo — la API puede usar distintas claves
    home = (raw_event.get("homeTeam") or raw_event.get("home_team") or
            raw_event.get("home") or raw_event.get("teamHome") or "")
    away = (raw_event.get("awayTeam") or raw_event.get("away_team") or
            raw_event.get("away") or raw_event.get("teamAway") or "")
    ev_id = str(raw_event.get("id") or raw_event.get("eventId") or "")
    lg = raw_event.get("league") or {}
    if isinstance(lg, dict):
        competition = lg.get("slug") or lg.get("name") or ""
    else:
        competition = (raw_event.get("competition") or str(lg) or
                       raw_event.get("tournament") or raw_event.get("competitionName") or "")

    if not home or not away:
        return None

    bookmakers_out = []
    markets_agg: dict[str, list] = {}

    if odds_item:
        raw_bkms = (odds_item.get("bookmakers") or odds_item.get("data") or [])

        if isinstance(raw_bkms, dict):
            # odds-api.io v3 format: {"Bet365": [{name, odds:[{home,draw,away}]}], ...}
            for bkm_name, mkt_list in raw_bkms.items():
                bkm_key = bkm_name.lower().replace(" ", "_")
                markets_out = []
                if not isinstance(mkt_list, list):
                    mkt_list = [mkt_list] if mkt_list else []
                for mkt in mkt_list:
                    if not isinstance(mkt, dict):
                        continue
                    mkt_key = mkt.get("name") or mkt.get("key") or mkt.get("type") or ""
                    # Odds are nested under "odds": [{"home":..., "draw":..., "away":...}]
                    raw_odds = mkt.get("odds") or mkt.get("outcomes") or []
                    if (isinstance(raw_odds, list) and raw_odds
                            and isinstance(raw_odds[0], dict)
                            and any(k in raw_odds[0]
                                    for k in ("home", "draw", "away", "1", "2", "x"))):
                        mkt_data = raw_odds[0]
                    else:
                        mkt_data = raw_odds or mkt
                    outcomes = _parse_market(mkt_key, mkt_data, home)
                    if outcomes:
                        norm_key = _normalise_market_key(mkt_key)
                        markets_out.append({"key": norm_key, "outcomes": outcomes})
                        markets_agg.setdefault(norm_key, []).append(
                            {"bookmaker": bkm_key, "outcomes": outcomes}
                        )
                if markets_out:
                    bookmakers_out.append({"key": bkm_key, "markets": markets_out})

        elif isinstance(raw_bkms, list):
            # Generic list format: [{name/key, markets: {...}|[...]}]
            for bkm in raw_bkms:
                bkm_key = (bkm.get("slug") or bkm.get("key") or
                           bkm.get("name") or "unknown").lower().replace(" ", "_")
                markets_out = []
                raw_markets = bkm.get("markets") or {}

                if isinstance(raw_markets, dict):
                    for mkt_key, mkt_data in raw_markets.items():
                        outcomes = _parse_market(mkt_key, mkt_data, home)
                        if outcomes:
                            norm_key = _normalise_market_key(mkt_key)
                            markets_out.append({"key": norm_key, "outcomes": outcomes})
                            markets_agg.setdefault(norm_key, []).append(
                                {"bookmaker": bkm_key, "outcomes": outcomes}
                            )
                elif isinstance(raw_markets, list):
                    for mkt in raw_markets:
                        mkt_key = mkt.get("key") or mkt.get("type") or mkt.get("name") or ""
                        outcomes = _parse_market(mkt_key, mkt.get("outcomes", []), home)
                        if outcomes:
                            norm_key = _normalise_market_key(mkt_key)
                            markets_out.append({"key": norm_key, "outcomes": outcomes})
                            markets_agg.setdefault(norm_key, []).append(
                                {"bookmaker": bkm_key, "outcomes": outcomes}
                            )

                if markets_out:
                    bookmakers_out.append({"key": bkm_key, "markets": markets_out})

    extra_keys = [k for k in markets_agg if k != "h2h"]
    if extra_keys:
        logger.info("MARKETS_PARSED: %s vs %s → %s", home, away, extra_keys)
    markets_best = _extract_markets_summary(markets_agg, home) if markets_agg else {}

    return {
        "id": ev_id,
        "home_team": home,
        "away_team": away,
        "competition": competition,
        "bookmakers": bookmakers_out,
        "markets": markets_best,
    }


def _normalise_market_key(raw: str) -> str:
    """Normaliza claves de mercado al estándar de The Odds API."""
    raw = raw.lower().strip()
    mapping = {
        "1x2": "h2h", "moneyline": "h2h", "ml": "h2h", "match_winner": "h2h",
        "match winner": "h2h", "1_x_2": "h2h", "three_way": "h2h",
        "asian_handicap": "spreads", "handicap": "spreads", "ah": "spreads",
        "over_under": "totals", "totals": "totals", "goals": "totals",
        "over/under": "totals", "total_goals": "totals",
        "btts": "btts", "both_teams_to_score": "btts", "gg": "btts",
        "correct_score": "correct_score", "score": "correct_score",
        "1st_half_goals": "ht_totals", "halftime_goals": "ht_totals",
        "1st_half_total": "ht_totals", "ht_over_under": "ht_totals",
        "1st_half_total_goals": "ht_totals", "h1_totals": "ht_totals",
        "first_half_goals": "ht_totals", "first_half_total": "ht_totals",
        "corners": "corners_ou", "corner": "corners_ou",
        "total_corners": "corners_ou", "corners_over_under": "corners_ou",
        "corners_totals": "corners_ou",
    }
    return mapping.get(raw, raw)


def _parse_market(mkt_key: str, mkt_data, home_team: str) -> list[dict]:
    """Parsea un mercado a lista de outcomes {name, price}."""
    outcomes = []
    norm = _normalise_market_key(mkt_key)

    if norm == "h2h":
        if isinstance(mkt_data, dict):
            h = _to_float(mkt_data.get("home") or mkt_data.get("1") or mkt_data.get("homeOdds"))
            d = _to_float(mkt_data.get("draw") or mkt_data.get("x") or mkt_data.get("drawOdds"))
            a = _to_float(mkt_data.get("away") or mkt_data.get("2") or mkt_data.get("awayOdds"))
            if h and h > 1:
                outcomes.append({"name": home_team, "price": h})
            if d and d > 1:
                outcomes.append({"name": "Draw", "price": d})
            if a and a > 1:
                outcomes.append({"name": "Away", "price": a})
        elif isinstance(mkt_data, list):
            for o in mkt_data:
                name = o.get("name") or o.get("team") or ""
                price = _to_float(o.get("price") or o.get("odds"))
                if price and price > 1:
                    outcomes.append({"name": name, "price": price})

    elif norm == "totals":
        if isinstance(mkt_data, list):
            for o in mkt_data:
                line = _to_float(o.get("line") or o.get("total") or o.get("handicap"))
                over = _to_float(o.get("over") or o.get("overOdds"))
                under = _to_float(o.get("under") or o.get("underOdds"))
                if line and over and under:
                    outcomes.append({"name": "Over", "price": over, "point": line})
                    outcomes.append({"name": "Under", "price": under, "point": line})
        elif isinstance(mkt_data, dict):
            line = _to_float(mkt_data.get("line") or mkt_data.get("total"))
            over = _to_float(mkt_data.get("over"))
            under = _to_float(mkt_data.get("under"))
            if over and under:
                outcomes.append({"name": "Over", "price": over, "point": line or 2.5})
                outcomes.append({"name": "Under", "price": under, "point": line or 2.5})

    elif norm == "spreads":
        if isinstance(mkt_data, list):
            for o in mkt_data:
                name = o.get("name") or o.get("team") or ""
                price = _to_float(o.get("price") or o.get("odds"))
                point = _to_float(o.get("point") or o.get("handicap") or o.get("line"))
                if price and price > 1:
                    outcomes.append({"name": name, "price": price, "point": point})

    elif norm == "btts":
        if isinstance(mkt_data, dict):
            yes = _to_float(mkt_data.get("yes") or mkt_data.get("Yes"))
            no = _to_float(mkt_data.get("no") or mkt_data.get("No"))
            if yes and yes > 1:
                outcomes.append({"name": "Yes", "price": yes})
            if no and no > 1:
                outcomes.append({"name": "No", "price": no})

    elif norm == "correct_score":
        if isinstance(mkt_data, list):
            for o in mkt_data:
                name = o.get("name") or o.get("score") or o.get("label") or ""
                price = _to_float(o.get("price") or o.get("odds") or o.get("odd"))
                if name and price:
                    name = name.replace(":", "-").replace(" ", "").strip()
                    outcomes.append({"name": name, "price": price})

    elif norm == "ht_totals":
        # Same structure as totals but for first half
        if isinstance(mkt_data, list):
            for o in mkt_data:
                line = _to_float(o.get("line") or o.get("total") or o.get("handicap"))
                over = _to_float(o.get("over") or o.get("overOdds"))
                under = _to_float(o.get("under") or o.get("underOdds"))
                if line and over and under:
                    outcomes.append({"name": "Over", "price": over, "point": line})
                    outcomes.append({"name": "Under", "price": under, "point": line})
        elif isinstance(mkt_data, dict):
            line = _to_float(mkt_data.get("line") or mkt_data.get("total"))
            over = _to_float(mkt_data.get("over"))
            under = _to_float(mkt_data.get("under"))
            if over and under:
                outcomes.append({"name": "Over", "price": over, "point": line or 0.5})
                outcomes.append({"name": "Under", "price": under, "point": line or 0.5})

    elif norm == "corners_ou":
        # Corners Over/Under — misma estructura que totals (líneas típicas: 8.5, 9.5, 10.5, 11.5)
        if isinstance(mkt_data, list):
            for o in mkt_data:
                line = _to_float(o.get("line") or o.get("total") or o.get("handicap"))
                over = _to_float(o.get("over") or o.get("overOdds"))
                under = _to_float(o.get("under") or o.get("underOdds"))
                if line and over and under and 5.0 <= line <= 20.0:  # filtrar líneas absurdas
                    outcomes.append({"name": "Over", "price": over, "point": line})
                    outcomes.append({"name": "Under", "price": under, "point": line})
        elif isinstance(mkt_data, dict):
            line = _to_float(mkt_data.get("line") or mkt_data.get("total"))
            over = _to_float(mkt_data.get("over"))
            under = _to_float(mkt_data.get("under"))
            if over and under and (line is None or 5.0 <= line <= 20.0):
                outcomes.append({"name": "Over", "price": over, "point": line or 9.5})
                outcomes.append({"name": "Under", "price": under, "point": line or 9.5})

    return outcomes


def _to_float(v) -> float | None:
    try:
        f = float(v)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


def _build_comp_string(ev: dict) -> str:
    """
    Agrega todos los campos de competición de un evento en una cadena lowercase.
    odds-api.io cambia los nombres de campos entre versiones de la API;
    esta función comprueba todos los candidatos conocidos para maximizar el matching.
    """
    parts: list[str] = []
    lg = ev.get("league") or {}
    if isinstance(lg, dict):
        for k in ("slug", "name", "id", "title", "shortName"):
            v = lg.get(k)
            if v and isinstance(v, str):
                parts.append(v.lower())
    elif lg:
        parts.append(str(lg).lower())

    for field in (
        "competition", "competitionName", "competitionSlug",
        "tournament", "tournamentName",
        "category", "categoryName",
        "sport_title", "leagueName", "league_name",
        "division", "cup", "groupName", "name",
    ):
        v = ev.get(field)
        if not v:
            continue
        if isinstance(v, str):
            parts.append(v.lower())
        elif isinstance(v, dict):
            for sub in ("slug", "name", "id", "title"):
                sv = v.get(sub)
                if sv and isinstance(sv, str):
                    parts.append(sv.lower())

    return " ".join(parts)


def _extract_markets_summary(markets_agg: dict[str, list], home: str) -> dict:
    """
    Construye el mejor precio por mercado a partir de los datos de todos los bookmakers.
    Entrada: {norm_key: [{bookmaker, outcomes: [{name, price[, point]}]}]}
    Salida:  {h2h: {...}, btts: {...}, totals: [...], spreads: [...]}
    """
    out: dict = {}
    for mkt_key, entries in markets_agg.items():

        if mkt_key == "h2h":
            bh = bd = ba = 0.0
            bk = ""
            for e in entries:
                for o in e["outcomes"]:
                    p = float(o.get("price") or 0)
                    if o.get("name") == home and p > bh:
                        bh, bk = p, e["bookmaker"]
                    elif o.get("name") == "Draw" and p > bd:
                        bd = p
                    elif o.get("name") not in (home, "Draw") and p > ba:
                        ba = p
            if bh and ba:
                out["h2h"] = {
                    "home_odds": round(bh, 3),
                    "draw_odds": round(bd, 3) if bd else None,
                    "away_odds": round(ba, 3),
                    "bookmaker": bk,
                }

        elif mkt_key == "btts":
            by_ = bn = 0.0
            bk = ""
            for e in entries:
                for o in e["outcomes"]:
                    p = float(o.get("price") or 0)
                    if o.get("name") == "Yes" and p > by_:
                        by_, bk = p, e["bookmaker"]
                    elif o.get("name") == "No" and p > bn:
                        bn = p
            if by_:
                out["btts"] = {
                    "yes_odds": round(by_, 3),
                    "no_odds": round(bn, 3) if bn else None,
                    "bookmaker": bk,
                }

        elif mkt_key == "totals":
            by_line: dict[float, dict] = {}
            for e in entries:
                bk = e["bookmaker"]
                for o in e["outcomes"]:
                    ln = round(float(o.get("point") or 2.5), 1)
                    if ln not in by_line:
                        by_line[ln] = {"line": ln, "over_odds": 0.0, "under_odds": 0.0, "bookmaker": ""}
                    p = float(o.get("price") or 0)
                    if o.get("name") == "Over" and p > by_line[ln]["over_odds"]:
                        by_line[ln]["over_odds"] = p
                        by_line[ln]["bookmaker"] = bk
                    elif o.get("name") == "Under" and p > by_line[ln]["under_odds"]:
                        by_line[ln]["under_odds"] = p
            out["totals"] = [
                {k: round(v, 3) if isinstance(v, float) else v for k, v in entry.items()}
                for entry in by_line.values()
                if entry["over_odds"] and entry["under_odds"]
            ]

        elif mkt_key == "spreads":
            by_pt: dict[float, dict] = {}
            for e in entries:
                bk = e["bookmaker"]
                for idx, o in enumerate(e["outcomes"]):
                    pt = round(float(o.get("point") or 0.0), 2)
                    if pt not in by_pt:
                        by_pt[pt] = {"point": pt, "home_odds": 0.0, "away_odds": 0.0, "bookmaker": ""}
                    p = float(o.get("price") or 0)
                    name = o.get("name", "")
                    is_home = (name == home) or (not name and idx == 0)
                    if is_home and p > by_pt[pt]["home_odds"]:
                        by_pt[pt]["home_odds"] = p
                        by_pt[pt]["bookmaker"] = bk
                    elif not is_home and p > by_pt[pt]["away_odds"]:
                        by_pt[pt]["away_odds"] = p
            out["spreads"] = [
                {k: round(v, 3) if isinstance(v, float) else v for k, v in entry.items()}
                for entry in by_pt.values()
                if entry["home_odds"] and entry["away_odds"]
            ]

        elif mkt_key == "correct_score":
            by_score: dict[str, dict] = {}
            for e in entries:
                bk = e["bookmaker"]
                for o in e["outcomes"]:
                    score = o.get("name", "").replace(":", "-").strip()
                    p = float(o.get("price") or 0)
                    if score and p > 1:
                        if score not in by_score or p > by_score[score]["odds"]:
                            by_score[score] = {"odds": round(p, 3), "bookmaker": bk}
            if by_score:
                out["correct_score"] = by_score

        elif mkt_key == "ht_totals":
            by_ht_line: dict[float, dict] = {}
            for e in entries:
                bk = e["bookmaker"]
                for o in e["outcomes"]:
                    ln = round(float(o.get("point") or 0.5), 1)
                    if ln not in by_ht_line:
                        by_ht_line[ln] = {"line": ln, "over_odds": 0.0, "under_odds": 0.0, "bookmaker": ""}
                    p = float(o.get("price") or 0)
                    if o.get("name") == "Over" and p > by_ht_line[ln]["over_odds"]:
                        by_ht_line[ln]["over_odds"] = p
                        by_ht_line[ln]["bookmaker"] = bk
                    elif o.get("name") == "Under" and p > by_ht_line[ln]["under_odds"]:
                        by_ht_line[ln]["under_odds"] = p
            out["ht_totals"] = [
                {k: round(v, 3) if isinstance(v, float) else v for k, v in entry.items()}
                for entry in by_ht_line.values()
                if entry["over_odds"] and entry["under_odds"]
            ]

        elif mkt_key == "corners_ou":
            by_cn_line: dict[float, dict] = {}
            for e in entries:
                bk = e["bookmaker"]
                for o in e["outcomes"]:
                    ln = round(float(o.get("point") or 9.5), 1)
                    if ln not in by_cn_line:
                        by_cn_line[ln] = {"line": ln, "over_odds": 0.0, "under_odds": 0.0, "bookmaker": ""}
                    p = float(o.get("price") or 0)
                    if o.get("name") == "Over" and p > by_cn_line[ln]["over_odds"]:
                        by_cn_line[ln]["over_odds"] = p
                        by_cn_line[ln]["bookmaker"] = bk
                    elif o.get("name") == "Under" and p > by_cn_line[ln]["under_odds"]:
                        by_cn_line[ln]["under_odds"] = p
            out["corners_ou"] = [
                {k: round(v, 3) if isinstance(v, float) else v for k, v in entry.items()}
                for entry in by_cn_line.values()
                if entry["over_odds"] and entry["under_odds"]
            ]

    return out


# ── Public interface ───────────────────────────────────────────────────────────

def clear_caches() -> dict:
    """
    Limpia todos los cachés en memoria de odds-api.io.
    Útil tras un rate limit 429 para forzar reintento inmediato.
    """
    global _SPORTS_CACHE, _SPORTS_CACHED_AT, _ODDS_MAP_CACHED_AT, _ODDS_MAP_PREFETCHED_AT
    n_events = len(_SPORT_EVENTS_CACHE)
    n_leagues = len(_EVENT_CACHE)
    _SPORT_EVENTS_CACHE.clear()
    _EVENT_CACHE.clear()
    _ODDS_MAP_CACHE.clear()
    _ODDS_MAP_CACHED_AT = None
    _ODDS_MAP_PREFETCHED_AT = None
    _SPORTS_CACHE = {}
    _SPORTS_CACHED_AT = None
    logger.info("odds-api.io: cachés limpiados (sports=%d, events=%d ligas)", n_events, n_leagues)
    return {"cleared": {"sport_events": n_events, "league_events": n_leagues, "sports": True}}


async def _prefetch_priority_odds(all_events: list[dict], now: datetime) -> None:
    """
    Pre-carga odds para ligas prioritarias en _ODDS_MAP_CACHE.
    Muestreo por liga: _PREFETCH_PER_LEAGUE eventos más próximos por cada liga
    prioritaria usando su keyword más específico (primero de la lista, incluye país).
    Esto evita que el orden cronológico global llene la caché con ligas asiáticas
    que empiezan antes que las ligas EU (BL1/PD/SA/PL juegan a las 14-21h UTC).
    """
    global _ODDS_MAP_PREFETCHED_AT

    _PREFETCH_PER_LEAGUE = 5  # eventos por liga → 9 ligas × 5 = máx 45 IDs

    def _commence_key(ev: dict) -> str:
        for field in ("commenceTime", "commence_time", "startTime", "start_time", "date", "kickoff"):
            v = ev.get(field)
            if v:
                return str(v)
        return ""

    # Muestreo por liga con el keyword más específico (primero = incluye país)
    seen_ids: set[str] = set()
    priority_events: list[dict] = []

    for lg in _PRIORITY_LEAGUES_FOR_ODDS:
        lg_keywords = _LEAGUE_KEYWORDS.get(lg, [])
        if not lg_keywords:
            continue
        # Usar el keyword más específico (primero de la lista: "england-premier-league" etc.)
        specific_kw = lg_keywords[0]
        lg_events = sorted(
            [ev for ev in all_events if specific_kw in _build_comp_string(ev)],
            key=_commence_key,
        )
        if not lg_events:
            # Fallback: intentar con todos los keywords de la liga
            lg_events = sorted(
                [ev for ev in all_events
                 if any(kw in _build_comp_string(ev) for kw in lg_keywords)],
                key=_commence_key,
            )
        added = 0
        for ev in lg_events:
            if added >= _PREFETCH_PER_LEAGUE:
                break
            eid = str(ev.get("id") or ev.get("eventId") or "")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                priority_events.append(ev)
                added += 1
        if lg_events:
            logger.info("odds-api.io: pre-fetch %s → %d candidatos → %d añadidos",
                        lg, len(lg_events), added)

    if not priority_events and all_events:
        _sample_fields = ("id", "league", "competition", "competitionName",
                          "tournament", "sport_title", "leagueName", "name")
        _sample = [{k: ev.get(k) for k in _sample_fields if ev.get(k)}
                   for ev in all_events[:5]]
        logger.warning(
            "odds-api.io: pre-fetch — 0 prioritarios de %d eventos con keywords EU. "
            "Fallback: primeros %d sin filtro liga. Sample: %s",
            len(all_events), _MAX_ODDS_PREFETCH, str(_sample)[:800],
        )
        priority_events = all_events[:_MAX_ODDS_PREFETCH]

    capped = priority_events[:_MAX_ODDS_PREFETCH]
    event_ids = [
        str(ev.get("id") or ev.get("eventId") or "")
        for ev in capped if ev.get("id") or ev.get("eventId")
    ]

    logger.info(
        "odds-api.io: pre-fetch odds — %d prioritarios → %d IDs (~%d req)",
        len(priority_events), len(event_ids), -(-len(event_ids) // 10),
    )

    async with _ODDS_MAP_LOCK:
        if event_ids:
            items = await _fetch_odds_batch(event_ids)
            for item in items:
                eid = str(item.get("eventId") or item.get("id") or "")
                if eid:
                    _ODDS_MAP_CACHE[eid] = item
        _ODDS_MAP_PREFETCHED_AT = now  # siempre marcar — bloquea reintentos por liga

    logger.info(
        "odds-api.io: pre-fetch completado — %d odds en cache (de %d IDs pedidos)",
        len(_ODDS_MAP_CACHE), len(event_ids),
    )


async def _fetch_all_soccer_events() -> list[dict]:
    """
    Un único GET /events?sport=soccer que cubre TODAS las ligas de fútbol.
    Resultado cacheado en _EVENT_CACHE[_SOCCER_ALL_KEY].
      éxito  → TTL 4h   (_TTL_OK)
      429    → TTL 3600s (_TTL_RATE_LIMIT) — esperar reset completo del rate limit
      otros  → TTL 60s   (_TTL_ERR)
    Solo se hace 1 request por analyze en lugar de N (uno por liga).
    """
    now = datetime.now(timezone.utc)

    entry = _EVENT_CACHE.get(_SOCCER_ALL_KEY)
    if _cache_hit(entry, now):
        return entry["events"]

    async with _SOCCER_ALL_LOCK:
        entry = _EVENT_CACHE.get(_SOCCER_ALL_KEY)
        if _cache_hit(entry, now):
            return entry["events"]

        logger.info("odds-api.io: fetch soccer global (1 request para todas las ligas)")

        from_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_dt   = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for slug in _FOOTBALL_SLUG_CANDIDATES:
            status, body, data = await _get_raw(
                "/events",
                {"sport": slug, "commenceTimeFrom": from_dt, "commenceTimeTo": to_dt},
            )

            if status == 429:
                import re as _re
                _m = _re.search(r"resets in (\d+) minutes? and (\d+) seconds?", body)
                if _m:
                    _ttl = int(_m.group(1)) * 60 + int(_m.group(2)) + 30
                else:
                    _ttl = 3600
                _EVENT_CACHE[_SOCCER_ALL_KEY] = {
                    "events": [], "error": True, "rate_limited": True,
                    "cached_at": now, "ttl_override": _ttl,
                }
                logger.warning(
                    "odds-api.io: 429 rate limit — TTL=%ds — body=%s", _ttl, body[:200]
                )
                return []

            if status == 401:
                logger.warning("odds-api.io: 401 key inválida — body=%s", body[:100])
                return []

            if data is not None:
                raw = data if isinstance(data, list) else data.get("data", data.get("events", []))
                if isinstance(raw, list) and raw:
                    logger.info(
                        "odds-api.io: slug=%s → %d eventos soccer (1 request)", slug, len(raw)
                    )
                    _EVENT_CACHE[_SOCCER_ALL_KEY] = {"events": raw, "error": False, "cached_at": now}
                    # Pre-fetch de odds para ligas prioritarias mientras tenemos los eventos frescos
                    await _prefetch_priority_odds(raw, now)
                    return raw
                logger.info("odds-api.io: slug=%s → 0 eventos", slug)

        logger.warning("odds-api.io: 0 eventos en todos los slugs de fútbol — error TTL 60s")
        _EVENT_CACHE[_SOCCER_ALL_KEY] = {"events": [], "error": True, "cached_at": now}
        return []


async def get_league_odds(league: str) -> list[dict]:
    """
    Devuelve lista de eventos con cuotas normalizados al formato The Odds API.

    Para ligas de FÚTBOL: usa _fetch_all_soccer_events() — 1 request por analyze
    para TODAS las ligas. Filtra los eventos globales por keywords de la liga.

    Para otros deportes (baloncesto, tenis): mantiene flujo por-sport original.
    """
    if not ODDSAPIIO_KEY:
        logger.warning("odds-api.io: ODDSAPIIO_KEY no configurada — saltando liga %s", league)
        return []

    now = datetime.now(timezone.utc)

    # Cache check — respeta TTL según flag error (60s error, 4h ok)
    entry = _EVENT_CACHE.get(league)
    if _cache_hit(entry, now):
        age_s = int((now - entry["cached_at"]).total_seconds())
        ttl_s = int(_cache_ttl(entry).total_seconds())
        logger.info(
            "DIAG_CACHE_HIT: _EVENT_CACHE[%s] → %d eventos error=%s age=%ds TTL=%ds",
            league, len(entry["events"]), entry["error"], age_s, ttl_s,
        )
        return entry["events"]

    if not quota.can_call_monthly("oddsapiio"):
        logger.warning(
            "DIAG_QUOTA_BLOCK: quota.can_call_monthly('oddsapiio')=False — saltando liga %s",
            league,
        )
        return []

    category = _league_to_category(league)

    # ── FÚTBOL: un solo request global, filtrado local por liga ──────────────
    if category == "football":
        all_events = await _fetch_all_soccer_events()
        all_count = len(all_events)

        if not all_events:
            _EVENT_CACHE[league] = {"events": [], "error": True, "cached_at": now}
            return []

        quota.track_monthly("oddsapiio")

        keywords = _LEAGUE_KEYWORDS.get(league, [])
        filtered = []
        for ev in all_events:
            if not keywords or any(kw in _build_comp_string(ev) for kw in keywords):
                filtered.append(ev)

        logger.info("odds-api.io: %d eventos soccer totales, %d para %s (keywords=%s)",
                    all_count, len(filtered), league, keywords)

        if not filtered:
            if all_events:
                _sample_comps = [_build_comp_string(ev) for ev in all_events[:5]]
                logger.warning(
                    "odds-api.io: %s — 0 de %d eventos coinciden con keywords=%s. "
                    "Comp strings muestra: %s",
                    league, all_count, keywords, _sample_comps,
                )
            _EVENT_CACHE[league] = {"events": [], "error": True, "cached_at": now}
            return []

        # Obtener odds via caché global — compartido entre todas las ligas del analyze
        # (evita N×batches de /odds/multi, una sola carga para todas las ligas)
        event_ids = [str(ev.get("id") or ev.get("eventId") or "") for ev in filtered
                     if ev.get("id") or ev.get("eventId")]
        odds_map = await _fetch_odds_map_for_events(event_ids)

        normalised = []
        for ev in filtered:
            eid = str(ev.get("id") or ev.get("eventId") or "")
            result = _normalise_event(ev, odds_map.get(eid))
            if result and result.get("bookmakers"):
                normalised.append(result)

        logger.info("odds-api.io: %s → %d con odds (de %d filtrados, %d totales soccer)",
                    league, len(normalised), len(filtered), all_count)

        if normalised:
            _EVENT_CACHE[league] = {"events": normalised, "error": False, "cached_at": now}
        else:
            logger.warning("odds-api.io: %s — %d filtrados pero 0 con odds — TTL 60s", league, len(filtered))
            _EVENT_CACHE[league] = {"events": [], "error": True, "cached_at": now}

        return normalised

    # ── OTROS DEPORTES: flujo original por-sport (baloncesto, tenis) ─────────
    logger.info("DIAG_GET_LEAGUE: caché miss + quota OK → llamando _find_sport_slug para %s", league)

    sport_slug = await _find_sport_slug(category)
    if not sport_slug:
        logger.warning("odds-api.io: no se encontró sport slug para %s (%s)", league, category)
        _EVENT_CACHE[league] = {"events": [], "error": True, "cached_at": now}
        return []

    raw_events = await _fetch_events(sport_slug)
    if not raw_events:
        _EVENT_CACHE[league] = {"events": [], "error": True, "cached_at": now}
        return []

    quota.track_monthly("oddsapiio")

    keywords = _LEAGUE_KEYWORDS.get(league, [])
    filtered = [ev for ev in raw_events if not keywords or any(
        kw in _build_comp_string(ev) for kw in keywords
    )]

    if not filtered:
        _EVENT_CACHE[league] = {"events": [], "error": True, "cached_at": now}
        return []

    event_ids = [str(ev.get("id") or ev.get("eventId") or "") for ev in filtered
                 if ev.get("id") or ev.get("eventId")]
    odds_map = {}
    for item in await _fetch_odds_batch(event_ids):
        eid = str(item.get("eventId") or item.get("id") or "")
        if eid:
            odds_map[eid] = item

    normalised = []
    for ev in filtered:
        eid = str(ev.get("id") or ev.get("eventId") or "")
        r = _normalise_event(ev, odds_map.get(eid))
        if r and r.get("bookmakers"):
            normalised.append(r)

    if normalised:
        _EVENT_CACHE[league] = {"events": normalised, "error": False, "cached_at": now}
    else:
        _EVENT_CACHE[league] = {"events": [], "error": True, "cached_at": now}

    return normalised
