"""
services/sports-agent/analyzers/corners_bookings.py

Modelo C: corners y tarjetas (1X2) usando OddsPapi v4 + stats FDCO.

Mercados activos verificados 2026-04-20:
  10764  Corners 1X2 FT      outcomes: 10764=home, 10765=draw, 10766=away
  10911  Bookings 1X2 FT     outcomes: 10911=home, 10912=draw, 10913=away
  101532 Corners 1X2 1H      (43 bkms)
  101120 Bookings 1X2 1H     (6 bkms)

Flujo por partido:
  1. Extraer odds activos de los bookmakers en el fixture OddsPapi v4
  2. Implied prob (vig removida) por bookmaker
  3. Consensus = mediana de implied probs
  4. Si hay stats FDCO en Firestore → Poisson estimate
  5. Edge = max(poisson_estimate - consensus, 0) si Poisson disponible
             o edge_line_shop si solo hay consensus
  6. Señal si edge > MIN_EDGE y confianza > MIN_CONF
"""
import asyncio
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import httpx
import numpy as np
from scipy.stats import poisson as _poisson

from shared.config import ODDSPAPI_KEY, SPORTS_MIN_EDGE, SPORTS_MIN_CONFIDENCE, SPORTS_ALERT_EDGE
from shared.api_quota_manager import quota

logger = logging.getLogger(__name__)

# ── Constantes de mercado ──────────────────────────────────────────────────────
_ODDSPAPI_V4 = "https://api.oddspapi.io/v4"
_HTTP_TIMEOUT = 20.0

# Mínimo de bookmakers para considerar el consensus válido
_MIN_BOOKMAKERS = 5
# Desviación mínima de un bkm vs consensus para line-shopping
_LINE_SHOP_THRESHOLD = 0.05
# Máximo goles/corners a simular en Poisson
_POISSON_MAX = 20

# Mercados a procesar: {marketId: (name, {outcomeId: label})}
MARKET_DEFS: dict[str, tuple[str, dict[str, str]]] = {
    "10764": ("corners_1x2",   {"10764": "home", "10765": "draw", "10766": "away"}),
    "10911": ("bookings_1x2",  {"10911": "home", "10912": "draw", "10913": "away"}),
    "101532": ("corners_1x2_1h", {"101532": "home", "101533": "draw", "101534": "away"}),
    "101120": ("bookings_1x2_1h", {"101120": "home", "101121": "draw", "101122": "away"}),
}

# Mercados binarios (BTTS, Over/Under, Asian Handicap)
# {marketId: (name, type)}  type∈{btts, ou, ah}
# Primer precio activo → opción A (Yes/Over/Home); segundo → opción B (No/Under/Away).
MARKET_DEFS_BINARY: dict[str, tuple[str, str]] = {
    "104":  ("btts",   "btts"),
    "106":  ("ou_ft",  "ou"),
    "1010": ("ou_2_5", "ou"),
    "1068": ("ah_m0_5","ah"),
}

# Mapeo liga interna → tournamentId OddsPapi (verificado)
_TOURNAMENT_IDS: dict[str, int] = {
    "PD":   8,    # La Liga
    "PL":   1,    # Premier League
    "BL1":  4,    # Bundesliga
    "SA":   5,    # Serie A
    "FL1":  2,    # Ligue 1
    "CL":   7,    # Champions League
    "EL":   6,    # Europa League
    "ECL":  480,  # Conference League
    "WC26": 77,   # FIFA World Cup 2026
}

# Cache de fixtures v4 (TTL 24h, clave = "{from}_{to}" en ISO)
_FIXTURES_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(hours=24)

# ROBUSTEZ 429 OddsPapi: backoff corto en memoria. Un 429 de OddsPapi suele ser rate-limit
# (por segundo/minuto), NO agotamiento de la cuota mensual (250). Antes se escribía
# remaining=0 → bloqueaba can_call_monthly TODO el mes con un único 429 espurio (lockout
# observado el 1-jul). El agotamiento mensual REAL se detecta vía el header en las 200.
_ODDSPAPI_BACKOFF = timedelta(minutes=30)
_ODDSPAPI_BACKOFF_UNTIL: "datetime | None" = None


