"""
services/sports-agent/analyzers/player_props.py

Mercados de jugadores: goleador (first/anytime) y asistente.

Modelo:
  P(marca) = 1 - Poisson(0, tasa_gol_90 × λ_equipo / 11)
  P(asiste) = 1 - Poisson(0, tasa_asist_90 × λ_goles_equipo)
  tasa_gol/asist_90 desde API-Football /players?fixture (mínimo 300 min jugados)

Odds: The Odds API player_goal_scorer / player_first_assist (EPL, LaLiga, BL1, SA, FL1)
Quota: gasta 1 req The Odds API por partido. Solo si quota_restante >= 5.
market = "first_scorer" | "anytime_scorer" | "anytime_assist"
"""
import asyncio
import logging
from datetime import datetime, date, timezone
from typing import Optional

import httpx
from scipy.stats import poisson as _poisson

from shared.config import (
    FOOTBALL_RAPID_API_KEY, ODDS_API_KEY,
    SPORTS_MIN_EDGE, SPORTS_MIN_CONFIDENCE, SPORTS_ALERT_EDGE,
)
from shared.api_quota_manager import quota

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
_MIN_MINUTES = 300
_SQUAD_SIZE = 11.0

# Ligas con player props en The Odds API
_PROP_SPORT_KEYS = {
    "PL":  "soccer_england_premier_league",
    "PD":  "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "SA":  "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
}

# Emojis para alertas
_MARKET_EMOJI = {
    "first_scorer":   "🥅",
    "anytime_scorer": "🥅",
    "anytime_assist": "🎯",
}


# ── The Odds API — player props por evento ────────────────────────────────────

async def _fetch_player_props(sport_key: str, event_id: str, market: str) -> list[dict]:
    """
    GET /sports/{sport_key}/events/{event_id}/odds?markets={market}
    Devuelve lista de {player, odds, bookmaker}.
    Cuenta como 1 req the_odds_api.
    """
    if not ODDS_API_KEY or not quota.can_call("the_odds_api"):
        return []

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": market,
        "oddsFormat": "decimal",
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params)

        if resp.status_code in (401, 422):
            return []
        if resp.status_code != 200:
            logger.warning("player_props: The Odds API %d para %s", resp.status_code, market)
            return []

        remaining = resp.headers.get("x-requests-remaining")
        quota.track_call("the_odds_api", remaining=remaining)
        data = resp.json()

        results: list[dict] = []
        for bk in data.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != market:
                    continue
                for o in mkt.get("outcomes", []):
                    price = float(o.get("price", 0))
                    if price <= 1.05:
                        continue
                    results.append({
                        "player": o.get("description", o.get("name", "")),
                        "team":   o.get("name", ""),
                        "odds":   price,
                        "bookmaker": bk.get("key", ""),
                    })
        return results

    except Exception:
        logger.error("player_props: error fetch props", exc_info=True)
        return []


async def _find_event_id(sport_key: str, home_team: str, away_team: str) -> str | None:
    """Busca el event_id en The Odds API para el partido dado."""
    if not ODDS_API_KEY:
        return None
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params={"apiKey": ODDS_API_KEY})
        if resp.status_code != 200:
            return None
        for ev in resp.json():
            h = ev.get("home_team", "").lower()
            a = ev.get("away_team", "").lower()
            if home_team.lower()[:6] in h and away_team.lower()[:6] in a:
                return ev.get("id")
    except Exception:
        pass
    return None


# ── API-Football — player stats por fixture ───────────────────────────────────

