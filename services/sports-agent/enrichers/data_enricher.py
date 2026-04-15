"""
Orquestador de enrichers — combina Poisson, ELO, forma, H2H y cuotas.
Salida: enriched_match completo para value_bet_engine.
"""
import logging

logger = logging.getLogger(__name__)


async def enrich_match(match: dict) -> dict:
    """
    Orquesta todos los enrichers. Flujo Poisson obligatorio:
    1. Recoger partidos de home_team y away_team de Firestore team_stats
    2. Llamar poisson_model.fit_attack_defense(all_matches) → team_params dict
    3. Llamar poisson_model.predict_match_probs(home_id, away_id, team_params) → probs
       Los team_params de fit_attack_defense se pasan directamente a predict_match_probs
       dentro de esta funcion — NO se persisten en Firestore (son temporales).
    4. Leer h2h_advantage de Firestore h2h_data
       Si home_id > away_id la ventaja almacenada es relativa al away (menor ID).
       En ese caso INVERTIR: h2h_for_home = -stored_h2h_advantage
    Input: documento de upcoming_matches.
    Output: enriched_match con todos los campos para value_bet_engine.
    Guarda en Firestore coleccion enriched_matches.
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError


async def run_enrichment() -> int:
    """
    Identifica partidos sin enriquecer:
    1. Lee todos los upcoming_matches con status == "SCHEDULED"
    2. Para cada match_id, busca si existe doc en enriched_matches
    3. Si NO existe en enriched_matches → llama enrich_match()
    4. Si existe pero enriched_at < now - 6h → re-enriquece (cuotas pueden haber cambiado)
    Devuelve numero de partidos enriquecidos en esta ejecucion.
    """
    # TODO: implementar en Sesion 3
    raise NotImplementedError