# ── Fetch fixtures OddsPapi v4 ─────────────────────────────────────────────────

def _cached_range_covering(start: date, end: date, now: datetime) -> list | None:
    """
    Devuelve los fixtures de una entrada de caché vigente cuyo rango contenga
    [start, end], o None. Permite que la búsqueda de un día suelto reutilice el
    rango semanal cargado por el pre-fetch en vez de disparar otro request.
    Los llamantes filtran después por equipo (_find_fixture), así que un conjunto
    más amplio es equivalente para ellos.
    """
    for key, (fetched_at, fixtures) in _FIXTURES_CACHE.items():
        if (now - fetched_at) >= _CACHE_TTL:
            continue
        try:
            c_start_s, c_end_s = key.split("_", 1)
            c_start, c_end = date.fromisoformat(c_start_s), date.fromisoformat(c_end_s)
        except ValueError:
            continue
        if c_start <= start and end <= c_end:
            logger.debug(
                "corners_bookings: fixtures %s→%s servidos del rango cacheado %s→%s",
                start, end, c_start, c_end,
            )
            return fixtures
    return None


async def _fetch_fixtures_for_date(target_date: date, to_date: date | None = None) -> list[dict]:
    """
    GET /v4/fixtures?sportId=10&from=DATE&to=DATE
    Devuelve todos los fixtures de fútbol del rango con bookmakerOdds embebidos.
    to_date=None → solo el día target_date.
    """
    if not ODDSPAPI_KEY:
        return []

    end_date = to_date or target_date
    cache_key = f"{target_date}_{end_date}"
    now = datetime.now(timezone.utc)
    cached = _FIXTURES_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    # Reutilizar un rango ya cacheado que CONTENGA el pedido. El pre-fetch de analyze
    # carga [hoy, hoy+7d] de una vez (~4.100 fixtures), pero la búsqueda por partido pedía
    # "{fecha}_{fecha}" — clave distinta, cache miss, y un request nuevo por cada partido.
    # OddsPapi free son 250/mes: cada miss evitado cuenta.
    covering = _cached_range_covering(target_date, end_date, now)
    if covering is not None:
        return covering

    global _ODDSPAPI_BACKOFF_UNTIL
    if _ODDSPAPI_BACKOFF_UNTIL is not None and now < _ODDSPAPI_BACKOFF_UNTIL:
        logger.debug("corners_bookings: OddsPapi en backoff hasta %s — saltando", _ODDSPAPI_BACKOFF_UNTIL)
        return []

    if not quota.can_call_monthly("oddspapi"):
        logger.warning("corners_bookings: oddspapi cuota mensual agotada, saltando fetch")
        return []

    params = {
        "sportId": "10",
        "from": target_date.isoformat(),
        "to": end_date.isoformat(),
        "apiKey": ODDSPAPI_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_ODDSPAPI_V4}/fixtures", params=params)

        if resp.status_code == 429:
            # ROBUSTEZ: 429 = rate-limit, NO agotamiento mensual. Backoff corto en vez de
            # remaining=0 (que lockeaba OddsPapi todo el mes con un único 429 — bug 1-jul).
            _ODDSPAPI_BACKOFF_UNTIL = now + _ODDSPAPI_BACKOFF
            logger.warning("corners_bookings: OddsPapi 429 (rate-limit) — backoff %s", _ODDSPAPI_BACKOFF)
            return []
        if resp.status_code != 200:
            logger.warning("corners_bookings: OddsPapi HTTP %d", resp.status_code)
            return []

        # Leer remaining desde las cabeceras para tracking preciso de cuota
        _remaining = (
            resp.headers.get("x-requests-remaining-month")
            or resp.headers.get("x-monthly-remaining")
            or resp.headers.get("x-remaining")
        )
        quota.track_monthly("oddspapi", remaining=int(_remaining) if _remaining and _remaining.isdigit() else None)
        data = resp.json()
        fixtures = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(fixtures, list):
            fixtures = []

        _FIXTURES_CACHE[cache_key] = (now, fixtures)
        logger.info(
            "corners_bookings: %d fixtures cargados (%s → %s) oddspapi_remaining=%s",
            len(fixtures), target_date.isoformat(), end_date.isoformat(), _remaining,
        )
        return fixtures

    except Exception:
        logger.error("corners_bookings: error fetch fixtures", exc_info=True)
        return []


