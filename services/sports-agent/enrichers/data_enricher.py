"""
Orquestador de enrichers — combina Poisson, ELO, forma, H2H y cuotas.
Lee upcoming_matches de Firestore y escribe enriched_matches completos.

Flujo por partido:
  1. team_stats (home + away) → form_score, streak, raw_matches
  2. raw_matches → fit_attack_defense → predict_match_probs (Poisson — solo futbol)
  3. team_elo → elo_win_probability (solo futbol)
  4. h2h_data → h2h_advantage (corrigiendo perspectiva segun canonical pair_key)
  5. odds_cache → odds_opening, odds_current, odds_movement
  6. Escribe enriched_matches en Firestore
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from shared.config import MIN_MATCHES_TO_FIT, SUPPORTED_FOOTBALL_LEAGUES
from shared.firestore_client import col

from enrichers.poisson_model import fit_attack_defense, predict_match_probs
from enrichers.elo_rating import elo_win_probability, get_team_elo
from collectors.odds_movement import get_odds_movement

logger = logging.getLogger(__name__)

# Ligas de futbol conocidas (para deteccion automatica de deporte)
_FOOTBALL_LEAGUE_CODES = set(SUPPORTED_FOOTBALL_LEAGUES.keys())

# Mapping de palabras clave en league → sport
_LEAGUE_SPORT_MAP = {
    "NBA": "nba",
    "NFL": "nfl",
    "MLB": "mlb",
    "NHL": "nhl",
    "UFC": "mma",
    "MMA": "mma",
    "ATP": "tennis",
    "WTA": "tennis",
    "TENNIS": "tennis",
}


def _detect_sport(match: dict) -> str:
    """Detecta el deporte del partido a partir del campo league."""
    league = match.get("league", "")
    if league in _FOOTBALL_LEAGUE_CODES:
        return "football"
    league_upper = league.upper()
    for keyword, sport in _LEAGUE_SPORT_MAP.items():
        if keyword in league_upper:
            return sport
    # Si no se reconoce, asumir futbol por defecto
    return "football"


async def enrich_match(match: dict) -> dict:
    """
    Orquesta todos los enrichers para un partido.
    Input: documento de upcoming_matches.
    Output: enriched_match guardado en Firestore coleccion enriched_matches.

    Para futbol: Poisson bivariado + ELO + forma + H2H + cuotas.
    Para otros deportes: forma + H2H + cuotas (sin Poisson ni ELO).
    """
    match_id = str(match.get("match_id", ""))
    home_id: int | None = match.get("home_team_id")
    away_id: int | None = match.get("away_team_id")
    sport = match.get("sport") or _detect_sport(match)

    is_football = sport == "football"
    data_quality = "full"

    logger.debug("enrich_match: %s (%s) — %s vs %s", match_id, sport, home_id, away_id)

    # --- 1-4. Reads en paralelo: team_stats ×2 + h2h_data + odds_cache ---
    canonical_t1 = min(home_id, away_id) if home_id and away_id else 0
    canonical_t2 = max(home_id, away_id) if home_id and away_id else 0
    pair_key = f"{canonical_t1}_{canonical_t2}"

    loop = asyncio.get_event_loop()

    def _get(collection: str, doc_id: str):
        return col(collection).document(doc_id).get()

    try:
        home_doc, away_doc, h2h_doc, odds_doc = await asyncio.gather(
            loop.run_in_executor(None, _get, "team_stats", str(home_id) if home_id else ""),
            loop.run_in_executor(None, _get, "team_stats", str(away_id) if away_id else ""),
            loop.run_in_executor(None, _get, "h2h_data", pair_key),
            loop.run_in_executor(None, _get, "odds_cache", match_id),
        )
    except Exception:
        logger.error("enrich_match(%s): error en reads paralelos", match_id, exc_info=True)
        home_doc = away_doc = h2h_doc = odds_doc = None

    # --- 1. team_stats ---
    home_stats: dict = {}
    away_stats: dict = {}

    if home_doc is not None:
        if home_doc.exists:
            home_stats = home_doc.to_dict() or {}
        elif home_id:
            logger.warning("enrich_match(%s): sin team_stats para home_id=%d", match_id, home_id)
            data_quality = "partial"

    if away_doc is not None:
        if away_doc.exists:
            away_stats = away_doc.to_dict() or {}
        elif away_id:
            logger.warning("enrich_match(%s): sin team_stats para away_id=%d", match_id, away_id)
            data_quality = "partial"

    # --- 2. Form score y racha ---
    home_form_score = float(home_stats.get("form_score", 50.0))
    away_form_score = float(away_stats.get("form_score", 50.0))
    home_streak = home_stats.get("streak", {"type": "draw", "count": 0})
    away_streak = away_stats.get("streak", {"type": "draw", "count": 0})

    # --- 3. H2H advantage ---
    h2h_advantage = 0.0
    h2h_sufficient = False  # True solo si hay partidos H2H reales almacenados
    if h2h_doc is not None and h2h_doc.exists and home_id and away_id:
        try:
            h2h_doc_data = h2h_doc.to_dict()
            stored_advantage = h2h_doc_data.get("h2h_advantage", 0.0)
            h2h_matches_stored = h2h_doc_data.get("matches", [])
            h2h_sufficient = len(h2h_matches_stored) > 0
            h2h_advantage = float(stored_advantage) if home_id == canonical_t1 else -float(stored_advantage)
            if not h2h_sufficient:
                logger.debug(
                    "enrich_match(%s): h2h_data existe pero sin partidos reales — h2h_insufficient",
                    match_id,
                )
        except Exception:
            logger.error("enrich_match(%s): error procesando h2h_data", match_id, exc_info=True)
            data_quality = "partial"

    # --- 4. Cuotas desde odds_cache ---
    odds_opening: dict = {}
    odds_current: dict = {}

    if odds_doc is not None and odds_doc.exists:
        try:
            od = odds_doc.to_dict()
            home_c = od.get("home_odds")
            draw_c = od.get("draw_odds")
            away_c = od.get("away_odds")
            home_o = od.get("opening_home_odds")
            draw_o = od.get("opening_draw_odds")
            away_o = od.get("opening_away_odds")
            if home_c and away_c:
                odds_opening = {
                    "home": float(home_o or home_c),
                    "draw": float(draw_o or draw_c or 3.2),
                    "away": float(away_o or away_c),
                }
                odds_current = {
                    "home": float(home_c),
                    "draw": float(draw_c or 3.2),
                    "away": float(away_c),
                }
            else:
                data_quality = "partial"
        except Exception:
            logger.error("enrich_match(%s): error procesando odds_cache", match_id, exc_info=True)
            data_quality = "partial"
    else:
        data_quality = "partial"

    # --- 5. Movimiento de cuotas (derivado del odds_doc ya leído — sin read adicional) ---
    odds_movement = 0.0
    try:
        if odds_doc is not None and odds_doc.exists:
            od = odds_doc.to_dict()
            home_odds = od.get("home_odds")
            opening_home_odds = od.get("opening_home_odds")
            if home_odds and opening_home_odds and opening_home_odds != 0:
                odds_movement = round((home_odds - opening_home_odds) / opening_home_odds, 4)
    except Exception:
        logger.error("enrich_match(%s): error calculando odds_movement", match_id, exc_info=True)

    # --- 6. Poisson + ELO (solo futbol) ---
    poisson_home_win: float | None = None
    poisson_draw: float | None = None
    poisson_away_win: float | None = None
    home_xg: float | None = None
    away_xg: float | None = None
    elo_home_win_prob: float | None = None
    home_elo: float | None = None
    away_elo: float | None = None

    if is_football and home_id and away_id:
        # Poisson bivariado — solo si ambos equipos tienen datos reales suficientes
        try:
            home_raw = home_stats.get("raw_matches", [])
            away_raw = away_stats.get("raw_matches", [])
            home_raw_count = len(home_raw)
            away_raw_count = len(away_raw)

            if home_raw_count < MIN_MATCHES_TO_FIT or away_raw_count < MIN_MATCHES_TO_FIT:
                logger.warning(
                    "enrich_match(%s): datos insuficientes para Poisson "
                    "(home=%d away=%d raw_matches, minimo=%d) — Poisson omitido",
                    match_id, home_raw_count, away_raw_count, MIN_MATCHES_TO_FIT,
                )
                data_quality = "partial"
            else:
                all_raw = home_raw + away_raw
                team_params = fit_attack_defense(all_raw)
                probs = predict_match_probs(home_id, away_id, team_params)
                poisson_home_win = probs["home_win"]
                poisson_draw = probs["draw"]
                poisson_away_win = probs["away_win"]
                home_xg = probs["home_xg"]
                away_xg = probs["away_xg"]
        except Exception:
            logger.error(
                "enrich_match(%s): error en modelo Poisson", match_id, exc_info=True
            )
            data_quality = "partial"

        # ELO
        try:
            elo_home_win_prob = elo_win_probability(home_id, away_id)
            home_elo = get_team_elo(home_id)
            away_elo = get_team_elo(away_id)
        except Exception:
            logger.error(
                "enrich_match(%s): error en ELO", match_id, exc_info=True
            )
            data_quality = "partial"

    # --- 7. Construir documento enriched_match ---
    enriched: dict = {
        "match_id": match_id,
        "sport": sport,
        "home_team_id": home_id,
        "away_team_id": away_id,
        # Nombres e info del partido — copiados desde upcoming_matches
        "home_team": match.get("home_team", match.get("home_team_name", "")),
        "away_team": match.get("away_team", match.get("away_team_name", "")),
        "league": match.get("league", ""),
        "match_date": match.get("match_date", match.get("date")),
        # Campos futbol (None para otros deportes)
        "poisson_home_win": poisson_home_win,
        "poisson_draw": poisson_draw,
        "poisson_away_win": poisson_away_win,
        "home_xg": home_xg,
        "away_xg": away_xg,
        "elo_home_win_prob": elo_home_win_prob,
        "home_elo": home_elo,
        "away_elo": away_elo,
        # Campos todos los deportes
        "home_form_score": home_form_score,
        "away_form_score": away_form_score,
        "h2h_advantage": h2h_advantage,
        "h2h_sufficient": h2h_sufficient,
        "home_streak": home_streak,
        "away_streak": away_streak,
        "odds_opening": odds_opening,
        "odds_current": odds_current,
        "odds_movement": odds_movement,
        "data_quality": data_quality,
        "enriched_at": datetime.now(timezone.utc),
    }

    # --- 8. Guardar en Firestore ---
    try:
        col("enriched_matches").document(match_id).set(enriched)
        logger.info(
            "enrich_match(%s) [%s]: quality=%s poisson=%.3f elo=%.3f form=%.1f/%.1f",
            match_id, sport, data_quality,
            poisson_home_win or -1.0,
            elo_home_win_prob or -1.0,
            home_form_score, away_form_score,
        )
    except Exception:
        logger.error(
            "enrich_match(%s): error guardando en enriched_matches", match_id, exc_info=True
        )

    return enriched


async def run_enrichment() -> int:
    """
    Identifica partidos sin enriquecer (o con enriquecimiento antiguo) y los procesa.

    Logica:
    1. Lee todos los upcoming_matches con status == "SCHEDULED"
    2. Para cada match_id busca doc en enriched_matches
    3. Si NO existe → enrich_match()
    4. Si existe pero enriched_at < now - 6h → re-enriquece (cuotas pueden haber cambiado)

    Devuelve numero de partidos enriquecidos en esta ejecucion.
    """
    # football-data.org devuelve "TIMED" para partidos programados, no "SCHEDULED"
    try:
        scheduled_matches = list(col("upcoming_matches").where("status", "==", "SCHEDULED").stream())
        timed_matches = list(col("upcoming_matches").where("status", "==", "TIMED").stream())
        scheduled_matches = scheduled_matches + timed_matches
    except Exception:
        logger.error("run_enrichment: error leyendo upcoming_matches", exc_info=True)
        return 0

    if not scheduled_matches:
        logger.info("run_enrichment: sin partidos SCHEDULED/TIMED que enriquecer")
        return 0

    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(hours=6)
    enriched_count = 0

    logger.info("run_enrichment: evaluando %d partidos SCHEDULED", len(scheduled_matches))

    for match_doc in scheduled_matches:
        match = match_doc.to_dict()
        match_id = str(match.get("match_id", ""))

        if not match_id:
            continue

        # Verificar si ya existe un enriched reciente
        should_enrich = True
        try:
            enriched_doc = col("enriched_matches").document(match_id).get()
            if enriched_doc.exists:
                enriched_at = enriched_doc.to_dict().get("enriched_at")
                if enriched_at is not None:
                    # Comparar como datetimes aware
                    if hasattr(enriched_at, "tzinfo") and enriched_at.tzinfo is None:
                        from datetime import timezone as tz
                        enriched_at = enriched_at.replace(tzinfo=tz.utc)
                    if enriched_at > stale_threshold:
                        should_enrich = False
        except Exception:
            logger.error(
                "run_enrichment: error verificando enriched_matches[%s]",
                match_id, exc_info=True,
            )
            # Si hay error al verificar, intentar enriquecer de todas formas

        if not should_enrich:
            logger.debug("run_enrichment: %s ya enriquecido recientemente — omitiendo", match_id)
            continue

        try:
            await enrich_match(match)
            enriched_count += 1
        except Exception:
            logger.error(
                "run_enrichment: error enriqueciendo partido %s", match_id, exc_info=True
            )

    logger.info("run_enrichment: %d partidos enriquecidos", enriched_count)
    return enriched_count
