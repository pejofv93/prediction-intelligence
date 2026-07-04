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
    ODDS_API_KEY, ODDSAPIIO_KEY, SPORTS_ALERT_EDGE, SPORTS_DIVERGENCE_UNDERDOG_ODDS,
    SPORTS_MAX_DIVERGENCE, SPORTS_MIN_CONFIDENCE, SPORTS_MIN_EDGE, TENNIS_WEIGHTS,
)
from shared.firestore_client import col
from shared.api_quota_manager import quota
from shared import odds_cache

logger = logging.getLogger(__name__)

_THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
_HTTP_TIMEOUT = 15.0
_LEAGUE_ODDS_CACHE: dict[str, tuple[datetime, list]] = {}
_ODDSAPIIO_ODDS_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_TTL = timedelta(hours=24)
_ODDS_ONLY_MIN_EDGE = 0.03        # umbral relajado: sin stats de RapidAPI
_ODDS_ONLY_MIN_CONFIDENCE = 0.55  # confianza base odds-only
# w=1.0 → prob justa (margin-stripped) DIRECTA, sin regresión hacia 0.5.
# El antiguo 0.80 (= 0.80·fair + 0.20·0.5) fabricaba edge fantasma en underdogs:
# el término +0.10 empujaba la prob del underdog por encima de su precio implícito.
# Con w=1.0 la prob justa nunca bate al precio con margen → odds-only no inventa
# ventaja donde no la hay. (Histórico fútbol banda cuota>5: WR 4.2%, ROI -73.8%.)
_ODDS_REGRESSION_WEIGHT = 1.0
# Red de seguridad: descartar longshots extremos en odds-only (sin stats no hay
# edge real ahí, solo line-shopping espurio). Justificado por fútbol cuota>5 = -74% ROI.
_ODDS_ONLY_MAX_ODDS = 4.0

# Sport keys de The Odds API para tenis
# Claves específicas mejoran precisión; genéricas (ATP/WTA/ITF) actúan de fallback.
_TENNIS_SPORT_KEYS = {
    # Grand Slams
    "ATP_AUS_OPEN":    "tennis_atp_australian_open",
    "WTA_AUS_OPEN":    "tennis_wta_australian_open",
    "ATP_FRENCH_OPEN": "tennis_atp_french_open",
    "WTA_FRENCH_OPEN": "tennis_wta_french_open",
    "ATP_WIMBLEDON":   "tennis_atp_wimbledon",
    "WTA_WIMBLEDON":   "tennis_wta_wimbledon",
    "ATP_US_OPEN":     "tennis_atp_us_open",
    "WTA_US_OPEN":     "tennis_wta_us_open",
    # Masters 1000 / WTA 1000 (primavera — activos en abril-mayo)
    "ATP_BARCELONA":   "tennis_atp_barcelona_open",
    "ATP_MADRID":      "tennis_atp_madrid_open",
    "WTA_MADRID":      "tennis_wta_madrid_open",
    "ATP_MUNICH":      "tennis_atp_munich",
    "ATP_ROME":        "tennis_atp_rome",
    "WTA_ROME":        "tennis_wta_rome",
    "WTA_STUTTGART":   "tennis_wta_stuttgart_open",
    # Generic fallback — cubre cualquier torneo no mapeado explícitamente
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
    # FIX B: caché Firestore persistente — sobrevive cold starts (min-instances=0).
    fs = odds_cache.get_events("tennis", sport_key, _CACHE_TTL.total_seconds())
    if fs is not None:
        _LEAGUE_ODDS_CACHE[sport_key] = (now, fs)
        return fs
    # FIX C: gate de cuota. Si The Odds API está agotada, ir directo al fallback odds-api.io.
    if not quota.can_call_monthly("the_odds_api"):
        logger.warning("tennis_analyzer: The Odds API cuota agotada — fallback odds-api.io (%s)", sport_key)
        _LEAGUE_ODDS_CACHE[sport_key] = (now - _CACHE_TTL + timedelta(minutes=15), [])
        return await _fetch_tennis_odds_oddsapiio()
    url = f"{_THE_ODDS_API_BASE}/{sport_key}/odds"
    try:
        markets = "h2h,spreads,totals"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": markets,
                "oddsFormat": "decimal",
            })
        if resp.status_code == 200:
            events = resp.json()
            remaining = resp.headers.get("x-requests-remaining", "?")
            # FIX C: reportar consumo (header = fuente de verdad; cost = markets×regiones).
            quota.track_monthly("the_odds_api",
                                remaining=(remaining if remaining != "?" else None),
                                cost=len(markets.split(",")))
            logger.info("tennis_analyzer: The Odds API '%s' — %d eventos, %s req restantes",
                        sport_key, len(events), remaining)
            _LEAGUE_ODDS_CACHE[sport_key] = (now, events)
            odds_cache.set_events("tennis", sport_key, events)
            return events
        if resp.status_code == 429:
            # Cuota real agotada → marcar remaining=0 para que el gate corte el resto.
            quota.track_monthly("the_odds_api", remaining=0)
            logger.warning("tennis_analyzer: The Odds API 429 (cuota agotada) %s", sport_key)
            _LEAGUE_ODDS_CACHE[sport_key] = (now - _CACHE_TTL + timedelta(minutes=15), [])
            return await _fetch_tennis_odds_oddsapiio()
        logger.warning("tennis_analyzer: The Odds API %s → HTTP %d", sport_key, resp.status_code)
    except Exception:
        logger.error("tennis_analyzer: error fetching odds %s", sport_key, exc_info=True)
    # Cachear fallo 15 min — impide storm de requests por cada partido con mismo sport_key
    _LEAGUE_ODDS_CACHE[sport_key] = (now - _CACHE_TTL + timedelta(minutes=15), [])
    logger.info("tennis_analyzer: %s falla → fallback odds-api.io", sport_key)
    return await _fetch_tennis_odds_oddsapiio()


