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
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
from scipy.stats import poisson as _poisson

from shared.config import (
    ODDSPAPI_KEY, SPORTS_ALERT_EDGE, SPORTS_MIN_CONFIDENCE, SPORTS_MIN_EDGE,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Líneas de AH que buscamos (negativas = home da ventaja)
_AH_LINES = (-0.5, -1.0, -1.5, 0.5, 1.0, 1.5)

# ── OddsPapi client ────────────────────────────────────────────────────────────
_ODDSPAPI_BASE = "https://api.oddspapi.com"
_ODDSPAPI_CACHE: dict[str, tuple[datetime, list]] = {}
_ODDSPAPI_TTL = timedelta(hours=1)
_HTTP_TIMEOUT = 15.0

# Mapeo de league code Firestore → sport/competition en OddsPapi
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


async def _fetch_oddspapi(market: str, league: str) -> list:
    """
    Obtiene cuotas de OddsPapi para un mercado y liga.
    Cachea por market+league durante 1h.
    Devuelve lista de eventos con odds o [] si falla/no configurada.
    """
    if not ODDSPAPI_KEY:
        return []

    cache_key = f"{market}_{league}"
    now = datetime.now(timezone.utc)
    cached = _ODDSPAPI_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _ODDSPAPI_TTL:
        return cached[1]

    competition = _ODDSPAPI_LEAGUE_MAP.get(league, "")
    params: dict = {"apiKey": ODDSPAPI_KEY, "market": market, "sport": "football"}
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
            logger.warning("OddsPapi: HTTP %d para %s/%s", resp.status_code, market, league)
            return []

        data = resp.json()
        # OddsPapi puede devolver {"data": [...]} o directamente una lista
        events = data if isinstance(data, list) else data.get("data", data.get("events", []))
        if not isinstance(events, list):
            events = []

        logger.info("OddsPapi: %s/%s → %d eventos", market, league, len(events))
        _ODDSPAPI_CACHE[cache_key] = (now, events)
        return events

    except Exception:
        logger.error("OddsPapi: error fetching %s/%s", market, league, exc_info=True)
        return []


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
    """
    from analyzers.value_bet_engine import (
        _teams_match, _send_telegram_alert, _build_alert_payload
    )

    home_xg = enriched_match.get("home_xg")
    away_xg = enriched_match.get("away_xg")
    hw = enriched_match.get("poisson_home_win")
    d  = enriched_match.get("poisson_draw")
    aw = enriched_match.get("poisson_away_win")

    if home_xg is None or away_xg is None:
        return []

    home_xg = float(home_xg)
    away_xg = float(away_xg)

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

    # Fuentes de cuotas: OddsPapi primero (BTTS/AH), The Odds API como fallback
    op_btts_events = await _fetch_oddspapi("btts", league)
    op_ah_events   = await _fetch_oddspapi("asian_handicap", league)
    op_btts_ev = _oddspapi_find_event(op_btts_events, home_team, away_team) if op_btts_events else None
    op_ah_ev   = _oddspapi_find_event(op_ah_events,   home_team, away_team) if op_ah_events else None

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
                        col("predictions").document(doc_id).set(pred)
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
                        col("predictions").document(doc_id).set(pred)
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
                    col("predictions").document(doc_id).set(pred)
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
                        col("predictions").document(doc_id).set(pred)
                    except Exception:
                        logger.error("football_markets: error guardando %s", doc_id, exc_info=True)
                    if pred["edge"] > SPORTS_ALERT_EDGE:
                        await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
                    signals_out.append(pred)

    if signals_out:
        logger.info("football_markets(%s): %d señales extra (btts/dc/ah/t35)",
                    match_id, len(signals_out))
    return signals_out
