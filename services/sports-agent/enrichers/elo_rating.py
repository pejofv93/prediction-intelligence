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


def get_team_elo(team_id: int | str) -> float:
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
            "get_team_elo(%s): error leyendo Firestore — usando DEFAULT_ELO",
            team_id, exc_info=True,
        )
        return DEFAULT_ELO


async def _save_team_elo(
    team_id: int | str,
    new_elo: float,
    match: dict,
    opponent_id: int | str,
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
            "_save_team_elo(%s): error guardando ELO en Firestore",
            team_id, exc_info=True,
        )


LEDGER_COLLECTION = "elo_applied"


def _fingerprints(matches: list[dict]) -> dict[int, str]:
    """{indice → huella} de los partidos que traen datos suficientes."""
    from collectors.team_identity import match_fingerprint
    out: dict[int, str] = {}
    for i, m in enumerate(matches):
        h, a = m.get("home_team_id"), m.get("away_team_id")
        if h and a:
            out[i] = match_fingerprint(m.get("date", ""), h, a)
    return out


async def _already_applied(fps: list[str]) -> set[str]:
    """Huellas que ya se aplicaron al ELO alguna vez. Lectura en lote (1 RPC por 300)."""
    from shared.firestore_client import get_client
    from shared.config import COLLECTION_PREFIX
    if not fps:
        return set()
    seen: set[str] = set()
    try:
        client = get_client()
        loop = asyncio.get_event_loop()
        for i in range(0, len(fps), 300):
            refs = [
                client.collection(f"{COLLECTION_PREFIX}{LEDGER_COLLECTION}").document(fp)
                for fp in fps[i:i + 300]
            ]
            docs = await loop.run_in_executor(None, lambda r=refs: list(client.get_all(r)))
            seen.update(d.id for d in docs if d.exists)
    except Exception:
        # Si el ledger no se puede leer NO se aplica nada: repetir la aplicacion es
        # justo el fallo que este registro existe para evitar.
        logger.error(
            "_already_applied: error leyendo %s — se omite la actualizacion de ELO",
            LEDGER_COLLECTION, exc_info=True,
        )
        return set(fps)
    return seen


async def _mark_applied(entries: dict[str, dict], source: str) -> None:
    """Registra las huellas aplicadas. Escritura en lotes de 400."""
    from shared.firestore_client import get_client
    from shared.config import COLLECTION_PREFIX
    if not entries:
        return
    try:
        client = get_client()
        loop = asyncio.get_event_loop()
        items = list(entries.items())
        now = datetime.now(timezone.utc)

        def _commit(chunk):
            batch = client.batch()
            for fp, meta in chunk:
                ref = client.collection(
                    f"{COLLECTION_PREFIX}{LEDGER_COLLECTION}"
                ).document(fp)
                batch.set(ref, {**meta, "source": source, "applied_at": now})
            batch.commit()

        for i in range(0, len(items), 400):
            await loop.run_in_executor(None, _commit, items[i:i + 400])
    except Exception:
        logger.error("_mark_applied: error registrando huellas en %s",
                     LEDGER_COLLECTION, exc_info=True)


async def update_all_elos(
    finished_matches: list[dict], source: str = "unknown", use_ledger: bool = True
) -> None:
    """
    Procesa partidos terminados en orden cronologico y actualiza Firestore team_elo.
    Cada partido actualiza el ELO de ambos equipos.
    finished_matches: lista de partidos con home_team_id, away_team_id, result, date.
    result debe ser "HOME_WIN" | "AWAY_WIN" | "DRAW".

    IDEMPOTENTE desde 2026-08-19: cada partido se aplica UNA sola vez. Antes no habia
    ningun registro de lo ya aplicado y los llamantes le pasaban las mismas listas cada
    ciclo (get_finished_matches trae 30 dias, team_stats.raw_matches los ultimos 20), asi
    que un mismo resultado entraba decenas de veces con K=32. El efecto medido sobre la
    base real: 1.469 entradas de elo_history para 186 partidos distintos (7,9x), amplitud
    del ELO de 1.045 puntos donde una sola pasada da 261, y el orden de fuerza roto
    (Valencia por encima de Liverpool). El ELO habia degenerado en un indicador de forma
    reciente amplificado, duplicando el factor `form` del ensemble.

    La huella es (fecha, local, visitante) con ids canonicos — no el match_id — porque el
    mismo partido llega con ids distintos segun la fuente (football-data vs allsportsapi2).

    source: quien llama (collect_football, learning, rebuild...) — se guarda en el ledger.
    use_ledger: solo False para recomputos completos que ya controlan la unicidad.
    """
    if not finished_matches:
        logger.info("update_all_elos: lista vacia, nada que actualizar")
        return

    # Ordenar cronologicamente (mas antiguo primero)
    sorted_matches = sorted(finished_matches, key=lambda m: m.get("date", ""))

    # Filtrar lo ya aplicado ANTES de tocar ningun ELO
    fps = _fingerprints(sorted_matches)
    applied_now: dict[str, dict] = {}
    if use_ledger:
        seen = await _already_applied(sorted(set(fps.values())))
        pending_idx = {i for i, fp in fps.items() if fp not in seen}
        skipped = len(sorted_matches) - len(pending_idx)
        if skipped:
            logger.info(
                "update_all_elos[%s]: %d partidos ya aplicados anteriormente — omitidos",
                source, skipped,
            )
        sorted_matches = [m for i, m in enumerate(sorted_matches) if i in pending_idx]
        fps = _fingerprints(sorted_matches)
        if not sorted_matches:
            logger.info("update_all_elos[%s]: nada nuevo que aplicar", source)
            return

    updated = 0
    for idx, match in enumerate(sorted_matches):
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
            # Registrar la huella SOLO tras persistir ambos ELOs: si algo falla a medias,
            # el partido queda sin marcar y se reintenta en el siguiente ciclo.
            _fp = fps.get(idx)
            if _fp:
                applied_now[_fp] = {
                    "home_team_id": str(home_id),
                    "away_team_id": str(away_id),
                    "date": str(match.get("date", "")),
                    "result": result,
                }

            logger.debug(
                "update_all_elos: %s(%+.0f) vs %s(%+.0f) [%s]",
                home_id, new_home_base - home_elo_base,
                away_id, new_away - away_elo_base,
                score_for_log,
            )

        except Exception:
            logger.error(
                "update_all_elos: error procesando partido %s vs %s",
                home_id, away_id, exc_info=True,
            )

    if use_ledger:
        await _mark_applied(applied_now, source)

    logger.info(
        "update_all_elos[%s]: %d partidos aplicados (%d huellas registradas)",
        source, updated, len(applied_now),
    )


def elo_win_probability(home_id: int | str, away_id: int | str) -> float:
    """
    Devuelve la probabilidad de victoria del equipo local incluyendo HOME_ADVANTAGE.
    Resultado en [0.0, 1.0].
    """
    home_elo = get_team_elo(home_id)
    away_elo = get_team_elo(away_id)
    prob = expected_score(home_elo + HOME_ADVANTAGE, away_elo)
    return round(float(prob), 4)
