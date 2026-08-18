"""
Script one-shot: re-siembra el histórico de team_stats desde la temporada anterior.

Contexto: football-data.org acota `/teams/{id}/matches` a la temporada EN CURSO. Al arrancar
la 2026-27 devolvía 0 partidos terminados y save_team_stats sobrescribía el doc con defaults
vacíos, destruyendo el histórico de todos los equipos. Sin raw_matches (>=MIN_MATCHES_TO_FIT)
el enricher no calcula Poisson y el value bet engine bloquea el partido en DIAG_POISSON_GUARD.

El colector ya se auto-repara (ver _collect_football._team_history), pero a razón de
SEASON_BACKFILL_MAX_PER_RUN equipos por ciclo. Este script hace el recorrido completo de una
vez para no esperar días.

Uso:
    python scripts/reseed_team_stats.py             # dry-run: enumera sin escribir
    python scripts/reseed_team_stats.py --confirm   # escribe en Firestore

Requisitos: FOOTBALL_API_KEY + credenciales de Firestore (ADC o GOOGLE_APPLICATION_CREDENTIALS)
y GOOGLE_CLOUD_PROJECT / FIRESTORE_COLLECTION_PREFIX como en Cloud Run.
"""
import argparse
import asyncio
import logging
import os
import sys

# Permitir imports: shared/ desde la raíz, módulos desde services/sports-agent/
_ROOT = os.path.dirname(os.path.dirname(__file__))
_SPORTS = os.path.join(_ROOT, "services", "sports-agent")
sys.path.insert(0, _ROOT)
sys.path.insert(0, _SPORTS)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reseed_team_stats")
for noisy in ("urllib3", "google", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from shared.config import MIN_MATCHES_TO_FIT, SUPPORTED_FOOTBALL_LEAGUES  # noqa: E402
from shared.firestore_client import col  # noqa: E402
from collectors.football_api import (  # noqa: E402
    current_season_start_year, get_team_stats,
)
from collectors.firestore_writer import (  # noqa: E402
    save_team_stats, stored_raw_match_count,
)

_FOOTBALL_LEAGUES = set(SUPPORTED_FOOTBALL_LEAGUES.keys())


def _teams_from_upcoming() -> dict[int, str]:
    """IDs de equipo con partido programado en ligas de fútbol soportadas."""
    teams: dict[int, str] = {}
    for doc in col("upcoming_matches").stream():
        d = doc.to_dict() or {}
        if d.get("league") not in _FOOTBALL_LEAGUES:
            continue
        if d.get("status") not in ("SCHEDULED", "TIMED"):
            continue
        for id_key, name_key in (("home_team_id", "home_team"), ("away_team_id", "away_team")):
            tid = d.get(id_key)
            if tid:
                teams[int(tid)] = d.get(name_key, "") or ""
    return teams


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="escribe en Firestore (por defecto solo enumera)")
    ap.add_argument("--seasons-back", type=int, default=1,
                    help="cuántas temporadas hacia atrás pedir (default 1)")
    args = ap.parse_args()

    season = current_season_start_year() - args.seasons_back
    teams = _teams_from_upcoming()
    logger.info(
        "equipos con partido programado: %d — re-siembra desde temporada %d (mínimo %d partidos)",
        len(teams), season, MIN_MATCHES_TO_FIT,
    )

    pendientes = []
    for tid, name in sorted(teams.items()):
        n = stored_raw_match_count(tid)
        if n < MIN_MATCHES_TO_FIT:
            pendientes.append((tid, name, n))

    logger.info("necesitan re-siembra: %d de %d", len(pendientes), len(teams))
    for tid, name, n in pendientes:
        logger.info("  - %s (%d): raw_matches=%d", name or f"Team_{tid}", tid, n)

    if not args.confirm:
        logger.info(
            "DRY-RUN: no se ha escrito nada. Repetir con --confirm "
            "(coste estimado: %d llamadas × 6,5s ≈ %.1f min)",
            len(pendientes), len(pendientes) * 6.5 / 60,
        )
        return 0

    ok = fail = 0
    for tid, name, _ in pendientes:
        try:
            raw = await get_team_stats(tid, season=season)
            if not raw:
                logger.warning("  %s (%d): la temporada %d tampoco devuelve partidos",
                               name or f"Team_{tid}", tid, season)
                fail += 1
                continue
            await save_team_stats(tid, raw)
            ok += 1
        except Exception:
            logger.error("  error re-sembrando equipo %d", tid, exc_info=True)
            fail += 1

    logger.info("re-siembra completada: %d equipos OK, %d fallidos", ok, fail)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
