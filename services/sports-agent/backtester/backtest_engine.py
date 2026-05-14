"""
Sports backtester — evalua rendimiento historico del modelo.
Colecciones: backtest_fixtures, backtest_results
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Optional

import httpx

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col

logger = logging.getLogger(__name__)

_DEFAULT_SPORTS_MIN_EDGE = 0.08

# Ligas soportadas: nombre → league_id (api-sports)
_BACKTEST_LEAGUES = {
    "Premier League": 39,
    "La Liga": 140,
    "Bundesliga": 78,
    "Serie A": 135,
    "Ligue 1": 61,
}

_BACKTEST_SEASONS = [2022, 2023, 2024]
_BACKTEST_MARKETS = ["h2h", "totals", "btts"]
_MAX_API_CALLS = 50


# ---------------------------------------------------------------------------
# fetch_historical_fixtures
# ---------------------------------------------------------------------------

async def fetch_historical_fixtures(
    league_id: int, season: int, api_key: str, league_name: str = ""
) -> list[dict]:
    """
    Lee partidos FINISHED de upcoming_matches (Firestore) para la liga indicada.
    Filtra por el campo string 'league' (ej: "Premier League"), no por league_id entero.
    Fallback a backtest_fixtures si ya hay datos cacheados.

    El plan gratuito de api-football-v1.p.rapidapi.com no incluye fixtures históricos
    (responde 403), así que usamos los datos ya recolectados por el pipeline de collect.
    """
    # 1. Caché backtest_fixtures (si hubo runs anteriores)
    cached = _read_fixtures_from_firestore(league_id, season)
    if cached:
        logger.info(
            "fetch_historical_fixtures: league=%d season=%d → %d fixtures (backtest_fixtures cache)",
            league_id, season, len(cached),
        )
        return cached

    # 2. Leer de prodmatch_results (colección permanente, sin TTL)
    # prodmatch_results almacena el código de football-data.org (ej: "PL"), no el nombre
    _LEAGUE_NAME_TO_CODE = {
        "Premier League": "PL",
        "La Liga":        "PD",
        "Bundesliga":     "BL1",
        "Serie A":        "SA",
        "Ligue 1":        "FL1",
    }
    league_code = _LEAGUE_NAME_TO_CODE.get(league_name, league_name)

    if not league_code:
        logger.warning("fetch_historical_fixtures: league_name vacío, no se puede filtrar prodmatch_results")
        return []

    try:
        from google.cloud.firestore_v1.base_query import FieldFilter as FF
        docs = list(
            col("prodmatch_results")
            .where(filter=FF("league", "==", league_code))
            .stream()
        )
        fixtures: list[dict] = []
        for d in docs:
            raw = d.to_dict()
            gh = raw.get("goals_home")
            ga = raw.get("goals_away")
            if gh is None or ga is None:
                continue
            gh, ga = int(gh), int(ga)
            result = "H" if gh > ga else ("A" if ga > gh else "D")
            fixture_id = hash(d.id) & 0x7FFFFFFF
            fixtures.append({
                "fixture_id":    fixture_id,
                "league_id":     league_id,
                "league":        raw.get("league", ""),
                "season":        season,
                "date":          str(raw.get("match_date", ""))[:10],
                "home_team":     raw.get("home_team", ""),
                "away_team":     raw.get("away_team", ""),
                "goals_home":    gh,
                "goals_away":    ga,
                "result":        result,
                "home_team_id":  raw.get("home_team_id"),
                "away_team_id":  raw.get("away_team_id"),
                "odds_home":     raw.get("odds_home") or raw.get("odds_1") or None,
                "odds_draw":     raw.get("odds_draw") or raw.get("odds_x") or None,
                "odds_away":     raw.get("odds_away") or raw.get("odds_2") or None,
                "odds_over25":   raw.get("odds_over25") or None,
                "odds_under25":  raw.get("odds_under25") or None,
                "odds_btts_yes": raw.get("odds_btts_yes") or None,
                "odds_btts_no":  raw.get("odds_btts_no") or None,
            })
        logger.info(
            "fetch_historical_fixtures: league=%s season=%d → %d fixtures (prodmatch_results)",
            league_name, season, len(fixtures),
        )
        return fixtures
    except Exception as e:
        logger.error(
            "fetch_historical_fixtures(%s/%d): error leyendo Firestore: %s",
            league_name, season, e,
        )
        return []


def _read_fixtures_from_firestore(league_id: int, season: int) -> list[dict]:
    """Lee fixtures guardados en Firestore para league_id y season."""
    try:
        docs = (
            col("backtest_fixtures")
            .where(filter=FieldFilter("league_id", "==", league_id))
            .where(filter=FieldFilter("season", "==", season))
            .stream()
        )
        return [d.to_dict() for d in docs]
    except Exception as e:
        logger.error(
            "_read_fixtures_from_firestore(%d/%d): error: %s", league_id, season, e
        )
        return []


def _parse_fixture(raw: dict, league_id: int, season: int) -> Optional[dict]:
    """Normaliza un fixture del endpoint api-sports."""
    try:
        fixture_info = raw.get("fixture", {})
        league_info = raw.get("league", {})
        teams = raw.get("teams", {})
        goals = raw.get("goals", {})
        score = raw.get("score", {})

        fixture_id = fixture_info.get("id")
        if fixture_id is None:
            return None

        goals_home = goals.get("home")
        goals_away = goals.get("away")
        if goals_home is None or goals_away is None:
            return None

        goals_home = int(goals_home)
        goals_away = int(goals_away)

        if goals_home > goals_away:
            result = "H"
        elif goals_home < goals_away:
            result = "A"
        else:
            result = "D"

        return {
            "fixture_id": int(fixture_id),
            "league_id": league_id,
            "league": str(league_info.get("name", "")),
            "season": season,
            "date": str(fixture_info.get("date", "")),
            "home_team": str(teams.get("home", {}).get("name", "")),
            "away_team": str(teams.get("away", {}).get("name", "")),
            "goals_home": goals_home,
            "goals_away": goals_away,
            "result": result,
            # Odds placeholders — se rellenan externamente si disponibles
            "odds_home": None,
            "odds_draw": None,
            "odds_away": None,
            "odds_over25": None,
            "odds_under25": None,
            "odds_btts_yes": None,
            "odds_btts_no": None,
            "fetched_at": datetime.now(timezone.utc),
        }
    except Exception as e:
        logger.error("_parse_fixture: error: %s", e)
        return None


# ---------------------------------------------------------------------------
# simulate_bet
# ---------------------------------------------------------------------------

def simulate_bet(fixture: dict, signal: dict) -> dict:
    """
    Simula una apuesta con el resultado real del fixture.

    signal: {market_type, team_to_back, odds, edge, confidence, kelly_fraction}
    Devuelve: {won, pnl, virtual_stake, result}
    """
    kelly = float(signal.get("kelly_fraction") or 0.05)
    virtual_stake = min(25.0, max(0.50, kelly * 50.0))
    odds = float(signal.get("odds") or 2.0)

    actual_result = fixture.get("result", "")  # "H", "D", "A"
    team_to_back = str(signal.get("team_to_back") or "home").lower()
    market_type = str(signal.get("market_type") or "h2h").lower()

    # Determinar si la apuesta ganó
    won = False
    draw_void = False

    if market_type in ("h2h", "1x2"):
        if team_to_back in ("home", "h"):
            won = actual_result == "H"
        elif team_to_back in ("away", "a"):
            won = actual_result == "A"
        elif team_to_back in ("draw", "d", "x"):
            won = actual_result == "D"

    elif market_type == "totals":
        goals_home = int(fixture.get("goals_home") or 0)
        goals_away = int(fixture.get("goals_away") or 0)
        total_goals = goals_home + goals_away
        if team_to_back == "over":
            won = total_goals > 2.5
        elif team_to_back == "under":
            won = total_goals < 2.5
        else:
            draw_void = True

    elif market_type == "btts":
        goals_home = int(fixture.get("goals_home") or 0)
        goals_away = int(fixture.get("goals_away") or 0)
        btts = goals_home > 0 and goals_away > 0
        if team_to_back == "yes":
            won = btts
        elif team_to_back == "no":
            won = not btts
        else:
            draw_void = True

    if draw_void:
        return {
            "won": False,
            "pnl": 0.0,
            "virtual_stake": virtual_stake,
            "result": "draw_void",
        }

    if won:
        pnl = round((odds - 1) * virtual_stake, 4)
        result_str = "win"
    else:
        pnl = round(-virtual_stake, 4)
        result_str = "loss"

    return {
        "won": won,
        "pnl": pnl,
        "virtual_stake": virtual_stake,
        "result": result_str,
    }


# ---------------------------------------------------------------------------
# calculate_backtest_metrics
# ---------------------------------------------------------------------------

def calculate_backtest_metrics(bets: list[dict]) -> dict:
    """
    bets: lista de dicts con {won, pnl, virtual_stake, odds, edge}

    Calcula metricas de backtest y threshold recomendado.
    """
    if not bets:
        return {
            "n_bets": 0,
            "n_wins": 0,
            "win_rate": 0.0,
            "roi": 0.0,
            "avg_edge": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "avg_clv": 0.0,
            "threshold_recommended": _DEFAULT_SPORTS_MIN_EDGE,
        }

    n_bets = len(bets)
    n_wins = sum(1 for b in bets if b.get("won"))
    win_rate = round(n_wins / n_bets, 4)

    total_stake = sum(float(b.get("virtual_stake") or 0) for b in bets)
    total_pnl = sum(float(b.get("pnl") or 0) for b in bets)
    roi = round(total_pnl / total_stake, 4) if total_stake > 0 else 0.0

    all_edges = [float(b.get("edge") or 0) for b in bets]
    avg_edge = round(mean(all_edges), 4) if all_edges else 0.0

    # Profit factor
    positive_pnls = [float(b.get("pnl") or 0) for b in bets if (b.get("pnl") or 0) > 0]
    negative_pnls = [float(b.get("pnl") or 0) for b in bets if (b.get("pnl") or 0) < 0]
    sum_pos = sum(positive_pnls)
    sum_neg = abs(sum(negative_pnls))
    profit_factor = round(sum_pos / sum_neg, 4) if sum_neg > 0 else (float("inf") if sum_pos > 0 else 0.0)

    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for b in bets:
        cumulative += float(b.get("pnl") or 0)
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    max_drawdown = round(max_drawdown, 4)

    # Sharpe
    pnl_per_bet = [float(b.get("pnl") or 0) for b in bets]
    if len(pnl_per_bet) >= 3:
        try:
            sharpe = round(mean(pnl_per_bet) / stdev(pnl_per_bet), 4)
        except Exception:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Avg CLV (si disponible)
    clv_values = [float(b["clv"]) for b in bets if b.get("clv") is not None]
    avg_clv = round(mean(clv_values), 4) if clv_values else 0.0

    # Threshold recomendado basado en ROI
    roi_pct = roi * 100  # ROI en porcentaje
    if roi_pct < -5:
        threshold_recommended = round(_DEFAULT_SPORTS_MIN_EDGE * 1.2, 4)
    elif roi_pct > 10:
        threshold_recommended = round(_DEFAULT_SPORTS_MIN_EDGE * 0.9, 4)
    else:
        threshold_recommended = _DEFAULT_SPORTS_MIN_EDGE

    return {
        "n_bets": n_bets,
        "n_wins": n_wins,
        "win_rate": win_rate,
        "roi": roi,
        "avg_edge": avg_edge,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "avg_clv": avg_clv,
        "threshold_recommended": threshold_recommended,
    }


# ---------------------------------------------------------------------------
# run_backtest
# ---------------------------------------------------------------------------

async def run_backtest(
    league_id: int,
    league_name: str,
    market: str,
    seasons: list[int],
    api_key: str,
    base_threshold: float = 0.08,
) -> dict:
    """
    Ejecuta backtest completo para una liga y mercado.
    market: "h2h" | "totals" | "btts"

    1. Fetch fixtures historicos para cada temporada
    2. Para cada fixture, simular senal con logica simplificada
    3. Calcular metricas
    4. Guardar en col("backtest_results")
    5. Auto-calibracion de threshold en col("model_weights")/"backtest_thresholds"
    """
    global _DEFAULT_SPORTS_MIN_EDGE
    all_bets: list[dict] = []
    all_fixtures: list[dict] = []
    date_from = None
    date_to = None

    # ── FASE 1: Recolectar todos los fixtures de todas las temporadas ──────────
    for season in seasons:
        try:
            fixtures = await fetch_historical_fixtures(league_id, season, api_key, league_name)
            if not fixtures:
                logger.warning("run_backtest: sin fixtures para %s/%d", league_name, season)
                continue
            all_fixtures.extend(fixtures)
            for f in fixtures:
                fdate = f.get("date", "")[:10]
                if fdate:
                    if date_from is None or fdate < date_from:
                        date_from = fdate
                    if date_to is None or fdate > date_to:
                        date_to = fdate
        except Exception as e:
            logger.error("run_backtest: error en temporada %d para %s: %s", season, league_name, e)

    if not all_fixtures:
        logger.warning("run_backtest: 0 fixtures totales para %s", league_name)
        return {
            "backtest_id": str(uuid.uuid4()), "league": league_name, "market": market,
            "season": ",".join(str(s) for s in seasons), "date_from": "", "date_to": "",
            "n_bets": 0, "n_wins": 0, "win_rate": 0.0, "roi": 0.0, "avg_edge": 0.0,
            "profit_factor": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "avg_clv": 0.0,
            "threshold_recommended": _DEFAULT_SPORTS_MIN_EDGE,
            "created_at": datetime.now(timezone.utc),
        }

    # ── FASE 2: Tasas históricas de la liga (modelo para totals/btts) ─────────
    n_fix = len(all_fixtures)
    over_rate = sum(
        1 for f in all_fixtures
        if int(f.get("goals_home") or 0) + int(f.get("goals_away") or 0) > 2.5
    ) / n_fix
    btts_rate = sum(
        1 for f in all_fixtures
        if int(f.get("goals_home") or 0) > 0 and int(f.get("goals_away") or 0) > 0
    ) / n_fix
    logger.info(
        "run_backtest(%s/%s): %d fixtures — over_rate=%.2f btts_rate=%.2f",
        league_name, market, n_fix, over_rate, btts_rate,
    )

    # ── FASE 3: Cargar ELO en batch para h2h (una llamada por equipo) ─────────
    _elo_map: dict[int, float] = {}
    _elo_available = False
    if market == "h2h":
        try:
            from enrichers.elo_rating import (
                get_team_elo as _get_elo,
                expected_score as _elo_exp,
                HOME_ADVANTAGE as _ELO_HOME_ADV,
            )
            team_ids: set[int] = set()
            for f in all_fixtures:
                for key in ("home_team_id", "away_team_id"):
                    tid = f.get(key)
                    if tid is not None:
                        try:
                            team_ids.add(int(tid))
                        except (ValueError, TypeError):
                            pass
            for tid in team_ids:
                _elo_map[tid] = _get_elo(tid)
            _elo_available = bool(team_ids)
            logger.info("run_backtest(%s/h2h): %d ELOs cargados", league_name, len(_elo_map))
        except Exception as _ee:
            logger.warning("run_backtest: ELO no disponible — %s", _ee)

    # ── FASE 4: Simular apuestas con lógica real (ELO + tasas históricas) ─────
    for fixture in all_fixtures:
        try:
            signal: dict | None = None

            if market == "h2h":
                odds_home = float(fixture.get("odds_home") or 0)
                odds_away = float(fixture.get("odds_away") or 0)
                odds_draw = float(fixture.get("odds_draw") or 0)
                if odds_home <= 1.0: odds_home = 2.10
                if odds_away <= 1.0: odds_away = 3.50
                if odds_draw <= 1.0: odds_draw = 3.30

                # Probabilidad del modelo: ELO si disponible, implied ajustada si no
                h_id = fixture.get("home_team_id")
                a_id = fixture.get("away_team_id")
                if _elo_available and h_id and a_id:
                    try:
                        h_elo = _elo_map.get(int(h_id), 1500.0)
                        a_elo = _elo_map.get(int(a_id), 1500.0)
                        prob_home = _elo_exp(h_elo + _ELO_HOME_ADV, a_elo)
                        prob_away = _elo_exp(a_elo, h_elo + _ELO_HOME_ADV)
                        confidence = 0.60 + min(0.25, abs(prob_home - 0.50))
                    except Exception:
                        prob_home, prob_away, confidence = 0.45, 0.30, 0.60
                else:
                    # Implied probability: descontar ~8% de margen del bookmaker
                    raw_h = 1.0 / odds_home
                    raw_a = 1.0 / odds_away
                    raw_d = 1.0 / odds_draw
                    total_raw = raw_h + raw_a + raw_d
                    if total_raw > 0:
                        prob_home = raw_h / total_raw * 0.92
                        prob_away = raw_a / total_raw * 0.92
                    else:
                        prob_home, prob_away = 0.45, 0.30
                    confidence = 0.58

                ev_home = (prob_home * odds_home) - 1.0
                ev_away = (prob_away * odds_away) - 1.0

                if ev_home >= ev_away:
                    team_to_back = "home"
                    sel_ev = ev_home
                    sel_odds = odds_home
                else:
                    team_to_back = "away"
                    sel_ev = ev_away
                    sel_odds = odds_away

                if sel_ev <= base_threshold:
                    continue

                signal = {
                    "market_type": "h2h",
                    "team_to_back": team_to_back,
                    "odds": sel_odds,
                    "edge": sel_ev,
                    "confidence": min(0.90, max(0.55, confidence)),
                    "kelly_fraction": min(0.25, max(0.01, sel_ev / 2)),
                }

            elif market == "totals":
                odds_over  = float(fixture.get("odds_over25")  or 1.85)
                odds_under = float(fixture.get("odds_under25") or 2.05)
                if odds_over  <= 1.0: odds_over  = 1.85
                if odds_under <= 1.0: odds_under = 2.05

                ev_over  = (over_rate         * odds_over)  - 1.0
                ev_under = ((1.0 - over_rate) * odds_under) - 1.0

                if ev_over >= ev_under and ev_over > base_threshold:
                    signal = {
                        "market_type": "totals", "team_to_back": "over",
                        "odds": odds_over, "edge": ev_over, "confidence": 0.62,
                        "kelly_fraction": min(0.25, max(0.01, ev_over / 2)),
                    }
                elif ev_under > ev_over and ev_under > base_threshold:
                    signal = {
                        "market_type": "totals", "team_to_back": "under",
                        "odds": odds_under, "edge": ev_under, "confidence": 0.60,
                        "kelly_fraction": min(0.25, max(0.01, ev_under / 2)),
                    }

            elif market == "btts":
                odds_btts    = float(fixture.get("odds_btts_yes") or 1.75)
                odds_no_btts = float(fixture.get("odds_btts_no")  or 2.10)
                if odds_btts    <= 1.0: odds_btts    = 1.75
                if odds_no_btts <= 1.0: odds_no_btts = 2.10

                ev_yes = (btts_rate         * odds_btts)    - 1.0
                ev_no  = ((1.0 - btts_rate) * odds_no_btts) - 1.0

                if ev_yes >= ev_no and ev_yes > base_threshold:
                    signal = {
                        "market_type": "btts", "team_to_back": "yes",
                        "odds": odds_btts, "edge": ev_yes, "confidence": 0.60,
                        "kelly_fraction": min(0.25, max(0.01, ev_yes / 2)),
                    }
                elif ev_no > ev_yes and ev_no > base_threshold:
                    signal = {
                        "market_type": "btts", "team_to_back": "no",
                        "odds": odds_no_btts, "edge": ev_no, "confidence": 0.58,
                        "kelly_fraction": min(0.25, max(0.01, ev_no / 2)),
                    }

            if signal is None:
                continue

            bet_result = simulate_bet(fixture, signal)
            bet_result["edge"] = signal["edge"]
            bet_result["odds"] = signal["odds"]
            all_bets.append(bet_result)

        except Exception as e:
            logger.error("run_backtest: error simulando fixture %s: %s", fixture.get("fixture_id"), e)

    # Calcular metricas
    metrics = calculate_backtest_metrics(all_bets)

    # Construir resultado
    backtest_id = str(uuid.uuid4())
    result = {
        "backtest_id": backtest_id,
        "league": league_name,
        "market": market,
        "season": ",".join(str(s) for s in seasons),
        "date_from": date_from or "",
        "date_to": date_to or "",
        "n_bets": metrics["n_bets"],
        "n_wins": metrics["n_wins"],
        "win_rate": metrics["win_rate"],
        "roi": metrics["roi"],
        "avg_edge": metrics["avg_edge"],
        "profit_factor": metrics["profit_factor"],
        "max_drawdown": metrics["max_drawdown"],
        "sharpe": metrics["sharpe"],
        "avg_clv": metrics["avg_clv"],
        "threshold_recommended": metrics["threshold_recommended"],
        "created_at": datetime.now(timezone.utc),
    }

    # Guardar en Firestore
    try:
        col("backtest_results").add(result)
        logger.info(
            "run_backtest: guardado league=%s market=%s roi=%.1f%% n_bets=%d",
            league_name, market, metrics["roi"] * 100, metrics["n_bets"],
        )
    except Exception as e:
        logger.error("run_backtest: error guardando resultado: %s", e)

    # Auto-calibracion: guardar threshold recomendado
    try:
        doc_ref = col("model_weights").document("backtest_thresholds")
        existing = doc_ref.get()
        existing_data = existing.to_dict() if existing.exists else {}
        league_entry = existing_data.get(league_name, {})
        league_entry[market] = metrics["threshold_recommended"]
        existing_data[league_name] = league_entry
        doc_ref.set(existing_data)
    except Exception as e:
        logger.error("run_backtest: error en auto-calibracion: %s", e)

    return result


# ---------------------------------------------------------------------------
# run_full_backtest
# ---------------------------------------------------------------------------

async def run_full_backtest(api_key: str) -> dict:
    """
    Ejecuta backtest para todas las ligas y mercados configurados.

    Ligas: Premier League (39), La Liga (140), Bundesliga (78), Serie A (135), Ligue 1 (61)
    Temporadas: [2022, 2023, 2024]
    Mercados: ["h2h"]

    Rate limiting: 2s entre llamadas a la API.
    Maximo 50 llamadas total (quota protection).

    Devuelve {league: {market: metrics}}.
    """
    summary: dict = {}
    call_count = 0

    for league_name, league_id in _BACKTEST_LEAGUES.items():
        summary[league_name] = {}

        for market in _BACKTEST_MARKETS:
            if call_count >= _MAX_API_CALLS:
                logger.warning(
                    "run_full_backtest: limite de %d llamadas alcanzado, deteniendo",
                    _MAX_API_CALLS,
                )
                break

            try:
                logger.info(
                    "run_full_backtest: iniciando %s / %s", league_name, market
                )
                result = await run_backtest(
                    league_id=league_id,
                    league_name=league_name,
                    market=market,
                    seasons=_BACKTEST_SEASONS,
                    api_key=api_key,
                    base_threshold=_DEFAULT_SPORTS_MIN_EDGE,
                )
                summary[league_name][market] = {
                    "roi": result.get("roi"),
                    "win_rate": result.get("win_rate"),
                    "n_bets": result.get("n_bets"),
                    "threshold_recommended": result.get("threshold_recommended"),
                }
                call_count += len(_BACKTEST_SEASONS)
                # Rate limiting entre ligas
                await asyncio.sleep(2.0)

            except Exception as e:
                logger.error(
                    "run_full_backtest: error en %s/%s: %s", league_name, market, e
                )
                summary[league_name][market] = {"error": str(e)}

        if call_count >= _MAX_API_CALLS:
            break

    logger.info(
        "run_full_backtest: completado — %d ligas, %d llamadas API",
        len(summary), call_count,
    )
    return summary
