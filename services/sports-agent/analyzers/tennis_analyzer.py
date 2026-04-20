"""
Analizador de tenis — ensemble sin Poisson ni ELO.

Señales:
  form      — win% últimos 10 partidos ponderado por recencia
  surface   — win% en la superficie del torneo
  ranking   — diferencia normalizada de ranking (0-1, mayor = mejor jugador)
  h2h       — win% histórico directo (solo si >= 3 partidos)

Mercados: h2h (quién gana), set_handicap (-1.5), total_sets (o/u 2.5)

Odds: The Odds API — tennis_atp, tennis_wta, etc.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

from shared.config import (
    ODDS_API_KEY, SPORTS_ALERT_EDGE, SPORTS_MIN_CONFIDENCE,
    SPORTS_MIN_EDGE, TENNIS_WEIGHTS,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
_HTTP_TIMEOUT = 15.0
_LEAGUE_ODDS_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(hours=2)

# Sport keys de The Odds API para tenis
_TENNIS_SPORT_KEYS = {
    "ATP_FRENCH_OPEN":  "tennis_atp_french_open",
    "WTA_FRENCH_OPEN":  "tennis_wta_french_open",
    "ATP_WIMBLEDON":    "tennis_atp_wimbledon",
    "WTA_WIMBLEDON":    "tennis_wta_wimbledon",
    "ATP_US_OPEN":      "tennis_atp_us_open",
    "WTA_US_OPEN":      "tennis_wta_us_open",
    "ATP_BARCELONA":    "tennis_atp_barcelona_open",
    "ATP_MUNICH":       "tennis_atp_munich",
    "WTA_STUTTGART":    "tennis_wta_stuttgart_open",
    # Generic fallback: si no hay key específica usamos éstos
    "ATP": "tennis_atp",
    "WTA": "tennis_wta",
    "ITF": "tennis_itf",
}


async def _fetch_tennis_odds(sport_key: str, match_id: str) -> list:
    now = datetime.now(timezone.utc)
    cached = _LEAGUE_ODDS_CACHE.get(sport_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    if not ODDS_API_KEY:
        return []
    url = f"{_THE_ODDS_API_BASE}/{sport_key}/odds"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "decimal",
            })
        if resp.status_code == 200:
            events = resp.json()
            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.info("tennis_analyzer: The Odds API '%s' — %d eventos, %s req restantes",
                        sport_key, len(events), remaining)
            _LEAGUE_ODDS_CACHE[sport_key] = (now, events)
            return events
        logger.warning("tennis_analyzer: The Odds API %s → HTTP %d", sport_key, resp.status_code)
    except Exception:
        logger.error("tennis_analyzer: error fetching odds %s", sport_key, exc_info=True)
    return []


def _normalize(name: str) -> str:
    import re, unicodedata
    n = unicodedata.normalize("NFD", name.lower().strip()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", n).strip()


def _find_event(events: list, p1: str, p2: str) -> dict | None:
    for ev in events:
        h = _normalize(ev.get("home_team", ""))
        a = _normalize(ev.get("away_team", ""))
        n1, n2 = _normalize(p1), _normalize(p2)
        if (n1[:6] in h or h[:6] in n1) and (n2[:6] in a or a[:6] in n2):
            return ev
        if (n2[:6] in h or h[:6] in n2) and (n1[:6] in a or a[:6] in n1):
            return ev  # swapped
    return None


def _get_h2h_odds(event: dict, p1: str) -> dict | None:
    home_is_p1 = _normalize(p1)[:6] in _normalize(event.get("home_team", ""))[:6]
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            p1_odds = p2_odds = None
            home_team = event.get("home_team", "")
            for o in mkt.get("outcomes", []):
                nm = o.get("name", "")
                pr = float(o.get("price", 0))
                is_home = _normalize(nm)[:5] == _normalize(home_team)[:5]
                if is_home:
                    (p1_odds if home_is_p1 else None) or None
                    if home_is_p1:
                        p1_odds = pr
                    else:
                        p2_odds = pr
                else:
                    if home_is_p1:
                        p2_odds = pr
                    else:
                        p1_odds = pr
            if p1_odds and p2_odds:
                return {"p1_odds": p1_odds, "p2_odds": p2_odds,
                        "bookmaker": bk.get("key", "bet365")}
    return None


def _get_spreads_odds(event: dict) -> dict | None:
    """Busca set handicap -1.5 para el favorito."""
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "spreads":
                continue
            home_team = event.get("home_team", "")
            for o in mkt.get("outcomes", []):
                try:
                    pt = float(o.get("point", 0))
                except (TypeError, ValueError):
                    continue
                if abs(abs(pt) - 1.5) < 0.1:
                    pr = float(o.get("price", 0))
                    nm = o.get("name", "")
                    is_home = _normalize(nm)[:5] == _normalize(home_team)[:5]
                    return {"team": nm, "line": pt, "odds": pr, "is_home": is_home,
                            "bookmaker": bk.get("key", "bet365")}
    return None


def _get_totals_odds(event: dict, line: float = 2.5) -> dict | None:
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "totals":
                continue
            over = under = None
            for o in mkt.get("outcomes", []):
                try:
                    pt = float(o.get("point") or o.get("description") or 0)
                except (TypeError, ValueError):
                    continue
                if abs(pt - line) > 0.3:
                    continue
                pr = float(o.get("price", 0))
                if o.get("name", "").lower() == "over":
                    over = pr
                elif o.get("name", "").lower() == "under":
                    under = pr
            if over and under:
                return {"over_odds": over, "under_odds": under,
                        "line": line, "bookmaker": bk.get("key", "pinnacle")}
    return None


# ── Modelo ────────────────────────────────────────────────────────────────────

def _build_signals(p1_stats: dict, p2_stats: dict, surface: str,
                   h2h_p1_wins: int, h2h_total: int) -> dict:
    """Devuelve señales en [0,1] para el jugador 1."""
    w = TENNIS_WEIGHTS

    # Form: form_score ya viene en 0-100 desde tennis_collector
    form1 = float(p1_stats.get("form_score", 50.0)) / 100.0
    form2 = float(p2_stats.get("form_score", 50.0)) / 100.0
    form_sig = round((form1 - form2 + 1.0) / 2.0, 4)

    # Surface
    surf_key = f"win_rate_{surface.lower()}"
    s1 = float(p1_stats.get(surf_key, p1_stats.get("win_rate_hard", 0.5)))
    s2 = float(p2_stats.get(surf_key, p2_stats.get("win_rate_hard", 0.5)))
    surface_sig = round((s1 - s2 + 1.0) / 2.0, 4)

    # Ranking (menor = mejor): convert to 0-1 where 1=p1 much better
    r1 = float(p1_stats.get("ranking", 500))
    r2 = float(p2_stats.get("ranking", 500))
    if r1 > 0 and r2 > 0:
        rank_sig = round(1.0 / (1.0 + r1 / max(r2, 1)), 4)
    else:
        rank_sig = 0.5

    # H2H
    if h2h_total >= 3:
        h2h_sig = round(h2h_p1_wins / h2h_total, 4)
        h2h_sufficient = True
    else:
        h2h_sig = None
        h2h_sufficient = False

    signals = {"form": form_sig, "surface": surface_sig, "ranking": rank_sig}
    if h2h_sufficient:
        signals["h2h"] = h2h_sig

    # Renormalizar pesos si H2H excluido
    active_weights = {k: w.get(k, 0.25) for k in signals}
    total_w = sum(active_weights.values())
    norm_w = {k: v / total_w for k, v in active_weights.items()} if total_w > 0 \
        else {k: 1 / len(signals) for k in signals}

    prob = sum(signals[k] * norm_w[k] for k in signals)
    prob = max(0.02, min(0.98, prob))  # clamp razonable
    conf = max(0.0, 1.0 - float(np.std(list(signals.values()))))

    return {
        "prob": round(prob, 4),
        "confidence": round(conf, 4),
        "signals": {k: round(float(v), 4) for k, v in signals.items()},
        "h2h_sufficient": h2h_sufficient,
    }


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
        "data_source": "tennis_model",
        "match_date": match_date,
        "weights_version": weights_version,
        "created_at": datetime.now(timezone.utc),
        "result": None, "correct": None, "error_type": None,
    }


async def generate_tennis_signals(match: dict, weights_version: int = 0) -> list[dict]:
    """
    Genera señales para un partido de tenis.
    Lee team_stats de Firestore para ambos jugadores.
    Devuelve lista de predictions guardadas.
    """
    from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload

    match_id = str(match.get("match_id", ""))
    p1_name  = match.get("home_team", "")
    p2_name  = match.get("away_team", "")
    league   = match.get("league", "ATP")
    surface  = match.get("surface", "hard").lower()
    match_date = match.get("date") or match.get("match_date")
    home_id  = str(match.get("home_team_id", ""))
    away_id  = str(match.get("away_team_id", ""))

    if not p1_name or not p2_name or not home_id or not away_id:
        return []

    # Leer stats de Firestore
    try:
        loop = asyncio.get_event_loop()
        p1_doc, p2_doc = await asyncio.gather(
            loop.run_in_executor(None, lambda: col("team_stats").document(home_id).get()),
            loop.run_in_executor(None, lambda: col("team_stats").document(away_id).get()),
        )
        p1_stats = p1_doc.to_dict() if p1_doc.exists else {}
        p2_stats = p2_doc.to_dict() if p2_doc.exists else {}
    except Exception:
        logger.error("tennis_analyzer(%s): error leyendo team_stats", match_id, exc_info=True)
        return []

    if not p1_stats and not p2_stats:
        logger.debug("tennis_analyzer(%s): sin stats para %s o %s", match_id, p1_name, p2_name)
        return []

    # H2H desde el campo del match (guardado por tennis_collector)
    h2h_adv = float(match.get("h2h_advantage", 0.0))
    # Reconstruir conteos aproximados desde h2h_advantage
    h2h_total = 5  # estimado si hay ventaja registrada
    h2h_p1_wins = round((h2h_adv + 1.0) / 2.0 * h2h_total)

    # Señales
    result = _build_signals(p1_stats, p2_stats, surface, h2h_p1_wins, h2h_total)
    prob1 = result["prob"]
    conf  = result["confidence"]
    sigs  = result["signals"]

    # Fetch odds
    sport_key = _TENNIS_SPORT_KEYS.get(league, _TENNIS_SPORT_KEYS.get("ATP", "tennis_atp"))
    events = await _fetch_tennis_odds(sport_key, match_id)
    event = _find_event(events, p1_name, p2_name)

    base = {
        "match_id": match_id,
        "home_team": p1_name,
        "away_team": p2_name,
        "sport": "tennis",
        "league": league,
        "elo_sufficient": False,   # tenis no usa ELO
        "h2h_sufficient": result["h2h_sufficient"],
    }

    predictions: list[dict] = []

    # ── H2H (ganador del partido) ─────────────────────────────────────────────
    if event:
        h2h_odds = _get_h2h_odds(event, p1_name)
        if h2h_odds:
            # p1 victoria
            pred = _make_pred(base, "h2h", p1_name, h2h_odds["p1_odds"],
                              prob1, sigs, conf, match_date, weights_version,
                              h2h_odds["bookmaker"])
            if pred:
                doc_id = f"{match_id}_h2h_p1"
                pred["match_id"] = doc_id
                _save_and_alert(pred, doc_id, base, event)
                predictions.append(pred)

            # p2 victoria
            pred2 = _make_pred(base, "h2h", p2_name, h2h_odds["p2_odds"],
                               1.0 - prob1, sigs, conf, match_date, weights_version,
                               h2h_odds["bookmaker"])
            if pred2:
                doc_id = f"{match_id}_h2h_p2"
                pred2["match_id"] = doc_id
                _save_and_alert(pred2, doc_id, base, event)
                predictions.append(pred2)

    # ── SET HANDICAP -1.5 para favorito ──────────────────────────────────────
    if event:
        sh_odds = _get_spreads_odds(event)
        if sh_odds:
            # P(gana 2-0) ≈ prob_win^2
            p_sets = prob1 if sh_odds["is_home"] else (1.0 - prob1)
            p_20 = round(p_sets ** 2, 4)
            sel = f"{sh_odds['team']} {sh_odds['line']:+.1f} sets"
            pred = _make_pred(base, "set_handicap", sel, sh_odds["odds"],
                              p_20, {"win_prob": round(p_sets, 4), **sigs},
                              conf * 0.9, match_date, weights_version,
                              sh_odds["bookmaker"])
            if pred:
                doc_id = f"{match_id}_sh"
                pred["match_id"] = doc_id
                _save_and_alert(pred, doc_id, base, event)
                predictions.append(pred)

    # ── TOTAL SETS over/under 2.5 ─────────────────────────────────────────────
    if event:
        ts_odds = _get_totals_odds(event, 2.5)
        if ts_odds:
            # P(3 sets) = 2 × P(p1 wins set) × P(p2 wins set)
            p1s = max(0.1, min(0.9, prob1))
            p_over = round(2.0 * p1s * (1.0 - p1s), 4)
            p_under = round(1.0 - p_over, 4)
            for sel, prob_t, ok in [("Over 2.5 sets", p_over, ts_odds["over_odds"]),
                                     ("Under 2.5 sets", p_under, ts_odds["under_odds"])]:
                pred = _make_pred(base, "total_sets", sel, ok, prob_t,
                                  {"dominance": round(abs(prob1 - 0.5) * 2, 4), **sigs},
                                  conf * 0.85, match_date, weights_version,
                                  ts_odds["bookmaker"])
                if pred:
                    tag = "over" if "Over" in sel else "under"
                    doc_id = f"{match_id}_ts_{tag}"
                    pred["match_id"] = doc_id
                    _save_and_alert(pred, doc_id, base, event)
                    predictions.append(pred)

    if predictions:
        logger.info("tennis_analyzer(%s): %d señales — %s vs %s",
                    match_id, len(predictions), p1_name, p2_name)
    return predictions


def _save_and_alert(pred: dict, doc_id: str, base: dict, enriched: dict) -> None:
    from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload
    import asyncio
    try:
        col("predictions").document(doc_id).set(pred)
    except Exception:
        logger.error("tennis_analyzer: error guardando %s", doc_id, exc_info=True)
    if pred["edge"] > SPORTS_ALERT_EDGE:
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_send_telegram_alert(_build_alert_payload(pred, enriched)))
        except Exception:
            logger.error("tennis_analyzer: error enviando alerta %s", doc_id, exc_info=True)
