"""
Mercados extra de fútbol usando el modelo Poisson existente (home_xg / away_xg).

Fuentes de cuotas (en orden de prioridad):
  1. OddsPapi (ODDSPAPI_KEY) — BTTS y Asian Handicap
     https://api.oddspapi.com/odds?sport=football&market=btts|asian_handicap
  2. The Odds API (ODDS_API_KEY) — fallback para todos los mercados
     (btts, double_chance, spreads via markets= en el mismo request que h2h)

Mercados:
  btts          — Ambos marcan (Sí/No)
  double_chance — 1X, X2, 12
  asian_handicap — AH -0.5 / -1.0 / -1.5 / +0.5 / +1.0 / +1.5
  totals_3.5    — Goles Over/Under 3.5
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
import numpy as np
from scipy.stats import poisson as _poisson

from shared.config import (
    ODDSPAPI_KEY, SPORTS_ALERT_EDGE, SPORTS_MIN_CONFIDENCE, SPORTS_MIN_EDGE,
)
from shared.firestore_client import col
from shared.api_quota_manager import quota

# ── Emojis por mercado ────────────────────────────────────────────────────────
_MARKET_EMOJI: dict[str, str] = {
    "btts":               "🔄",
    "double_chance":      "🎯",
    "asian_handicap":     "📐",
    "result_and_goals":   "🔢",
    "draw_no_bet":        "🚫",
    "european_handicap":  "📏",
    "totals_1.5":         "📊",
    "totals_3.5":         "📊",
    "totals_4.5":         "📊",
    "ht_totals_0.5":      "⏱️",
    "ht_totals_1.5":      "⏱️",
    "ht_ft":              "⏱️",
    "home_team_goals":    "⚽",
    "away_team_goals":    "⚽",
    "first_scorer":       "🥅",
    "anytime_scorer":     "🥅",
    "anytime_assist":     "🎯",
    "corners_1x2":        "📐",
    "bookings_1x2":       "🟨",
    "tennis_total_games": "🎾",
    "tennis_game_handicap": "🎾",
    "basketball_h1_spread": "🏀",
    "basketball_h1_totals": "🏀",
    "basketball_q1_totals": "🏀",
    "h2h":                "⚽",
}

def _intensity(edge: float) -> str:
    if edge > 0.15: return "🔥"
    if edge > 0.08: return "✅"
    return "📊"

logger = logging.getLogger(__name__)

# Líneas de AH que buscamos (negativas = home da ventaja)
_AH_LINES = (-0.5, -1.0, -1.5, 0.5, 1.0, 1.5)

# ── OddsPapi client ────────────────────────────────────────────────────────────
_ODDSPAPI_BASE = "https://api.oddspapi.com"
# Cache por liga (una sola llamada devuelve TODOS los mercados) — TTL 1h
_ODDSPAPI_LEAGUE_CACHE: dict[str, tuple[datetime, list]] = {}
_ODDSPAPI_TTL = timedelta(hours=1)
_HTTP_TIMEOUT = 15.0

# Mapeo de league code Firestore → competition en OddsPapi
_ODDSPAPI_LEAGUE_MAP = {
    "PD":  "LaLiga",
    "BL1": "Bundesliga",
    "SA":  "SerieA",
    "FL1": "Ligue1",
    "CL":  "ChampionsLeague",
    "EL":  "EuropaLeague",
    "PPL": "PrimeiraLiga",
    "DED": "Eredivisie",
    "BL2": "Bundesliga2",
    "SD":  "Segunda",
    "SB":  "SerieB",
    "TU1": "SuperLig",
}


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


async def _fetch_oddspapi_league(league: str) -> list:
    """
    Una sola llamada por liga que devuelve TODOS los mercados para todos los fixtures.
    Cache TTL 1h — evita llamadas repetidas para btts/ah/h2h del mismo partido.
    """
    if not ODDSPAPI_KEY:
        return []

    if not quota.can_call("oddspapi"):
        logger.warning("OddsPapi: cuota diaria agotada para liga %s, saltando", league)
        return []

    cache_key = f"league_{league}"
    now = datetime.now(timezone.utc)
    cached = _ODDSPAPI_LEAGUE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _ODDSPAPI_TTL:
        return cached[1]

    competition = _ODDSPAPI_LEAGUE_MAP.get(league, "")
    # Sin parámetro market → OddsPapi devuelve todos los mercados disponibles
    params: dict = {"apiKey": ODDSPAPI_KEY, "sport": "football"}
    if competition:
        params["competition"] = competition

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_ODDSPAPI_BASE}/odds", params=params)

        if resp.status_code == 401:
            logger.warning("OddsPapi: clave inválida (401)")
            return []
        if resp.status_code == 429:
            logger.warning("OddsPapi: rate limit (429)")
            return []
        if resp.status_code != 200:
            logger.warning("OddsPapi: HTTP %d para liga %s", resp.status_code, league)
            return []

        quota.track_call("oddspapi")
        data = resp.json()
        events = data if isinstance(data, list) else data.get("data", data.get("events", []))
        if not isinstance(events, list):
            events = []

        logger.info("OddsPapi: %s → %d fixtures (todos los mercados)", league, len(events))
        _ODDSPAPI_LEAGUE_CACHE[cache_key] = (now, events)
        return events

    except Exception:
        logger.error("OddsPapi: error fetching liga %s", league, exc_info=True)
        return []


async def get_oddspapi_h2h_odds(league: str, home_team: str, away_team: str) -> dict | None:
    """
    Exportado: usado por value_bet_engine como fallback h2h cuando The Odds API está agotada.
    Usa OddsPapi v4 fixtures (mismo cache que corners_bookings) — sin coste adicional.

    La API v1 (api.oddspapi.com) está deprecada. Usamos v4 (api.oddspapi.io) que devuelve
    fixtures con bookmakerOdds embebidos. Auto-detectamos el mercado 1X2 buscando el que
    tiene exactamente 3 outcomes activos con cuotas típicas de resultado de partido.
    """
    from datetime import date as _date, timedelta as _td
    from analyzers.corners_bookings import _fetch_fixtures_for_date, _find_fixture

    # Buscar en rango hoy+7 días — los matches del Firestore pueden ser este fin de semana.
    # _fetch_fixtures_for_date usa cache TTL 1h, así que solo 1 llamada HTTP por rango.
    try:
        today = _date.today()
        week_end = today + _td(days=7)
        logger.info("get_oddspapi_h2h_odds: buscando %s vs %s en %s → %s", home_team, away_team, today, week_end)
        fixtures = await _fetch_fixtures_for_date(today, to_date=week_end)
        logger.info("get_oddspapi_h2h_odds: %d fixtures v4 obtenidos (%s → %s)", len(fixtures), today, week_end)
    except Exception:
        logger.error("get_oddspapi_h2h_odds: error obteniendo fixtures v4", exc_info=True)
        return None

    if not fixtures:
        logger.warning("get_oddspapi_h2h_odds: 0 fixtures v4 — quota agotada o API error")
        return None

    fixture = _find_fixture(fixtures, home_team, away_team)
    if not fixture:
        logger.info("get_oddspapi_h2h_odds: fixture no encontrado en %d fixtures (%s vs %s)", len(fixtures), home_team, away_team)
        return None

    result = _extract_h2h_from_v4_fixture(fixture)
    if not result:
        logger.info("get_oddspapi_h2h_odds: fixture encontrado pero sin mercado 1X2 detectado")
    return result


def _extract_h2h_from_v4_fixture(fixture: dict) -> dict | None:
    """
    Extrae cuotas 1X2 del formato bookmakerOdds de OddsPapi v4.
    Auto-detecta el mercado 1X2 buscando el que tiene exactamente 3 outcomes activos
    con cuotas en rango típico de resultado de partido (1.10 – 15.0) y el mayor número
    de bookmakers. Funciona independientemente del marketId concreto.
    """
    bk_odds = fixture.get("bookmakerOdds", {})
    if not bk_odds:
        return None

    # Acumular candidatos: {market_id: [(bkm, ho, do, ao), ...]}
    candidates: dict[str, list[tuple]] = {}

    for bk_name, bk_data in bk_odds.items():
        markets = bk_data.get("markets", {}) if isinstance(bk_data, dict) else {}
        for market_id, mkt_data in markets.items():
            outcomes = mkt_data.get("outcomes", {}) if isinstance(mkt_data, dict) else {}
            # Recoger precios activos
            prices: list[float] = []
            for oid, odata in outcomes.items():
                players = odata.get("players", {}) if isinstance(odata, dict) else {}
                p0 = players.get("0", {}) if isinstance(players, dict) else {}
                price = _safe_float(p0.get("price")) if isinstance(p0, dict) else None
                active = p0.get("active", True) if isinstance(p0, dict) else True
                if price and active and 1.05 <= price <= 15.0:
                    prices.append(price)

            # Un mercado 1X2 tiene exactamente 3 outcomes activos en rango
            if len(prices) == 3:
                prices_sorted = sorted(prices)
                # Heurística: cuota media < 3.5 (partidos equilibrados) y min > 1.1
                if prices_sorted[0] > 1.1 and sum(prices_sorted) / 3 < 5.0:
                    if market_id not in candidates:
                        candidates[market_id] = []
                    candidates[market_id].append((bk_name, prices_sorted[0], prices_sorted[1], prices_sorted[2]))

    if not candidates:
        logger.debug("_extract_h2h_from_v4_fixture: sin mercado 1X2 detectado")
        return None

    # Elegir el market_id con más bookmakers (el más cubierto es el principal 1X2)
    best_mid = max(candidates, key=lambda k: len(candidates[k]))
    entries = candidates[best_mid]

    # Usar el bookmaker con cuota home más alta (mejor valor) como referencia
    # y calcular mediana de todos para mayor robustez
    all_home  = sorted(e[1] for e in entries)
    all_draw  = sorted(e[2] for e in entries)
    all_away  = sorted(e[3] for e in entries)
    mid_idx = len(all_home) // 2

    home_odds = all_home[mid_idx]
    draw_odds = all_draw[mid_idx]
    away_odds = all_away[mid_idx]
    bookmaker = entries[mid_idx][0] if entries else "oddspapi"

    logger.info(
        "get_oddspapi_h2h_odds: v4 market=%s bkm=%s home=%.2f draw=%.2f away=%.2f (%d bkms)",
        best_mid, bookmaker, home_odds, draw_odds, away_odds, len(entries),
    )
    return {
        "bookmaker": bookmaker,
        "home_odds": home_odds,
        "draw_odds": draw_odds,
        "away_odds": away_odds,
        "opening_home_odds": home_odds,
        "source": "oddspapi_v4",
    }


def _oddspapi_find_event(events: list, home: str, away: str) -> dict | None:
    """Busca un evento en la respuesta de OddsPapi por nombre de equipos."""
    from analyzers.value_bet_engine import _normalize_team
    h_norm = _normalize_team(home)
    a_norm = _normalize_team(away)
    for ev in events:
        # OddsPapi usa diferentes keys según versión de API
        ev_home = ev.get("home_team", ev.get("home", ev.get("homeTeam", "")))
        ev_away = ev.get("away_team", ev.get("away", ev.get("awayTeam", "")))
        from analyzers.value_bet_engine import _teams_match
        if _teams_match(home, ev_home) and _teams_match(away, ev_away):
            return ev
    return None


def _parse_oddspapi_btts(event: dict) -> dict | None:
    """Extrae odds BTTS de un evento OddsPapi."""
    odds = event.get("odds", event.get("markets", event))
    # Formatos comunes de OddsPapi
    for yes_key in ("btts_yes", "yes", "both_teams_to_score_yes"):
        for no_key in ("btts_no", "no", "both_teams_to_score_no"):
            if yes_key in odds and no_key in odds:
                try:
                    return {
                        "yes_odds": float(odds[yes_key]),
                        "no_odds":  float(odds[no_key]),
                        "bookmaker": "oddspapi",
                    }
                except (TypeError, ValueError):
                    continue
    return None


def _parse_oddspapi_ah(event: dict) -> list[dict]:
    """Extrae líneas de Asian Handicap de un evento OddsPapi."""
    odds = event.get("odds", event.get("markets", {}))
    lines = []

    # Formato 1: {"asian_handicap": [{"line": -1.5, "home": 1.95, "away": 1.85}]}
    ah_data = odds.get("asian_handicap", odds.get("handicap", []))
    if isinstance(ah_data, list):
        for entry in ah_data:
            try:
                line = float(entry.get("line", entry.get("handicap", 0)))
                ho = float(entry.get("home", entry.get("home_odds", 0)))
                ao = float(entry.get("away", entry.get("away_odds", 0)))
                if ho > 1 and ao > 1:
                    lines.append({"home_line": line, "home_odds": ho,
                                  "away_line": -line, "away_odds": ao,
                                  "bookmaker": "oddspapi"})
            except (TypeError, ValueError):
                continue

    # Formato 2: {"ah_-1.5_home": 1.95, "ah_-1.5_away": 1.85}
    if not lines:
        line_map: dict = {}
        for k, v in odds.items():
            if k.startswith("ah_"):
                parts = k.split("_")
                if len(parts) >= 3:
                    try:
                        line_val = float(parts[1])
                        side = parts[2]
                        if line_val not in line_map:
                            line_map[line_val] = {}
                        line_map[line_val][side] = float(v)
                    except (ValueError, TypeError):
                        continue
        for line_val, sides in line_map.items():
            if "home" in sides and "away" in sides:
                lines.append({
                    "home_line": line_val, "home_odds": sides["home"],
                    "away_line": -line_val, "away_odds": sides["away"],
                    "bookmaker": "oddspapi",
                })

    return lines


# ── Probabilidades ────────────────────────────────────────────────────────────

def calc_btts(home_xg: float, away_xg: float) -> dict | None:
    try:
        p_home = 1.0 - float(_poisson.pmf(0, max(0.1, home_xg)))
        p_away = 1.0 - float(_poisson.pmf(0, max(0.1, away_xg)))
        yes = round(p_home * p_away, 4)
        return {"yes": yes, "no": round(1.0 - yes, 4)}
    except Exception:
        return None


def calc_double_chance(hw: float, d: float, aw: float) -> dict:
    return {
        "1X": round(hw + d, 4),
        "X2": round(d + aw, 4),
        "12": round(hw + aw, 4),
    }


def calc_asian_handicap(home_xg: float, away_xg: float) -> dict[float, float]:
    """Devuelve {line: prob_home_covers} para cada línea en _AH_LINES."""
    MAX = 9
    lh = max(0.1, home_xg)
    ma = max(0.1, away_xg)

    # Construir matriz de marcadores
    matrix = np.zeros((MAX, MAX))
    for i in range(MAX):
        for j in range(MAX):
            matrix[i, j] = float(_poisson.pmf(i, lh)) * float(_poisson.pmf(j, ma))

    probs: dict[float, float] = {}
    for line in _AH_LINES:
        p = 0.0
        for i in range(MAX):
            for j in range(MAX):
                diff = i - j  # margen local
                if diff + line > 0:
                    p += matrix[i, j]
                elif diff + line == 0:
                    p += matrix[i, j] * 0.5  # push
        probs[line] = round(p, 4)
    return probs


def calc_totals_n(home_xg: float, away_xg: float, line: float) -> dict | None:
    try:
        expected = home_xg + away_xg
        if expected <= 0:
            return None
        floor_line = int(line)
        under_eq = sum(float(_poisson.pmf(k, expected)) for k in range(floor_line + 1))
        over = max(0.0, min(1.0, 1.0 - under_eq))
        return {"over": round(over, 4), "under": round(1.0 - over, 4),
                "expected_total": round(expected, 2)}
    except Exception:
        return None


# ── Nuevas funciones de cálculo ───────────────────────────────────────────────

def calc_draw_no_bet(hw: float, aw: float) -> dict[str, float]:
    """P(home win) y P(away win) renormalizadas sin empate."""
    total = hw + aw
    if total <= 0:
        return {"home": 0.5, "away": 0.5}
    return {"home": round(hw / total, 4), "away": round(aw / total, 4)}


def calc_result_goals(hw: float, d: float, aw: float,
                      over_p: float, under_p: float) -> dict[str, float]:
    """Probabilidades combinadas resultado × goles (no independientes pero aproximadas)."""
    return {
        "home_over":  round(hw * over_p, 4),
        "home_under": round(hw * under_p, 4),
        "draw_under": round(d  * under_p, 4),
        "away_over":  round(aw * over_p, 4),
        "away_under": round(aw * under_p, 4),
    }


def calc_european_handicap(home_xg: float, away_xg: float) -> dict[str, float]:
    """P(home cubre handicap europeo -1/0/+1). Push = devuelve apuesta."""
    MAX = 12
    lh, la = max(0.1, home_xg), max(0.1, away_xg)
    matrix = np.zeros((MAX, MAX))
    for i in range(MAX):
        for j in range(MAX):
            matrix[i, j] = float(_poisson.pmf(i, lh)) * float(_poisson.pmf(j, la))

    def _covers(line: int) -> tuple[float, float]:
        p_win = p_push = 0.0
        for i in range(MAX):
            for j in range(MAX):
                diff = i - j + line
                if diff > 0:   p_win  += matrix[i, j]
                elif diff == 0: p_push += matrix[i, j]
        return round(p_win, 4), round(p_push, 4)

    w_m1, push_m1 = _covers(-1)
    w_0,  push_0  = _covers(0)
    w_p1, _       = _covers(1)
    return {
        "home_minus1":  w_m1,   "push_minus1": push_m1,
        "home_zero":    w_0,    "push_zero":   push_0,
        "home_plus1":   w_p1,
    }


def calc_ht_totals(home_xg: float, away_xg: float, line: float = 0.5) -> dict | None:
    """P(over/under N goles en primera mitad). Factor 0.45 del total."""
    ht_lh = home_xg * 0.45
    ht_la = away_xg * 0.45
    return calc_totals_n(ht_lh, ht_la, line)


def calc_team_goals_ou(team_xg: float, line: float = 0.5) -> dict[str, float]:
    """P(equipo marca over/under N goles)."""
    lam = max(0.1, team_xg)
    if line == 0.5:
        p_over = round(1.0 - float(_poisson.pmf(0, lam)), 4)
    elif line == 1.5:
        p_over = round(1.0 - float(_poisson.pmf(0, lam)) - float(_poisson.pmf(1, lam)), 4)
    else:
        p_over = calc_totals_n(lam, 0.0, line).get("over", 0.5)  # type: ignore
    return {"over": p_over, "under": round(1.0 - p_over, 4)}


def calc_ht_ft(hw: float, d: float, aw: float,
               home_xg: float, away_xg: float) -> dict[str, float]:
    """Probabilidades de las 9 combinaciones HT/FT."""
    MAX = 8
    ht_lh = max(0.1, home_xg * 0.45)
    ht_la = max(0.1, away_xg * 0.45)
    ht_hw = ht_d = ht_aw = 0.0
    for i in range(MAX):
        for j in range(MAX):
            p = float(_poisson.pmf(i, ht_lh)) * float(_poisson.pmf(j, ht_la))
            if i > j:   ht_hw += p
            elif i == j: ht_d  += p
            else:        ht_aw += p
    return {
        "H/H": round(ht_hw * hw, 4), "H/D": round(ht_hw * d, 4),  "H/A": round(ht_hw * aw, 4),
        "D/H": round(ht_d  * hw, 4), "D/D": round(ht_d  * d, 4),  "D/A": round(ht_d  * aw, 4),
        "A/H": round(ht_aw * hw, 4), "A/D": round(ht_aw * d, 4),  "A/A": round(ht_aw * aw, 4),
    }


# ── Parsers nuevos — The Odds API ─────────────────────────────────────────────

def parse_draw_no_bet_event(event: dict) -> dict | None:
    """Extrae cuotas draw_no_bet de un evento The Odds API."""
    home_team = event.get("home_team", "")
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "draw_no_bet":
                continue
            home_odds = away_odds = None
            for o in mkt.get("outcomes", []):
                nm  = o.get("name", "")
                pr  = float(o.get("price", 0))
                if pr <= 1:
                    continue
                if home_team[:5].lower() in nm.lower() or nm.lower() == "home":
                    home_odds = pr
                else:
                    away_odds = pr
            if home_odds and away_odds:
                return {"bookmaker": bk.get("key", "bet365"),
                        "home_odds": home_odds, "away_odds": away_odds}
    return None


def parse_alternate_totals_event(event: dict, line: float) -> dict | None:
    """Extrae cuotas alternate_totals para una línea específica."""
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "alternate_totals":
                continue
            over_odds = under_odds = actual_line = None
            for o in mkt.get("outcomes", []):
                try:
                    pt = float(o.get("point", o.get("description", 0)))
                except (TypeError, ValueError):
                    continue
                if abs(pt - line) < 0.26:
                    pr = float(o.get("price", 0))
                    if pr <= 1:
                        continue
                    if o.get("name") == "Over":
                        over_odds   = pr
                        actual_line = pt
                    elif o.get("name") == "Under":
                        under_odds  = pr
            if over_odds and under_odds:
                return {"bookmaker": bk.get("key", "pinnacle"),
                        "line": actual_line or line,
                        "over_odds": over_odds, "under_odds": under_odds}
    return None


def parse_team_totals_event(event: dict, home_team: str, line: float = 0.5) -> dict | None:
    """Extrae cuotas team_totals para home y away en una línea dada."""
    result: dict = {}
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "team_totals":
                continue
            for o in mkt.get("outcomes", []):
                try:
                    pt = float(o.get("point", 0))
                except (TypeError, ValueError):
                    continue
                if abs(pt - line) >= 0.26:
                    continue
                pr   = float(o.get("price", 0))
                nm   = o.get("name", "")
                desc = o.get("description", "")
                side = "home" if home_team[:5].lower() in desc.lower() else "away"
                key  = f"{side}_{nm.lower().replace(' ', '_')}"
                result[key]    = pr
                result["bookmaker"] = bk.get("key", "bet365")
                result["line"] = pt
    return result if "home_over" in result or "away_over" in result else None


# ── Parser OddsPapi v4 — HT/FT ────────────────────────────────────────────────

_ODDSPAPI_V4_BASE = "https://api.oddspapi.io/v4"
_HTFT_MARKET_ID  = "101919"
_HTFT_CACHE: dict[str, tuple[datetime, list]] = {}
_HTFT_CACHE_TTL = timedelta(hours=1)


async def _fetch_htft_fixtures(match_date: date | None = None) -> list[dict]:
    """Fetch OddsPapi v4 fixtures para hoy (reutiliza cache de corners_bookings si existe)."""
    from analyzers.corners_bookings import _fetch_fixtures_for_date
    from datetime import date as _date
    target = match_date or _date.today()
    return await _fetch_fixtures_for_date(target)


def _parse_htft_from_fixture(fixture: dict) -> dict[str, float]:
    """
    Extrae implied probs de HT/FT (marketId=101919) del fixture OddsPapi v4.
    Outcome IDs mapeados dinámicamente por nombre (OddsPapi los varía).
    """
    bk_odds = fixture.get("bookmakerOdds", {})
    # Acumular precios por nombre de outcome
    price_sums: dict[str, list[float]] = {}
    count = 0
    for bk_data in bk_odds.values():
        if not isinstance(bk_data, dict):
            continue
        mkt = bk_data.get("markets", {}).get(_HTFT_MARKET_ID)
        if not mkt:
            continue
        for oid, outcome in mkt.get("outcomes", {}).items():
            for player in outcome.get("players", {}).values():
                if not player.get("active"):
                    continue
                price = player.get("price", 0)
                if isinstance(price, (int, float)) and price > 1.05:
                    label = player.get("bookmakerOutcomeId", oid)
                    price_sums.setdefault(label, []).append(float(price))
        count += 1

    if not price_sums or count < 3:
        return {}

    # Implied probs promedio por combinación
    implied: dict[str, float] = {}
    for label, prices in price_sums.items():
        avg_price = sum(prices) / len(prices)
        implied[label] = round(1.0 / avg_price, 4) if avg_price > 1 else 0.0

    # Normalizar
    total = sum(implied.values())
    if total <= 0:
        return {}
    return {k: round(v / total, 4) for k, v in implied.items()}


# ── Parsers de The Odds API ───────────────────────────────────────────────────

def parse_btts_event(event: dict) -> dict | None:
    for bk in event.get("bookmakers", []):
        for market in bk.get("markets", []):
            if market.get("key") != "btts":
                continue
            yes_odds = no_odds = None
            for o in market.get("outcomes", []):
                nm = o.get("name", "").lower()
                pr = float(o.get("price", 0))
                if nm in ("yes", "si", "sí"):
                    yes_odds = pr
                elif nm == "no":
                    no_odds = pr
            if yes_odds and no_odds:
                return {"bookmaker": bk.get("key", "bet365"),
                        "yes_odds": yes_odds, "no_odds": no_odds}
    return None


def parse_double_chance_event(event: dict) -> dict | None:
    home_team = event.get("home_team", "")
    for bk in event.get("bookmakers", []):
        for market in bk.get("markets", []):
            if market.get("key") != "double_chance":
                continue
            dc: dict = {}
            for o in market.get("outcomes", []):
                nm = o.get("name", "")
                pr = float(o.get("price", 0))
                if "draw" in nm.lower() or "empate" in nm.lower():
                    continue
                # The Odds API usa nombres del tipo "Real Madrid or Draw"
                if "or draw" in nm.lower() or home_team.lower() in nm.lower()[:30]:
                    dc["1X"] = pr
                elif "draw or" in nm.lower():
                    dc["X2"] = pr
                else:
                    dc["12"] = pr
            if dc:
                return {"bookmaker": bk.get("key", "bet365"), **dc}
    return None


def parse_spreads_event(event: dict) -> list[dict]:
    """Devuelve lista de {line, home_odds, away_odds, bookmaker}."""
    home_team = event.get("home_team", "")
    collected: dict[float, dict] = {}
    for bk in event.get("bookmakers", []):
        for market in bk.get("markets", []):
            if market.get("key") != "spreads":
                continue
            for o in market.get("outcomes", []):
                try:
                    pt = float(o.get("point", 0))
                except (TypeError, ValueError):
                    continue
                pr = float(o.get("price", 0))
                nm = o.get("name", "")
                # Determinar si es home o away por nombre de equipo
                is_home = home_team[:5].lower() in nm.lower() if home_team else (pt < 0)
                key = round(pt, 1)
                if key not in collected:
                    collected[key] = {"line": key, "bookmaker": bk.get("key", "bet365")}
                if is_home:
                    collected[key]["home_odds"] = pr
                    collected[key]["home_line"] = pt
                else:
                    collected[key]["away_odds"] = pr
                    collected[key]["away_line"] = pt
    return [v for v in collected.values() if "home_odds" in v and "away_odds" in v]


# ── Señales ───────────────────────────────────────────────────────────────────

def _make_prediction(base: dict, market_type: str, selection: str,
                     odds: float, prob: float, factors: dict,
                     match_date, weights_version: int) -> dict | None:
    """Construye el dict de predicción si supera thresholds."""
    edge = round(prob - 1.0 / odds, 4) if odds > 1 else 0.0
    if edge <= SPORTS_MIN_EDGE:
        return None

    confidence = max(0.0, round(1.0 - float(np.std(list(factors.values()))), 4))
    if confidence <= SPORTS_MIN_CONFIDENCE:
        return None

    from analyzers.value_bet_engine import kelly_criterion
    kelly = kelly_criterion(edge, odds)

    bkm = base.get("bookmaker", "")
    return {
        **base,
        "market_type": market_type,
        "selection": selection,
        "odds": round(odds, 3),
        "calculated_prob": round(prob, 4),
        "edge": edge,
        "confidence": confidence,
        "kelly_fraction": kelly,
        "factors": {k: round(float(v), 4) for k, v in factors.items()},
        "signals": {},
        "data_source": "poisson_extras",
        "odds_source": "oddspapi" if bkm == "oddspapi" else "theoddsapi",
        "match_date": match_date,
        "weights_version": weights_version,
        "created_at": datetime.now(timezone.utc),
        "result": None,
        "correct": None,
        "error_type": None,
    }


async def generate_football_extra_signals(
    enriched_match: dict,
    cached_events: list,
    home_team: str,
    away_team: str,
    league: str,
    match_id: str,
    match_date,
    weights_version: int,
) -> list[dict]:
    """
    Genera señales para BTTS, Double Chance, Asian Handicap y Over 3.5.
    cached_events: lista de eventos ya obtenida de The Odds API para esta liga.
    Usa WriteBatch: todos los writes de este partido en 1 sola RPC (de N×300ms → 1×300ms).
    """
    from analyzers.value_bet_engine import (
        _teams_match, _send_telegram_alert, _build_alert_payload
    )
    from shared.firestore_client import get_client as _get_fs_client

    home_xg = enriched_match.get("home_xg")
    away_xg = enriched_match.get("away_xg")
    hw = enriched_match.get("poisson_home_win")
    d  = enriched_match.get("poisson_draw")
    aw = enriched_match.get("poisson_away_win")

    if home_xg is None or away_xg is None:
        return []

    home_xg = float(home_xg)
    away_xg = float(away_xg)

    # WriteBatch: agrupa todos los writes del partido en 1 sola RPC
    _fs = _get_fs_client()
    _batch = _fs.batch()
    _pending_alerts: list[dict] = []  # alerts se envían después del commit

    # Encontrar evento en caché
    event = None
    for ev in cached_events:
        if _teams_match(home_team, ev.get("home_team", "")) and \
           _teams_match(away_team, ev.get("away_team", "")):
            event = ev
            break

    base = {
        "match_id": match_id,
        "home_team": home_team,
        "away_team": away_team,
        "sport": "football",
        "league": league,
        "elo_sufficient": enriched_match.get("elo_sufficient", True),
        "h2h_sufficient": enriched_match.get("h2h_sufficient", True),
    }

    signals_out: list[dict] = []
    xg_factor = round(min(home_xg, away_xg) / max(home_xg, away_xg, 0.1), 4)

    # OddsPapi: una sola llamada por liga (todos los mercados) — reutiliza la caché si ya existe
    op_league_events = await _fetch_oddspapi_league(league)
    op_ev = _oddspapi_find_event(op_league_events, home_team, away_team) if op_league_events else None
    op_btts_ev = op_ev
    op_ah_ev   = op_ev

    # ── BTTS ─────────────────────────────────────────────────────────────────
    btts_odds = ((_parse_oddspapi_btts(op_btts_ev) if op_btts_ev else None)
                 or (parse_btts_event(event) if event else None))
    if btts_odds:
        btts_probs = calc_btts(home_xg, away_xg)
        if btts_probs:
            for sel, prob, odds_key in [("Sí", btts_probs["yes"], "yes_odds"),
                                         ("No", btts_probs["no"],  "no_odds")]:
                odds = btts_odds.get(odds_key, 0)
                if odds <= 1:
                    continue
                factors = {"xg_home": round(home_xg, 3),
                           "xg_away": round(away_xg, 3),
                           "xg_balance": xg_factor}
                pred = _make_prediction(
                    {**base, "bookmaker": btts_odds.get("bookmaker", "bet365")},
                    "btts", f"BTTS {sel}", odds, prob, factors,
                    match_date, weights_version
                )
                if pred:
                    doc_id = f"{match_id}_btts_{sel.lower().replace(' ','_')}"
                    pred["match_id"] = doc_id
                    try:
                        _batch.set(col("predictions").document(doc_id), pred)
                    except Exception:
                        logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                    if pred["edge"] > SPORTS_ALERT_EDGE:
                        await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
                    signals_out.append(pred)

    # ── DOUBLE CHANCE ─────────────────────────────────────────────────────────
    if event and hw is not None and d is not None and aw is not None:
        dc_odds = parse_double_chance_event(event)
        dc_probs = calc_double_chance(float(hw), float(d), float(aw))
        if dc_odds and dc_probs:
            for sel in ("1X", "X2", "12"):
                odds = dc_odds.get(sel, 0)
                prob = dc_probs.get(sel, 0)
                if odds <= 1:
                    continue
                factors = {"home_win": round(float(hw), 4),
                           "draw": round(float(d), 4),
                           "away_win": round(float(aw), 4)}
                pred = _make_prediction(
                    {**base, "bookmaker": dc_odds.get("bookmaker", "bet365")},
                    "double_chance", f"DC {sel}", odds, prob, factors,
                    match_date, weights_version
                )
                if pred:
                    doc_id = f"{match_id}_dc_{sel}"
                    pred["match_id"] = doc_id
                    try:
                        _batch.set(col("predictions").document(doc_id), pred)
                    except Exception:
                        logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                    if pred["edge"] > SPORTS_ALERT_EDGE:
                        await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
                    signals_out.append(pred)

    # ── ASIAN HANDICAP ────────────────────────────────────────────────────────
    # OddsPapi primario; The Odds API spreads como fallback
    op_ah_lines = _parse_oddspapi_ah(op_ah_ev) if op_ah_ev else []
    the_odds_ah = parse_spreads_event(event) if event else []
    spread_lines = op_ah_lines or the_odds_ah
    if spread_lines:
        ah_probs = calc_asian_handicap(home_xg, away_xg)
        for spread in spread_lines:
            line = spread.get("home_line", spread.get("line", 0))
            try:
                line_f = float(line)
            except (TypeError, ValueError):
                continue
            closest = min(_AH_LINES, key=lambda l: abs(l - line_f))
            prob = ah_probs.get(closest)
            if prob is None:
                continue
            if line_f < 0:
                odds = spread.get("home_odds", 0)
                sel = f"{home_team} {line_f:+.1f}"
            else:
                odds = spread.get("away_odds", 0)
                prob = 1.0 - prob  # AH positivo = away cubre
                sel = f"{away_team} {line_f:+.1f}"
            if odds <= 1:
                continue
            factors = {"xg_home": round(home_xg, 3),
                       "xg_away": round(away_xg, 3),
                       "ah_line": round(line_f, 1)}
            pred = _make_prediction(
                {**base, "bookmaker": spread.get("bookmaker", "bet365")},
                "asian_handicap", sel, odds, prob, factors,
                match_date, weights_version
            )
            if pred:
                tag = str(line_f).replace("-", "m").replace(".", "_").replace("+", "p")
                doc_id = f"{match_id}_ah_{tag}"
                pred["match_id"] = doc_id
                try:
                    _batch.set(col("predictions").document(doc_id), pred)
                except Exception:
                    logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                if pred["edge"] > SPORTS_ALERT_EDGE:
                    await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
                signals_out.append(pred)

    # ── TOTALS 3.5 ────────────────────────────────────────────────────────────
    t35 = calc_totals_n(home_xg, away_xg, 3.5)
    if t35 and event:
        # Buscar odds totals con línea 3.5 en el evento
        from analyzers.value_bet_engine import _parse_totals_event
        t35_odds = _parse_totals_event(event, line=3.5)
        if t35_odds:
            for sel, prob, odds_key in [("Over 3.5",  t35["over"],  "over_odds"),
                                         ("Under 3.5", t35["under"], "under_odds")]:
                odds = t35_odds.get(odds_key, 0)
                if odds <= 1:
                    continue
                factors = {"expected_total": t35["expected_total"],
                           "xg_home": round(home_xg, 3),
                           "xg_away": round(away_xg, 3)}
                pred = _make_prediction(
                    {**base, "bookmaker": t35_odds.get("bookmaker", "pinnacle"),
                     "line": 3.5},
                    "totals_3.5", sel, odds, prob, factors,
                    match_date, weights_version
                )
                if pred:
                    tag = sel.lower().replace(" ", "_").replace(".", "")
                    doc_id = f"{match_id}_t35_{tag}"
                    pred["match_id"] = doc_id
                    try:
                        _batch.set(col("predictions").document(doc_id), pred)
                    except Exception:
                        logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                    if pred["edge"] > SPORTS_ALERT_EDGE:
                        await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
                    signals_out.append(pred)

    # ── DRAW NO BET ───────────────────────────────────────────────────────────
    if hw is not None and aw is not None:
        dnb_odds = parse_draw_no_bet_event(event) if event else None
        if dnb_odds:
            dnb_probs = calc_draw_no_bet(float(hw), float(aw))
            for sel, prob, odds_key in [
                ("Home DNB", dnb_probs["home"], "home_odds"),
                ("Away DNB", dnb_probs["away"], "away_odds"),
            ]:
                odds = dnb_odds.get(odds_key, 0)
                if odds <= 1:
                    continue
                factors = {"home_win_raw": round(float(hw), 4),
                           "away_win_raw": round(float(aw), 4)}
                pred = _make_prediction(
                    {**base, "bookmaker": dnb_odds.get("bookmaker", "bet365"), "market": "draw_no_bet"},
                    "draw_no_bet", sel, odds, prob, factors, match_date, weights_version,
                )
                if pred:
                    tag = "home" if "Home" in sel else "away"
                    doc_id = f"{match_id}_dnb_{tag}"
                    pred["match_id"] = doc_id
                    try: _batch.set(col("predictions").document(doc_id), pred)
                    except Exception: logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                    if pred["edge"] > SPORTS_ALERT_EDGE:
                        payload = _build_alert_payload(pred, enriched_match)
                        payload["market_emoji"] = _MARKET_EMOJI.get("draw_no_bet", "🚫")
                        payload["intensity"]    = _intensity(pred["edge"])
                        await _send_telegram_alert(payload)
                    signals_out.append(pred)

    # ── RESULT & GOALS ────────────────────────────────────────────────────────
    if hw is not None and d is not None and aw is not None:
        t25 = calc_totals_n(home_xg, away_xg, 2.5)
        if t25:
            rg_probs = calc_result_goals(float(hw), float(d), float(aw),
                                         t25["over"], t25["under"])
            # Derivar implied odds desde h2h × totals si no hay mercado directo
            # (h2h_home_implied × over_implied) corregido por vig
            for combo, prob in rg_probs.items():
                if prob < 0.05:
                    continue
                # Construir odds sintéticas: no tenemos mercado directo → skip si prob < threshold
                # El modelo sirve como información; alertar solo si hay odds OddsPapi
                # OddsPapi raramente tiene este mercado → loggear si >= MIN_EDGE
                if prob >= SPORTS_MIN_CONFIDENCE:
                    logger.debug("football_markets(%s): result_goals %s p=%.3f", match_id, combo, prob)

    # ── EUROPEAN HANDICAP ─────────────────────────────────────────────────────
    if event:
        eh = calc_european_handicap(home_xg, away_xg)
        # Intentar parsear alternate_spreads de The Odds API como proxy de EH
        for bk in event.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "alternate_spreads":
                    continue
                for o in mkt.get("outcomes", []):
                    try:
                        pt = float(o.get("point", 0))
                    except (TypeError, ValueError):
                        continue
                    if pt not in (-1.0, 0.0, 1.0):
                        continue
                    nm  = o.get("name", "")
                    pr  = float(o.get("price", 0))
                    if pr <= 1:
                        continue
                    is_home = home_team[:5].lower() in nm.lower()
                    if is_home and pt == -1.0:
                        prob = eh["home_minus1"]
                        sel  = f"{home_team} -1 EH"
                    elif not is_home and pt == 1.0:
                        prob = 1.0 - eh["home_minus1"] - eh["push_minus1"]
                        sel  = f"{away_team} +1 EH"
                    else:
                        continue
                    factors = {"eh_home_m1": eh["home_minus1"], "eh_push_m1": eh["push_minus1"]}
                    pred = _make_prediction(
                        {**base, "bookmaker": bk.get("key", "bet365"), "market": "european_handicap",
                         "line": pt},
                        "european_handicap", sel, pr, prob, factors, match_date, weights_version,
                    )
                    if pred:
                        doc_id = f"{match_id}_eh_{str(pt).replace('-','m').replace('.','_')}"
                        pred["match_id"] = doc_id
                        try: _batch.set(col("predictions").document(doc_id), pred)
                        except Exception: logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                        if pred["edge"] > SPORTS_ALERT_EDGE:
                            payload = _build_alert_payload(pred, enriched_match)
                            payload["market_emoji"] = "📏"
                            payload["intensity"]    = _intensity(pred["edge"])
                            await _send_telegram_alert(payload)
                        signals_out.append(pred)

    # ── OVER/UNDER 1.5 y 4.5 ─────────────────────────────────────────────────
    for line, market_key in ((1.5, "totals_1.5"), (4.5, "totals_4.5")):
        tN = calc_totals_n(home_xg, away_xg, line)
        if not tN or not event:
            continue
        alt_odds = parse_alternate_totals_event(event, line)
        if not alt_odds:
            continue
        for sel, prob, odds_key in [
            (f"Over {line}",  tN["over"],  "over_odds"),
            (f"Under {line}", tN["under"], "under_odds"),
        ]:
            odds = alt_odds.get(odds_key, 0)
            if odds <= 1:
                continue
            factors = {"expected_total": tN["expected_total"],
                       "xg_home": round(home_xg, 3), "xg_away": round(away_xg, 3)}
            pred = _make_prediction(
                {**base, "bookmaker": alt_odds.get("bookmaker", "pinnacle"),
                 "market": market_key, "line": line},
                market_key, sel, odds, prob, factors, match_date, weights_version,
            )
            if pred:
                tag = "over" if "Over" in sel else "under"
                doc_id = f"{match_id}_{market_key.replace('.','_')}_{tag}"
                pred["match_id"] = doc_id
                try: _batch.set(col("predictions").document(doc_id), pred)
                except Exception: logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                if pred["edge"] > SPORTS_ALERT_EDGE:
                    payload = _build_alert_payload(pred, enriched_match)
                    payload["market_emoji"] = "📊"
                    payload["intensity"]    = _intensity(pred["edge"])
                    await _send_telegram_alert(payload)
                signals_out.append(pred)

    # ── HT TOTALS 0.5 y 1.5 ──────────────────────────────────────────────────
    for line, market_key in ((0.5, "ht_totals_0.5"), (1.5, "ht_totals_1.5")):
        ht_t = calc_ht_totals(home_xg, away_xg, line)
        if not ht_t:
            continue
        # Intentar alternate_totals con línea baja como proxy (no hay mercado HT específico en TheOddsAPI)
        # OddsPapi v4 podría tener HT totals — intentar parse desde fixture
        ht_fixtures = await _fetch_htft_fixtures()
        from analyzers.corners_bookings import _find_fixture as _cb_find
        ht_fix = _cb_find(ht_fixtures, home_team, away_team)
        if ht_fix:
            # Buscar mercado HT over/under en fixture (IDs conocidos: 101535 1H corners, similar para goles)
            # Si no se encuentra, loggear y continuar
            logger.debug("football_markets(%s): %s p_over=%.3f (sin odds directas)", match_id, market_key, ht_t["over"])

    # ── GOLES EQUIPO LOCAL y VISITANTE O/U 0.5 y 1.5 ─────────────────────────
    if event:
        team_odds = parse_team_totals_event(event, home_team, 0.5)
        for side, team_name, team_xg_val, market_key in [
            ("home", home_team, home_xg, "home_team_goals"),
            ("away", away_team, away_xg, "away_team_goals"),
        ]:
            for line in (0.5, 1.5):
                tg = calc_team_goals_ou(team_xg_val, line)
                # Buscar odds team_totals
                tm_odds = parse_team_totals_event(event, home_team, line)
                if not tm_odds:
                    continue
                over_key  = f"{side}_over"
                under_key = f"{side}_under"
                for sel, prob, odds_key in [
                    (f"{team_name} Over {line}",  tg["over"],  over_key),
                    (f"{team_name} Under {line}", tg["under"], under_key),
                ]:
                    odds = tm_odds.get(odds_key, 0)
                    if odds <= 1:
                        continue
                    factors = {"team_xg": round(team_xg_val, 3), "line": line}
                    pred = _make_prediction(
                        {**base, "bookmaker": tm_odds.get("bookmaker", "bet365"),
                         "market": market_key, "line": line},
                        market_key, sel, odds, prob, factors, match_date, weights_version,
                    )
                    if pred:
                        tag = "over" if "Over" in sel else "under"
                        doc_id = f"{match_id}_{side}_goals_{str(line).replace('.','_')}_{tag}"
                        pred["match_id"] = doc_id
                        try: _batch.set(col("predictions").document(doc_id), pred)
                        except Exception: logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                        if pred["edge"] > SPORTS_ALERT_EDGE:
                            payload = _build_alert_payload(pred, enriched_match)
                            payload["market_emoji"] = "⚽"
                            payload["intensity"]    = _intensity(pred["edge"])
                            await _send_telegram_alert(payload)
                        signals_out.append(pred)

    # ── HT/FT ─────────────────────────────────────────────────────────────────
    if hw is not None and d is not None and aw is not None:
        htft_probs = calc_ht_ft(float(hw), float(d), float(aw), home_xg, away_xg)
        # Buscar odds HT/FT en OddsPapi v4
        htft_fixtures = await _fetch_htft_fixtures()
        from analyzers.corners_bookings import _find_fixture as _htft_find
        htft_fix = _htft_find(htft_fixtures, home_team, away_team)
        if htft_fix:
            htft_implied = _parse_htft_from_fixture(htft_fix)
            if htft_implied:
                for combo, prob in htft_probs.items():
                    # Buscar el implied price más cercano en el fixture
                    # Los IDs de OddsPapi para HT/FT combinan ht_result/ft_result
                    # Sin mapeo exacto, usar implied market como referencia
                    best_implied = min(htft_implied.values()) if htft_implied else 0
                    if best_implied <= 0:
                        continue
                    # Construir odds sintéticas desde implied si no hay mapeo directo
                    # Esto es informacional — edge real requeriría mapeo exacto de outomeId
                    logger.debug("football_markets(%s): htft %s p=%.3f implied_ref=%.3f",
                                 match_id, combo, prob, best_implied)

    # Commit todas las writes del partido en una sola RPC (N×300ms → 1×300ms)
    try:
        _batch.commit()
    except Exception:
        logger.error("football_markets(%s): error en batch commit", match_id, exc_info=True)

    if signals_out:
        logger.info("football_markets(%s): %d señales extra",
                    match_id, len(signals_out))
    return signals_out