def _find_fixture(fixtures: list[dict], home_team: str, away_team: str,
                  tournament_id: int | None = None) -> dict | None:
    """
    Busca fixture por nombre de equipo (fuzzy bidireccional, sin acentos).
    Estrategia: prueba múltiples campos de nombre porque OddsPapi v4 varía la estructura.
    Si se pasa tournament_id, filtra primero por tournamentId para mayor precisión.
    """
    import unicodedata, re

    def _norm(s) -> str:
        if isinstance(s, dict):
            s = s.get("name", s.get("shortName", s.get("fullName", "")))
        s = str(s)
        # Eliminar acentos (ü→u, é→e, ñ→n …)
        s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def _match(our: str, api_str: str) -> bool:
        if not our or not api_str or len(api_str) < 3:
            return False
        return our in api_str or api_str in our

    h = _norm(home_team)
    a = _norm(away_team)

    # Campos candidatos donde OddsPapi puede poner el nombre (en orden de preferencia)
    HOME_KEYS = ("participant1Name", "homeTeamName", "homeName",
                 "participant1", "home_team", "homeTeam", "home", "team1")
    AWAY_KEYS = ("participant2Name", "awayTeamName", "awayName",
                 "participant2", "away_team", "awayTeam", "away", "team2")

    pool = fixtures
    if tournament_id is not None:
        tid_str = str(tournament_id)
        pool = [f for f in fixtures
                if str(f.get("tournamentId", f.get("leagueId", f.get("competitionId", "")))) == tid_str]
        if not pool:
            pool = fixtures  # fallback: sin filtro si el torneoId no matchea

    for f in pool:
        # Intentar cada campo de nombre de equipo conocido
        fh = next((_norm(f[k]) for k in HOME_KEYS if k in f and f[k]), "")
        fa = next((_norm(f[k]) for k in AWAY_KEYS if k in f and f[k]), "")

        # Si no encontramos por los campos conocidos, buscar en TODOS los strings del fixture
        if not fh and not fa:
            str_vals = [_norm(v) for v in f.values() if isinstance(v, (str, dict)) and v != f.get("bookmakerOdds")]
            fh = next((v for v in str_vals if _match(h, v)), "")
            fa = next((v for v in str_vals if _match(a, v) and v != fh), "")

        if fh and fa and _match(h, fh) and _match(a, fa):
            return f
    return None


# ── Parser de odds embebidos ───────────────────────────────────────────────────

def _extract_market_odds(fixture: dict, market_id: str, outcome_map: dict[str, str]) -> list[dict]:
    """
    Extrae odds activos para un mercado del formato bookmakerOdds embebido.

    Formato OddsPapi v4:
      bookmakerOdds → {bk_name → {markets → {marketId → {outcomes → {outcomeId → {players → {0 → {price, active}}}}}}}}

    Devuelve lista de {bookmaker, home, draw, away, vig, active_count}
    """
    results = []
    bk_odds = fixture.get("bookmakerOdds", {})

    for bk_name, bk_data in bk_odds.items():
        if not isinstance(bk_data, dict):
            continue
        mkt = bk_data.get("markets", {}).get(market_id)
        if not mkt or not isinstance(mkt, dict):
            continue

        outcomes_data = mkt.get("outcomes", {})
        prices: dict[str, float] = {}

        for oid, outcome in outcomes_data.items():
            label = outcome_map.get(oid)
            if not label:
                continue
            players = outcome.get("players", {})
            for player in players.values():
                if not isinstance(player, dict):
                    continue
                if not player.get("active", False):
                    continue
                price = player.get("price")
                if price and isinstance(price, (int, float)) and price > 1.05:
                    prices[label] = float(price)
                    break

        if len(prices) >= 2 and "home" in prices and "away" in prices:
            results.append({
                "bookmaker": bk_name,
                "home":  prices.get("home", 0.0),
                "draw":  prices.get("draw", 0.0),
                "away":  prices.get("away", 0.0),
            })

    return results