async def _fetch_tennis_odds_oddsapiio() -> list:
    """
    Fetch h2h odds para tenis desde odds-api.io.
    Fallback cuando The Odds API devuelve 404. Caché 2h.
    """
    cache_key = "oddsapiio_tennis_odds"
    now = datetime.now(timezone.utc)
    cached = _ODDSAPIIO_ODDS_CACHE.get(cache_key)
    if cached and (now - cached[0]) < timedelta(hours=2):
        return cached[1]
    if not ODDSAPIIO_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.odds-api.io/v3/odds",
                params={"sport": "tennis", "markets": "h2h", "apiKey": ODDSAPIIO_KEY},
            )
        if resp.status_code == 200:
            data = resp.json()
            events = data if isinstance(data, list) else data.get("data", data.get("events", []))
            if isinstance(events, list):
                _ODDSAPIIO_ODDS_CACHE[cache_key] = (now, events)
                logger.info("tennis_analyzer oddsapiio: %d eventos con odds h2h", len(events))
                return events
        # Cachear 429/error como lista vacía 15 min — evita storm de requests por partido
        logger.debug("tennis_analyzer oddsapiio odds: HTTP %d — cacheando vacío 15min", resp.status_code)
        _ODDSAPIIO_ODDS_CACHE[cache_key] = (now - timedelta(hours=2) + timedelta(minutes=15), [])
    except Exception:
        logger.debug("tennis_analyzer oddsapiio odds: error de red", exc_info=True)
        _ODDSAPIIO_ODDS_CACHE[cache_key] = (now - timedelta(hours=2) + timedelta(minutes=15), [])
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


def _get_best_h2h_odds(event: dict, p1: str) -> dict | None:
    """
    Mejores cuotas h2h para p1 a través de todos los bookmakers.
    Devuelve best (para calcular edge) y avg (para fair-value).
    """
    home_is_p1 = _normalize(p1)[:6] in _normalize(event.get("home_team", ""))[:6]
    home_team = event.get("home_team", "")
    best_p1 = best_p2 = 0.0
    best_bk = "unknown"
    all_p1: list[float] = []
    all_p2: list[float] = []

    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            p1o = p2o = None
            for o in mkt.get("outcomes", []):
                nm = o.get("name", "")
                pr = float(o.get("price", 0))
                is_home = _normalize(nm)[:5] == _normalize(home_team)[:5]
                if home_is_p1:
                    if is_home:
                        p1o = pr
                    else:
                        p2o = pr
                else:
                    if is_home:
                        p2o = pr
                    else:
                        p1o = pr
            if p1o and p1o > 1.0 and p2o and p2o > 1.0:
                all_p1.append(p1o)
                all_p2.append(p2o)
                if p1o > best_p1:
                    best_p1, best_bk = p1o, bk.get("key", "unknown")
                if p2o > best_p2:
                    best_p2 = p2o

    if not all_p1 or not all_p2:
        return None
    return {
        "p1_odds":      round(best_p1, 3),
        "p2_odds":      round(best_p2, 3),
        "avg_p1_odds":  round(sum(all_p1) / len(all_p1), 3),
        "avg_p2_odds":  round(sum(all_p2) / len(all_p2), 3),
        "bookmaker":    best_bk,
        "bookmaker_count": len(all_p1),
    }


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


