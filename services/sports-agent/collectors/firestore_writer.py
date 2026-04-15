"""
Escritor Firestore para collectors.
Persistencia de upcoming_matches, team_stats y h2h_data.
OBLIGATORIO: save_team_stats guarda raw_matches para que Poisson funcione en Session 3.
"""
import asyncio
import logging
from datetime import datetime, timezone

from shared.firestore_client import col
from shared.config import SUPPORTED_FOOTBALL_LEAGUES

from collectors.stats_processor import (
    build_results_list,
    calculate_form_score,
    calculate_h2h_advantage,
    calculate_home_away_split,
    calculate_xg_proxy,
    detect_streak,
)

logger = logging.getLogger(__name__)

_FOOTBALL_LEAGUE_CODES = set(SUPPORTED_FOOTBALL_LEAGUES.keys())


async def _firestore_set(
    collection_name: str, doc_id: str, data: dict, retries: int = 3
) -> bool:
    """
    Guarda un documento en Firestore con retry exponencial.
    Devuelve True si exito, False si falla tras todos los reintentos.
    """
    for attempt in range(retries):
        try:
            col(collection_name).document(doc_id).set(data)
            return True
        except Exception as e:
            wait = 2 ** attempt  # 1s, 2s, 4s
            if attempt < retries - 1:
                logger.warning(
                    "Firestore error en %s/%s (intento %d) — reintentando en %ds: %s",
                    collection_name, doc_id, attempt + 1, wait, e,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Firestore fallo definitivo en %s/%s tras %d intentos: %s",
                    collection_name, doc_id, retries, e,
                    exc_info=True,
                )
    return False


async def save_upcoming_matches(matches: list[dict]) -> None:
    """
    Guarda lista de upcoming_matches en Firestore. Doc ID = match_id.
    Solo sobreescribe partidos SCHEDULED — no toca los que ya estan FINISHED o LIVE.
    """
    if not matches:
        logger.info("save_upcoming_matches: lista vacia, nada que guardar")
        return

    now = datetime.now(timezone.utc)
    saved = 0

    for m in matches:
        match_id = m.get("match_id")
        if not match_id:
            continue

        doc = {
            "match_id": match_id,
            "home_team": m.get("home_team_name", m.get("home_team", "")),
            "away_team": m.get("away_team_name", m.get("away_team", "")),
            "home_team_id": m.get("home_team_id"),
            "away_team_id": m.get("away_team_id"),
            "league": m.get("league", ""),
            "match_date": m.get("date", m.get("match_date", "")),
            "status": m.get("status", "SCHEDULED"),
            "collected_at": now,
        }

        ok = await _firestore_set("upcoming_matches", match_id, doc)
        if ok:
            saved += 1

    logger.info("save_upcoming_matches: %d/%d partidos guardados", saved, len(matches))


async def save_team_stats(team_id: int, raw_api_matches: list[dict]) -> None:
    """
    Procesa raw_api_matches y guarda en Firestore coleccion team_stats.
    Calcula: last_10, form_score, home_stats, away_stats, streak, xg_per_game.
    IMPRESCINDIBLE: raw_matches guardado para que Poisson funcione en Session 3.

    raw_api_matches: lista de partidos normalizados del formato interno:
      {match_id, date, home_team_id, away_team_id, home_team_name, away_team_name,
       goals_home, goals_away, league, ...}
    """
    if not raw_api_matches:
        logger.warning("save_team_stats(%d): sin datos de partidos — usando defaults", team_id)
        # Guardar con datos neutrales y marcar como partial
        doc = _build_empty_team_stats(team_id)
        await _firestore_set("team_stats", str(team_id), doc)
        return

    # Determinar nombre y liga del equipo (del partido mas reciente)
    team_name = _extract_team_name(team_id, raw_api_matches)
    league = _extract_league(raw_api_matches)

    # Construir lista W/D/L (mas reciente primero)
    results = build_results_list(raw_api_matches, team_id)
    last_10 = results[:10]

    # Stats calculadas
    form_score = calculate_form_score(last_10)
    home_stats, away_stats = calculate_home_away_split(raw_api_matches, team_id)
    streak = detect_streak(last_10)

    # xG proxy — requiere datos de tiros (rara vez disponibles en free tier)
    # Preparar matches con goals_scored relativo al equipo para xg_proxy
    matches_for_xg = _build_xg_matches(team_id, raw_api_matches)
    xg_per_game = calculate_xg_proxy(matches_for_xg)

    # raw_matches para modelo Poisson — formato exacto requerido por poisson_model.py
    raw_matches_poisson = [
        {
            "match_id": m["match_id"],
            "date": m["date"],
            "home_team_id": m["home_team_id"],
            "away_team_id": m["away_team_id"],
            "goals_home": m.get("goals_home") or 0,
            "goals_away": m.get("goals_away") or 0,
            "was_home": m["home_team_id"] == team_id,
        }
        for m in raw_api_matches
        if m.get("goals_home") is not None and m.get("goals_away") is not None
    ]

    doc = {
        "team_id": team_id,
        "team_name": team_name,
        "league": league,
        "last_10": last_10,
        "form_score": form_score,
        "home_stats": home_stats,
        "away_stats": away_stats,
        "streak": streak,
        "xg_per_game": xg_per_game,
        "raw_matches": raw_matches_poisson,
        "updated_at": datetime.now(timezone.utc),
    }

    ok = await _firestore_set("team_stats", str(team_id), doc)
    if ok:
        logger.info(
            "save_team_stats(%d) %s: form=%.1f streak=%s×%d xg=%.2f",
            team_id, team_name, form_score,
            streak["type"], streak["count"], xg_per_game,
        )


