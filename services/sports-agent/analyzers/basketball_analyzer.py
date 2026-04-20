"""
Analizador de baloncesto — offensive/defensive ratings + ventaja local.

Señales:
  off_edge   — (off_rating_home - def_rating_away) normalizado
  def_edge   — (def_rating_away - def_rating_home) normalizado
  form       — win% ponderado por recencia últimos 10
  home_adv   — constante por liga (NBA 3.2 pts, Euroliga 2.8 pts)

Mercados: h2h (moneyline), spread (handicap puntos), totals (o/u puntos)

Odds: The Odds API — basketball_nba, basketball_euroleague
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from scipy.stats import norm

import httpx
import numpy as np

from shared.config import (
    BASKETBALL_HOME_ADV_NBA, BASKETBALL_HOME_ADV_EURO, BASKETBALL_SPREAD_SIGMA,
    ODDS_API_KEY, SPORTS_ALERT_EDGE, SPORTS_MIN_CONFIDENCE, SPORTS_MIN_EDGE,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
_HTTP_TIMEOUT = 15.0
_LEAGUE_ODDS_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(hours=2)

_SPORT_KEY_MAP = {
    "NBA":        "basketball_nba",
    "EUROLEAGUE": "basketball_euroleague",
    "NBA_GL":     "basketball_nba",
}

_HOME_ADV = {
    "NBA":        BASKETBALL_HOME_ADV_NBA,
    "EUROLEAGUE": BASKETBALL_HOME_ADV_EURO,
}


async def _fetch_basketball_odds(sport_key: str) -> list:
    now = datetime.now(timezone.utc)
    cached = _LEAGUE_ODDS_CACHE.get(sport_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    if not ODDS_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{_THE_ODDS_API_BASE}/{sport_key}/odds",
                params={"apiKey": ODDS_API_KEY, "regions": "eu",
                        "markets": "h2h,spreads,totals", "oddsFormat": "decimal"},
            )
        if resp.status_code == 200:
            events = resp.json()
            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.info("basketball_analyzer: The Odds API '%s' — %d eventos, %s req restantes",
                        sport_key, len(events), remaining)
            _LEAGUE_ODDS_CACHE[sport_key] = (now, events)
            return events
        logger.warning("basketball_analyzer: The Odds API %s → HTTP %d", sport_key, resp.status_code)
    except Exception:
        logger.error("basketball_analyzer: error fetching odds %s", sport_key, exc_info=True)
    return []


def _normalize(name: str) -> str:
    import re, unicodedata
    n = unicodedata.normalize("NFD", name.lower().strip()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", n).strip()


def _find_event(events: list, home: str, away: str) -> dict | None:
    for ev in events:
        h = _normalize(ev.get("home_team", ""))
        a = _normalize(ev.get("away_team", ""))
        n1, n2 = _normalize(home), _normalize(away)
        if (n1[:6] in h or h[:6] in n1) and (n2[:6] in a or a[:6] in n2):
            return ev
    return None


def _pts_per_game(raw_matches: list, team_id: int, scored: bool) -> float:
    """pts anotados (scored=True) o recibidos (scored=False) por partido."""
    totals, count = 0.0, 0
    for m in raw_matches:
        is_home = m.get("home_team_id") == team_id or m.get("was_home") is True
        gh = float(m.get("goals_home", 0) or 0)
        ga = float(m.get("goals_away", 0) or 0)
        pts = (gh if is_home else ga) if scored else (ga if is_home else gh)
        totals += pts
        count += 1
    return round(totals / count, 2) if count > 0 else 100.0


def _build_ratings(home_stats: dict, away_stats: dict, league: str) -> dict:
    home_id = int(home_stats.get("team_id", 0))
    away_id = int(away_stats.get("team_id", 0))
    raw_h = home_stats.get("raw_matches", [])
    raw_a = away_stats.get("raw_matches", [])

    off_home = _pts_per_game(raw_h, home_id, scored=True)
    def_home = _pts_per_game(raw_h, home_id, scored=False)
    off_away = _pts_per_game(raw_a, away_id, scored=True)
    def_away = _pts_per_game(raw_a, away_id, scored=False)

    home_adv = _HOME_ADV.get(league, BASKETBALL_HOME_ADV_NBA)

    # Esperanza de puntos
    exp_home = (off_home + def_away) / 2 + home_adv
    exp_away = (off_away + def_home) / 2
    expected_margin = exp_home - exp_away  # positivo = home gana

    form_h = float(home_stats.get("form_score", 50.0)) / 100.0
    form_a = float(away_stats.get("form_score", 50.0)) / 100.0

    # Probabilidades
    # Moneyline: P(home wins) = P(margin > 0)
    p_home = float(norm.cdf(0, loc=-expected_margin, scale=BASKETBALL_SPREAD_SIGMA))

    # Confianza por dispersión de señales
    off_sig = round(max(0.0, min(1.0, (off_home - off_away + 30) / 60)), 4)
    def_sig = round(max(0.0, min(1.0, (def_away - def_home + 30) / 60)), 4)
    form_sig = round((form_h - form_a + 1.0) / 2.0, 4)
    signals = {"off_edge": off_sig, "def_edge": def_sig, "form": form_sig}
    conf = max(0.0, 1.0 - float(np.std(list(signals.values()))))

    return {
        "off_home": off_home, "def_home": def_home,
        "off_away": off_away, "def_away": def_away,
        "exp_home": round(exp_home, 1), "exp_away": round(exp_away, 1),
        "expected_margin": round(expected_margin, 2),
        "p_home_win": round(p_home, 4),
        "signals": signals,
        "confidence": round(conf, 4),
    }


def _get_moneyline_odds(event: dict) -> dict | None:
    home_team = event.get("home_team", "")
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            home_odds = away_odds = None
            for o in mkt.get("outcomes", []):
                nm = o.get("name", "")
                pr = float(o.get("price", 0))
                if _normalize(nm)[:6] == _normalize(home_team)[:6]:
                    home_odds = pr
                else:
                    away_odds = pr
            if home_odds and away_odds:
                return {"home_odds": home_odds, "away_odds": away_odds,
                        "bookmaker": bk.get("key", "bet365")}
    return None


def _get_spread_odds(event: dict) -> dict | None:
    home_team = event.get("home_team", "")
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "spreads":
                continue
            for o in mkt.get("outcomes", []):
                nm = o.get("name", "")
                if _normalize(nm)[:6] != _normalize(home_team)[:6]:
                    continue
                try:
                    pt = float(o.get("point", 0))
                except (TypeError, ValueError):
                    continue
                pr = float(o.get("price", 0))
                return {"home_line": pt, "home_odds": pr,
                        "bookmaker": bk.get("key", "bet365")}
    return None


def _get_totals_odds(event: dict) -> dict | None:
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "totals":
                continue
            over = under = line = None
            for o in mkt.get("outcomes", []):
                try:
                    pt = float(o.get("point") or o.get("description") or 0)
                except (TypeError, ValueError):
                    continue
                pr = float(o.get("price", 0))
                nm = o.get("name", "").lower()
                if nm == "over":
                    over = pr
                    line = pt
                elif nm == "under":
                    under = pr
            if over and under and line:
                return {"over_odds": over, "under_odds": under, "line": line,
                        "bookmaker": bk.get("key", "pinnacle")}
    return None


def _make_pred(base: dict, market: str, selection: str, odds: float,
               prob: float, signals: dict, conf: float, match_date,
               weights_version: int, bookmaker: str) -> dict | None:
    from analyzers.value_bet_engine import kelly_criterion
    edge = round(prob - 1.0 / odds, 4) if odds > 1 else 0.0
    if edge <= SPORTS_MIN_EDGE or conf <= SPORTS_MIN_CONFIDENCE:
        return None
    return {
        **base,
        "market_type": market,
        "selection": selection,
        "bookmaker": bookmaker,
        "odds": round(odds, 3),
        "calculated_prob": round(prob, 4),
        "edge": edge,
        "confidence": round(conf, 4),
        "kelly_fraction": kelly_criterion(edge, odds),
        "signals": signals,
        "factors": signals,
        "data_source": "basketball_model",
        "match_date": match_date,
        "weights_version": weights_version,
        "created_at": datetime.now(timezone.utc),
        "result": None, "correct": None, "error_type": None,
    }


def _save_and_alert(pred: dict, doc_id: str, enriched: dict) -> None:
    from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload
    import asyncio
    try:
        col("predictions").document(doc_id).set(pred)
    except Exception:
        logger.error("basketball_analyzer: error guardando %s", doc_id, exc_info=True)
    if pred["edge"] > SPORTS_ALERT_EDGE:
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_send_telegram_alert(_build_alert_payload(pred, enriched)))
        except Exception:
            logger.error("basketball_analyzer: error enviando alerta %s", doc_id, exc_info=True)


async def generate_basketball_signals(game: dict, weights_version: int = 0) -> list[dict]:
    match_id  = str(game.get("match_id", ""))
    home_name = game.get("home_team_name", game.get("home_team", ""))
    away_name = game.get("away_team_name", game.get("away_team", ""))
    league    = game.get("league", "NBA")
    home_id   = str(game.get("home_team_id", ""))
    away_id   = str(game.get("away_team_id", ""))
    match_date = game.get("date") or game.get("match_date")

    if not home_id or not away_id:
        return []

    # Leer stats de Firestore
    try:
        loop = asyncio.get_event_loop()
        hd, ad = await asyncio.gather(
            loop.run_in_executor(None, lambda: col("team_stats").document(home_id).get()),
            loop.run_in_executor(None, lambda: col("team_stats").document(away_id).get()),
        )
        home_stats = hd.to_dict() if hd.exists else {}
        away_stats = ad.to_dict() if ad.exists else {}
    except Exception:
        logger.error("basketball_analyzer(%s): error leyendo team_stats", match_id, exc_info=True)
        return []

    if not home_stats.get("raw_matches") and not away_stats.get("raw_matches"):
        return []

    # Ratings
    rats = _build_ratings(home_stats, away_stats, league)
    sigs = rats["signals"]
    conf = rats["confidence"]
    margin = rats["expected_margin"]

    # Odds
    sport_key = _SPORT_KEY_MAP.get(league, "basketball_nba")
    events = await _fetch_basketball_odds(sport_key)
    event = _find_event(events, home_name, away_name)

    base = {
        "match_id": match_id,
        "home_team": home_name,
        "away_team": away_name,
        "sport": "basketball",
        "league": league,
        "elo_sufficient": False,
        "h2h_sufficient": False,
    }

    predictions: list[dict] = []

    # ── Moneyline ─────────────────────────────────────────────────────────────
    if event:
        ml = _get_moneyline_odds(event)
        if ml:
            for team, prob, odds, tag in [
                (home_name, rats["p_home_win"], ml["home_odds"], "home"),
                (away_name, 1.0 - rats["p_home_win"], ml["away_odds"], "away"),
            ]:
                pred = _make_pred(base, "h2h", team, odds, prob,
                                  sigs, conf, match_date, weights_version, ml["bookmaker"])
                if pred:
                    doc_id = f"{match_id}_ml_{tag}"
                    pred["match_id"] = doc_id
                    _save_and_alert(pred, doc_id, game)
                    predictions.append(pred)

    # ── Spread ────────────────────────────────────────────────────────────────
    if event:
        sp = _get_spread_odds(event)
        if sp:
            home_line = sp["home_line"]  # negativo = home da puntos
            p_covers = float(norm.cdf(0, loc=-(margin + home_line),
                                       scale=BASKETBALL_SPREAD_SIGMA))
            sel = f"{home_name} {home_line:+.1f}"
            pred = _make_pred(base, "spread", sel, sp["home_odds"],
                              round(p_covers, 4),
                              {**sigs, "expected_margin": round(margin, 2)},
                              conf * 0.9, match_date, weights_version, sp["bookmaker"])
            if pred:
                doc_id = f"{match_id}_spread"
                pred["match_id"] = doc_id
                _save_and_alert(pred, doc_id, game)
                predictions.append(pred)

    # ── Totals ────────────────────────────────────────────────────────────────
    if event:
        tot = _get_totals_odds(event)
        if tot:
            exp_total = rats["exp_home"] + rats["exp_away"]
            line = tot["line"]
            p_over = float(1.0 - norm.cdf(line, loc=exp_total,
                                           scale=BASKETBALL_SPREAD_SIGMA * 1.2))
            for sel, prob, odds_k in [
                (f"Over {line:.1f}", round(p_over, 4), tot["over_odds"]),
                (f"Under {line:.1f}", round(1.0 - p_over, 4), tot["under_odds"]),
            ]:
                pred = _make_pred(
                    base, "totals", sel, odds_k, prob,
                    {**sigs, "expected_total": round(exp_total, 1)},
                    conf * 0.9, match_date, weights_version, tot["bookmaker"]
                )
                if pred:
                    tag = "over" if "Over" in sel else "under"
                    doc_id = f"{match_id}_tot_{tag}"
                    pred["match_id"] = doc_id
                    _save_and_alert(pred, doc_id, game)
                    predictions.append(pred)

    if predictions:
        logger.info("basketball_analyzer(%s): %d señales — %s vs %s",
                    match_id, len(predictions), home_name, away_name)
    return predictions
