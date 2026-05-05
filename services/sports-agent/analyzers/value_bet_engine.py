"""
Motor de value bets — recibe enriched_match → genera senal si hay edge suficiente.
Thresholds: edge > SPORTS_MIN_EDGE (0.08) + confianza > SPORTS_MIN_CONFIDENCE (0.65).
Si edge > SPORTS_ALERT_EDGE (0.10) → POST al telegram-bot /send-alert.

Flujo por llamada a generate_signal():
  load_weights → ensemble_probability (home y away) → fetch_bookmaker_odds
  → calculate_edge → si supera threshold → kelly_criterion → guarda predictions
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

from shared.config import (
    CLOUD_RUN_TOKEN,
    DEFAULT_WEIGHTS,
    LEAGUE_MIN_EDGE,
    ODDS_API_KEY,
    SPORTS_ALERT_EDGE,
    SPORTS_MIN_CONFIDENCE,
    SPORTS_MIN_EDGE,
    TAVILY_API_KEY,
    TELEGRAM_BOT_URL,
)
from google.cloud.firestore_v1.base_query import FieldFilter
from shared.firestore_client import col
from shared.api_quota_manager import quota
from enrichers.elo_rating import DEFAULT_ELO

logger = logging.getLogger(__name__)

# The Odds API — fuente primaria de cuotas
_THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

# The Odds API — sport key map (league field in Firestore → The Odds API sport key)
_ODDS_SPORT_MAP: dict[str, str] = {
    # ── Fútbol masculino Europa (football-data.org) ────────────────────────────
    "PL":  "soccer_england_premier_league",
    "PD":  "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "SA":  "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
    "CL":  "soccer_uefa_champs_league",
    "EL":  "soccer_uefa_europa_league",
    "ECL": "soccer_uefa_europa_conference_league",
    "TU1": "soccer_turkey_super_league",

    # ── Fútbol masculino internacional (selecciones) ───────────────────────────
    # Competiciones de clubes por confederación
    "CSUD": "soccer_conmebol_sudamericana",
    # Competiciones de selecciones — Europa
    "NL":  "soccer_uefa_nations_league",
    "WCQ": "soccer_fifa_world_cup_qualification_europe",
    "EC":  "soccer_uefa_european_championship",          # Euro 2024/2028 (solo activo durante torneo)
    # Competiciones de selecciones — Sudamérica (AllSportsAPI colector activo)
    "CAM":  "soccer_conmebol_copa_america",              # solo activo durante torneo
    # Competiciones de selecciones — otras confederaciones (sin colector aún)
    "WCQ_CONMEBOL":  "soccer_conmebol_world_cup_qualification",
    "WCQ_CONCACAF":  "soccer_concacaf_world_cup_qualification",  # ⚠️ verify key
    "WCQ_AFC":       "soccer_afc_asian_cup_qualification",       # ⚠️ verify key
    "WCQ_CAF":       "soccer_africa_cup_of_nations_qualification", # ⚠️ verify key
    "INTL":          "soccer_international",                     # friendlies internacionales
    "WC":            "soccer_fifa_world_cup",                    # solo activo durante torneo

    # ── Fútbol masculino Sudamérica / ligas domésticas ────────────────────────
    "ARG": "soccer_argentina_primera_division",

    # ── Fútbol femenino (sin colector activo — listo para cuando se implemente) ─
    # Torneos internacionales
    "W_WWC":     "soccer_fifa_womens_world_cup",
    "W_WEURO":   "soccer_uefa_womens_euro",                      # ⚠️ verify key
    "W_WNATIONS":"soccer_uefa_womens_nations_league",            # ⚠️ verify key
    "W_WCL":     "soccer_uefa_womens_champions_league",         # ⚠️ verify key
    # Ligas domésticas femeninas
    "W_WSL":     "soccer_england_womens_super_league",
    "W_NWSL":    "soccer_usa_nwsl",
    "W_LIGA_F":  "soccer_spain_primera_division_w",              # ⚠️ verify key (Liga F)
    "W_D1F":     "soccer_france_d1_feminine",                   # ⚠️ verify key
    "W_FRAUEN_BL":"soccer_germany_frauen_bundesliga",            # ⚠️ verify key

    # ── Baloncesto ─────────────────────────────────────────────────────────────
    # Con colector activo (basketball_collector.py)
    "NBA":        "basketball_nba",
    "EUROLEAGUE": "basketball_euroleague",
    # Sin colector activo (preparado para cuando se implemente)
    "ACB":        "basketball_spain_acb",                        # ⚠️ verify key
    "NCAA_BB":    "basketball_ncaab",
    "FIBA_WC":    "basketball_fiba_world_cup",                   # ⚠️ verify key; solo torneo
    "EUROBASKET": "basketball_eurobasket",                       # ⚠️ verify key; solo torneo

    # ── Tenis (prefetch; señal real vía _TENNIS_SPORT_KEYS en tennis_analyzer) ──
    "ATP_AUS_OPEN":    "tennis_atp_australian_open",
    "WTA_AUS_OPEN":    "tennis_wta_australian_open",
    "ATP_FRENCH_OPEN": "tennis_atp_french_open",
    "WTA_FRENCH_OPEN": "tennis_wta_french_open",
    "ATP_WIMBLEDON":   "tennis_atp_wimbledon",
    "WTA_WIMBLEDON":   "tennis_wta_wimbledon",
    "ATP_US_OPEN":     "tennis_atp_us_open",
    "WTA_US_OPEN":     "tennis_wta_us_open",
    "ATP_BARCELONA":   "tennis_atp_barcelona_open",
    "ATP_MADRID":      "tennis_atp_madrid_open",
    "WTA_MADRID":      "tennis_wta_madrid_open",
    "ATP_MUNICH":      "tennis_atp_munich",
    "ATP_ROME":        "tennis_atp_rome",
    "WTA_ROME":        "tennis_wta_rome",
    "WTA_STUTTGART":   "tennis_wta_stuttgart_open",
}

# Football sport keys where Poisson totals model is applicable
_FOOTBALL_SPORT_KEYS: frozenset[str] = frozenset({
    # ── Ligas domésticas masculinas (modelo Poisson+ELO completo) ─────────────
    # Europa
    "soccer_england_premier_league",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",            "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_portugal_primeira_liga",        "soccer_netherlands_eredivisie",
    "soccer_turkey_super_league",
    # Sudamérica — ligas domésticas con fixture regular
    "soccer_brazil_campeonato",
    "soccer_argentina_primera_division",
    # Copas sudamericanas: fase de grupos tiene 6 partidos fijos → Poisson aplicable
    "soccer_conmebol_libertadores",
    "soccer_conmebol_sudamericana",

    # ── Ligas domésticas femeninas (Poisson aplicable con datos suficientes) ───
    # Nota: actualmente sin colector — se activarán cuando se implemente
    "soccer_england_womens_super_league",   # W_WSL
    "soccer_usa_nwsl",                      # W_NWSL
    "soccer_germany_frauen_bundesliga",     # W_FRAUEN_BL ⚠️ verify key
    "soccer_france_d1_feminine",            # W_D1F ⚠️ verify key
    "soccer_spain_primera_division_w",      # W_LIGA_F ⚠️ verify key

    # EXCLUIDOS intencionalmente (Poisson no fiable — selecciones nacionales,
    # rotación de plantillas, partidos únicos sin historial de equipo estable):
    # soccer_uefa_nations_league, soccer_fifa_world_cup_qualification_europe,
    # soccer_conmebol_world_cup_qualification, soccer_concacaf_world_cup_qualification,
    # soccer_conmebol_copa_america, soccer_uefa_european_championship,
    # soccer_fifa_world_cup, soccer_international,
    # soccer_uefa_womens_euro, soccer_fifa_womens_world_cup,
    # soccer_uefa_womens_champions_league, soccer_uefa_womens_nations_league
})

_FOOTBALL_TOTALS_LINE: float = 2.5

# Ligas que reciben markets=h2h,h2h_h1 en The Odds API (top ligas con máxima cobertura)
_TOP_LEAGUES_H2H_H1: frozenset[str] = frozenset({
    "soccer_uefa_champs_league",
    "soccer_england_premier_league",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
})

# Cache en memoria de odds por liga: {sport_key: (fetched_at, [events])}
# TTL 24h: con guard de upcoming_matches se llama solo cuando hay partidos.
# Además se persiste en Firestore (league_odds_cache) para sobrevivir reinicios Cloud Run.
_LEAGUE_ODDS_CACHE: dict[str, tuple[datetime, list]] = {}
_LEAGUE_CACHE_TTL = timedelta(hours=24)

# Cache de odds-api.io en memoria: {league_code: (fetched_at, [events_normalised])}
_ODDSAPIIO_CACHE: dict[str, tuple[datetime, list]] = {}
_ODDSAPIIO_CACHE_TTL = timedelta(hours=24)

# Cache de optic-odds en memoria: {league_code: (fetched_at, [events_normalised])}
_OPTICODDS_CACHE: dict[str, tuple[datetime, list]] = {}
_OPTICODDS_CACHE_TTL = timedelta(hours=24)

# Mutex para serializar los track_call concurrentes del pre-fetch (evita race condition
# donde N coroutines leen can_call=True y hacen N llamadas antes de que se actualice used)
_THE_ODDS_API_LOCK = asyncio.Lock()

# Flag de quota agotada — True solo cuando la API devuelve 422 o Firestore confirma agotada.
# NO bloquea hits de cache de Firestore (solo bloquea nuevas llamadas HTTP).
_THE_ODDS_API_EXHAUSTED: bool = False

# Timeout para llamadas HTTP a API externa
_HTTP_TIMEOUT = 15.0


def _has_upcoming_matches_for_league(league_code: str, within_hours: int) -> bool:
    """
    True si hay partidos SCHEDULED/TIMED para esta liga en las próximas `within_hours` horas.
    Fail-open: si Firestore falla devuelve True para no bloquear la llamada.
    """
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=within_hours)
        docs = col("upcoming_matches").where(
            filter=FieldFilter("league", "==", league_code)
        ).where(
            filter=FieldFilter("status", "in", ["SCHEDULED", "TIMED"])
        ).stream()
        for doc in docs:
            match_date_str = doc.to_dict().get("match_date", "")
            if not match_date_str:
                continue
            try:
                if "T" in str(match_date_str):
                    md = datetime.fromisoformat(str(match_date_str).replace("Z", "+00:00"))
                else:
                    md = datetime.strptime(str(match_date_str), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if now <= md <= cutoff:
                    return True
            except (ValueError, TypeError):
                continue
        return False
    except Exception:
        logger.warning("_has_upcoming_matches_for_league(%s): error Firestore — fail-open", league_code)
        return True


def load_weights() -> dict:
    """
    Lee doc 'current' de Firestore model_weights.
    Si no existe, usa DEFAULT_WEIGHTS de config.py.
    """
    try:
        doc = col("model_weights").document("current").get()
        if doc.exists:
            data = doc.to_dict()
            weights = data.get("weights", {})
            # Verificar que tiene las cuatro claves necesarias
            if all(k in weights for k in DEFAULT_WEIGHTS):
                return dict(weights)
            logger.warning("load_weights: pesos incompletos en Firestore — usando DEFAULT_WEIGHTS")
    except Exception:
        logger.error("load_weights: error leyendo Firestore — usando DEFAULT_WEIGHTS", exc_info=True)
    return dict(DEFAULT_WEIGHTS)


def ensemble_probability(enriched_match: dict, weights: dict, team: str = "home") -> dict:
    """
    Combina senales estadisticas con pesos del modelo.
    team: "home" analiza victoria local | "away" analiza victoria visitante.

    Senales base:
      poisson = poisson_home/away_win  (o 0.5 si None — no-football)
      elo     = elo_home_win_prob      — SOLO si elo_sufficient=True
      form    = form_score / 100
      h2h     = (h2h_advantage + 1) / 2  — SOLO si h2h_sufficient=True

    Señales excluidas cuando no hay datos reales (patrón uniforme):
      - ELO excluido si ambos equipos tienen ELO=DEFAULT_ELO (1500): nunca actualizado,
        infla probabilidades de visitantes débiles contra equipos de élite.
      - H2H excluido si h2h_sufficient=False: lista de partidos directos vacía.
      En ambos casos los pesos se renormalizan sobre las señales activas.

    final_prob = sum(signal * norm_weight) sobre senales activas
    confidence = max(0.0, 1 - std(senales activas))

    Devuelve {"prob", "confidence", "signals", "elo_sufficient", "h2h_used"}
    """
    poisson_home = enriched_match.get("poisson_home_win")
    poisson_away = enriched_match.get("poisson_away_win")
    elo_home = enriched_match.get("elo_home_win_prob")
    home_form = enriched_match.get("home_form_score", 50.0)
    away_form = enriched_match.get("away_form_score", 50.0)
    h2h_adv = enriched_match.get("h2h_advantage", 0.0)
    h2h_sufficient = enriched_match.get("h2h_sufficient", True)

    # ELO solo aporta información real si al menos uno difiere del default
    home_elo_val = enriched_match.get("home_elo")
    away_elo_val = enriched_match.get("away_elo")
    elo_sufficient = not (
        home_elo_val is not None
        and away_elo_val is not None
        and abs(float(home_elo_val) - DEFAULT_ELO) < 1.0
        and abs(float(away_elo_val) - DEFAULT_ELO) < 1.0
    )

    # Fallback neutral para deportes sin modelo Poisson/ELO
    poisson_home_s = float(poisson_home) if poisson_home is not None else 0.5
    poisson_away_s = float(poisson_away) if poisson_away is not None else 0.5
    elo_home_s = float(elo_home) if elo_home is not None else 0.5

    if team == "home":
        signals = {"poisson": poisson_home_s, "form": float(home_form) / 100.0}
        if elo_sufficient:
            signals["elo"] = elo_home_s
        if h2h_sufficient:
            signals["h2h"] = (float(h2h_adv) + 1.0) / 2.0
    else:  # away
        signals = {"poisson": poisson_away_s, "form": float(away_form) / 100.0}
        if elo_sufficient:
            signals["elo"] = 1.0 - elo_home_s
        if h2h_sufficient:
            signals["h2h"] = 1.0 - (float(h2h_adv) + 1.0) / 2.0

    # Clampear todas las senales al rango [0.0, 1.0]
    signals = {k: max(0.0, min(1.0, v)) for k, v in signals.items()}

    # Renormalizar pesos sobre señales activas (ELO y/o H2H pueden estar ausentes)
    raw_weights = {k: weights.get(k, 0.25) for k in signals}
    total_w = sum(raw_weights.values())
    norm_weights = (
        {k: v / total_w for k, v in raw_weights.items()}
        if total_w > 0
        else {k: 1.0 / len(signals) for k in signals}
    )

    # Probabilidad final ponderada con pesos normalizados
    final_prob = sum(signals[k] * norm_weights[k] for k in signals)
    final_prob = max(0.0, min(1.0, final_prob))

    # Confianza: mayor dispersion de senales → menor confianza
    confidence = max(0.0, 1.0 - float(np.std(list(signals.values()))))

    return {
        "prob": round(final_prob, 4),
        "confidence": round(confidence, 4),
        "signals": {k: round(v, 4) for k, v in signals.items()},
        "elo_sufficient": elo_sufficient,
        "h2h_used": h2h_sufficient,
    }


async def fetch_bookmaker_odds(
    match_id: str,
    home_team: str = "",
    away_team: str = "",
    league: str = "",
) -> dict | None:
    """
    Obtiene cuotas 1X2 para un partido. Orden de prioridad:
    1. Cache Firestore (TTL 4h) — evita llamadas redundantes
    2. The Odds API (fuente primaria — free tier incluye cuotas reales)
    2b. OddsPapi v1 fallback — usa caché ya cargada en el pre-fetch, lookup en memoria
    Devuelve {bookmaker, home_odds, draw_odds, away_odds, opening_home_odds} o None.
    """
    # DIAG: si este log no aparece → generate_signal() sale antes de llegar aquí (Poisson guard)
    logger.warning("DIAG_FBO: iniciando fetch para match_id=%s league=%s", match_id, league)
    now = datetime.now(timezone.utc)
    cache_ttl = timedelta(hours=24)

    # --- 1. Verificar cache ---
    try:
        doc = col("odds_cache").document(match_id).get()
        if doc.exists:
            data = doc.to_dict()
            fetched_at = data.get("fetched_at")
            if fetched_at and hasattr(fetched_at, "tzinfo") and fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            if fetched_at and (now - fetched_at) < cache_ttl:
                return {
                    "bookmaker": data.get("bookmaker", "bet365"),
                    "home_odds": float(data.get("home_odds", 2.0)),
                    "draw_odds": float(data.get("draw_odds", 3.2)),
                    "away_odds": float(data.get("away_odds", 3.5)),
                    "opening_home_odds": float(data.get("opening_home_odds", data.get("home_odds", 2.0))),
                }
    except Exception:
        logger.error("fetch_bookmaker_odds(%s): error leyendo odds_cache", match_id, exc_info=True)

    # --- 2. odds-api.io (fuente PRIMARIA — 5000 req/h, >72k/mes) ---
    if home_team and away_team:
        try:
            from shared.config import ODDSAPIIO_KEY as _ODDSAPIIO_KEY
            if _ODDSAPIIO_KEY:
                oaio = await _fetch_oddsapiio(match_id, home_team, away_team, league, now)
                if oaio:
                    return {**oaio, "source": "oddsapiio"}
        except Exception:
            logger.error("fetch_bookmaker_odds(%s): error en odds-api.io", match_id, exc_info=True)

    # --- 3. The Odds API (secundaria — 500/mes) ---
    if ODDS_API_KEY and home_team and away_team and league in _ODDS_SPORT_MAP:
        odds_result = await _fetch_the_odds_api(match_id, home_team, away_team, league, now)
        if odds_result:
            return {**odds_result, "source": "theoddsapi"}

    # --- 4. OddsPapi (terciaria — 250/mes, cuando quota activa) ---
    if home_team and away_team:
        try:
            from analyzers.football_markets import get_oddspapi_h2h_odds, _ODDSPAPI_LEAGUE_MAP as _op_map
            if league in _op_map:
                op_result = await get_oddspapi_h2h_odds(league, home_team, away_team)
                if op_result:
                    await _save_odds_cache(match_id, op_result, now)
                    logger.info("fetch_bookmaker_odds(%s): OddsPapi h2h — %s @ home=%.2f away=%.2f",
                                match_id, op_result.get("bookmaker", "oddspapi"),
                                op_result.get("home_odds", 0), op_result.get("away_odds", 0))
                    return op_result
        except Exception:
            logger.error("fetch_bookmaker_odds(%s): error en OddsPapi fallback", match_id, exc_info=True)

    # --- 5. Optic Odds (cuaternaria — 1000/mes, fallback cuando todo lo anterior falla) ---
    if home_team and away_team:
        try:
            from shared.config import OPTIC_ODDS_KEY as _OPTIC_KEY
            if _OPTIC_KEY:
                optic = await _fetch_opticodds(match_id, home_team, away_team, league, now)
                if optic:
                    await _save_odds_cache(match_id, optic, now)
                    logger.info("fetch_bookmaker_odds(%s): opticodds — %s @ home=%.2f away=%.2f",
                                match_id, optic.get("bookmaker", "opticodds"),
                                optic.get("home_odds", 0), optic.get("away_odds", 0))
                    return {**optic, "source": "opticodds"}
        except Exception:
            logger.error("fetch_bookmaker_odds(%s): error en opticodds fallback", match_id, exc_info=True)

    # RapidAPI /odds no está disponible en el plan free de API-Football (siempre 403).
    # apifootball_odds.py cubre BTTS/AH/DC via /v3/odds en generate_football_extra_signals.
    logger.debug("fetch_bookmaker_odds(%s): sin cuotas h2h disponibles", match_id)
    return None


_GENERIC_WORDS = {"fc", "cf", "ac", "sc", "ss", "ca", "cd", "ud", "sd", "rc", "rcd",
                  "afc", "fk", "sk", "bv", "sv", "vfb", "fsv", "tsg", "rb", "us",
                  "ad", "de", "futbol", "football", "balompie", "club", "sports"}


def _normalize_team(name: str) -> str:
    """Minusculas, sin acentos, sin prefijos/sufijos genericos."""
    import re, unicodedata
    # Eliminar acentos (é→e, ü→u, etc.) antes de filtrar caracteres
    n = unicodedata.normalize("NFD", name.lower().strip())
    n = n.encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    words = [w for w in n.split() if w not in _GENERIC_WORDS]
    return " ".join(words)


def _teams_match(our_name: str, api_name: str) -> bool:
    """
    True si los nombres de equipo son el mismo club.
    Estrategias en orden:
    1. Coincidencia exacta tras normalizar
    2. Uno contiene al otro (min 5 chars)
    3. Primera palabra significativa coincide en ambos (cubre Athletic Club / Athletic Bilbao)
    """
    a = _normalize_team(our_name)
    b = _normalize_team(api_name)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 5 and a in b:
        return True
    if len(b) >= 5 and b in a:
        return True
    # Comparar primera palabra significativa (min 4 chars)
    first_a = a.split()[0] if a.split() else ""
    first_b = b.split()[0] if b.split() else ""
    if len(first_a) >= 4 and len(first_b) >= 4 and first_a == first_b:
        return True
    return False


async def _fetch_oddsapiio(
    match_id: str, home_team: str, away_team: str, league: str, now: datetime
) -> dict | None:
    """
    Obtiene cuotas de odds-api.io (fuente primaria).
    Usa el cliente odds_apiio_client que cachea por liga TTL 4h.
    Normaliza la respuesta al mismo formato que The Odds API.
    """
    # Verificar caché en memoria (evita re-llamar al cliente entre partidos de la misma liga)
    cached = _ODDSAPIIO_CACHE.get(league)
    if cached is not None:
        fetched_at, events = cached
        if (now - fetched_at) < _ODDSAPIIO_CACHE_TTL:
            age_min = int((now - fetched_at).total_seconds() / 60)
            logger.warning(
                "DIAG_VBE_CACHE_HIT: _ODDSAPIIO_CACHE[%s] → %d eventos (age=%dmin) — NO llama get_league_odds",
                league, len(events), age_min,
            )
            return _search_oddsapiio_event(events, home_team, away_team, match_id)
        # Cache expirado → dejar que el cliente decida (también tiene su propio TTL)

    logger.warning("DIAG_VBE_CACHE_MISS: _ODDSAPIIO_CACHE[%s] miss → llamando get_league_odds", league)

    try:
        from collectors.odds_apiio_client import get_league_odds
        events = await get_league_odds(league)
        # Solo cachear si hay eventos reales — si [] no almacenar aquí:
        # odds_apiio_client._EVENT_CACHE ya tiene TTL 60s para errores y
        # cachear (now, []) aquí con 4h bloquearía reintentos (age=0min).
        if events:
            _ODDSAPIIO_CACHE[league] = (now, events)
    except Exception:
        logger.error("_fetch_oddsapiio(%s): error llamando cliente", match_id, exc_info=True)
        return None

    return _search_oddsapiio_event(events, home_team, away_team, match_id)


def _search_oddsapiio_event(events: list, home_team: str, away_team: str, match_id: str) -> dict | None:
    """Busca el partido en la lista de eventos de odds-api.io y extrae cuotas h2h + all_markets."""
    for ev in events:
        api_home = ev.get("home_team", "")
        api_away = ev.get("away_team", "")
        if not (_teams_match(home_team, api_home) and _teams_match(away_team, api_away)):
            continue
        result = _parse_the_odds_event(ev)
        if result:
            ev_markets = ev.get("markets", {})
            if ev_markets:
                result["all_markets"] = ev_markets
            logger.info(
                "fetch_bookmaker_odds(%s): odds-api.io — %s @ home=%.2f draw=%.2f away=%.2f markets=%s",
                match_id, result["bookmaker"],
                result["home_odds"], result["draw_odds"], result["away_odds"],
                list(ev_markets.keys()) if ev_markets else [],
            )
            return result
    if events:
        sample = [f"{_normalize_team(e.get('home_team','?'))} vs {_normalize_team(e.get('away_team','?'))}"
                  for e in events[:5]]
        logger.info(
            "fetch_bookmaker_odds(%s): odds-api.io — partido no encontrado (%s vs %s) en %d eventos | "
            "muestra normalizados: %s",
            match_id, _normalize_team(home_team), _normalize_team(away_team), len(events), sample,
        )
    return None


async def _fetch_opticodds(
    match_id: str, home_team: str, away_team: str, league: str, now: datetime
) -> dict | None:
    """
    Obtiene cuotas de optic-odds.io (fallback cuaternario — 1000 req/mes).
    Mismo patrón que _fetch_oddsapiio: caché en memoria por liga TTL 4h.
    Reutiliza _search_oddsapiio_event porque el formato normalizado es idéntico.
    """
    cached = _OPTICODDS_CACHE.get(league)
    if cached is not None:
        fetched_at, events = cached
        if (now - fetched_at) < _OPTICODDS_CACHE_TTL:
            return _search_oddsapiio_event(events, home_team, away_team, match_id)

    try:
        from collectors.opticodds_client import get_league_odds as _optic_get
        events = await _optic_get(league)
        if events:
            _OPTICODDS_CACHE[league] = (now, events)
    except Exception:
        logger.error("_fetch_opticodds(%s): error llamando cliente", match_id, exc_info=True)
        return None

    return _search_oddsapiio_event(events, home_team, away_team, match_id)


async def _fetch_the_odds_api(
    match_id: str, home_team: str, away_team: str, league: str, now: datetime
) -> dict | None:
    """
    Obtiene cuotas de The Odds API para un partido.
    Cache en memoria por liga (TTL 1h): un run con N fixtures de la misma liga
    hace 1 sola llamada HTTP en vez de N.
    """
    sport_key = _ODDS_SPORT_MAP[league]

    # Guard: skip si no hay partidos en las próximas 48h (ahorra quota)
    cached = _LEAGUE_ODDS_CACHE.get(sport_key)
    cache_fresh = cached is not None and (now - cached[0]) < _LEAGUE_CACHE_TTL
    if not cache_fresh and not _has_upcoming_matches_for_league(league, within_hours=48):
        logger.info("_fetch_the_odds_api: %s — sin partidos en 48h, skip", league)
        return None

    # --- Cache en memoria por liga ---
    events = await _get_league_events(sport_key, match_id, now)
    if events is None:
        return None

    # Buscar el evento que coincida con home_team y away_team
    for event in events:
        api_home = event.get("home_team", "")
        api_away = event.get("away_team", "")
        if _teams_match(home_team, api_home) and _teams_match(away_team, api_away):
            odds_result = _parse_the_odds_event(event)
            if odds_result:
                logger.info(
                    "fetch_bookmaker_odds(%s): The Odds API — %s @ home=%.2f draw=%.2f away=%.2f",
                    match_id, odds_result["bookmaker"],
                    odds_result["home_odds"], odds_result["draw_odds"], odds_result["away_odds"],
                )
                await _save_odds_cache(match_id, odds_result, now)
                return odds_result

    # Logging detallado para diagnosticar por qué no matchea.
    # Muestra los nombres normalizados que buscábamos y los primeros 5 eventos del caché.
    h_norm = _normalize_team(home_team)
    a_norm = _normalize_team(away_team)
    sample = [
        f"{_normalize_team(ev.get('home_team','?'))} vs {_normalize_team(ev.get('away_team','?'))}"
        for ev in events[:5]
    ]
    logger.info(
        "fetch_bookmaker_odds(%s): The Odds API — no encontrado | "
        "buscando: '%s' vs '%s' | caché %d eventos | muestra: %s",
        match_id, h_norm, a_norm, len(events), sample,
    )
    return None


async def _get_league_events(sport_key: str, match_id: str, now: datetime) -> list | None:
    """
    Devuelve todos los eventos de una liga. Orden de precedencia:
    1. Cache en memoria (rápido, sin I/O) — TTL 8h
    2. Cache en Firestore (persiste entre reinicios Cloud Run) — TTL 8h
    3. The Odds API (HTTP) — solo si cuota disponible y ambos caches expirados
    Devuelve None si no hay cache Y la cuota está agotada.
    """
    global _THE_ODDS_API_EXHAUSTED

    # --- 1. Cache en memoria ---
    cached = _LEAGUE_ODDS_CACHE.get(sport_key)
    if cached is not None:
        fetched_at, events = cached
        if (now - fetched_at) < _LEAGUE_CACHE_TTL:
            # INFO solo en el primer partido de cada liga (no spam por cada uno)
            if match_id == "prefetch" or not events:
                logger.info("The Odds API: caché memoria '%s' — %d eventos", sport_key, len(events))
            else:
                logger.debug("The Odds API: caché memoria '%s' vigente (%d eventos)", sport_key, len(events))
            return events

    # --- 2. Cache en Firestore (sobrevive reinicios Cloud Run) ---
    try:
        fs_doc = col("league_odds_cache").document(sport_key + "_v4").get()
        if fs_doc.exists:
            fs_data = fs_doc.to_dict()
            fs_fetched = fs_data.get("fetched_at")
            if fs_fetched:
                if hasattr(fs_fetched, "tzinfo") and fs_fetched.tzinfo is None:
                    fs_fetched = fs_fetched.replace(tzinfo=timezone.utc)
                if (now - fs_fetched) < _LEAGUE_CACHE_TTL:
                    events = fs_data.get("events", [])
                    _LEAGUE_ODDS_CACHE[sport_key] = (fs_fetched, events)
                    age_min = round((now - fs_fetched).total_seconds() / 60)
                    logger.info("The Odds API: caché Firestore '%s' — %d eventos (edad %d min)",
                                sport_key, len(events), age_min)
                    return events
    except Exception:
        logger.warning("fetch_bookmaker_odds(%s): error leyendo cache Firestore para '%s'", match_id, sport_key, exc_info=True)

    # --- 3. The Odds API (solo si quota disponible) ---
    # Mutex: evita race condition donde N coroutines concurrentes todas pasan can_call=True
    # y todas hacen HTTP request antes de que ninguna llame track_call.
    if _THE_ODDS_API_EXHAUSTED:
        return None

    async with _THE_ODDS_API_LOCK:
        # Re-check dentro del lock (otro coroutine pudo haberlo agotado mientras esperábamos)
        if _THE_ODDS_API_EXHAUSTED:
            return None

        # Re-check cache (podría haber sido actualizado por otro coroutine)
        cached = _LEAGUE_ODDS_CACHE.get(sport_key)
        if cached is not None:
            fetched_at, events = cached
            if (now - fetched_at) < _LEAGUE_CACHE_TTL:
                return events

        # Verificar cuota mensual (The Odds API es 500/mes, no diaria)
        if not quota.can_call_monthly("the_odds_api"):
            logger.warning("fetch_bookmaker_odds(%s): The Odds API — cuota mensual agotada", match_id)
            _THE_ODDS_API_EXHAUSTED = True
            return None

        url = f"{_THE_ODDS_API_BASE}/{sport_key}/odds"
        try:
            markets_param = "h2h,spreads,totals"
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(url, params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "eu",
                    "markets": markets_param,
                    "oddsFormat": "decimal",
                })

            if resp.status_code == 401:
                logger.warning("fetch_bookmaker_odds(%s): The Odds API — clave invalida (401)", match_id)
                _THE_ODDS_API_EXHAUSTED = True
                # Marcar también en el quota manager para que all_monthly_exhausted() lo refleje
                quota.track_monthly("the_odds_api", remaining=0)
                return None
            if resp.status_code == 404:
                # 404 = liga no activa ahora (p.ej. lunes sin partidos). NO es error global.
                # Caché vacío con TTL 30 min (no 8h) para reintentar en el siguiente analyze.
                logger.info("fetch_bookmaker_odds(%s): The Odds API — liga %s sin eventos activos (404)", match_id, sport_key)
                _LEAGUE_ODDS_CACHE[sport_key] = (now - (_LEAGUE_CACHE_TTL - timedelta(minutes=30)), [])
                return []
            if resp.status_code == 422:
                # 422 = mercado no disponible en este plan. NO es cuota agotada (eso sería 429).
                # Caché vacío con TTL 30 min para reintentar tras deploy de fix de markets.
                logger.warning("fetch_bookmaker_odds(%s): The Odds API — liga %s 422 (plan/markets) — retry en 30 min", match_id, sport_key)
                _LEAGUE_ODDS_CACHE[sport_key] = (now - (_LEAGUE_CACHE_TTL - timedelta(minutes=30)), [])
                return []
            if resp.status_code == 429:
                # 429 = cuota real agotada → bloquear todas las ligas
                _THE_ODDS_API_EXHAUSTED = True
                logger.warning("fetch_bookmaker_odds(%s): The Odds API — cuota agotada (429)", match_id)
                return None
            if resp.status_code != 200:
                logger.warning("fetch_bookmaker_odds(%s): The Odds API respondio %d para %s", match_id, resp.status_code, sport_key)
                return None

            events = resp.json()
            remaining = resp.headers.get("x-requests-remaining")
            quota.track_monthly("the_odds_api", remaining=remaining)  # límite es mensual
            logger.info("The Odds API: '%s' — %d eventos cargados, %s requests restantes",
                        sport_key, len(events), remaining or "?")

            # Actualizar cache en memoria y en Firestore
            _LEAGUE_ODDS_CACHE[sport_key] = (now, events)
            try:
                col("league_odds_cache").document(sport_key + "_v4").set({
                    "sport_key": sport_key,
                    "fetched_at": now,
                    "events": events,
                })
            except Exception:
                logger.warning("fetch_bookmaker_odds(%s): error guardando cache Firestore para '%s'", match_id, sport_key, exc_info=True)

            return events

        except Exception:
            logger.error("fetch_bookmaker_odds(%s): error llamando The Odds API", match_id, exc_info=True)
            return None


def _parse_the_odds_event(event: dict) -> dict | None:
    """Extrae cuotas h2h de un evento de The Odds API."""
    try:
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None

        home_team = event.get("home_team", "")

        for bk in bookmakers:
            for market in bk.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                home_odds = draw_odds = away_odds = None
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name", "")
                    price = float(outcome.get("price", 0))
                    if name == home_team:
                        home_odds = price
                    elif name == "Draw":
                        draw_odds = price
                    else:
                        away_odds = price
                if home_odds and away_odds:
                    return {
                        "bookmaker": bk.get("key", "bet365"),
                        "home_odds": home_odds,
                        "draw_odds": draw_odds or 3.2,
                        "away_odds": away_odds,
                        "opening_home_odds": home_odds,
                    }
        return None
    except Exception:
        logger.error("_parse_the_odds_event: error", exc_info=True)
        return None


def _parse_totals_event(event: dict, line: float = _FOOTBALL_TOTALS_LINE) -> dict | None:
    """Extrae cuotas over/under para una línea específica de un evento de The Odds API."""
    try:
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None
        for bk in bookmakers:
            for market in bk.get("markets", []):
                if market.get("key") != "totals":
                    continue
                over_odds = under_odds = None
                actual_line = line
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name", "")
                    # The Odds API usa "point" para la línea en totals
                    pt = outcome.get("point") or outcome.get("description")
                    try:
                        pt_val = float(pt) if pt is not None else None
                    except (TypeError, ValueError):
                        pt_val = None
                    price = float(outcome.get("price", 0))
                    if pt_val is not None and abs(pt_val - line) < 0.26:
                        if name == "Over":
                            over_odds = price
                            actual_line = pt_val
                        elif name == "Under":
                            under_odds = price
                if over_odds and under_odds:
                    return {
                        "bookmaker": bk.get("key", "pinnacle"),
                        "line": actual_line,
                        "over_odds": over_odds,
                        "under_odds": under_odds,
                    }
        return None
    except Exception:
        logger.error("_parse_totals_event: error", exc_info=True)
        return None


def _calculate_totals_prob(enriched_match: dict, line: float = _FOOTBALL_TOTALS_LINE) -> dict | None:
    """
    Calcula P(over/under line goles) usando el modelo Poisson bivariado.
    Requiere home_xg y away_xg del enriquecedor (solo fútbol con modelo Poisson completo).
    """
    from scipy.stats import poisson as _poisson
    home_xg = enriched_match.get("home_xg")
    away_xg = enriched_match.get("away_xg")
    if home_xg is None or away_xg is None:
        return None
    try:
        expected_total = float(home_xg) + float(away_xg)
        if expected_total <= 0:
            return None
        floor_line = int(line)  # 2 para línea 2.5
        prob_under_or_equal = sum(_poisson.pmf(k, expected_total) for k in range(floor_line + 1))
        over_prob = max(0.0, min(1.0, 1.0 - prob_under_or_equal))
        under_prob = max(0.0, min(1.0, prob_under_or_equal))
        return {
            "over_prob": round(over_prob, 4),
            "under_prob": round(under_prob, 4),
            "expected_total": round(expected_total, 2),
            "line": line,
        }
    except Exception:
        logger.error("_calculate_totals_prob: error", exc_info=True)
        return None


def _calculate_btts_prob(enriched_match: dict) -> dict | None:
    """P(BTTS) = P(home≥1) × P(away≥1) usando Poisson independiente."""
    from scipy.stats import poisson as _poisson
    home_xg = enriched_match.get("home_xg")
    away_xg = enriched_match.get("away_xg")
    if home_xg is None or away_xg is None:
        return None
    try:
        lh = max(0.01, float(home_xg))
        la = max(0.01, float(away_xg))
        p_yes = (1.0 - float(_poisson.pmf(0, lh))) * (1.0 - float(_poisson.pmf(0, la)))
        p_yes = max(0.0, min(1.0, p_yes))
        return {"btts_prob": round(p_yes, 4), "no_btts_prob": round(1.0 - p_yes, 4)}
    except Exception:
        logger.error("_calculate_btts_prob: error", exc_info=True)
        return None


def _calculate_ah_prob(enriched_match: dict, line: float = -0.5) -> dict | None:
    """
    P(home covers AH line).
    AH -0.5 para home = P(home wins) — equivalente a poisson_home_win si disponible.
    """
    if abs(line + 0.5) < 0.01:
        p_home = enriched_match.get("poisson_home_win")
        if p_home is None:
            return None
        p_home = max(0.0, min(1.0, float(p_home)))
        return {"home_covers": round(p_home, 4), "away_covers": round(1.0 - p_home, 4), "line": line}
    return None


async def _generate_oddsapiio_extra_signals(
    enriched_match: dict,
    all_markets: dict,
    match_id: str,
    home_team: str,
    away_team: str,
    league: str,
    sport: str,
    match_date,
    weights_version: int,
) -> list[dict]:
    """
    Emite señales BTTS, Over/Under 2.5 y Asian Handicap -0.5 usando:
      - Probabilidades Poisson del enriched_match
      - Cuotas de odds-api.io (all_markets dict del evento normalizado)
    """
    results: list[dict] = []
    now = datetime.now(timezone.utc)

    # ── BTTS ──────────────────────────────────────────────────────────────────
    btts_mkt = all_markets.get("btts")
    if not btts_mkt:
        logger.info("EXTRA_MARKETS_BTTS(%s): no disponible en all_markets", match_id)
    else:
        logger.info("EXTRA_MARKETS_BTTS(%s): mkt=%s", match_id, btts_mkt)
    if btts_mkt:
        btts_probs = _calculate_btts_prob(enriched_match)
        if btts_probs:
            yes_odds = btts_mkt.get("yes_odds") or 0.0
            if yes_odds > 1.05:
                edge = calculate_edge(btts_probs["btts_prob"], yes_odds)
                conf = round(min(1.0, max(0.0, btts_probs["btts_prob"] + 0.1)), 4)
                logger.info(
                    "EXTRA_MARKETS_BTTS(%s): btts_prob=%.3f yes_odds=%.2f "
                    "edge=%.4f(min=%.3f) conf=%.3f(min=%.3f) → %s",
                    match_id, btts_probs["btts_prob"], yes_odds,
                    edge, SPORTS_MIN_EDGE, conf, SPORTS_MIN_CONFIDENCE,
                    "OK" if edge > SPORTS_MIN_EDGE and conf > SPORTS_MIN_CONFIDENCE else "SKIP",
                )
                if edge > SPORTS_MIN_EDGE and conf > SPORTS_MIN_CONFIDENCE:
                    doc_id = f"{match_id}_btts"
                    pred = {
                        "match_id": doc_id, "home_team": home_team, "away_team": away_team,
                        "sport": sport, "league": league, "market_type": "btts",
                        "selection": "Yes", "bookmaker": btts_mkt.get("bookmaker", ""),
                        "odds": round(yes_odds, 3),
                        "calculated_prob": btts_probs["btts_prob"],
                        "edge": round(edge, 4), "confidence": conf,
                        "kelly_fraction": kelly_criterion(edge, yes_odds),
                        "factors": {
                            "btts_prob": btts_probs["btts_prob"],
                            "home_xg": enriched_match.get("home_xg"),
                            "away_xg": enriched_match.get("away_xg"),
                        },
                        "signals": {}, "data_source": "poisson_btts", "odds_source": "oddsapiio",
                        "match_date": match_date, "weights_version": weights_version,
                        "created_at": now, "result": None, "correct": None, "error_type": None,
                    }
                    try:
                        col("predictions").document(doc_id).set(pred)
                        logger.info("generate_signal(%s): BTTS Yes @ %.2f edge=%.1f%%",
                                    match_id, yes_odds, edge * 100)
                    except Exception:
                        logger.error("generate_signal(%s): error guardando btts", match_id, exc_info=True)
                    if edge > SPORTS_ALERT_EDGE:
                        await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
                    results.append(pred)

    # ── Over/Under 2.5 ────────────────────────────────────────────────────────
    totals_list = all_markets.get("totals", [])
    available_lines = [t.get("line") for t in totals_list]
    t25 = next((t for t in totals_list if abs(t.get("line", 0) - 2.5) < 0.01), None)
    logger.info(
        "EXTRA_MARKETS_OU(%s): totals_lines=%s t25=%s",
        match_id, available_lines, bool(t25),
    )
    if t25:
        totals_probs = _calculate_totals_prob(enriched_match, line=2.5)
        if totals_probs:
            over_odds = t25.get("over_odds") or 0.0
            under_odds = t25.get("under_odds") or 0.0
            over_edge = calculate_edge(totals_probs["over_prob"], over_odds) if over_odds > 1.05 else -1
            under_edge = calculate_edge(totals_probs["under_prob"], under_odds) if under_odds > 1.05 else -1

            logger.info(
                "EXTRA_MARKETS_OU(%s): over_prob=%.3f over_odds=%.2f over_edge=%.4f "
                "| under_prob=%.3f under_odds=%.2f under_edge=%.4f | min_edge=%.3f",
                match_id,
                totals_probs["over_prob"], over_odds, over_edge,
                totals_probs["under_prob"], under_odds, under_edge,
                SPORTS_MIN_EDGE,
            )
            if over_edge >= under_edge and over_edge > SPORTS_MIN_EDGE:
                sel, sel_p, sel_odds, sel_edge = "Over", totals_probs["over_prob"], over_odds, over_edge
            elif under_edge > over_edge and under_edge > SPORTS_MIN_EDGE:
                sel, sel_p, sel_odds, sel_edge = "Under", totals_probs["under_prob"], under_odds, under_edge
            else:
                sel = None
                logger.info("EXTRA_MARKETS_OU(%s): SKIP — ningún lado supera min_edge=%.3f", match_id, SPORTS_MIN_EDGE)

            if sel:
                sel_conf = round(abs(sel_p - 0.5) * 2, 4)
                logger.info(
                    "EXTRA_MARKETS_OU(%s): sel=%s sel_p=%.3f conf=%.3f(min=%.3f) → %s",
                    match_id, sel, sel_p, sel_conf, SPORTS_MIN_CONFIDENCE,
                    "OK" if sel_conf > SPORTS_MIN_CONFIDENCE else "SKIP",
                )
                if sel_conf > SPORTS_MIN_CONFIDENCE:
                    doc_id = f"{match_id}_ou25_oaio"
                    pred = {
                        "match_id": doc_id, "home_team": home_team, "away_team": away_team,
                        "sport": sport, "league": league, "market_type": "totals",
                        "selection": f"{sel} 2.5", "line": 2.5,
                        "bookmaker": t25.get("bookmaker", ""),
                        "odds": round(sel_odds, 3), "calculated_prob": sel_p,
                        "edge": round(sel_edge, 4), "confidence": sel_conf,
                        "kelly_fraction": kelly_criterion(sel_edge, sel_odds),
                        "factors": {
                            "expected_total": totals_probs["expected_total"],
                            "home_xg": enriched_match.get("home_xg"),
                            "away_xg": enriched_match.get("away_xg"),
                        },
                        "signals": {}, "data_source": "poisson_totals", "odds_source": "oddsapiio",
                        "match_date": match_date, "weights_version": weights_version,
                        "created_at": now, "result": None, "correct": None, "error_type": None,
                    }
                    try:
                        col("predictions").document(doc_id).set(pred)
                        logger.info("generate_signal(%s): %s 2.5 @ %.2f edge=%.1f%%",
                                    match_id, sel, sel_odds, sel_edge * 100)
                    except Exception:
                        logger.error("generate_signal(%s): error guardando ou25_oaio", match_id, exc_info=True)
                    results.append(pred)

    # ── Asian Handicap -0.5 ───────────────────────────────────────────────────
    spreads_list = all_markets.get("spreads", [])
    available_points = [s.get("point") for s in spreads_list]
    ah_m05 = next(
        (s for s in spreads_list if s.get("point") is not None and abs(s.get("point", 0) + 0.5) < 0.01),
        None,
    )
    logger.info(
        "EXTRA_MARKETS_AH(%s): spreads_points=%s ah_m05=%s",
        match_id, available_points, bool(ah_m05),
    )
    if ah_m05:
        ah_probs = _calculate_ah_prob(enriched_match, line=-0.5)
        if ah_probs:
            home_ah_odds = ah_m05.get("home_odds") or 0.0
            away_ah_odds = ah_m05.get("away_odds") or 0.0
            home_edge = calculate_edge(ah_probs["home_covers"], home_ah_odds) if home_ah_odds > 1.05 else -1
            away_edge = calculate_edge(ah_probs["away_covers"], away_ah_odds) if away_ah_odds > 1.05 else -1

            logger.info(
                "EXTRA_MARKETS_AH(%s): home_covers=%.3f home_odds=%.2f home_edge=%.4f "
                "| away_covers=%.3f away_odds=%.2f away_edge=%.4f | min_edge=%.3f",
                match_id,
                ah_probs["home_covers"], home_ah_odds, home_edge,
                ah_probs["away_covers"], away_ah_odds, away_edge,
                SPORTS_MIN_EDGE,
            )
            if home_edge >= away_edge and home_edge > SPORTS_MIN_EDGE:
                sel, sel_p, sel_odds, sel_edge = home_team, ah_probs["home_covers"], home_ah_odds, home_edge
            elif away_edge > home_edge and away_edge > SPORTS_MIN_EDGE:
                sel, sel_p, sel_odds, sel_edge = away_team, ah_probs["away_covers"], away_ah_odds, away_edge
            else:
                sel = None
                logger.info("EXTRA_MARKETS_AH(%s): SKIP — ningún lado supera min_edge=%.3f", match_id, SPORTS_MIN_EDGE)

            if sel:
                sel_conf = round(min(1.0, sel_p), 4)
                logger.info(
                    "EXTRA_MARKETS_AH(%s): sel=%s sel_p=%.3f conf=%.3f(min=%.3f) → %s",
                    match_id, sel, sel_p, sel_conf, SPORTS_MIN_CONFIDENCE,
                    "OK" if sel_conf > SPORTS_MIN_CONFIDENCE else "SKIP",
                )
                if sel_conf > SPORTS_MIN_CONFIDENCE:
                    doc_id = f"{match_id}_ah05"
                    pred = {
                        "match_id": doc_id, "home_team": home_team, "away_team": away_team,
                        "sport": sport, "league": league, "market_type": "asian_handicap",
                        "selection": sel, "line": -0.5,
                        "bookmaker": ah_m05.get("bookmaker", ""),
                        "odds": round(sel_odds, 3), "calculated_prob": sel_p,
                        "edge": round(sel_edge, 4), "confidence": sel_conf,
                        "kelly_fraction": kelly_criterion(sel_edge, sel_odds),
                        "factors": {
                            "home_covers": ah_probs["home_covers"],
                            "home_xg": enriched_match.get("home_xg"),
                            "away_xg": enriched_match.get("away_xg"),
                        },
                        "signals": {}, "data_source": "poisson_ah", "odds_source": "oddsapiio",
                        "match_date": match_date, "weights_version": weights_version,
                        "created_at": now, "result": None, "correct": None, "error_type": None,
                    }
                    try:
                        col("predictions").document(doc_id).set(pred)
                        logger.info("generate_signal(%s): AH -0.5 %s @ %.2f edge=%.1f%%",
                                    match_id, sel, sel_odds, sel_edge * 100)
                    except Exception:
                        logger.error("generate_signal(%s): error guardando ah05", match_id, exc_info=True)
                    if sel_edge > SPORTS_ALERT_EDGE:
                        await _send_telegram_alert(_build_alert_payload(pred, enriched_match))
                    results.append(pred)

    return results


def _parse_odds_response(fixture_data: dict) -> dict | None:
    """Extrae cuotas 1X2 de la respuesta de API-Football /odds."""
    try:
        bookmakers = fixture_data.get("bookmakers", [])
        if not bookmakers:
            return None

        bookmaker = bookmakers[0]
        bookmaker_name = bookmaker.get("name", "bet365")
        bets = bookmaker.get("bets", [])

        for bet in bets:
            if bet.get("name") in ("Match Winner", "1X2"):
                home_odds = draw_odds = away_odds = None
                for value in bet.get("values", []):
                    label = value.get("value", "")
                    odd = float(value.get("odd", 0))
                    if label == "Home":
                        home_odds = odd
                    elif label == "Draw":
                        draw_odds = odd
                    elif label == "Away":
                        away_odds = odd

                if home_odds and draw_odds and away_odds:
                    return {
                        "bookmaker": bookmaker_name,
                        "home_odds": home_odds,
                        "draw_odds": draw_odds,
                        "away_odds": away_odds,
                        "opening_home_odds": home_odds,
                    }
        return None
    except Exception:
        logger.error("_parse_odds_response: error parseando respuesta", exc_info=True)
        return None


async def _save_odds_cache(match_id: str, odds: dict, now: datetime) -> None:
    """Guarda cuotas en odds_cache. opening_* solo se establece la primera vez."""
    try:
        doc_ref = col("odds_cache").document(match_id)
        existing = doc_ref.get()

        if existing.exists:
            # Actualizar cuotas actuales — NO tocar opening_*
            doc_ref.update({
                "home_odds": odds["home_odds"],
                "draw_odds": odds["draw_odds"],
                "away_odds": odds["away_odds"],
                "bookmaker": odds["bookmaker"],
                "fetched_at": now,
            })
        else:
            # Primera vez — guardar opening_* y actuales
            doc_ref.set({
                "fixture_id": match_id,
                "home_odds": odds["home_odds"],
                "draw_odds": odds["draw_odds"],
                "away_odds": odds["away_odds"],
                "opening_home_odds": odds["home_odds"],
                "opening_draw_odds": odds["draw_odds"],
                "opening_away_odds": odds["away_odds"],
                "bookmaker": odds["bookmaker"],
                "first_fetched_at": now,
                "fetched_at": now,
            })
    except Exception:
        logger.error("_save_odds_cache(%s): error guardando en Firestore", match_id, exc_info=True)


def calculate_edge(prob_calculated: float, decimal_odds: float) -> float:
    """edge = prob_calculated - (1 / decimal_odds)"""
    if decimal_odds <= 1.0:
        return 0.0
    return round(prob_calculated - (1.0 / decimal_odds), 4)


def kelly_criterion(edge: float, decimal_odds: float) -> float:
    """
    Kelly fraction = edge / (decimal_odds - 1).
    Si edge <= 0 devuelve 0.0 (nunca apostar con edge negativo).
    Clampea resultado entre 0.0 y 0.05 (max 5% del bankroll — cap global de riesgo).
    """
    if edge <= 0.0 or decimal_odds <= 1.0:
        return 0.0
    fraction = edge / (decimal_odds - 1.0)
    return round(max(0.0, min(0.05, fraction)), 4)


async def _send_telegram_alert(prediction: dict) -> bool:
    """
    Envia alerta al telegram-bot via POST /send-alert.
    Devuelve True si el bot confirma sent=True (mensaje entregado al usuario).
    Devuelve False si deduplicado, error HTTP o excepcion.
    """
    if not TELEGRAM_BOT_URL or not CLOUD_RUN_TOKEN:
        logger.debug("_send_telegram_alert: TELEGRAM_BOT_URL o CLOUD_RUN_TOKEN no configurados")
        return False

    try:
        payload = {"type": "sports", "data": prediction}
        payload_bytes = json.dumps(
            payload,
            default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o),
        ).encode()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TELEGRAM_BOT_URL}/send-alert",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-cloud-token": CLOUD_RUN_TOKEN,
                },
            )
        if resp.status_code not in (200, 202):
            logger.warning("_send_telegram_alert: bot respondio %d", resp.status_code)
            return False

        sent = resp.json().get("sent", False)
        if sent:
            logger.info("_send_telegram_alert: alerta entregada al usuario")
        else:
            logger.info("_send_telegram_alert: deduplicada por el bot (sent=False)")
        return bool(sent)

    except Exception:
        logger.error("_send_telegram_alert: error al enviar alerta — continuando", exc_info=True)
        return False


# Palabras clave de equipos top-6 por liga para el filtro de underdog extremo.
# Comparación case-insensitive contra los nombres completos de football-data.org.
_TOP6_KEYWORDS: dict[str, list[str]] = {
    "PD":  ["real madrid", "barcelona", "atlético", "atletico", "athletic", "villarreal", "real sociedad"],
    "SA":  ["inter", "napoli", "atalanta", "juventus", "lazio", "fiorentina", "milan"],
    "PL":  ["manchester city", "arsenal", "liverpool", "chelsea", "manchester united", "aston villa"],
    "BL1": ["bayern", "leverkusen", "dortmund", "leipzig", "frankfurt", "stuttgart"],
    "FL1": ["psg", "paris saint-germain", "marseille", "monaco", "lens", "nice"],
}

# Subconjunto top-3 por liga: los 3 equipos que dominan cada liga.
# Usados para el filtro rival-élite y el descuento del 20% en el edge calculado.
_TOP3_KEYWORDS: dict[str, list[str]] = {
    "PD":  ["real madrid", "barcelona", "atlético", "atletico"],
    "SA":  ["inter", "napoli", "atalanta"],
    "PL":  ["manchester city", "arsenal", "liverpool"],
    "BL1": ["bayern", "leverkusen", "dortmund"],
    "FL1": ["psg", "paris saint-germain", "marseille"],
    "CL":  ["real madrid", "barcelona", "manchester city", "arsenal", "liverpool",
             "bayern", "inter", "atletico", "atlético"],
    "EL":  [],  # Europa League: demasiada variabilidad de nivel — sin filtro top-3
    "ECL": [],
}

# Umbral de cuota por liga: ligas con dominancia extrema usan 4.5, más competitivas 5.0.
_EXTREME_UNDERDOG_ODDS: dict[str, float] = {
    "PD":  4.5,
    "SA":  4.5,
    "PL":  4.5,
    "BL1": 5.0,
    "FL1": 5.0,
}

# Número total de equipos por liga (para calcular zona de descenso / bottom-6).
_LEAGUE_TOTAL_TEAMS: dict[str, int] = {
    "PD": 20, "SA": 20, "PL": 20, "FL1": 18, "BL1": 18,
}

# Cache en memoria de posiciones de tabla: {league_team_key: position}
# TTL no crítico — se invalida al reiniciar el proceso (Cloud Run, max 24h).
_STANDINGS_CACHE: dict[str, int] = {}


def _get_table_position(team_id: int | str | None, league: str) -> int | None:
    """
    Lee la posición en tabla de un equipo desde col("standings") con cache en memoria.
    Devuelve None si el dato no está disponible.
    """
    if not team_id:
        return None
    key = f"{league}_{team_id}"
    if key in _STANDINGS_CACHE:
        return _STANDINGS_CACHE[key]
    try:
        docs = list(
            col("standings")
            .where(filter=FieldFilter("team_id", "==", int(team_id)))
            .limit(1)
            .stream()
        )
        if docs:
            pos = docs[0].to_dict().get("position")
            if pos:
                _STANDINGS_CACHE[key] = int(pos)
                return int(pos)
    except Exception as e:
        logger.debug("_get_table_position(%s, %s): %s", team_id, league, e)
    return None


async def _fetch_external_context(home_team: str, away_team: str, match_date_str: str) -> dict:
    """
    Busca contexto externo (lesiones, rotaciones) via Tavily.
    Devuelve dict con:
      confidence_adj: float (multiplicador, 1.0 = sin cambio)
      notes: list[str]  (mensajes para la alerta)
    Nunca lanza excepción — en cualquier error devuelve {"confidence_adj": 1.0, "notes": []}.
    """
    if not TAVILY_API_KEY:
        return {"confidence_adj": 1.0, "notes": []}

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_API_KEY)

        queries = [
            f"{home_team} lesiones {match_date_str}",
            f"{away_team} lesiones {match_date_str}",
            f"{home_team} rotaciones alineación {match_date_str}",
        ]

        _INJURY_KEYWORDS = {"lesión", "lesionado", "baja confirmada", "out", "doubt", "injured"}
        _ROTATION_KEYWORDS = {"rotaciones", "rotación confirmada", "descansa", "suplentes", "lineup changes"}

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

        all_results = await asyncio.gather(*[_run_query(q) for q in queries])

        confidence_adj = 1.0
        notes: list[str] = []
        injury_found = False
        rotation_found = False

        for results in all_results:
            for item in results:
                content = (item.get("content", "") + " " + item.get("title", "")).lower()
                url = item.get("url", "")
                title = item.get("title", "")

                if not injury_found:
                    for kw in _INJURY_KEYWORDS:
                        if kw in content:
                            injury_found = True
                            domain = url.split("/")[2] if url.count("/") >= 2 else url
                            notes.append(f"{title[:80]} (fuente: {domain})")
                            break

                if not rotation_found:
                    for kw in _ROTATION_KEYWORDS:
                        if kw in content:
                            rotation_found = True
                            domain = url.split("/")[2] if url.count("/") >= 2 else url
                            if not any(domain in n for n in notes):
                                notes.append(f"Rotaciones detectadas — {title[:60]} (fuente: {domain})")
                            break

        if injury_found:
            confidence_adj = round(confidence_adj * 0.85, 4)
        if rotation_found:
            confidence_adj = round(confidence_adj * 0.90, 4)

        # Cap: nunca reducir por debajo de 0.75 desde esta función
        confidence_adj = max(0.75, confidence_adj)

        if confidence_adj < 1.0:
            logger.info(
                "_fetch_external_context(%s vs %s): adj=%.2f injuries=%s rotations=%s notes=%s",
                home_team, away_team, confidence_adj, injury_found, rotation_found, notes,
            )

        return {"confidence_adj": confidence_adj, "notes": notes}

    except Exception:
        logger.error(
            "_fetch_external_context(%s vs %s): error — devolviendo sin ajuste",
            home_team, away_team, exc_info=True,
        )
        return {"confidence_adj": 1.0, "notes": []}


async def generate_signal(enriched_match: dict) -> list[dict]:
    """
    Pipeline completo de generacion de senal para un partido enriquecido.
    Analiza home y away por separado y elige el mejor edge.

    1. load_weights()
    2. ensemble_probability() para home y away
    3. fetch_bookmaker_odds() con fallback a odds_current del enriched_match
    4. calculate_edge() para home y away
    5. Seleccionar el lado con mayor edge (si supera threshold)
    6. Si edge > SPORTS_MIN_EDGE AND confidence > SPORTS_MIN_CONFIDENCE:
       - kelly_criterion()
       - Guarda en Firestore predictions
       - Si edge > SPORTS_ALERT_EDGE: envia alerta Telegram
    7. Devuelve lista de predictions (h2h y/o totals); lista vacia si no hay edge suficiente
    """
    match_id = str(enriched_match.get("match_id", ""))
    sport = enriched_match.get("sport", "football")
    home_team = enriched_match.get("home_team", enriched_match.get("home_team_id", ""))
    away_team = enriched_match.get("away_team", enriched_match.get("away_team_id", ""))
    league = enriched_match.get("league", "")
    match_date = enriched_match.get("match_date") or enriched_match.get("date")
    data_quality = enriched_match.get("data_quality", "partial")
    _min_edge = LEAGUE_MIN_EDGE.get(league, SPORTS_MIN_EDGE)

    # Si faltan nombres o liga (docs enriquecidos antes del fix), leerlos de upcoming_matches
    if not home_team or not away_team or not league:
        try:
            um_doc = col("upcoming_matches").document(match_id).get()
            if um_doc.exists:
                um = um_doc.to_dict()
                home_team = home_team or um.get("home_team", str(enriched_match.get("home_team_id", "")))
                away_team = away_team or um.get("away_team", str(enriched_match.get("away_team_id", "")))
                league = league or um.get("league", "")
                match_date = match_date or um.get("match_date") or um.get("date")
        except Exception:
            logger.warning("generate_signal(%s): no se pudo resolver nombre de equipo desde upcoming_matches", match_id)

    # Ligas donde Poisson no es viable: Copa Lib/BSA tienen <3 partidos de fase de grupos
    # en la primera mitad de la temporada. Se permite continuar con ELO solo si está disponible.
    _POISSON_EXEMPT_LEAGUES = {"ARG", "CSUD", "CAM"}

    # Fix 1: futbol sin Poisson valido → descartar, EXCEPTO ligas exentas con ELO disponible
    if sport == "football" and enriched_match.get("poisson_home_win") is None:
        elo_available = enriched_match.get("elo_home_win_prob") is not None
        if league in _POISSON_EXEMPT_LEAGUES and elo_available:
            logger.info(
                "generate_signal(%s): %s vs %s [%s] — Poisson exento (liga Copa/BSA), "
                "continuando con ELO elo=%.3f",
                match_id, home_team, away_team, league,
                enriched_match.get("elo_home_win_prob", 0.0),
            )
            # Inyectar Poisson sintético desde ELO para que el ensemble funcione
            elo_p = enriched_match.get("elo_home_win_prob", 0.45)
            enriched_match = {
                **enriched_match,
                "poisson_home_win": elo_p,
                "poisson_draw":     max(0.0, 1.0 - elo_p - (1.0 - elo_p) * 0.6),
                "poisson_away_win": (1.0 - elo_p) * 0.6,
            }
        else:
            logger.warning(
                "DIAG_POISSON_GUARD: %s vs %s [%s] — poisson=None quality=%s "
                "elo=%.3f form=%.1f/%.1f — fetch_bookmaker_odds NO se llamará",
                home_team, away_team, league,
                enriched_match.get("data_quality", "?"),
                enriched_match.get("elo_home_win_prob") or -1.0,
                enriched_match.get("home_form_score", 50.0),
                enriched_match.get("away_form_score", 50.0),
            )
            return []

    # Guardia de calidad: omitir partidos sin datos reales (no-football)
    poisson_none = enriched_match.get("poisson_home_win") is None
    form_default = (
        enriched_match.get("home_form_score", 50.0) == 50.0
        and enriched_match.get("away_form_score", 50.0) == 50.0
    )
    h2h_neutral = enriched_match.get("h2h_advantage", 0.0) == 0.0
    if poisson_none and form_default and h2h_neutral:
        logger.warning(
            "generate_signal(%s): omitido por datos insuficientes "
            "(poisson=None, form=50/50 default, h2h=0)",
            match_id,
        )
        return []

    # --- Filtro de relevancia por standings ---
    # No generar señales cuando un equipo ya no tiene nada que ganar o perder.
    _standings_confidence_adj = 1.0
    try:
        _standings_doc = col("standings").document(league).get()
        if _standings_doc.exists:
            _standings = _standings_doc.to_dict().get("teams", {})
            _home_standing = _standings.get(str(enriched_match.get("home_team_id", home_team)), {})
            _away_standing = _standings.get(str(enriched_match.get("away_team_id", away_team)), {})

            for _team_name, _standing in [(home_team, _home_standing), (away_team, _away_standing)]:
                if not _standing:
                    continue
                _relegated = _standing.get("mathematically_relegated", False)
                _champion = _standing.get("mathematically_champion", False)
                _nothing_at_stake = _standing.get("nothing_at_stake", False)

                if _relegated:
                    logger.info(
                        "generate_signal(%s): %s matemáticamente descendido — señal omitida",
                        match_id, _team_name,
                    )
                    return []
                if _champion:
                    logger.info(
                        "generate_signal(%s): %s matemáticamente campeón — señal omitida",
                        match_id, _team_name,
                    )
                    return []
                if _nothing_at_stake:
                    _standings_confidence_adj = min(_standings_confidence_adj, 0.80)
                    logger.info(
                        "generate_signal(%s): %s sin nada en juego — confianza reducida 20%%",
                        match_id, _team_name,
                    )
    except Exception as _se:
        logger.debug("generate_signal(%s): error leyendo standings — %s", match_id, _se)

    # Determinar data_source segun si hay modelo estadistico
    has_statistical_model = (
        enriched_match.get("poisson_home_win") is not None
        and enriched_match.get("elo_home_win_prob") is not None
    )
    data_source = "statistical_model" if has_statistical_model else "groq_ai"

    # --- 1. Pesos del modelo ---
    weights = load_weights()
    weights_version = _get_weights_version()

    # --- 2. Ensemble probability para home y away ---
    result_home = ensemble_probability(enriched_match, weights, team="home")
    result_away = ensemble_probability(enriched_match, weights, team="away")

    elo_sufficient = result_home.get("elo_sufficient", True)
    if not elo_sufficient:
        logger.info(
            "generate_signal(%s): ELO en DEFAULT para ambos equipos — señal ELO excluida del ensemble",
            match_id,
        )

    # --- 2b. Filtro empate: descartar si ningún equipo es favorito claro ---
    prob_home_raw = result_home["prob"]
    prob_away_raw = result_away["prob"]
    if prob_home_raw < 0.45 and prob_away_raw < 0.45:
        prob_draw_est = max(0.0, 1.0 - prob_home_raw - prob_away_raw)
        if prob_draw_est > 0.30:
            logger.info(
                "generate_signal(%s): señal descartada — alta probabilidad de empate estimada "
                "(p_home=%.2f p_away=%.2f p_draw≈%.2f) [%s vs %s | %s]",
                match_id, prob_home_raw, prob_away_raw, prob_draw_est,
                home_team, away_team, league,
            )
            return []

    # --- 3. Cuotas ---
    odds_data = await fetch_bookmaker_odds(match_id, home_team=str(home_team), away_team=str(away_team), league=league)
    if odds_data is None:
        # Fallback Poisson sintético cuando todas las fuentes externas están inaccesibles.
        # Condiciones:
        #   a) quota manager confirma the_odds_api + oddspapi agotadas (429/cuota), O
        #   b) _THE_ODDS_API_EXHAUSTED=True (401 global en The Odds API) — el flag de
        #      memoria no actualiza el quota manager, por eso se comprueba explícitamente.
        all_sources_down = (
            quota.all_monthly_exhausted(["the_odds_api", "oddspapi"])
            or (_THE_ODDS_API_EXHAUSTED and quota.all_monthly_exhausted(["oddspapi"]))
        )
        if all_sources_down:
            # No crear _synthetic si ya existe predicción plain con odds reales
            try:
                plain_doc = col("predictions").document(match_id).get()
                if plain_doc.exists and plain_doc.to_dict().get("data_source") == "statistical_model":
                    logger.info(
                        "generate_signal(%s): plain ya existe — omitiendo _synthetic", match_id
                    )
                    return []
            except Exception:
                pass  # si falla el check, continúa con el flujo normal
            return await _generate_poisson_signal(
                enriched_match, match_id, str(home_team), str(away_team),
                league, sport, match_date, weights_version, result_home, result_away,
            )
        logger.warning(
            "generate_signal(%s): sin cuotas reales de bookmaker — partido descartado (%s vs %s | %s)",
            match_id, home_team, away_team, league,
        )
        return []

    home_odds = odds_data["home_odds"]
    away_odds = odds_data["away_odds"]

    # --- 3b. Odds movement detection ---
    try:
        from analyzers.line_movement import _detect_odds_movement
        odds_movement = _detect_odds_movement(match_id)
    except Exception:
        odds_movement = {"flag": "NONE", "direction": None, "pct_change_6h": 0.0,
                        "pct_change_24h": 0.0, "timeframe": None, "message": ""}

    # --- 4. Edge para home y away ---
    edge_home = calculate_edge(result_home["prob"], home_odds)
    edge_away = calculate_edge(result_away["prob"], away_odds)

    logger.info(
        "generate_signal(%s): edges — HOME %s p=%.2f @%.2f edge=%.3f | AWAY %s p=%.2f @%.2f edge=%.3f",
        match_id,
        home_team, result_home["prob"], home_odds, edge_home,
        away_team, result_away["prob"], away_odds, edge_away,
    )

    # --- 5. Seleccionar el lado con mayor edge ---
    if edge_home >= edge_away:
        best_edge = edge_home
        best_prob = result_home["prob"]
        best_confidence = result_home["confidence"]
        best_signals = result_home["signals"]
        best_odds = home_odds
        team_to_back = str(home_team)
    else:
        best_edge = edge_away
        best_prob = result_away["prob"]
        best_confidence = result_away["confidence"]
        best_signals = result_away["signals"]
        best_odds = away_odds
        team_to_back = str(away_team)

    # --- 5b. Filtro underdog extremo: umbral dinámico por liga vs rival top-6 ---
    rival_team = str(away_team) if team_to_back == str(home_team) else str(home_team)
    top6_keywords = _TOP6_KEYWORDS.get(league, [])
    underdog_threshold = _EXTREME_UNDERDOG_ODDS.get(league, 5.0)
    if best_odds > underdog_threshold and top6_keywords:
        rival_lower = rival_team.lower()
        if any(kw in rival_lower for kw in top6_keywords):
            logger.info(
                "generate_signal(%s): señal descartada — underdog extremo (odds=%.2f > %.1f) "
                "vs rival top-6 '%s' [%s]",
                match_id, best_odds, underdog_threshold, rival_team, league,
            )
            return []

    # --- 5c. Filtro rival élite (top-3 + posición en tabla) ---
    # Los bookmakers son más eficientes en partidos con equipos top-3 → descuento del 20% en edge.
    # Si además el equipo seleccionado está en bottom-6 → señal directamente descartada.
    top3_keywords = _TOP3_KEYWORDS.get(league, [])
    rival_lower = rival_team.lower()
    rival_is_top3 = top3_keywords and any(kw in rival_lower for kw in top3_keywords)
    if rival_is_top3:
        # Descuento del 20% por eficiencia del mercado en partidos de élite
        best_edge = round(best_edge * 0.80, 4)
        logger.info(
            "generate_signal(%s): rival top-3 '%s' → edge descontado 20%% → edge=%.3f [%s]",
            match_id, rival_team, best_edge, league,
        )
        # Filtro bottom-6: si el equipo seleccionado está en los últimos 6 puestos → descartar
        total_teams = _LEAGUE_TOTAL_TEAMS.get(league, 20)
        bottom6_threshold = total_teams - 5  # posición > (total-5) → bottom 6
        selected_id = (
            enriched_match.get("home_team_id") if team_to_back == str(home_team)
            else enriched_match.get("away_team_id")
        )
        selected_pos = _get_table_position(selected_id, league)
        if selected_pos is not None and selected_pos > bottom6_threshold:
            logger.info(
                "generate_signal(%s): señal descartada — bottom-6 (pos=%d > %d) "
                "vs rival top-3 '%s' [%s]",
                match_id, selected_pos, bottom6_threshold, rival_team, league,
            )
            return []

    # --- 5e. Filtros AWAY anti-sesgo (diagnóstico 2026-04-29: 12.5% acc vs 21.4% HOME) ---
    _lado = "HOME" if team_to_back == str(home_team) else "AWAY"
    if _lado == "AWAY":
        # F1: zona muerta 2.5–3.5 (0% acierto histórico en este rango)
        if 2.5 <= best_odds < 3.5:
            logger.info(
                "generate_signal(%s): AWAY zona muerta descartada (odds=%.2f entre 2.5-3.5) [%s vs %s | %s]",
                match_id, best_odds, home_team, away_team, league,
            )
            return []
        # F2: AWAY en PD/DED con odds > 2.5 — 0% accuracy histórico en ambas ligas
        if league == "PD" and best_odds > 2.5:
            logger.info(
                "generate_signal(%s): AWAY underdog descartada en %s (odds=%.2f > 2.5) [%s vs %s]",
                match_id, league, best_odds, home_team, away_team,
            )
            return []
        # F3: gate final — solo favorito visitante (<2.5) o underdog extremo (>3.5 + conf>0.85)
        if not (best_odds < 2.5 or (best_odds > 3.5 and best_confidence > 0.85)):
            logger.info(
                "generate_signal(%s): AWAY gate descartada "
                "(odds=%.2f conf=%.2f — requiere odds<2.5 o odds>3.5+conf>0.85) [%s vs %s | %s]",
                match_id, best_odds, best_confidence, home_team, away_team, league,
            )
            return []

    # --- 5f. Filtros adicionales underdogs (FIX 1) ---
    if _lado == "AWAY":
        if best_odds > 6.00:
            logger.info(
                "generate_signal(%s): AWAY extremo descartado (odds=%.2f > 6.00) [%s vs %s | %s]",
                match_id, best_odds, home_team, away_team, league,
            )
            return []
        if best_odds > 4.00:
            best_confidence = round(min(best_confidence, 0.70), 4)
    _sel_form = (
        enriched_match.get("home_form_score", 50.0) if team_to_back == str(home_team)
        else enriched_match.get("away_form_score", 50.0)
    ) / 100.0
    _sel_poisson = float(
        (enriched_match.get("poisson_home_win") if team_to_back == str(home_team)
         else enriched_match.get("poisson_away_win")) or 0.5
    )
    if _sel_form < 0.25 and _sel_poisson < 0.20:
        logger.info(
            "generate_signal(%s): descartado — form=%.2f y poisson=%.2f del equipo seleccionado [%s | %s]",
            match_id, _sel_form, _sel_poisson, team_to_back, league,
        )
        return []

    # Aplicar descuento de standings si algún equipo no tiene nada en juego
    if _standings_confidence_adj < 1.0:
        best_confidence = round(best_confidence * _standings_confidence_adj, 4)

    # --- 6. Umbrales de intensidad (FIX 2) ---
    # FUERTE: edge>15% + conf>80% + odds<5.00
    # MODERADA: edge>10% + conf>70% + odds<6.00
    # DETECTADA: edge>_min_edge + conf>65% + odds<4.00 (_min_edge calibrado por liga via backtest)
    _is_fuerte    = best_edge > 0.15 and best_confidence > 0.80 and best_odds < 5.00
    _is_moderada  = best_edge > 0.10 and best_confidence > 0.70 and best_odds < 6.00
    _is_detectada = best_edge > _min_edge and best_confidence > 0.65 and best_odds < 4.00

    if not (_is_fuerte or _is_moderada or _is_detectada):
        logger.debug(
            "generate_signal(%s): descartado — no cumple umbral "
            "(edge=%.1f%% min=%.1f%% conf=%.0f%% odds=%.2f) [%s vs %s | %s]",
            match_id, best_edge * 100, _min_edge * 100, best_confidence * 100, best_odds,
            home_team, away_team, league,
        )
        return []

    _signal_intensity = "🔥" if _is_fuerte else ("✅" if _is_moderada else "📊")

    # Calidad de datos: si es partial, reducir confianza un 10% y re-evaluar tiers
    if data_quality == "partial":
        best_confidence = round(max(0.0, best_confidence * 0.9), 4)
        _is_fuerte    = best_edge > 0.15 and best_confidence > 0.80 and best_odds < 5.00
        _is_moderada  = best_edge > 0.10 and best_confidence > 0.70 and best_odds < 6.00
        _is_detectada = best_edge > 0.08 and best_confidence > 0.65 and best_odds < 4.00
        if not (_is_fuerte or _is_moderada or _is_detectada):
            return []
        _signal_intensity = "🔥" if _is_fuerte else ("✅" if _is_moderada else "📊")

    # --- 7. Contexto externo (lesiones / rotaciones) ---
    external_ctx = await _fetch_external_context(
        str(home_team), str(away_team),
        str(match_date)[:10] if match_date else "",
    )
    if external_ctx["confidence_adj"] < 1.0:
        best_confidence = round(best_confidence * external_ctx["confidence_adj"], 4)
        if best_confidence <= SPORTS_MIN_CONFIDENCE:
            logger.info(
                "generate_signal(%s): descartado por contexto externo (conf ajustada a %.3f) — %s",
                match_id, best_confidence, external_ctx["notes"],
            )
            return []

    kelly = kelly_criterion(best_edge, best_odds)
    results: list[dict] = []

    # Construir factors segun data_source
    if data_source == "statistical_model":
        factors = dict(best_signals)
    else:
        home_form = enriched_match.get("home_form_score", 50.0)
        away_form = enriched_match.get("away_form_score", 50.0)
        h2h_adv = enriched_match.get("h2h_advantage", 0.0)
        factors = {
            "stats_score": round(
                (max(home_form, away_form) / 100.0 * 0.6)
                + (abs(h2h_adv) * 0.4), 4
            ),
            "groq_estimate": best_prob,
        }

    prediction = {
        "match_id": match_id,
        "home_team": str(home_team),
        "away_team": str(away_team),
        "sport": sport,
        "league": league,
        "market_type": "h2h",
        "data_source": data_source,
        "match_date": match_date,
        "team_to_back": team_to_back,
        "bookmaker": odds_data["bookmaker"],
        "odds": best_odds,
        "calculated_prob": best_prob,
        "edge": best_edge,
        "confidence": best_confidence,
        "kelly_fraction": kelly,
        "factors": factors,
        "signals": best_signals,
        "elo_sufficient": elo_sufficient,
        "h2h_sufficient": enriched_match.get("h2h_sufficient", True),
        "odds_source": odds_data.get("source", "theoddsapi"),
        "weights_version": weights_version,
        "created_at": datetime.now(timezone.utc),
        "result": None,
        "correct": None,
        "error_type": None,
        "external_context": external_ctx["notes"],
        "odds_movement": odds_movement,
        "intensity": _signal_intensity,
    }

    # --- Guardar en Firestore predictions ---
    try:
        col("predictions").document(match_id).set(prediction)
        logger.info(
            "generate_signal(%s): %s @ %.2f | edge=%.1f%% conf=%.0f%% kelly=%.1f%% | odds_movement=%s",
            match_id, team_to_back, best_odds,
            best_edge * 100, best_confidence * 100, kelly * 100,
            odds_movement.get("flag", "NONE"),
        )
    except Exception:
        logger.error("generate_signal(%s): error guardando prediction", match_id, exc_info=True)

    # --- Alerta Telegram si edge alto ---
    if best_edge > SPORTS_ALERT_EDGE:
        actually_sent = await _send_telegram_alert(_build_alert_payload(prediction, enriched_match))
        if actually_sent:
            try:
                col("predictions").document(match_id).update({"alerted": True})
            except Exception:
                logger.error("generate_signal(%s): error marcando alerted=True", match_id, exc_info=True)

    results.append(prediction)

    # --- Señal de totals (solo fútbol con modelo Poisson) ---
    sport_key = _ODDS_SPORT_MAP.get(league, "")
    if sport_key in _FOOTBALL_SPORT_KEYS:
        totals_probs = _calculate_totals_prob(enriched_match)
        if totals_probs:
            totals_odds = None
            # Buscar totals en el evento cacheado de The Odds API
            cached_league = _LEAGUE_ODDS_CACHE.get(sport_key)
            if cached_league:
                _, events = cached_league
                for ev in events:
                    ah = ev.get("home_team", "")
                    aa = ev.get("away_team", "")
                    if _teams_match(str(home_team), ah) and _teams_match(str(away_team), aa):
                        totals_odds = _parse_totals_event(ev)
                        break

            if totals_odds:
                line = totals_odds["line"]
                over_p = totals_probs["over_prob"]
                under_p = totals_probs["under_prob"]
                over_edge = calculate_edge(over_p, totals_odds["over_odds"])
                under_edge = calculate_edge(under_p, totals_odds["under_odds"])

                if over_edge >= under_edge and over_edge > _min_edge:
                    sel, sel_prob, sel_odds, sel_edge = "Over", over_p, totals_odds["over_odds"], over_edge
                elif under_edge > over_edge and under_edge > _min_edge:
                    sel, sel_prob, sel_odds, sel_edge = "Under", under_p, totals_odds["under_odds"], under_edge
                else:
                    sel = None

                if sel:
                    sel_confidence = max(0.0, 1.0 - abs(over_p - 0.5) * 2) if sel == "Over" else max(0.0, 1.0 - abs(under_p - 0.5) * 2)
                    sel_confidence = round(max(0.0, min(1.0, sel_confidence)), 4)
                    if sel_confidence > SPORTS_MIN_CONFIDENCE:
                        sel_kelly = kelly_criterion(sel_edge, sel_odds)
                        totals_pred = {
                            "match_id": f"{match_id}_totals",
                            "home_team": str(home_team),
                            "away_team": str(away_team),
                            "sport": sport,
                            "league": league,
                            "market_type": "totals",
                            "selection": f"{sel} {line}",
                            "line": line,
                            "bookmaker": totals_odds["bookmaker"],
                            "odds": sel_odds,
                            "calculated_prob": sel_prob,
                            "edge": sel_edge,
                            "confidence": sel_confidence,
                            "kelly_fraction": sel_kelly,
                            "factors": {
                                "expected_total": totals_probs["expected_total"],
                                "home_xg": enriched_match.get("home_xg"),
                                "away_xg": enriched_match.get("away_xg"),
                            },
                            "signals": {},
                            "data_source": "poisson_totals",
                            "match_date": match_date,
                            "weights_version": weights_version,
                            "created_at": datetime.now(timezone.utc),
                            "result": None,
                            "correct": None,
                            "error_type": None,
                        }
                        try:
                            col("predictions").document(f"{match_id}_totals").set(totals_pred)
                            logger.info(
                                "generate_signal(%s): %s %.1f @ %.2f | edge=%.1f%% conf=%.0f%%",
                                match_id, sel, line, sel_odds, sel_edge * 100, sel_confidence * 100,
                            )
                        except Exception:
                            logger.error("generate_signal(%s): error guardando totals prediction", match_id, exc_info=True)

                        if sel_edge > SPORTS_ALERT_EDGE:
                            totals_payload = {**totals_pred, "match_date": str(totals_pred.get("match_date", "")[:16] if totals_pred.get("match_date") else "?")}
                            actually_sent = await _send_telegram_alert(totals_payload)
                            if actually_sent:
                                try:
                                    col("predictions").document(f"{match_id}_totals").update({"alerted": True})
                                except Exception:
                                    pass

                        results.append(totals_pred)

    # --- Señales BTTS, OU 2.5, AH -0.5 desde odds-api.io all_markets ---
    if sport_key in _FOOTBALL_SPORT_KEYS:
        _odds_src = odds_data.get("source") if odds_data else None
        if _odds_src != "oddsapiio":
            logger.info(
                "EXTRA_MARKETS_SKIP(%s): source=%s (solo oddsapiio tiene all_markets) "
                "— BTTS/OU/AH no disponibles para este partido",
                match_id, _odds_src,
            )
        elif odds_data:
            _all_mkt = odds_data.get("all_markets", {})
            logger.info(
                "EXTRA_MARKETS_CHECK(%s): source=oddsapiio all_markets_keys=%s",
                match_id, list(_all_mkt.keys()),
            )
            if not _all_mkt:
                logger.info(
                    "EXTRA_MARKETS_SKIP(%s): all_markets vacío — "
                    "odds-api.io no devolvió BTTS/OU/AH para este partido",
                    match_id,
                )
            else:
                try:
                    extra_oaio = await _generate_oddsapiio_extra_signals(
                        enriched_match, _all_mkt, match_id,
                        str(home_team), str(away_team),
                        league, sport, match_date, weights_version,
                    )
                    results.extend(extra_oaio)
                except Exception:
                    logger.error("generate_signal(%s): error en oddsapiio extra signals", match_id, exc_info=True)

    # --- Señales extra de fútbol (BTTS, Double Chance, AH, Totals 3.5) ---
    if sport_key in _FOOTBALL_SPORT_KEYS:
        try:
            from analyzers.football_markets import generate_football_extra_signals
            cached_league = _LEAGUE_ODDS_CACHE.get(sport_key)
            cached_events = cached_league[1] if cached_league else []
            extra = await generate_football_extra_signals(
                enriched_match, cached_events,
                str(home_team), str(away_team),
                league, match_id, match_date, weights_version,
            )
            results.extend(extra)
        except Exception:
            logger.error("generate_signal(%s): error en football_markets", match_id, exc_info=True)

    return results


async def _generate_poisson_signal(
    enriched_match: dict,
    match_id: str,
    home_team: str,
    away_team: str,
    league: str,
    sport: str,
    match_date,
    weights_version: int,
    result_home: dict,
    result_away: dict,
) -> list[dict]:
    """
    Fallback cuando TODAS las APIs de odds están agotadas.
    Genera señal basada únicamente en el modelo Poisson propio.

    Sin bookmaker odds no hay edge real calculable, pero el modelo puede
    identificar un favorito claro con alta confianza. La señal se marca como
    'poisson_synthetic' y 'validated: False' para que el usuario sepa que
    NO es una value bet verificada contra cuotas reales.

    Threshold más alto que el normal: confianza > 0.72 y probabilidad > 0.62.
    """
    SYNTHETIC_MIN_CONFIDENCE = 0.72
    SYNTHETIC_MIN_PROB = 0.62

    prob_home = result_home["prob"]
    prob_away = result_away["prob"]
    conf_home = result_home["confidence"]
    conf_away = result_away["confidence"]

    # Elegir el lado más fuerte
    if prob_home >= prob_away:
        best_prob, best_conf, team_to_back = prob_home, conf_home, home_team
    else:
        best_prob, best_conf, team_to_back = prob_away, conf_away, away_team

    if best_conf < SYNTHETIC_MIN_CONFIDENCE or best_prob < SYNTHETIC_MIN_PROB:
        return []

    # Cuota sintética: 1/prob sin vig (no representa precio de mercado real)
    synthetic_odds = round(1.0 / max(best_prob, 0.01), 2)

    prediction = {
        "match_id": match_id,
        "home_team": home_team,
        "away_team": away_team,
        "sport": sport,
        "league": league,
        "market_type": "h2h",
        "data_source": "poisson_only",
        "odds_source": "poisson_synthetic",
        "validated": False,
        "match_date": match_date,
        "team_to_back": team_to_back,
        "bookmaker": None,
        "odds": synthetic_odds,
        "calculated_prob": round(best_prob, 4),
        "edge": None,
        "confidence": round(best_conf, 4),
        "kelly_fraction": 0.0,
        "factors": result_home["signals"] if prob_home >= prob_away else result_away["signals"],
        "signals": {},
        "weights_version": weights_version,
        "created_at": datetime.now(timezone.utc),
        "result": None,
        "correct": None,
        "error_type": None,
    }

    try:
        col("predictions").document(f"{match_id}_synthetic").set(prediction)
    except Exception:
        logger.error("_generate_poisson_signal(%s): error guardando en Firestore", match_id, exc_info=True)

    logger.info(
        "generate_signal(%s): POISSON_SYNTHETIC — %s p=%.2f conf=%.0f%% (sin odds externas)",
        match_id, team_to_back, best_prob, best_conf * 100,
    )

    # Alerta Telegram con formato diferenciado
    await _send_telegram_alert({
        **prediction,
        "market_emoji": "📊",
        "intensity": "📊",
        "match_date": str(match_date)[:16] if match_date else "?",
        "_synthetic_warning": "⚠️ Sin validación de bookmaker — modelo Poisson propio",
        "poisson": result_home["signals"].get("poisson"),
        "elo": result_home["signals"].get("elo"),
        "form": result_home["signals"].get("form"),
        "h2h": result_home["signals"].get("h2h"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    return [prediction]


def _get_weights_version() -> int:
    """Lee la version actual de model_weights. Devuelve 0 si no existe."""
    try:
        doc = col("model_weights").document("current").get()
        if doc.exists:
            return int(doc.to_dict().get("version", 0))
    except Exception:
        pass
    return 0


_MARKET_EMOJI: dict[str, str] = {
    "h2h": "⚽", "totals_2.5": "📊", "totals_1.5": "📊", "totals_3.5": "📊",
    "totals_4.5": "📊", "btts": "🔄", "double_chance": "🎯",
    "asian_handicap": "📐", "result_and_goals": "🔢", "draw_no_bet": "🚫",
    "european_handicap": "📏", "ht_totals_0.5": "⏱️", "ht_totals_1.5": "⏱️",
    "ht_ft": "⏱️", "home_team_goals": "⚽", "away_team_goals": "⚽",
    "first_scorer": "🥅", "anytime_scorer": "🥅", "anytime_assist": "🎯",
    "corners_1x2": "📐", "bookings_1x2": "🟨", "tennis_total_games": "🎾",
    "tennis_game_handicap": "🎾", "basketball_h1_spread": "🏀",
    "basketball_h1_totals": "🏀", "basketball_q1_totals": "🏀",
    "set_handicap": "🎾", "total_sets": "🎾", "spread": "🏀", "totals": "🏀",
}


def _intensity_emoji(edge: float) -> str:
    if edge > 0.15: return "🔥"
    if edge > 0.08: return "✅"
    return "📊"


def _build_alert_payload(prediction: dict, enriched_match: dict) -> dict:
    """Construye el payload de alerta con los campos del formato Telegram."""
    signals = prediction.get("signals", prediction.get("factors", {}))
    market  = prediction.get("market_type", prediction.get("market", "h2h"))
    edge    = float(prediction.get("edge", 0))
    return {
        **prediction,
        "home_team":    prediction.get("home_team", ""),
        "away_team":    prediction.get("away_team", ""),
        "match_date":   str(prediction.get("match_date", ""))[:16],
        "sport":        prediction.get("sport", "football"),
        "market_type":  market,
        "market_emoji": prediction.get("market_emoji") or _MARKET_EMOJI.get(market, "📊"),
        "intensity":    prediction.get("intensity") or _intensity_emoji(edge),
        "poisson":      signals.get("poisson"),
        "elo":          signals.get("elo"),
        "form":         signals.get("form"),
        "h2h":          signals.get("h2h"),
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }
