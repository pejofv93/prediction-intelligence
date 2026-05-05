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

from shared.config import ODDSPAPI_KEY, ODDS_API_KEY, SPORTS_MIN_EDGE, SPORTS_MIN_CONFIDENCE, SPORTS_ALERT_EDGE
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
    "PD":  8,    # La Liga
    "PL":  1,    # Premier League
    "BL1": 4,    # Bundesliga
    "SA":  5,    # Serie A
    "FL1": 2,    # Ligue 1
    "CL":  7,    # Champions League
    "EL":  6,    # Europa League
}

# Cache de fixtures v4 (TTL 24h, clave = fecha)
_FIXTURES_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(hours=24)

# Cache de eventos The Odds API con corners (TTL 1h, clave = sport_key)
_THEODDS_CORNERS_CACHE: dict[str, tuple[datetime, list]] = {}
_THEODDS_CACHE_TTL = timedelta(hours=1)
_THEODDS_BASE = "https://api.the-odds-api.com/v4/sports"


# ── Fetch fixtures OddsPapi v4 ─────────────────────────────────────────────────

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
            logger.warning("corners_bookings: OddsPapi rate limit 429")
            quota.track_monthly("oddspapi", remaining=0)  # marcar agotada en quota manager
            return []
        if resp.status_code != 200:
            logger.warning("corners_bookings: OddsPapi HTTP %d", resp.status_code)
            return []

        quota.track_monthly("oddspapi")
        data = resp.json()
        fixtures = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(fixtures, list):
            fixtures = []

        _FIXTURES_CACHE[cache_key] = (now, fixtures)
        logger.info(
            "corners_bookings: %d fixtures cargados (%s → %s)",
            len(fixtures), target_date.isoformat(), end_date.isoformat(),
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


# ── The Odds API — alternate_totals_corners ───────────────────────────────────

async def _fetch_corners_theodds(sport_key: str) -> list[dict]:
    """
    GET /v4/sports/{sport_key}/odds?markets=alternate_totals_corners
    Devuelve eventos con líneas O/U corners. Cache 1h por sport_key.
    422 = mercado no disponible en el plan actual → cachea vacío para no reintentar.
    """
    if not ODDS_API_KEY:
        return []

    now = datetime.now(timezone.utc)
    cached = _THEODDS_CORNERS_CACHE.get(sport_key)
    if cached and (now - cached[0]) < _THEODDS_CACHE_TTL:
        return cached[1]

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "alternate_totals_corners",
        "oddsFormat": "decimal",
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_THEODDS_BASE}/{sport_key}/odds", params=params)

        if resp.status_code == 422:
            # Mercado no disponible en el plan free para este sport_key
            logger.info(
                "corners_bookings[theodds]: corners no disponible (422) para %s", sport_key
            )
            _THEODDS_CORNERS_CACHE[sport_key] = (now, [])
            return []
        if resp.status_code == 401:
            logger.warning("corners_bookings[theodds]: ODDS_API_KEY inválida (401)")
            return []
        if resp.status_code == 429:
            logger.warning("corners_bookings[theodds]: rate limit 429")
            return []
        if resp.status_code != 200:
            logger.warning(
                "corners_bookings[theodds]: HTTP %d para %s", resp.status_code, sport_key
            )
            return []

        events = resp.json()
        if not isinstance(events, list):
            events = []
        _THEODDS_CORNERS_CACHE[sport_key] = (now, events)
        logger.info(
            "corners_bookings[theodds]: %d eventos corners para %s", len(events), sport_key
        )
        return events

    except Exception:
        logger.error("corners_bookings[theodds]: error fetch %s", sport_key, exc_info=True)
        return []


def _find_event_theodds(events: list[dict], home_team: str, away_team: str) -> dict | None:
    """Busca evento en The Odds API por nombre de equipo (fuzzy, sin acentos)."""
    import unicodedata
    import re

    def _n(s: str) -> str:
        s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", s.lower())

    h, a = _n(home_team), _n(away_team)
    for ev in events:
        eh = _n(ev.get("home_team", ""))
        ea = _n(ev.get("away_team", ""))
        if (h in eh or eh in h) and (a in ea or ea in a):
            return ev
    return None