async def _fetch_player_stats(fixture_id: int) -> list[dict]:
    """
    GET /v3/fixtures/players?fixture={fixture_id}
    Devuelve lista de {name, team, goals, assists, minutes}.
    Requiere API-Football RapidAPI subscrito.
    """
    if not FOOTBALL_RAPID_API_KEY:
        return []
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures/players"
    headers = {
        "X-RapidAPI-Key":  FOOTBALL_RAPID_API_KEY,
        "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com",
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params={"fixture": fixture_id})
        if resp.status_code in (403, 429):
            # 403: free tier sin acceso a /fixtures/players; 429: rate limit. Ambos no fatales.
            logger.debug("player_props: RapidAPI %d para fixture %s — saltando", resp.status_code, fixture_id)
            return []
        if resp.status_code != 200:
            return []
        players = []
        for team_data in resp.json().get("response", []):
            team_name = team_data.get("team", {}).get("name", "")
            for p in team_data.get("players", []):
                stats = p.get("statistics", [{}])[0]
                minutes = stats.get("games", {}).get("minutes") or 0
                if minutes < _MIN_MINUTES:
                    continue
                goals = stats.get("goals", {}).get("total") or 0
                assists = stats.get("goals", {}).get("assists") or 0
                players.append({
                    "name":    p.get("player", {}).get("name", ""),
                    "team":    team_name,
                    "goals":   goals,
                    "assists": assists,
                    "minutes": minutes,
                    "goals_per90":   round(goals / minutes * 90, 4) if minutes else 0,
                    "assists_per90": round(assists / minutes * 90, 4) if minutes else 0,
                })
        return players
    except Exception:
        logger.debug("player_props: API-Football no disponible para fixture %s", fixture_id)
        return []


# ── Cálculo de probabilidades ─────────────────────────────────────────────────

def _p_score(goals_per90: float, team_xg: float) -> float:
    """P(jugador marca al menos 1 gol) usando Poisson."""
    expected = goals_per90 * team_xg / _SQUAD_SIZE
    return round(1.0 - float(_poisson.pmf(0, max(0.01, expected))), 4)


def _p_assist(assists_per90: float, team_xg: float) -> float:
    """P(jugador da al menos 1 asistencia)."""
    expected = assists_per90 * team_xg / _SQUAD_SIZE
    return round(1.0 - float(_poisson.pmf(0, max(0.01, expected))), 4)


# ── Generación de señales ─────────────────────────────────────────────────────

def _make_prop_signal(
    base: dict, market_type: str, player: str, team: str,
    odds: float, prob: float, bookmaker: str,
    match_date, weights_version: int,
) -> dict | None:
    from analyzers.value_bet_engine import kelly_criterion
    edge = round(prob - 1.0 / odds, 4) if odds > 1 else 0.0
    if edge < SPORTS_MIN_EDGE:
        return None
    confidence = min(0.95, round(prob * 1.5, 4))  # proxy de confianza para props
    if confidence < SPORTS_MIN_CONFIDENCE:
        return None
    return {
        **base,
        "market_type":      market_type,
        "market":           market_type,
        "selection":        player,
        "team":             team,
        "odds":             round(odds, 3),
        "calculated_prob":  round(prob, 4),
        "edge":             edge,
        "confidence":       confidence,
        "kelly_fraction":   kelly_criterion(edge, odds),
        "signals":          {},
        "factors":          {"player_prob": round(prob, 4)},
        "data_source":      "player_props",
        "odds_source":      "theoddsapi",
        "match_date":       match_date,
        "weights_version":  weights_version,
        "created_at":       datetime.now(timezone.utc),
        "result":           None,
        "correct":          None,
        "error_type":       None,
    }