async def save_h2h(
    team1_id: int, team2_id: int, h2h_matches: list[dict]
) -> None:
    """
    Guarda h2h_data en Firestore.
    pair_key = f"{min(t1,t2)}_{max(t1,t2)}"
    h2h_advantage desde perspectiva del equipo con menor ID (= team1 canonico).
    """
    canonical_t1 = min(team1_id, team2_id)
    canonical_t2 = max(team1_id, team2_id)
    pair_key = f"{canonical_t1}_{canonical_t2}"

    if not h2h_matches:
        logger.info("save_h2h(%d, %d): sin datos H2H", team1_id, team2_id)
        doc = {
            "pair_key": pair_key,
            "team1_id": canonical_t1,
            "team2_id": canonical_t2,
            "matches": [],
            "team1_wins": 0,
            "team2_wins": 0,
            "draws": 0,
            "h2h_advantage": 0.0,
            "updated_at": datetime.now(timezone.utc),
        }
        await _firestore_set("h2h_data", pair_key, doc)
        return

    # Contar wins/losses/draws desde perspectiva del equipo canonical_t1
    t1_wins = t2_wins = draws = 0
    for m in h2h_matches:
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        gh = m.get("goals_home") or 0
        ga = m.get("goals_away") or 0

        if home_id == canonical_t1:
            gf, gc = gh, ga
        elif away_id == canonical_t1:
            gf, gc = ga, gh
        else:
            continue

        if gf > gc:
            t1_wins += 1
        elif gf < gc:
            t2_wins += 1
        else:
            draws += 1

    total = t1_wins + t2_wins + draws
    h2h_advantage = (t1_wins - t2_wins) / total if total > 0 else 0.0

    doc = {
        "pair_key": pair_key,
        "team1_id": canonical_t1,
        "team2_id": canonical_t2,
        "matches": h2h_matches[:10],  # max 10 partidos en Firestore
        "team1_wins": t1_wins,
        "team2_wins": t2_wins,
        "draws": draws,
        "h2h_advantage": h2h_advantage,
        "updated_at": datetime.now(timezone.utc),
    }

    ok = await _firestore_set("h2h_data", pair_key, doc)
    if ok:
        logger.info(
            "save_h2h(%d, %d): %dW-%dD-%dL advantage=%.2f",
            canonical_t1, canonical_t2, t1_wins, draws, t2_wins, h2h_advantage,
        )


# --- Helpers privados ---

def _extract_team_name(team_id: int, matches: list[dict]) -> str:
    """Extrae el nombre del equipo buscando en home/away de los partidos."""
    for m in matches:
        if m.get("home_team_id") == team_id:
            name = m.get("home_team_name", "")
            if name:
                return name
        elif m.get("away_team_id") == team_id:
            name = m.get("away_team_name", "")
            if name:
                return name
    return f"Team_{team_id}"


def _extract_league(matches: list[dict]) -> str:
    """Extrae la liga principal de una lista de partidos (prefiere ligas soportadas)."""
    for m in matches:
        league = m.get("league", "")
        if league in _FOOTBALL_LEAGUE_CODES:
            return league
    # Si no hay liga reconocida, devolver la del primer partido
    for m in matches:
        league = m.get("league", "")
        if league:
            return league
    return ""


def _build_xg_matches(team_id: int, matches: list[dict]) -> list[dict]:
    """
    Convierte matches al formato que espera calculate_xg_proxy:
    con goals_scored relativo al equipo dado.
    """
    result = []
    for m in matches:
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        gh = m.get("goals_home") or 0
        ga = m.get("goals_away") or 0
        shots_h = m.get("shots_home")
        shots_a = m.get("shots_away")
        sot_h = m.get("shots_on_target_home")
        sot_a = m.get("shots_on_target_away")

        if home_id == team_id:
            result.append({
                "goals_scored": gh,
                "shots_total": shots_h,
                "shots_on_target": sot_h,
            })
        elif away_id == team_id:
            result.append({
                "goals_scored": ga,
                "shots_total": shots_a,
                "shots_on_target": sot_a,
            })
    return result


def _build_empty_team_stats(team_id: int) -> dict:
    """Stats neutrales para equipos sin datos historicos."""
    empty_side = {"played": 0, "won": 0, "drawn": 0, "lost": 0,
                  "goals_for": 0, "goals_against": 0}
    return {
        "team_id": team_id,
        "team_name": f"Team_{team_id}",
        "league": "",
        "last_10": [],
        "form_score": 50.0,
        "home_stats": empty_side,
        "away_stats": empty_side,
        "streak": {"type": "draw", "count": 0},
        "xg_per_game": 1.0,
        "raw_matches": [],
        "data_quality": "partial",
        "updated_at": datetime.now(timezone.utc),
    }