def _extract_binary_odds(fixture: dict, market_id: str) -> list[dict]:
    """
    Extrae dos cuotas (A y B) para mercados binarios OddsPapi (BTTS, OU, AH).
    No asume outcomeIds — usa el primer y segundo precio activo encontrado (sorted por outcomeId).
    Devuelve lista de {bookmaker, a_odds, b_odds}.
    """
    results = []
    bk_odds = fixture.get("bookmakerOdds", {})
    for bk_name, bk_data in bk_odds.items():
        if not isinstance(bk_data, dict):
            continue
        mkt = bk_data.get("markets", {}).get(market_id)
        if not mkt or not isinstance(mkt, dict):
            continue
        outcomes_data = mkt.get("outcomes", {})
        prices: list[float] = []
        for oid in sorted(outcomes_data.keys()):
            outcome = outcomes_data[oid]
            players = outcome.get("players", {})
            for player in players.values():
                if not isinstance(player, dict) or not player.get("active", False):
                    continue
                price = player.get("price")
                if price and isinstance(price, (int, float)) and float(price) > 1.05:
                    prices.append(float(price))
                    break
            if len(prices) == 2:
                break
        if len(prices) == 2:
            results.append({"bookmaker": bk_name, "a_odds": prices[0], "b_odds": prices[1]})
    return results


def _consensus_binary(binary_list: list[dict]) -> dict:
    """Mediana de implied probs vig-removida para mercados binarios."""
    probs = []
    for e in binary_list:
        if e["a_odds"] > 1 and e["b_odds"] > 1:
            ra = 1.0 / e["a_odds"]
            rb = 1.0 / e["b_odds"]
            total = ra + rb
            if total > 0:
                probs.append({"a": ra / total, "b": rb / total})
    if not probs:
        return {}
    return {
        "a": float(np.median([p["a"] for p in probs])),
        "b": float(np.median([p["b"] for p in probs])),
        "n_bookmakers": len(probs),
    }


# ── Implied probabilities y consensus ─────────────────────────────────────────

def _implied_probs(home_odds: float, draw_odds: float, away_odds: float) -> dict[str, float]:
    """Convierte cuotas decimales a probabilidades sin vig (método ratio)."""
    raw = {
        "home": 1.0 / home_odds if home_odds > 1 else 0,
        "draw": 1.0 / draw_odds if draw_odds and draw_odds > 1 else 0,
        "away": 1.0 / away_odds if away_odds > 1 else 0,
    }
    total = sum(raw.values())
    if total <= 0:
        return {"home": 0.33, "draw": 0.33, "away": 0.34}
    return {k: round(v / total, 4) for k, v in raw.items()}


def _consensus(odds_list: list[dict]) -> dict[str, float]:
    """Mediana de implied probs entre bookmakers como estimación de consensus."""
    if not odds_list:
        return {}
    probs = [_implied_probs(o["home"], o["draw"], o["away"]) for o in odds_list]
    return {
        "home": float(np.median([p["home"] for p in probs])),
        "draw": float(np.median([p["draw"] for p in probs])),
        "away": float(np.median([p["away"] for p in probs])),
        "n_bookmakers": len(odds_list),
    }


def _best_odds(odds_list: list[dict]) -> dict[str, tuple[float, str]]:
    """Devuelve la mejor cuota por selección: {home: (odds, bookmaker), ...}"""
    best: dict[str, tuple[float, str]] = {}
    for o in odds_list:
        for sel in ("home", "draw", "away"):
            price = o.get(sel, 0.0)
            if price > best.get(sel, (0, ""))[0]:
                best[sel] = (price, o["bookmaker"])
    return best


# ── Poisson estimate con stats FDCO ───────────────────────────────────────────