def _prob_from_market_odds(avg_p1_odds: float, avg_p2_odds: float) -> tuple[float, float, dict]:
    """
    Probabilidad justa (margin-stripped) DIRECTA, sin regresión hacia 0.5.
    El antiguo término (1-w)·0.5 inflaba sistemáticamente a los underdogs y
    fabricaba un edge fantasma: la prob justa sin margen NUNCA debe superar al
    precio CON margen, así que cualquier "ventaja" extraída solo de las cuotas
    era artificial. Validado con el histórico de fútbol (banda cuota>5: WR 4.2%,
    ROI -73.8%) y las 9 señales de tenis vivas (todas underdogs, edge fantasma).
    Con _ODDS_REGRESSION_WEIGHT=1.0 → model_p1 = fair_p1.
    Devuelve (model_prob_p1, confidence, signals_dict).
    """
    if avg_p1_odds <= 1.0 or avg_p2_odds <= 1.0:
        return 0.5, 0.0, {}
    impl1 = 1.0 / avg_p1_odds
    impl2 = 1.0 / avg_p2_odds
    overround = impl1 + impl2
    fair_p1 = impl1 / overround
    model_p1 = round(_ODDS_REGRESSION_WEIGHT * fair_p1 + (1.0 - _ODDS_REGRESSION_WEIGHT) * 0.5, 4)
    conf = round(min(0.65, 0.40 + abs(fair_p1 - 0.5) * 0.50), 4)
    return model_p1, conf, {
        "odds_fair_p1":  round(fair_p1, 4),
        "odds_model_p1": model_p1,
        "overround":     round(overround - 1.0, 3),
    }


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
    # Guard de divergencia modelo-mercado DIRECCIONAL — SOLO h2h (espejo del de fútbol).
    # Solo bloquea si el lado es UNDERDOG (cuota >= frontera); favoritos claros exentos.
    # NO aplicar a set_handicap/total_sets/total_games/game_handicap: allí la divergencia
    # mide otra cosa (cobertura, over/under) y sobre-filtraría edges legítimos.
    if market == "h2h" and odds > 1.0:
        _divergence = prob - 1.0 / odds
        if odds >= SPORTS_DIVERGENCE_UNDERDOG_ODDS and _divergence > SPORTS_MAX_DIVERGENCE:
            logger.info(
                "tennis_analyzer: SKIP_HIGH_DIVERGENCE [underdog] %s @ %.2f | calculated_prob=%.3f "
                "implícita=%.3f divergencia=%.3f > %.2f — omitida (edge inflado de underdog; "
                "favoritos <%.2f exentos)",
                selection, odds, prob, 1.0 / odds, _divergence, SPORTS_MAX_DIVERGENCE,
                SPORTS_DIVERGENCE_UNDERDOG_ODDS,
            )
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
    Usa WriteBatch: todas las writes del partido en 1 sola RPC.
    """
    from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload
    from shared.firestore_client import get_client as _get_fs_client

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
        # ── ODDS-ONLY PATH ────────────────────────────────────────────────────
        # RapidAPI sin stats → usar cuotas de mercado como proxy de probabilidad.
        # Modelo: 80% fair-prob (margin-stripped) + 20% media (corrige sesgo favorito).
        # Edge positivo aparece cuando el favorito cotiza < ~1.35 y el underdog > ~3.50.
        logger.info(
            "tennis_analyzer(%s): sin stats → odds-only (%s vs %s)",
            match_id, p1_name, p2_name,
        )
        # Prioridad 1: odds embebidas por el collector (0 req HTTP extra)
        _h_emb = float(match.get("home_odds", 0))
        _a_emb = float(match.get("away_odds", 0))
        if _h_emb > 1.0 and _a_emb > 1.0:
            _bh = {
                "p1_odds": _h_emb, "p2_odds": _a_emb,
                "avg_p1_odds": _h_emb, "avg_p2_odds": _a_emb,
                "bookmaker": match.get("odds_bookmaker", "oddsapiio"),
                "bookmaker_count": 1,
            }
            _ev = None  # sin evento externo — solo h2h posible
        else:
            # Prioridad 2: fetch HTTP (puede estar rate-limited)
            _sport_key = _TENNIS_SPORT_KEYS.get(league, _TENNIS_SPORT_KEYS.get("ATP", "tennis_atp"))
            _ev_list = await _fetch_tennis_odds(_sport_key, match_id)
            _ev = _find_event(_ev_list, p1_name, p2_name)
            if not _ev:
                logger.debug("tennis_analyzer(%s): odds-only sin evento — sin señal", match_id)
                return []
            _bh = _get_best_h2h_odds(_ev, p1_name)
            if not _bh:
                return []
        _prob1, _conf, _sigs = _prob_from_market_odds(_bh["avg_p1_odds"], _bh["avg_p2_odds"])
        _sigs["bookmaker_count"] = _bh["bookmaker_count"]
        _base_oo = {
            "match_id": match_id, "home_team": p1_name, "away_team": p2_name,
            "sport": "tennis", "league": league,
            "elo_sufficient": False, "h2h_sufficient": False,
        }
        _preds_oo: list[dict] = []
        _batch_oo = _get_fs_client().batch()
        from analyzers.value_bet_engine import kelly_criterion as _kc
        for _sel, _prob, _best_odds, _did in [
            (p1_name,  _prob1,          _bh["p1_odds"], f"{match_id}_h2h_p1_oo"),
            (p2_name, 1.0 - _prob1,     _bh["p2_odds"], f"{match_id}_h2h_p2_oo"),
        ]:
            # Red de seguridad longshot: sin stats, los extremos no tienen edge real
            # (solo line-shopping espurio). Fútbol cuota>5: WR 4.2%, ROI -73.8%.
            if _best_odds > _ODDS_ONLY_MAX_ODDS:
                logger.info(
                    "tennis_analyzer(%s): SKIP_LONGSHOT_CAP %s @ %.2f > %.1f — descartada "
                    "(odds-only no apuesta longshots extremos)",
                    match_id, _sel, _best_odds, _ODDS_ONLY_MAX_ODDS,
                )
                continue
            _edge = round(_prob - 1.0 / _best_odds, 4)
            if _edge < _ODDS_ONLY_MIN_EDGE or _conf < _ODDS_ONLY_MIN_CONFIDENCE:
                continue
            # Guard de divergencia DIRECCIONAL (h2h) — espejo del de fútbol. Solo bloquea
            # underdogs (cuota >= frontera); favoritos claros exentos. Backstop residual:
            # con w=1.0 ya no se fabrica edge fantasma, pero mantenemos el cap por si el
            # line-shopping (best vs avg) genera un gap espurio en algún caso.
            _div_oo = _prob - 1.0 / _best_odds if _best_odds > 1.0 else 1.0
            if _best_odds >= SPORTS_DIVERGENCE_UNDERDOG_ODDS and _div_oo > SPORTS_MAX_DIVERGENCE:
                logger.info(
                    "tennis_analyzer(%s): SKIP_HIGH_DIVERGENCE [underdog] %s @ %.2f | calculated_prob=%.3f "
                    "implícita=%.3f divergencia=%.3f > %.2f — omitida (regresión 80/20 infla underdog; "
                    "favoritos <%.2f exentos)",
                    match_id, _sel, _best_odds, _prob, 1.0 / _best_odds, _div_oo, SPORTS_MAX_DIVERGENCE,
                    SPORTS_DIVERGENCE_UNDERDOG_ODDS,
                )
                continue
            # Display: el modelo razona sobre p1, pero la alerta debe mostrar la prob del
            # LADO realmente apostado (_prob ya es prob1 ó 1-prob1). Copia por-señal para
            # no contaminar la otra predicción (compartían _sigs por referencia) ni mostrar
            # odds_*_p1 (siempre del local) cuando se apuesta al p2 → lectura cruzada.
            _sigs_side = {k: v for k, v in _sigs.items()
                          if k not in ("odds_fair_p1", "odds_model_p1")}
            _sigs_side["modelo_prob"] = round(_prob, 4)
            _pred_oo = {
                **_base_oo,
                "market_type": "h2h",
                "selection":   _sel,
                "bookmaker":   _bh["bookmaker"],
                "odds":        round(_best_odds, 3),
                "calculated_prob": round(_prob, 4),
                "edge":        _edge,
                "confidence":  round(_conf, 4),
                "kelly_fraction": _kc(_edge, _best_odds),
                "signals": _sigs_side, "factors": _sigs_side,
                "data_source": "odds_only",
                "match_date":  match_date,
                "weights_version": weights_version,
                "created_at":  datetime.now(timezone.utc),
                "result": None, "correct": None, "error_type": None,
            }
            await _save_and_alert(_pred_oo, _did, _base_oo, _ev, batch=_batch_oo)
            _preds_oo.append(_pred_oo)
        try:
            _batch_oo.commit()
        except Exception:
            logger.error("tennis_analyzer(%s): error batch commit odds-only", match_id, exc_info=True)
        if _preds_oo:
            logger.info(
                "tennis_analyzer(%s): %d señales odds-only — %s vs %s (sin stats RapidAPI)",
                match_id, len(_preds_oo), p1_name, p2_name,
            )
        return _preds_oo

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
    _fs_batch = _get_fs_client().batch()  # WriteBatch: todas las writes en 1 RPC

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
                await _save_and_alert(pred, doc_id, base, event, batch=_fs_batch)
                predictions.append(pred)

            # p2 victoria
            pred2 = _make_pred(base, "h2h", p2_name, h2h_odds["p2_odds"],
                               1.0 - prob1, sigs, conf, match_date, weights_version,
                               h2h_odds["bookmaker"])
            if pred2:
                doc_id = f"{match_id}_h2h_p2"
                pred2["match_id"] = doc_id
                await _save_and_alert(pred2, doc_id, base, event, batch=_fs_batch)
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
                await _save_and_alert(pred, doc_id, base, event, batch=_fs_batch)
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
                    await _save_and_alert(pred, doc_id, base, event)
                    predictions.append(pred)

    # ── TOTAL GAMES O/U ───────────────────────────────────────────────────────
    # Modelo: games_per_set × expected_sets. Bo3: ~22-24 games; Bo5: ~35-38.
    # Usamos best-of-3 (ATP) a menos que sea Grand Slam (bo5).
    is_bo5   = match.get("best_of", 3) == 5
    avg_sets = 2.0 * prob1 ** 2 + 2.0 * (1 - prob1) ** 2 + 3.0 * 2 * prob1 * (1 - prob1)
    if is_bo5:
        avg_sets = sum(k * _bo5_set_prob(k, prob1) for k in range(3, 6))
    # Games por set: baseline 9.6 = media real games/set ATP (set típico 6-4/6-3);
    # dominante gana 6-2 (~8 games), contested gana 7-6 (~13 games). La pendiente
    # −|prob1−0.5|·6 acorta el set cuanto más desigual es el partido.
    # HIGIENE (2026-06-26): el baseline anterior (11.0) sobreestimaba ~+1.3 games/set
    # → +3-4 games totales → edge OVER fantasma vs la línea de mercado. No busca
    # rentabilidad (heurística 2 params no bate Pinnacle, n=1 sin muestra): solo deja
    # de fabricar edge falso → los totales se auto-suprimen bajo el umbral. Ver memoria
    # project_tennis_totals_bias. Mejora futura (si hay volumen + grader de totales):
    # baseline por superficie/tour (arcilla/hierba/WTA) y anclar exp_total_games a la línea.
    games_per_set = 9.6 - abs(prob1 - 0.5) * 6.0
    exp_total_games = round(avg_sets * games_per_set, 1)

    # Líneas más comunes: 20.5 y 22.5 para Bo3
    for games_line in (20.5, 22.5):
        tg_odds = _get_totals_odds(event, games_line) if event else None
        if not tg_odds:
            continue
        from scipy.stats import norm as _norm
        sigma_games = 4.5  # desviación típica del total de games
        p_over = float(1.0 - _norm.cdf(games_line, loc=exp_total_games, scale=sigma_games))
        for sel, prob_t, ok in [
            (f"Over {games_line} games",  round(p_over, 4), tg_odds["over_odds"]),
            (f"Under {games_line} games", round(1.0 - p_over, 4), tg_odds["under_odds"]),
        ]:
            pred = _make_pred(
                base, "tennis_total_games", sel, ok, prob_t,
                {**sigs, "exp_total_games": exp_total_games, "games_line": games_line},
                conf * 0.80, match_date, weights_version, tg_odds["bookmaker"],
            )
            if pred:
                tag = ("over" if "Over" in sel else "under") + str(games_line).replace(".", "_")
                doc_id = f"{match_id}_tg_{tag}"
                pred["match_id"] = doc_id
                await _save_and_alert(pred, doc_id, base, event, batch=_fs_batch)
                predictions.append(pred)

    # ── GAME HANDICAP ─────────────────────────────────────────────────────────
    # Líneas comunes: -3.5 / +3.5
    for gh_line in (-3.5, 3.5):
        gh_odds = _get_totals_odds(event, abs(gh_line)) if event else None  # proxy con totals
        # Intentar spreads directamente
        spreads_raw = _get_spreads_odds(event) if event else None
        if spreads_raw:
            try:
                gh_line_actual = float(spreads_raw.get("line", gh_line))
                if abs(abs(gh_line_actual) - 3.5) > 0.6:
                    continue
                gh_odds_price = spreads_raw.get("odds", 0)
            except (TypeError, ValueError):
                continue
        elif not gh_odds:
            continue
        else:
            gh_odds_price = gh_odds.get("over_odds", 0)  # proxy

        if gh_odds_price <= 1:
            continue

        from scipy.stats import norm as _norm
        # Diferencia esperada de games: (prob1 - 0.5) × games_per_set × avg_sets
        exp_diff = (prob1 - 0.5) * games_per_set * avg_sets * 0.6
        sigma_diff = 4.0
        if gh_line < 0:  # favorito da games
            p_cover = float(1.0 - _norm.cdf(abs(gh_line), loc=exp_diff, scale=sigma_diff))
        else:  # underdog recibe games
            p_cover = float(1.0 - _norm.cdf(gh_line, loc=-exp_diff, scale=sigma_diff))
        p_cover = round(max(0.0, min(1.0, p_cover)), 4)

        sel = f"{p1_name} {gh_line:+.1f} games"
        pred = _make_pred(
            base, "tennis_game_handicap", sel, gh_odds_price, p_cover,
            {**sigs, "exp_game_diff": round(exp_diff, 2), "games_line": gh_line},
            conf * 0.75, match_date, weights_version,
            spreads_raw.get("bookmaker", "bet365") if spreads_raw else "bet365",
        )
        if pred:
            doc_id = f"{match_id}_gh_{str(gh_line).replace('-','m').replace('.','_')}"
            pred["match_id"] = doc_id
            await _save_and_alert(pred, doc_id, base, event)
            predictions.append(pred)

    try:
        _fs_batch.commit()
    except Exception:
        logger.error("tennis_analyzer(%s): error en batch commit", match_id, exc_info=True)

    if predictions:
        logger.info("tennis_analyzer(%s): %d señales — %s vs %s",
                    match_id, len(predictions), p1_name, p2_name)
    return predictions


def _bo5_set_prob(k: int, p: float) -> float:
    """P(partido va a k sets en best-of-5). k in [3,4,5]."""
    from math import comb
    q = 1.0 - p
    if k == 3:
        return p ** 3 + q ** 3
    elif k == 4:
        return comb(3, 2) * (p ** 2 * q * p + q ** 2 * p * q)
    elif k == 5:
        return 1.0 - _bo5_set_prob(3, p) - _bo5_set_prob(4, p)
    return 0.0


async def _save_and_alert(pred: dict, doc_id: str, base: dict, enriched: dict,
                          batch=None) -> None:
    """batch: Firestore WriteBatch opcional. Si se pasa, acumula el write sin hacerlo."""
    from analyzers.value_bet_engine import _send_telegram_alert, _build_alert_payload
    try:
        if batch is not None:
            batch.set(col("predictions").document(doc_id), pred)
        else:
            col("predictions").document(doc_id).set(pred)
    except Exception:
        logger.error("tennis_analyzer: error guardando %s", doc_id, exc_info=True)
    if pred["edge"] > SPORTS_ALERT_EDGE:
        try:
            await _send_telegram_alert(_build_alert_payload(pred, enriched))
        except Exception:
            logger.error("tennis_analyzer: error enviando alerta %s", doc_id, exc_info=True)
