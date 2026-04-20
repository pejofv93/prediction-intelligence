"""
Mercados extra de fútbol usando el modelo Poisson existente (home_xg / away_xg).
No requiere llamadas API adicionales — usa los xG del enriched_match y
las cuotas ya cacheadas en _LEAGUE_ODDS_CACHE de value_bet_engine.

Mercados:
  btts          — Ambos marcan (Sí/No)
  double_chance — 1X, X2, 12
  asian_handicap — AH -0.5 / -1.0 / -1.5 / +0.5 / +1.0 / +1.5
  totals_3.5    — Goles Over/Under 3.5
"""
import logging
from datetime import datetime, timezone

import numpy as np
from scipy.stats import poisson as _poisson

from shared.config import SPORTS_ALERT_EDGE, SPORTS_MIN_CONFIDENCE, SPORTS_MIN_EDGE
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Líneas de AH que buscamos (negativas = home da ventaja)
_AH_LINES = (-0.5, -1.0, -1.5, 0.5, 1.0, 1.5)


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
    from shared.config import SPORTS_MIN_EDGE, SPORTS_MIN_CONFIDENCE
    from collectors.stats_processor import calculate_edge_simple  # noqa — inline below

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

    # ── BTTS ─────────────────────────────────────────────────────────────────
    if event:
        btts_odds = parse_btts_event(event)
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
                        {**base, "bookmaker": btts_odds["bookmaker"]},
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
    if event:
        spread_lines = parse_spreads_event(event)
        if spread_lines:
            ah_probs = calc_asian_handicap(home_xg, away_xg)
            for spread in spread_lines:
                line = spread.get("home_line", spread.get("line", 0))
                try:
                    line_f = float(line)
                except (TypeError, ValueError):
                    continue
                # Mapear la línea al prob más cercano calculado
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