def _poisson_1x2(lambda_home: float, lambda_away: float) -> dict[str, float]:
    """
    P(home wins count), P(draw), P(away wins count) usando Poisson bivariado.
    Aplicable tanto a corners como a tarjetas.
    """
    MAX = _POISSON_MAX
    lh = max(0.1, lambda_home)
    la = max(0.1, lambda_away)
    p_home = p_draw = p_away = 0.0
    for i in range(MAX):
        for j in range(MAX):
            p = float(_poisson.pmf(i, lh)) * float(_poisson.pmf(j, la))
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p
    total = p_home + p_draw + p_away
    if total <= 0:
        return {"home": 0.4, "draw": 0.2, "away": 0.4}
    return {
        "home": round(p_home / total, 4),
        "draw": round(p_draw / total, 4),
        "away": round(p_away / total, 4),
    }


async def _load_team_stats(league: str, home_team: str, away_team: str) -> tuple[dict, dict]:
    """Carga stats FDCO de Firestore. Devuelve (home_stats, away_stats), vacíos si no existen."""
    from shared.firestore_client import col
    import re

    def slugify(s):
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    def _get(doc_id):
        try:
            snap = col("team_corner_stats").document(doc_id).get()
            return snap.to_dict() if snap.exists else {}
        except Exception:
            return {}

    loop = asyncio.get_event_loop()
    h_id = f"{league}_{slugify(home_team)}"
    a_id = f"{league}_{slugify(away_team)}"
    home_stats, away_stats = await asyncio.gather(
        loop.run_in_executor(None, _get, h_id),
        loop.run_in_executor(None, _get, a_id),
    )
    return home_stats, away_stats


# ── The Odds API — alternate_totals_corners: RUTA ELIMINADA ──────────────────
# El mercado alternate_totals_corners NO existe en el endpoint bulk
# /v4/sports/{key}/odds (verificado 2026-08-19: 422 en soccer_spain_la_liga,
# soccer_italy_serie_a y soccer_france_ligue_one). Solo responde en el endpoint
# per-event /v4/sports/{key}/events/{id}/odds, a 1 crédito por partido.
#
# No se migra a per-event porque es redundante: odds-api.io ya devuelve
# 'Corners Totals', 'Corner Handicap', 'Total Corners' y 'Alternative Corners'
# de Bet365 y Unibet en el mismo /odds/multi que ya pedimos, sin coste de
# créditos. Gastar los 500/mes de The Odds API en córners nos dejaba sin margen
# para lo único que odds-api.io no da: las cuotas LAY del escáner matched.
#
# La ruta además era activamente dañina: la rama 404 no cacheaba, así que cada
# partido de PL relanzaba el request y se veían 10-15 llamadas fallidas por
# analyze (soccer_england_premier_league ni siquiera es una clave válida — es
# soccer_epl; ver el arreglo de _ODDS_SPORT_MAP en este mismo commit).

# ── Generación de señales ─────────────────────────────────────────────────────

def _make_signal(market_key: str, selection: str, odds: float, bookmaker: str,
                 edge: float, confidence: float, consensus: dict,
                 poisson_prob: float | None, match_date,
                 home_team: str, away_team: str) -> dict | None:
    """Construye el dict de señal si supera thresholds."""
    if edge < SPORTS_MIN_EDGE:
        return None
    if confidence < SPORTS_MIN_CONFIDENCE:
        return None
    return {
        "market":       market_key,
        "selection":    selection,
        "odds":         round(odds, 3),
        "bookmaker":    bookmaker,
        "edge":         round(edge, 4),
        "confidence":   round(confidence, 4),
        "poisson_prob": round(poisson_prob, 4) if poisson_prob else None,
        "consensus_prob": round(consensus.get(selection, 0), 4),
        "n_bookmakers": consensus.get("n_bookmakers", 0),
        "match_date":   str(match_date),
        "home_team":    home_team,
        "away_team":    away_team,
        "source":       "corners_bookings_v1",
    }


