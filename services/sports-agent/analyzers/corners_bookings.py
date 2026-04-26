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

# Mapeo liga interna → tournamentId OddsPapi (verificado)
_TOURNAMENT_IDS: dict[str, int] = {
    "PD":  8,    # La Liga
    "PL":  1,    # Premier League
    "BL1": 4,    # Bundesliga
    "SA":  5,    # Serie A
    "FL1": 2,    # Ligue 1
    "DED": 10,   # Eredivisie
    "PPL": 13,   # Primeira Liga
    "SD":  9,    # Segunda División
    "BL2": 78,   # Bundesliga 2
    "SB":  11,   # Serie B
    "CL":  7,    # Champions League
    "EL":  6,    # Europa League
}

# Cache de fixtures v4 (TTL 1h, clave = fecha)
_FIXTURES_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(hours=1)


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

    if not quota.can_call("oddspapi"):
        logger.warning("corners_bookings: oddspapi cuota agotada, saltando fetch")
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
            return []
        if resp.status_code != 200:
            logger.warning("corners_bookings: OddsPapi HTTP %d", resp.status_code)
            return []

        quota.track_call("oddspapi")
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
    Punto de entrada principal. Devuelve lista de señales para corners y tarjetas.

    Si fixture_data es None, lo busca en OddsPapi para la fecha del partido.
    """
    if match_date is None:
        match_date = date.today()

    signals = []

    # Obtener fixture
    if fixture_data is None:
        fixtures = await _fetch_fixtures_for_date(match_date)
        fixture_data = _find_fixture(fixtures, home_team, away_team)

    if not fixture_data:
        logger.debug("corners_bookings: fixture no encontrado para %s vs %s", home_team, away_team)
        return []

    # Cargar stats FDCO
    home_stats, away_stats = await _load_team_stats(league, home_team, away_team)
    has_fdco = bool(home_stats and away_stats)

    for market_id, (market_key, outcome_map) in MARKET_DEFS.items():
        odds_list = _extract_market_odds(fixture_data, market_id, outcome_map)
        if len(odds_list) < _MIN_BOOKMAKERS:
            continue

        consensus = _consensus(odds_list)
        best = _best_odds(odds_list)

        # Poisson estimate si hay stats FDCO
        poisson_est: dict[str, float] = {}
        if has_fdco:
            if "corners" in market_key:
                lh = home_stats.get("home_corners", 5.0)
                la = away_stats.get("away_corners", 4.0)
            else:  # bookings
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
                # Edge = diferencia entre Poisson y precio del mercado
                poisson_p = poisson_est.get(sel, 0.0)
                edge = round(poisson_p - implied, 4)
                # Confianza: estabilidad entre Poisson y consensus
                diff_pc = abs(poisson_p - consensus_p)
                confidence = round(max(0.0, 1.0 - diff_pc * 3), 4)
                poisson_prob = poisson_p
            else:
                # Solo line shopping: best price vs consensus implied
                edge = round((1.0 / consensus_p) - best_price, 4) if consensus_p > 0 else 0.0
                # Confianza proporcional al número de bookmakers
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