def _extract_corners_ou_consensus(event: dict) -> dict | None:
    """
    Calcula la probabilidad implícita (vig removida) de O/U corners para la línea
    con más cobertura de bookmakers.
    Devuelve {line, over_prob, under_prob, n_bookmakers} o None si < 2 bookmakers.
    """
    line_raw: dict[float, list[tuple[float, float]]] = {}

    for bkm in event.get("bookmakers", []):
        for mkt in bkm.get("markets", []):
            if mkt.get("key") != "alternate_totals_corners":
                continue
            by_line: dict[float, dict[str, float]] = {}
            for o in mkt.get("outcomes", []):
                pt = o.get("point")
                if pt is None:
                    continue
                ln = round(float(pt), 1)
                pr = float(o.get("price", 0))
                if pr <= 1.0:
                    continue
                nm = o.get("name", "")
                by_line.setdefault(ln, {})
                if nm == "Over":
                    by_line[ln]["over"] = pr
                elif nm == "Under":
                    by_line[ln]["under"] = pr
            for ln, pp in by_line.items():
                if "over" not in pp or "under" not in pp:
                    continue
                oi = 1.0 / pp["over"]
                ui = 1.0 / pp["under"]
                tot = oi + ui
                if tot > 0:
                    line_raw.setdefault(ln, []).append((oi / tot, ui / tot))

    if not line_raw:
        return None

    best_line = max(line_raw, key=lambda l: len(line_raw[l]))
    entries = line_raw[best_line]
    if len(entries) < 2:
        return None

    return {
        "line":         best_line,
        "over_prob":    round(float(np.median([e[0] for e in entries])), 4),
        "under_prob":   round(float(np.median([e[1] for e in entries])), 4),
        "n_bookmakers": len(entries),
    }


def _best_ou_odds(event: dict, name: str, line: float) -> dict | None:
    """Mejor cuota disponible para 'Over' o 'Under' en una línea corners concreta."""
    best_price = 0.0
    best_bk = ""
    for bkm in event.get("bookmakers", []):
        for mkt in bkm.get("markets", []):
            if mkt.get("key") != "alternate_totals_corners":
                continue
            for o in mkt.get("outcomes", []):
                if o.get("name") != name:
                    continue
                if abs(float(o.get("point", -1)) - line) > 0.01:
                    continue
                p = float(o.get("price", 0))
                if p > best_price:
                    best_price = p
                    best_bk = bkm.get("key", "")
    return {"price": best_price, "bookmaker": best_bk} if best_price > 1.0 else None


def _poisson_ou_corners(lambda_total: float, line: float) -> tuple[float, float]:
    """
    P(corners_total > line) y P(corners_total <= line) via Poisson.
    line es X.5 → floor(line) como umbral entero.
    """
    line_int = int(line)
    prob_le = float(_poisson.cdf(line_int, max(0.1, lambda_total)))
    return round(1.0 - prob_le, 4), round(prob_le, 4)


