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
    TAVILY_API_KEY,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
_HTTP_TIMEOUT = 15.0
_LEAGUE_ODDS_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(hours=24)

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
               weights_version: int, bookmaker: str,
               edge_discount: float = 1.0) -> dict | None:
    from analyzers.value_bet_engine import kelly_criterion, calculate_ev
    edge = round((prob - 1.0 / odds) * edge_discount, 4) if odds > 1 else 0.0
    ev = calculate_ev(prob, odds)
    if ev <= SPORTS_MIN_EDGE or conf <= SPORTS_MIN_CONFIDENCE:
        return None
    return {
        **base,
        "market_type": market,
        "selection": selection,
        "bookmaker": bookmaker,
        "odds": round(odds, 3),
        "calculated_prob": round(prob, 4),
        "edge": edge,
        "ev": round(ev, 4),
        "confidence": round(conf, 4),
        "kelly_fraction": kelly_criterion(ev, odds),
        "signals": signals,
        "factors": signals,
        "data_source": "basketball_model",
        "match_date": match_date,
        "weights_version": weights_version,
        "created_at": datetime.now(timezone.utc),
        "result": None, "correct": None, "error_type": None,
    }


def _save_and_alert(pred: dict, doc_id: str, enriched: dict, batch=None) -> None:
    from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload
    import asyncio
    try:
        if batch is not None:
            batch.set(col("predictions").document(doc_id), pred)
        else:
            col("predictions").document(doc_id).set(pred)
    except Exception:
        logger.error("basketball_analyzer: error guardando %s", doc_id, exc_info=True)
    if pred.get("ev", pred["edge"]) > SPORTS_ALERT_EDGE:
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_send_telegram_alert(_build_alert_payload(pred, enriched)))
        except Exception:
            logger.error("basketball_analyzer: error enviando alerta %s", doc_id, exc_info=True)