async def generate_corners_signals(
    home_team: str,
    away_team: str,
    league: str,
    match_date: date | None = None,
    fixture_data: dict | None = None,
) -> list[dict]:
    """
    Punto de entrada principal. Devuelve señales de corners y tarjetas.

    Fuente única: OddsPapi v4 (corners/bookings 1X2 + mercados binarios).
    Requiere ODDSPAPI_KEY con cuota mensual disponible.
    Las stats FDCO de Firestore alimentan el modelo Poisson de córners.
    """
    if match_date is None:
        match_date = date.today()

    signals: list[dict] = []

    # Cargar stats FDCO
    home_stats, away_stats = await _load_team_stats(league, home_team, away_team)
    has_fdco = bool(home_stats and away_stats)

    # ── Fuente A: OddsPapi (corners/bookings 1X2 + mercados binarios) ─────────
    # Guard 48h: no llamar a OddsPapi por partidos >2 días en el futuro ni ligas sin soporte.
    _days_ahead = (match_date - date.today()).days if isinstance(match_date, date) else 0
    _in_tournament = league in _TOURNAMENT_IDS
    if fixture_data is None and _days_ahead <= 2 and _in_tournament:
        fixtures = await _fetch_fixtures_for_date(match_date)
        fixture_data = _find_fixture(fixtures, home_team, away_team)
    elif not _in_tournament:
        logger.debug("corners_bookings(%s %s): liga %s no en _TOURNAMENT_IDS — saltando OddsPapi", home_team, away_team, league)
    elif _days_ahead > 2:
        logger.debug("corners_bookings(%s %s): partido a %d días — saltando OddsPapi", home_team, away_team, _days_ahead)

    if fixture_data:
        # Log mercados binarios disponibles en el fixture
        _all_market_ids: set[str] = set()
        for _bk in fixture_data.get("bookmakerOdds", {}).values():
            if isinstance(_bk, dict):
                _all_market_ids.update(_bk.get("markets", {}).keys())
        _binary_found = [k for k in MARKET_DEFS_BINARY if k in _all_market_ids]
        if _binary_found:
            logger.info("ODDSPAPI_MARKETS: %s vs %s → mercados binarios: %s",
                        home_team, away_team, _binary_found)

        for market_id, (market_key, outcome_map) in MARKET_DEFS.items():
            odds_list = _extract_market_odds(fixture_data, market_id, outcome_map)
            if len(odds_list) < _MIN_BOOKMAKERS:
                continue

            consensus = _consensus(odds_list)
            best = _best_odds(odds_list)

            poisson_est: dict[str, float] = {}
            if has_fdco:
                if "corners" in market_key:
                    lh = home_stats.get("home_corners", 5.0)
                    la = away_stats.get("away_corners", 4.0)
                else:
                    lh = home_stats.get("home_yellows", 2.0)
                    la = away_stats.get("away_yellows", 2.0)
                poisson_est = _poisson_1x2(lh, la)

            for sel in ("home", "draw", "away"):
                if sel not in best:
                    continue
                best_price, best_bk = best[sel]
                if best_price <= 1.05:
                    continue

                implied = 1.0 / best_price
                consensus_p = consensus.get(sel, 0.0)

                if poisson_est:
                    poisson_p = poisson_est.get(sel, 0.0)
                    edge = round(poisson_p - implied, 4)
                    diff_pc = abs(poisson_p - consensus_p)
                    confidence = round(max(0.0, min(0.99, 1.0 - diff_pc * 3)), 4)
                    poisson_prob = poisson_p
                else:
                    edge = round((1.0 / consensus_p) - best_price, 4) if consensus_p > 0 else 0.0
                    confidence = round(min(0.99, consensus.get("n_bookmakers", 0) / 20), 4)
                    poisson_prob = None

                sig = _make_signal(
                    market_key, sel, best_price, best_bk,
                    edge, confidence, consensus,
                    poisson_prob, match_date, home_team, away_team,
                )
                if sig:
                    signals.append(sig)
                    logger.info(
                        "corners_bookings: SEAL %s %s @ %.2f (%s) edge=%.3f conf=%.3f",
                        market_key, sel, best_price, best_bk, edge, confidence,
                    )

        # Mercados binarios: BTTS, OU, AH (line-shopping, sin Poisson)
        _LABEL_A = {"btts": "Yes",  "ou": "Over",  "ah": "Home"}
        _LABEL_B = {"btts": "No",   "ou": "Under", "ah": "Away"}

        for market_id, (market_key, mtype) in MARKET_DEFS_BINARY.items():
            binary_list = _extract_binary_odds(fixture_data, market_id)
            if len(binary_list) < _MIN_BOOKMAKERS:
                continue

            cons = _consensus_binary(binary_list)
            if not cons:
                continue

            best_a_entry = max(binary_list, key=lambda e: e["a_odds"])
            best_b_entry = max(binary_list, key=lambda e: e["b_odds"])
            n_bk = cons["n_bookmakers"]

            for sel_label, best_entry, best_price, consensus_p in (
                (_LABEL_A[mtype], best_a_entry, best_a_entry["a_odds"], cons["a"]),
                (_LABEL_B[mtype], best_b_entry, best_b_entry["b_odds"], cons["b"]),
            ):
                if best_price <= 1.05 or consensus_p <= 0:
                    continue
                fair_price = 1.0 / consensus_p
                edge = round(fair_price - best_price, 4)
                confidence = round(min(0.99, n_bk / 15), 4)

                sig = _make_signal(
                    market_key, sel_label, best_price, best_entry["bookmaker"],
                    edge, confidence,
                    {"home": consensus_p, "draw": 0.0, "away": 1.0 - consensus_p,
                     "n_bookmakers": n_bk},
                    None, match_date, home_team, away_team,
                )
                if sig:
                    signals.append(sig)
                    logger.info(
                        "corners_bookings: SEAL %s %s @ %.2f (%s) edge=%.3f conf=%.3f",
                        market_key, sel_label, best_price, best_entry["bookmaker"],
                        edge, confidence,
                    )
    else:
        logger.debug(
            "corners_bookings: fixture OddsPapi no encontrado para %s vs %s — "
            "continuando con The Odds API",
            home_team, away_team,
        )

    # (Fuente B — The Odds API alternate_totals_corners — eliminada: el mercado no
    #  existe en el endpoint bulk y en per-event cuesta 1 crédito por partido para
    #  datos que odds-api.io ya sirve gratis. Ver la nota de sección más arriba.)

    return signals