async def _generate_theodds_corners_signals(
    home_team: str,
    away_team: str,
    league: str,
    match_date: date,
    home_stats: dict,
    away_stats: dict,
) -> list[dict]:
    """
    Señales de O/U corners via The Odds API alternate_totals_corners.
    Con stats FDCO (home_corners, away_corners): calcula edge vs línea del libro.
    Sin FDCO: line-shopping básico (fair price vig-removida vs mejor cuota).
    """
    try:
        from analyzers.value_bet_engine import _ODDS_SPORT_MAP
    except ImportError:
        return []

    sport_key = _ODDS_SPORT_MAP.get(league, "")
    if not sport_key.startswith("soccer_"):
        return []

    events = await _fetch_corners_theodds(sport_key)
    if not events:
        return []

    event = _find_event_theodds(events, home_team, away_team)
    if not event:
        logger.debug(
            "corners_bookings[theodds]: evento no encontrado %s vs %s", home_team, away_team
        )
        return []

    consensus = _extract_corners_ou_consensus(event)
    if not consensus or consensus["n_bookmakers"] < 2:
        return []

    line     = consensus["line"]
    over_mkt = consensus["over_prob"]
    under_mkt = consensus["under_prob"]
    n_bk     = consensus["n_bookmakers"]
    signals: list[dict] = []

    if home_stats and away_stats:
        lh = float(home_stats.get("home_corners", 5.0))
        la = float(away_stats.get("away_corners", 4.0))
        over_m, under_m = _poisson_ou_corners(lh + la, line)

        for sel, model_p, mkt_p, ou_name in (
            (f"Over {line}",  over_m,  over_mkt,  "Over"),
            (f"Under {line}", under_m, under_mkt, "Under"),
        ):
            edge = round(model_p - mkt_p, 4)
            if edge < SPORTS_MIN_EDGE:
                continue
            conf = round(
                max(0.0, min(1.0, 1.0 - abs(model_p - mkt_p) * 2, n_bk / 10)), 4
            )
            if conf < SPORTS_MIN_CONFIDENCE:
                continue
            best = _best_ou_odds(event, ou_name, line)
            if not best:
                continue
            signals.append({
                "market":        "corners_ou",
                "selection":     sel,
                "odds":          round(best["price"], 3),
                "bookmaker":     best["bookmaker"],
                "edge":          edge,
                "confidence":    conf,
                "poisson_prob":  model_p,
                "consensus_prob": mkt_p,
                "n_bookmakers":  n_bk,
                "match_date":    str(match_date),
                "home_team":     home_team,
                "away_team":     away_team,
                "source":        "corners_theodds_v1",
            })
            logger.info(
                "corners_bookings[theodds]: SEAL corners_ou %s edge=%.3f conf=%.3f",
                sel, edge, conf,
            )
    else:
        # Sin FDCO: line-shopping (fair price vig-removida vs mejor cuota disponible)
        for sel, ou_name, mkt_p in (
            (f"Over {line}",  "Over",  over_mkt),
            (f"Under {line}", "Under", under_mkt),
        ):
            best = _best_ou_odds(event, ou_name, line)
            if not best or best["price"] <= 1.05 or mkt_p <= 0:
                continue
            edge = round(1.0 / mkt_p - best["price"], 4)
            conf = round(min(1.0, n_bk / 10), 4)
            if edge >= SPORTS_MIN_EDGE and conf >= SPORTS_MIN_CONFIDENCE:
                signals.append({
                    "market":        "corners_ou",
                    "selection":     sel,
                    "odds":          round(best["price"], 3),
                    "bookmaker":     best["bookmaker"],
                    "edge":          edge,
                    "confidence":    conf,
                    "poisson_prob":  None,
                    "consensus_prob": mkt_p,
                    "n_bookmakers":  n_bk,
                    "match_date":    str(match_date),
                    "home_team":     home_team,
                    "away_team":     away_team,
                    "source":        "corners_theodds_v1",
                })

    return signals


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

    Fuente A: OddsPapi v4 (corners/bookings 1X2 + mercados binarios).
              Requiere ODDSPAPI_KEY con cuota mensual disponible.
    Fuente B: The Odds API (alternate_totals_corners O/U).
              Requiere ODDS_API_KEY. Actúa siempre, independientemente de A.
    Las stats FDCO de Firestore se cargan una vez y se comparten entre ambas fuentes.
    """
    if match_date is None:
        match_date = date.today()

    signals: list[dict] = []

    # Cargar stats FDCO una sola vez (compartidas entre fuente A y B)
    home_stats, away_stats = await _load_team_stats(league, home_team, away_team)
    has_fdco = bool(home_stats and away_stats)

    # ── Fuente A: OddsPapi (corners/bookings 1X2 + mercados binarios) ─────────
    if fixture_data is None:
        fixtures = await _fetch_fixtures_for_date(match_date)
        fixture_data = _find_fixture(fixtures, home_team, away_team)

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
                    confidence = round(max(0.0, 1.0 - diff_pc * 3), 4)
                    poisson_prob = poisson_p
                else:
                    edge = round((1.0 / consensus_p) - best_price, 4) if consensus_p > 0 else 0.0
                    confidence = round(min(1.0, consensus.get("n_bookmakers", 0) / 20), 4)
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
                confidence = round(min(1.0, n_bk / 15), 4)

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

    # ── Fuente B: The Odds API (alternate_totals_corners O/U) ─────────────────
    # Se ejecuta siempre: complementa OddsPapi (mercados distintos) o actúa
    # como alternativa cuando OddsPapi está agotado o el fixture no existe.
    theodds_signals = await _generate_theodds_corners_signals(
        home_team, away_team, league, match_date, home_stats, away_stats
    )
    if theodds_signals:
        signals.extend(theodds_signals)
        logger.info(
            "corners_bookings[theodds]: %d señales O/U para %s vs %s",
            len(theodds_signals), home_team, away_team,
        )

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
            "odds_source":     "theoddsapi" if sig.get("source", "").startswith("corners_theodds") else "oddspapi_v4",
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