async def _fetch_nba_injury_context(home_name: str, away_name: str) -> dict:
    """
    Busca injury reports NBA via Tavily.
    Si un top-3 scorer es mencionado como 'out' o 'questionable' → reduce prob del equipo en 8%.
    Devuelve dict con:
      home_adj: float (multiplicador para p_home_win, 1.0 = sin cambio)
      away_adj: float (multiplicador para p_home_win por lesión visitante, 1.0 = sin cambio)
      notes: list[str]
    Nunca lanza excepción.
    """
    if not TAVILY_API_KEY:
        return {"home_adj": 1.0, "away_adj": 1.0, "notes": []}

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_API_KEY)

        _OUT_KEYWORDS = {"out", "questionable", "doubtful", "baja", "lesionado", "injured"}

        async def _run_query(q: str) -> list:
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: client.search(q, max_results=3, search_depth="basic"),
                )
                return result.get("results", [])
            except Exception:
                return []

        home_results, away_results = await asyncio.gather(
            _run_query(f"{home_name} injury report today"),
            _run_query(f"{away_name} injury report today"),
        )

        home_adj = 1.0
        away_adj = 1.0
        notes: list[str] = []

        for item in home_results:
            content = (item.get("content", "") + " " + item.get("title", "")).lower()
            if any(kw in content for kw in _OUT_KEYWORDS):
                home_adj = round(home_adj * (1.0 - 0.08), 4)
                domain = item.get("url", "").split("/")[2] if "/" in item.get("url", "") else ""
                notes.append(f"{home_name} injury concern — {item.get('title', '')[:60]} (fuente: {domain})")
                break

        for item in away_results:
            content = (item.get("content", "") + " " + item.get("title", "")).lower()
            if any(kw in content for kw in _OUT_KEYWORDS):
                away_adj = round(away_adj * (1.0 - 0.08), 4)
                domain = item.get("url", "").split("/")[2] if "/" in item.get("url", "") else ""
                notes.append(f"{away_name} injury concern — {item.get('title', '')[:60]} (fuente: {domain})")
                break

        if notes:
            logger.info(
                "_fetch_nba_injury_context(%s vs %s): home_adj=%.2f away_adj=%.2f notes=%s",
                home_name, away_name, home_adj, away_adj, notes,
            )

        return {"home_adj": home_adj, "away_adj": away_adj, "notes": notes}

    except Exception:
        logger.error(
            "_fetch_nba_injury_context(%s vs %s): error — devolviendo sin ajuste",
            home_name, away_name, exc_info=True,
        )
        return {"home_adj": 1.0, "away_adj": 1.0, "notes": []}


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
            loop.run_in_executor(None, lambda: col("team_stats").document(f"bball_{home_id}").get()),
            loop.run_in_executor(None, lambda: col("team_stats").document(f"bball_{away_id}").get()),
        )
        home_stats = hd.to_dict() if hd.exists else {}
        away_stats = ad.to_dict() if ad.exists else {}
    except Exception:
        logger.error("basketball_analyzer(%s): error leyendo team_stats", match_id, exc_info=True)
        return []

    if not home_stats.get("raw_matches") and not away_stats.get("raw_matches"):
        logger.debug(
            "basketball_analyzer(%s): sin team_stats para %s vs %s — skip",
            match_id, home_name, away_name,
        )
        return []

    # Ratings
    rats = _build_ratings(home_stats, away_stats, league)
    sigs = rats["signals"]
    conf = rats["confidence"]
    margin = rats["expected_margin"]

    # --- Back-to-back detection ---
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()

    def _played_yesterday(raw_matches: list) -> bool:
        for m in raw_matches[-3:]:  # solo últimos 3 para evitar N lecturas
            d = str(m.get("match_date", ""))[:10]
            if d == yesterday_iso:
                return True
        return False

    home_b2b = _played_yesterday(home_stats.get("raw_matches", []))
    away_b2b = _played_yesterday(away_stats.get("raw_matches", []))

    p_home = rats["p_home_win"]
    if home_b2b:
        p_home = round(max(0.01, p_home - 0.05), 4)
        logger.info(
            "basketball_analyzer(%s): %s back-to-back → p_home reducida a %.3f",
            match_id, home_name, p_home,
        )
    if away_b2b:
        p_home = round(min(0.99, p_home + 0.05), 4)
        logger.info(
            "basketball_analyzer(%s): %s back-to-back → p_home aumentada a %.3f (away B2B)",
            match_id, away_name, p_home,
        )
    # Actualizar rats con p_home ajustada
    rats = {**rats, "p_home_win": p_home}

    # --- NBA injury search via Tavily ---
    injury_ctx = await _fetch_nba_injury_context(home_name, away_name)
    if injury_ctx["home_adj"] < 1.0:
        adj_p = round(max(0.01, rats["p_home_win"] * injury_ctx["home_adj"]), 4)
        logger.info(
            "basketball_analyzer(%s): %s injury → p_home %.3f → %.3f",
            match_id, home_name, rats["p_home_win"], adj_p,
        )
        rats = {**rats, "p_home_win": adj_p}
    if injury_ctx["away_adj"] < 1.0:
        adj_p = round(min(0.99, rats["p_home_win"] / max(injury_ctx["away_adj"], 0.01)), 4)
        logger.info(
            "basketball_analyzer(%s): %s injury → p_home adjusted to %.3f (away injured)",
            match_id, away_name, adj_p,
        )
        rats = {**rats, "p_home_win": adj_p}

    # --- Seeding NBA Playoffs ---
    home_seed: int | None = game.get("home_seed")
    away_seed: int | None = game.get("away_seed")
    seed_diff = abs(home_seed - away_seed) if (home_seed and away_seed) else 0
    # Si la diferencia de seed es >2, reducir confianza al 50% (el modelo subestima la brecha de calidad)
    if seed_diff > 2:
        conf = round(conf * 0.5, 4)
        logger.info(
            "basketball_analyzer(%s): seed_diff=%d → confianza reducida al 50%% (%.3f) [%s vs %s]",
            match_id, seed_diff, conf, home_name, away_name,
        )

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
        "external_context": injury_ctx["notes"],
    }

    predictions: list[dict] = []
    from shared.firestore_client import get_client as _get_fs_client
    _fs_batch = _get_fs_client().batch()  # WriteBatch: todas las writes en 1 RPC

    # ── Moneyline ─────────────────────────────────────────────────────────────
    if event:
        ml = _get_moneyline_odds(event)
        if ml:
            # Guardia de divergencia extrema: descarta si modelo difiere >2.5× del mercado
            _impl_home = 1.0 / ml["home_odds"] if ml["home_odds"] > 1.0 else 1.0
            _impl_away = 1.0 / ml["away_odds"] if ml["away_odds"] > 1.0 else 1.0
            _p_home_chk = rats["p_home_win"]
            _p_away_chk = 1.0 - _p_home_chk
            if _p_home_chk > _impl_home * 2.5 or _p_away_chk > _impl_away * 2.5:
                logger.warning(
                    "basketball_analyzer(%s): divergencia extrema modelo/mercado — "
                    "p_home=%.3f impl_home=%.3f p_away=%.3f impl_away=%.3f — "
                    "señal moneyline descartada [%s vs %s]",
                    match_id, _p_home_chk, _impl_home, _p_away_chk, _impl_away,
                    home_name, away_name,
                )
                ml = None
        if ml:
            for team, prob, odds, tag, team_seed, opp_seed in [
                (home_name, rats["p_home_win"], ml["home_odds"], "home", home_seed, away_seed),
                (away_name, 1.0 - rats["p_home_win"], ml["away_odds"], "away", away_seed, home_seed),
            ]:
                # Filtro seed: si el equipo seleccionado es peor en seed por >3 → no señal
                if team_seed and opp_seed and (team_seed - opp_seed) > 3:
                    logger.info(
                        "basketball_analyzer(%s): señal %s descartada — "
                        "seed %d vs rival seed %d (diff>3) [%s]",
                        match_id, tag, team_seed, opp_seed, team,
                    )
                    continue
                # Descuento elite: rival con seed ≤2 → bookmakers más eficientes, −20% edge
                edge_discount = 0.80 if (opp_seed and opp_seed <= 2) else 1.0
                pred = _make_pred(base, "h2h", team, odds, prob,
                                  sigs, conf, match_date, weights_version, ml["bookmaker"],
                                  edge_discount=edge_discount)
                if pred:
                    doc_id = f"{match_id}_ml_{tag}"
                    pred["match_id"] = doc_id
                    _save_and_alert(pred, doc_id, game, batch=_fs_batch)
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
                _save_and_alert(pred, doc_id, game, batch=_fs_batch)
                predictions.append(pred)

    # ── Totals ────────────────────────────────────────────────────────────────
    if event:
        tot = _get_totals_odds(event)
        if tot:
            exp_total = rats["exp_home"] + rats["exp_away"]

            # Playoffs NBA: ritmo más lento → totales ~8% inferiores a temporada regular
            _series_title = str(game.get("series_title", "")).lower()
            _is_playoff = (
                game.get("playoff") is True
                or "round" in _series_title
                or "finals" in _series_title
                or "playoffs" in _series_title
            )
            if _is_playoff:
                _total_original = exp_total
                exp_total = round(exp_total * 0.92, 1)
                logger.info(
                    "basketball_analyzer(%s): PLAYOFF_DISCOUNT aplicado: %.1f → %.1f",
                    match_id, _total_original, exp_total,
                )

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
                    _save_and_alert(pred, doc_id, game, batch=_fs_batch)
                    predictions.append(pred)

    # ── PRIMERA MITAD — SPREAD y TOTALS ──────────────────────────────────────
    # Modelo: H1 ≈ 48% del total esperado (NBA historical average)
    H1_FACTOR = 0.48
    exp_total = rats["exp_home"] + rats["exp_away"]
    exp_h1_total  = exp_total  * H1_FACTOR
    exp_h1_margin = margin     * H1_FACTOR

    # H1 spread: intentar The Odds API markets h1_spreads; si no, derivar
    for mkt_key, (h1_line_ref, odds_key_sfx, p_h1_cover) in {
        "h1_spreads": (
            round(exp_h1_margin, 1),
            "home_odds",
            float(norm.cdf(0, loc=-(exp_h1_margin + 0.5), scale=BASKETBALL_SPREAD_SIGMA * 0.7)),
        ),
    }.items():
        h1_sp = _get_market_odds_by_key(event, mkt_key) if event else None
        if h1_sp:
            h1_line  = h1_sp.get("home_line", h1_line_ref)
            h1_odds  = h1_sp.get("home_odds", 0)
            p_h1_cov = float(norm.cdf(0, loc=-(exp_h1_margin + h1_line),
                                      scale=BASKETBALL_SPREAD_SIGMA * 0.7))
        else:
            # Sin odds directas → skip (no hay precio de mercado para calcular edge)
            continue

        sel = f"{home_name} {h1_line:+.1f} H1"
        pred = _make_pred(
            base, "basketball_h1_spread", sel, h1_odds, round(p_h1_cov, 4),
            {**sigs, "exp_h1_margin": round(exp_h1_margin, 2)},
            conf * 0.85, match_date, weights_version, h1_sp.get("bookmaker", "bet365"),
        )
        if pred:
            doc_id = f"{match_id}_h1_spread"
            pred["match_id"] = doc_id
            _save_and_alert(pred, doc_id, game, batch=_fs_batch)
            predictions.append(pred)

    # H1 totals
    h1_tot = _get_market_odds_by_key(event, "h1_totals") if event else None
    if h1_tot:
        h1_line_t = h1_tot.get("line", round(exp_h1_total, 1))
        p_h1_over = float(1.0 - norm.cdf(h1_line_t, loc=exp_h1_total,
                                          scale=BASKETBALL_SPREAD_SIGMA * 0.85))
        for sel, prob, ok in [
            (f"Over {h1_line_t:.1f} H1",  round(p_h1_over, 4), h1_tot.get("over_odds", 0)),
            (f"Under {h1_line_t:.1f} H1", round(1.0 - p_h1_over, 4), h1_tot.get("under_odds", 0)),
        ]:
            if ok <= 1:
                continue
            pred = _make_pred(
                base, "basketball_h1_totals", sel, ok, prob,
                {**sigs, "exp_h1_total": round(exp_h1_total, 1)},
                conf * 0.85, match_date, weights_version, h1_tot.get("bookmaker", "pinnacle"),
            )
            if pred:
                tag = "over" if "Over" in sel else "under"
                doc_id = f"{match_id}_h1_tot_{tag}"
                pred["match_id"] = doc_id
                _save_and_alert(pred, doc_id, game, batch=_fs_batch)
                predictions.append(pred)

    # ── PRIMER CUARTO TOTALS ──────────────────────────────────────────────────
    # Q1 ≈ 12% del total (NBA: cada cuarto ~25% de FH que es ~48% del total → 12%)
    Q1_FACTOR  = 0.12
    exp_q1_tot = exp_total * Q1_FACTOR
    q1_tot = _get_market_odds_by_key(event, "quarter_totals") if event else None
    if q1_tot:
        q1_line = q1_tot.get("line", round(exp_q1_tot, 1))
        p_q1_over = float(1.0 - norm.cdf(q1_line, loc=exp_q1_tot,
                                          scale=BASKETBALL_SPREAD_SIGMA * 0.5))
        for sel, prob, ok in [
            (f"Over {q1_line:.1f} Q1",  round(p_q1_over, 4), q1_tot.get("over_odds", 0)),
            (f"Under {q1_line:.1f} Q1", round(1.0 - p_q1_over, 4), q1_tot.get("under_odds", 0)),
        ]:
            if ok <= 1:
                continue
            pred = _make_pred(
                base, "basketball_q1_totals", sel, ok, prob,
                {**sigs, "exp_q1_total": round(exp_q1_tot, 1)},
                conf * 0.80, match_date, weights_version, q1_tot.get("bookmaker", "pinnacle"),
            )
            if pred:
                tag = "over" if "Over" in sel else "under"
                doc_id = f"{match_id}_q1_tot_{tag}"
                pred["match_id"] = doc_id
                _save_and_alert(pred, doc_id, game, batch=_fs_batch)
                predictions.append(pred)

    try:
        _fs_batch.commit()
    except Exception:
        logger.error("basketball_analyzer(%s): error en batch commit", match_id, exc_info=True)

    if predictions:
        logger.info("basketball_analyzer(%s): %d señales — %s vs %s",
                    match_id, len(predictions), home_name, away_name)
    return predictions


def _get_market_odds_by_key(event: dict, market_key: str) -> dict | None:
    """Busca un mercado por key exacta en The Odds API event."""
    if not event:
        return None
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            result: dict = {"bookmaker": bk.get("key", "bet365")}
            over = under = line = home_odds = home_line = None
            for o in mkt.get("outcomes", []):
                nm = o.get("name", "").lower()
                pr = float(o.get("price", 0))
                try:
                    pt = float(o.get("point", 0))
                except (TypeError, ValueError):
                    pt = 0.0
                if nm == "over":   over  = pr; line = pt
                elif nm == "under": under = pr
                elif "home" in nm: home_odds = pr; home_line = pt
            if over and under:
                result.update({"over_odds": over, "under_odds": under, "line": line})
            if home_odds:
                result.update({"home_odds": home_odds, "home_line": home_line})
            if len(result) > 1:
                return result
    return None