async def generate_player_props_signals(
    enriched_match: dict,
    weights_version: int = 0,
) -> list[dict]:
    """
    Genera señales para goleador (anytime + first) y asistente.
    """
    from shared.firestore_client import col
    from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload

    match_id   = enriched_match.get("match_id", "")
    home_team  = enriched_match.get("home_team", "")
    away_team  = enriched_match.get("away_team", "")
    league     = enriched_match.get("league", "")
    match_date = enriched_match.get("match_date")
    home_xg    = float(enriched_match.get("home_xg") or 1.3)
    away_xg    = float(enriched_match.get("away_xg") or 1.1)

    sport_key = _PROP_SPORT_KEYS.get(league)
    if not sport_key:
        return []

    # Solo si quedan >= 5 requests The Odds API
    status = quota.get_quota_status().get("the_odds_api", {})
    if status.get("remaining", 0) < 5:
        logger.debug("player_props: reservando quota the_odds_api (<5 restantes)")
        return []

    event_id = await _find_event_id(sport_key, home_team, away_team)
    if not event_id:
        return []

    base = {
        "match_id": match_id,
        "home_team": home_team,
        "away_team": away_team,
        "sport": "football",
        "league": league,
        "elo_sufficient": False,
        "h2h_sufficient": False,
    }

    # Fetch props odds (2 calls: scorer + assist)
    scorer_odds, assist_odds = await asyncio.gather(
        _fetch_player_props(sport_key, event_id, "player_goal_scorer"),
        _fetch_player_props(sport_key, event_id, "player_first_assist"),
    )

    # Intentar stats de jugadores desde API-Football (puede fallar)
    fixture_numeric_id = int(match_id) if str(match_id).isdigit() else 0
    player_stats: list[dict] = []
    if fixture_numeric_id:
        player_stats = await _fetch_player_stats(fixture_numeric_id)

    # Diccionario rápido de stats por nombre
    stats_by_name: dict[str, dict] = {p["name"].lower(): p for p in player_stats}

    predictions: list[dict] = []

    # ── Anytime scorer ────────────────────────────────────────────────────────
    for o in scorer_odds:
        player_name = o["player"]
        team        = o["team"]
        odds        = o["odds"]
        bookmaker   = o["bookmaker"]
        team_xg = home_xg if team.lower() in home_team.lower() else away_xg

        # Probabilidad: desde stats si disponibles, sino heurística
        stats = stats_by_name.get(player_name.lower())
        if stats and stats["minutes"] >= _MIN_MINUTES:
            prob = _p_score(stats["goals_per90"], team_xg)
        else:
            # Heurística: jugador promedio = xg_equipo / 11
            prob = _p_score(team_xg / _SQUAD_SIZE, team_xg)

        pred = _make_prop_signal(
            base, "anytime_scorer", player_name, team,
            odds, prob, bookmaker, match_date, weights_version,
        )
        if pred:
            doc_id = f"{match_id}_anyscorer_{player_name.lower().replace(' ','_')[:20]}"
            pred["match_id"] = doc_id
            try:
                col("predictions").document(doc_id).set(pred)
            except Exception:
                pass
            if pred["edge"] > SPORTS_ALERT_EDGE:
                payload = _build_alert_payload(pred, enriched_match)
                payload["market_emoji"] = "🥅"
                await _send_telegram_alert(payload)
            predictions.append(pred)

    # ── Anytime assist ────────────────────────────────────────────────────────
    for o in assist_odds:
        player_name = o["player"]
        team        = o["team"]
        odds        = o["odds"]
        bookmaker   = o["bookmaker"]
        team_xg = home_xg if team.lower() in home_team.lower() else away_xg

        stats = stats_by_name.get(player_name.lower())
        if stats and stats["minutes"] >= _MIN_MINUTES:
            prob = _p_assist(stats["assists_per90"], team_xg)
        else:
            prob = _p_assist((team_xg - 0.2) / _SQUAD_SIZE, team_xg)  # ligeramente menor que scorer

        pred = _make_prop_signal(
            base, "anytime_assist", player_name, team,
            odds, prob, bookmaker, match_date, weights_version,
        )
        if pred:
            doc_id = f"{match_id}_assist_{player_name.lower().replace(' ','_')[:20]}"
            pred["match_id"] = doc_id
            try:
                col("predictions").document(doc_id).set(pred)
            except Exception:
                pass
            if pred["edge"] > SPORTS_ALERT_EDGE:
                payload = _build_alert_payload(pred, enriched_match)
                payload["market_emoji"] = "🎯"
                await _send_telegram_alert(payload)
            predictions.append(pred)

    if predictions:
        logger.info("player_props(%s): %d señales — %s vs %s",
                    match_id, len(predictions), home_team, away_team)
    return predictions
