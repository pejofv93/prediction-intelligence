"""
Sistema ELO dinamico adaptado al futbol.
Lee y escribe en Firestore coleccion team_elo.

Nota HOME_ADVANTAGE: se suma al ELO del equipo local SOLO para calcular
expected_score y update_elo — el ELO almacenado en Firestore es siempre
el ELO base (sin la bonificacion de local).
"""
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

K_FACTOR = 32         # sensibilidad del sistema ELO a resultados
HOME_ADVANTAGE = 100  # puntos ELO extra para equipo local (solo en calculo, no almacenado)
DEFAULT_ELO = 1500


def expected_score(elo_a: float, elo_b: float) -> float:
    """Probabilidad esperada de victoria de A contra B segun ELO."""
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def update_elo(
    elo_a: float, elo_b: float, score: float
) -> tuple[float, float]:
    """
    Actualiza ELO de dos equipos tras un resultado.
    score: resultado desde perspectiva de A — 1.0=victoria, 0.5=empate, 0.0=derrota.
    Devuelve (nuevo_elo_a, nuevo_elo_b).
    """
    exp_a = expected_score(elo_a, elo_b)
    exp_b = 1.0 - exp_a  # expected_score(elo_b, elo_a)
    new_a = elo_a + K_FACTOR * (score - exp_a)
    new_b = elo_b + K_FACTOR * ((1.0 - score) - exp_b)
    return new_a, new_b


def get_team_elo(team_id: int) -> float:
    """
    Lee ELO actual de Firestore coleccion team_elo.
    Si no existe el documento, devuelve DEFAULT_ELO.
    Llamada sincrona — usar dentro de contexto donde Firestore es accesible.
    """
    from shared.firestore_client import col
    try:
        doc = col("team_elo").document(str(team_id)).get()
        if doc.exists:
            return float(doc.to_dict().get("elo", DEFAULT_ELO))
        return DEFAULT_ELO
    except Exception:
        logger.error(
            "get_team_elo(%d): error leyendo Firestore — usando DEFAULT_ELO",
            team_id, exc_info=True,
        )
        return DEFAULT_ELO


async def _save_team_elo(
    team_id: int,
    new_elo: float,
    match: dict,
    opponent_id: int,
) -> None:
    """
    Persiste el nuevo ELO en Firestore.
    Actualiza elo_history (max 10 entradas, mas reciente al final).
    """
    from shared.firestore_client import col
    try:
        doc_ref = col("team_elo").document(str(team_id))

        # Obtener historial existente (sin bloquear con await — llamada sincrona en hilo actual)
        loop = asyncio.get_event_loop()
        existing = await loop.run_in_executor(None, doc_ref.get)

        if existing.exists:
            data = existing.to_dict()
            history: list[dict] = data.get("elo_history", [])
            team_name: str = data.get("team_name", f"Team_{team_id}")
        else:
            history = []
            team_name = f"Team_{team_id}"

        # Anadir entrada al historial
        history.append({
            "date": match.get("date", ""),
            "elo": round(new_elo, 1),
            "opponent_id": opponent_id,
            "result": match.get("result", ""),
        })
        history = history[-10:]  # conservar solo las 10 ultimas entradas

        doc_ref.set({
            "team_id": team_id,
            "team_name": team_name,
            "elo": round(new_elo, 1),
            "elo_history": history,
            "updated_at": datetime.now(timezone.utc),
        })

    except Exception:
        logger.error(
            "_save_team_elo(%d): error guardando ELO en Firestore",
            team_id, exc_info=True,
        )


async def update_all_elos(finished_matches: list[dict]) -> None:
    """
    Procesa partidos terminados en orden cronologico y actualiza Firestore team_elo.
    Cada partido actualiza el ELO de ambos equipos.
    finished_matches: lista de partidos con home_team_id, away_team_id, result, date.
    result debe ser "HOME_WIN" | "AWAY_WIN" | "DRAW".
    """
    if not finished_matches:
        logger.info("update_all_elos: lista vacia, nada que actualizar")
        return

    # Ordenar cronologicamente (mas antiguo primero)
    sorted_matches = sorted(finished_matches, key=lambda m: m.get("date", ""))

    updated = 0
    for match in sorted_matches:
        home_id = match.get("home_team_id")
        away_id = match.get("away_team_id")
        result = match.get("result")

        if not home_id or not away_id or not result:
            logger.debug(
                "update_all_elos: partido incompleto (home=%s away=%s result=%s) — omitido",
                home_id, away_id, result,
            )
            continue

        try:
            # Leer ELOs actuales (base, sin home advantage)
            home_elo_base = get_team_elo(home_id)
            away_elo_base = get_team_elo(away_id)

            # Aplicar HOME_ADVANTAGE solo para el calculo de expected score y update
            home_elo_adj = home_elo_base + HOME_ADVANTAGE

            if result == "HOME_WIN":
                new_home_adj, new_away = update_elo(home_elo_adj, away_elo_base, 1.0)
                score_for_log = "W"
            elif result == "AWAY_WIN":
                new_home_adj, new_away = update_elo(home_elo_adj, away_elo_base, 0.0)
                score_for_log = "L"
            else:  # DRAW
                new_home_adj, new_away = update_elo(home_elo_adj, away_elo_base, 0.5)
                score_for_log = "D"

            # Convertir el ELO del local de vuelta a base (quitar HOME_ADVANTAGE)
            new_home_base = new_home_adj - HOME_ADVANTAGE

            await _save_team_elo(home_id, new_home_base, match, away_id)
            await _save_team_elo(away_id, new_away, match, home_id)
            updated += 1

            logger.debug(
                "update_all_elos: %d(%+.0f) vs %d(%+.0f) [%s]",
                home_id, new_home_base - home_elo_base,
                away_id, new_away - away_elo_base,
                score_for_log,
            )

        except Exception:
            logger.error(
                "update_all_elos: error procesando partido %s vs %s",
                home_id, away_id, exc_info=True,
            )

    logger.info("update_all_elos: %d partidos procesados", updated)


def elo_win_probability(home_id: int, away_id: int) -> float:
    """
    Devuelve la probabilidad de victoria del equipo local incluyendo HOME_ADVANTAGE.
    Resultado en [0.0, 1.0].
    """
    home_elo = get_team_elo(home_id)
    away_elo = get_team_elo(away_id)
    prob = expected_score(home_elo + HOME_ADVANTAGE, away_elo)
    return round(float(prob), 4)
