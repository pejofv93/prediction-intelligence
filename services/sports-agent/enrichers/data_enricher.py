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
import logging
from datetime import datetime, timedelta, timezone

from shared.config import SUPPORTED_FOOTBALL_LEAGUES
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

    # --- 1. Cargar team_stats de Firestore ---
    home_stats: dict = {}
    away_stats: dict = {}

    if home_id:
        try:
            doc = col("team_stats").document(str(home_id)).get()
            if doc.exists:
                home_stats = doc.to_dict() or {}
            else:
                logger.warning("enrich_match(%s): sin team_stats para home_id=%d", match_id, home_id)
                data_quality = "partial"
        except Exception:
            logger.error("enrich_match(%s): error leyendo home team_stats", match_id, exc_info=True)
            data_quality = "partial"

    if away_id:
        try:
            doc = col("team_stats").document(str(away_id)).get()
            if doc.exists:
                away_stats = doc.to_dict() or {}
            else:
                logger.warning("enrich_match(%s): sin team_stats para away_id=%d", match_id, away_id)
                data_quality = "partial"
        except Exception:
            logger.error("enrich_match(%s): error leyendo away team_stats", match_id, exc_info=True)
            data_quality = "partial"

    # --- 2. Form score y racha ---
    home_form_score = float(home_stats.get("form_score", 50.0))
    away_form_score = float(away_stats.get("form_score", 50.0))
    home_streak = home_stats.get("streak", {"type": "draw", "count": 0})
    away_streak = away_stats.get("streak", {"type": "draw", "count": 0})

    # --- 3. H2H advantage ---
    h2h_advantage = 0.0
    if home_id and away_id:
        try:
            canonical_t1 = min(home_id, away_id)
            canonical_t2 = max(home_id, away_id)
            pair_key = f"{canonical_t1}_{canonical_t2}"

            doc = col("h2h_data").document(pair_key).get()
            if doc.exists:
                stored_advantage = doc.to_dict().get("h2h_advantage", 0.0)
                # h2h_advantage almacenado es relativo a canonical_t1 (equipo con menor ID)
                # Si el local tiene mayor ID → la ventaja es del visitante → invertir
                if home_id == canonical_t1:
                    h2h_advantage = float(stored_advantage)
                else:
                    h2h_advantage = -float(stored_advantage)
        except Exception:
            logger.error("enrich_match(%s): error leyendo h2h_data", match_id, exc_info=True)
            data_quality = "partial"

    # --- 4. Cuotas desde odds_cache ---
    # Vacío si no hay datos reales — generate_signal omitirá el partido sin cuotas reales.
    odds_opening: dict = {}
    odds_current: dict = {}

    try:
        odds_doc = col("odds_cache").document(match_id).get()
        if odds_doc.exists:
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
        else:
            data_quality = "partial"
    except Exception:
        logger.error("enrich_match(%s): error leyendo odds_cache", match_id, exc_info=True)
        data_quality = "partial"

    # --- 5. Movimiento de cuotas ---
    odds_movement = 0.0
    try:
        odds_movement = await get_odds_movement(match_id)
    except Exception:
        logger.error("enrich_match(%s): error en odds_movement", match_id, exc_info=True)

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
        # Poisson bivariado
        try:
            home_raw = home_stats.get("raw_matches", [])
            away_raw = away_stats.get("raw_matches", [])
            all_raw = home_raw + away_raw

            if all_raw:
                team_params = fit_attack_defense(all_raw)
                probs = predict_match_probs(home_id, away_id, team_params)
                poisson_home_win = probs["home_win"]
                poisson_draw = probs["draw"]
                poisson_away_win = probs["away_win"]
                home_xg = probs["home_xg"]
                away_xg = probs["away_xg"]
            else:
                logger.warning(
                    "enrich_match(%s): sin raw_matches para Poisson — marca partial",
                    match_id,
                )
                data_quality = "partial"
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