async def save_signals(signals: list[dict], match_id: str, enriched_match: dict | None = None) -> None:
    """
    Guarda señales de corners/bookings en predictions (misma colección que el resto de señales).
    Una señal por documento — mismo esquema que football_markets.py.
    Envía alerta Telegram para señales con edge > SPORTS_ALERT_EDGE.
    """
    if not signals:
        return
    from shared.firestore_client import col

    enriched_match = enriched_match or {}
    league = enriched_match.get("league", "")
    now = datetime.now(timezone.utc)
    saved = 0

    for sig in signals:
        market_key = sig.get("market", "corners")
        selection  = sig.get("selection", "")
        tag        = selection.replace(" ", "_")
        doc_id     = f"{match_id}_{market_key}_{tag}"

        pred = {
            **sig,
            "match_id":        doc_id,
            "sport":           "football",
            "league":          league,
            "market_type":     market_key,
            "calculated_prob": sig.get("poisson_prob") or sig.get("consensus_prob", 0),
            "kelly_fraction":  0.0,
            "factors": {
                "poisson_prob":  sig.get("poisson_prob"),
                "consensus_prob": sig.get("consensus_prob"),
                "n_bookmakers":  sig.get("n_bookmakers", 0),
            },
            "signals":         {},
            "data_source":     "corners_bookings_v1",
            "odds_source":     "oddspapi_v4",
            "weights_version": 0,
            "created_at":      now,
            "result":          None,
            "correct":         None,
            "error_type":      None,
        }

        try:
            col("predictions").document(doc_id).set(pred)
            saved += 1
        except Exception:
            logger.error("corners_bookings: error guardando %s", doc_id, exc_info=True)
            continue

        if float(sig.get("edge", 0)) > SPORTS_ALERT_EDGE:
            try:
                from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload
                await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
            except Exception:
                logger.error("corners_bookings: error enviando alerta Telegram %s", doc_id, exc_info=True)

    logger.info("corners_bookings: %d señales guardadas en predictions para %s", saved, match_id)
