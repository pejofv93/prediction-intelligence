"""
sports-agent — FastAPI service
Endpoints: /run-collect /run-enrich /run-analyze /run-learning /run-backtest /health /status
Todos los endpoints /run-* devuelven 202 Accepted inmediatamente.
El trabajo real se ejecuta en background (asyncio.create_task).
Cloud Run timeout=900s para /run-collect (puede tardar hasta 15min por rate limit).
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="sports-agent")

CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")

# Timestamps de ultima ejecucion (en memoria — se pierden al reiniciar)
_status: dict = {"last_collect": None, "last_enrich": None, "last_analyze": None}


def verify_token(x_cloud_token: str = Header(...)) -> None:
    """Valida el token secreto para proteger los endpoints /run-*."""
    if x_cloud_token != CLOUD_RUN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict:
    return {
        "last_collect": _status["last_collect"],
        "last_enrich": _status["last_enrich"],
        "last_analyze": _status["last_analyze"],
    }


@app.post("/run-collect", dependencies=[Depends(verify_token)])
async def run_collect() -> JSONResponse:
    """202 inmediato → background: collectors/ → Firestore."""
    asyncio.create_task(_bg_collect())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "collect"})


@app.post("/run-enrich", dependencies=[Depends(verify_token)])
async def run_enrich() -> JSONResponse:
    """202 inmediato → background: enrichers/ → Firestore."""
    asyncio.create_task(_bg_enrich())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "enrich"})


@app.post("/run-analyze", dependencies=[Depends(verify_token)])
async def run_analyze() -> JSONResponse:
    """202 inmediato → background: value_bet_engine → Firestore."""
    asyncio.create_task(_bg_analyze())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "analyze"})


@app.post("/run-learning", dependencies=[Depends(verify_token)])
async def run_learning() -> JSONResponse:
    """202 inmediato → background: learning_engine → Firestore."""
    asyncio.create_task(_bg_learning())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "learning"})


@app.post("/run-backtest", dependencies=[Depends(verify_token)])
async def run_backtest() -> JSONResponse:
    """202 inmediato → background: backtester/backtest.py. Ejecutar UNA SOLA VEZ al arrancar."""
    asyncio.create_task(_bg_backtest())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "backtest"})


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _bg_collect() -> None:
    """
    Pipeline de recoleccion completo:
    1. Futbol (football-data.org): upcoming_matches + team_stats + h2h
    2. Otros deportes (API-Sports): games_today + team_stats
    Prioridad: futbol primero, resto con budget restante.
    """
    try:
        logger.info("collect: iniciando pipeline")
        start = datetime.now(timezone.utc)

        # --- 1. Futbol (football-data.org) ---
        await _collect_football()

        # --- 2. Otros deportes (API-Sports) ---
        await _collect_other_sports()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        _status["last_collect"] = datetime.now(timezone.utc).isoformat()
        logger.info("collect: completado en %.1fs", elapsed)

    except Exception as e:
        logger.error("collect: error no controlado — %s", e, exc_info=True)


async def _collect_football() -> None:
    """
    Recolecta partidos de futbol europeo (PL, PD, BL1, SA) via football-data.org.
    Flujo: upcoming_matches → team_stats (local + visitante) → h2h → save todo.
    Rate limit: RATE_LIMIT_DELAY=6.5s ya integrado en cada llamada HTTP.
    """
    from collectors.football_api import get_upcoming_matches, get_team_stats, get_h2h
    from collectors.firestore_writer import save_upcoming_matches, save_team_stats, save_h2h

    logger.info("collect.football: obteniendo partidos proximos 7 dias")
    matches = await get_upcoming_matches(days=7)

    if not matches:
        logger.warning("collect.football: ningun partido encontrado")
        return

    await save_upcoming_matches(matches)
    logger.info("collect.football: %d partidos guardados", len(matches))

    # Deduplicar equipos para no hacer peticiones repetidas
    team_ids_seen: set[int] = set()
    h2h_pairs_seen: set[tuple[int, int]] = set()

    for match in matches:
        home_id = match.get("home_team_id")
        away_id = match.get("away_team_id")

        if not home_id or not away_id:
            continue

        # Stats del equipo local
        if home_id not in team_ids_seen:
            try:
                raw_home = await get_team_stats(home_id)
                await save_team_stats(home_id, raw_home)
                team_ids_seen.add(home_id)
            except Exception:
                logger.error("collect.football: error stats equipo %d", home_id, exc_info=True)

        # Stats del equipo visitante
        if away_id not in team_ids_seen:
            try:
                raw_away = await get_team_stats(away_id)
                await save_team_stats(away_id, raw_away)
                team_ids_seen.add(away_id)
            except Exception:
                logger.error("collect.football: error stats equipo %d", away_id, exc_info=True)

        # H2H (par canonico ordenado)
        pair = (min(home_id, away_id), max(home_id, away_id))
        if pair not in h2h_pairs_seen:
            try:
                h2h_matches = await get_h2h(home_id, away_id)
                await save_h2h(home_id, away_id, h2h_matches)
                h2h_pairs_seen.add(pair)
            except Exception:
                logger.error(
                    "collect.football: error H2H %d vs %d", home_id, away_id, exc_info=True
                )

    logger.info(
        "collect.football: %d equipos, %d pares H2H procesados",
        len(team_ids_seen), len(h2h_pairs_seen),
    )


async def _collect_other_sports() -> None:
    """
    Recolecta partidos de hoy para NBA, NFL, MLB, NHL, MMA via API-Sports.
    Solo los partidos de hoy — conserva el budget de 100 req/dia.
    API_SPORTS_DELAY=2.0s ya integrado en cada llamada HTTP.
    """
    from collectors.api_sports_client import get_games_today, get_team_stats_bdl
    from collectors.firestore_writer import save_upcoming_matches, save_team_stats
    from shared.config import SUPPORTED_SPORTS_APISPORTS

    if not os.environ.get("FOOTBALL_RAPID_API_KEY"):
        logger.warning("collect.other_sports: FOOTBALL_RAPID_API_KEY no configurada — omitiendo")
        return

    total_games = 0
    total_teams = 0

    for sport_type, sport_name in SUPPORTED_SPORTS_APISPORTS.items():
        try:
            logger.info("collect.other_sports: obteniendo partidos de %s", sport_name.upper())
            games = await get_games_today(sport_name)

            if not games:
                logger.info("collect.other_sports: sin partidos hoy para %s", sport_name.upper())
                continue

            await save_upcoming_matches(games)
            total_games += len(games)

            # Stats de equipos que juegan hoy (max 2 partidos para conservar budget)
            team_ids_seen: set[int] = set()
            for game in games[:2]:  # limite conservador: 2 partidos por deporte
                home_id = game.get("home_team_id")
                away_id = game.get("away_team_id")

                for team_id in [home_id, away_id]:
                    if team_id and team_id not in team_ids_seen:
                        try:
                            raw_stats = await get_team_stats_bdl(sport_name, team_id)
                            await save_team_stats(team_id, raw_stats)
                            team_ids_seen.add(team_id)
                            total_teams += 1
                        except Exception:
                            logger.error(
                                "collect.other_sports: error stats %s equipo %d",
                                sport_name, team_id, exc_info=True,
                            )

        except Exception:
            logger.error(
                "collect.other_sports: error procesando %s", sport_name, exc_info=True
            )

    logger.info(
        "collect.other_sports: %d partidos, %d equipos procesados", total_games, total_teams
    )


async def _bg_enrich() -> None:
    """
    Pipeline de enriquecimiento:
    Lee upcoming_matches SCHEDULED → aplica Poisson+ELO (futbol) o Groq (otros deportes)
    → escribe enriched_matches en Firestore.
    """
    try:
        # Aviso si collect no se ha ejecutado en esta sesion (no bloquea)
        if _status["last_collect"] is None:
            logger.warning(
                "enrich: last_collect es None — "
                "puede que los datos no sean frescos; continuando igualmente"
            )

        start = datetime.now(timezone.utc)
        logger.info("enrich: iniciando pipeline")

        from enrichers.data_enricher import run_enrichment
        count = await run_enrichment()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        _status["last_enrich"] = datetime.now(timezone.utc).isoformat()
        logger.info("enrich: %d partidos enriquecidos en %.1fs", count, elapsed)

    except Exception as e:
        logger.error("enrich: error no controlado — %s", e, exc_info=True)


async def _bg_analyze() -> None:
    """
    Pipeline de analisis:
    Lee enriched_matches SCHEDULED → genera senales de value bet → escribe predictions.
    """
    try:
        if _status["last_enrich"] is None:
            logger.warning(
                "analyze: last_enrich es None — "
                "puede que los datos no sean frescos; continuando igualmente"
            )

        start = datetime.now(timezone.utc)
        logger.info("analyze: iniciando pipeline")

        from analyzers.value_bet_engine import generate_signal
        from shared.firestore_client import col

        # Leer todos los enriched_matches disponibles
        try:
            docs = list(col("enriched_matches").stream())
        except Exception:
            logger.error("analyze: error leyendo enriched_matches", exc_info=True)
            return

        signals_generated = 0
        for doc in docs:
            enriched = doc.to_dict()
            try:
                signals = await generate_signal(enriched)
                signals_generated += len(signals)
            except Exception:
                logger.error(
                    "analyze: error en generate_signal para %s",
                    enriched.get("match_id"), exc_info=True,
                )

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        _status["last_analyze"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "analyze: %d senales generadas de %d partidos en %.1fs",
            signals_generated, len(docs), elapsed,
        )

    except Exception as e:
        logger.error("analyze: error no controlado — %s", e, exc_info=True)


async def _bg_learning() -> None:
    """
    Pipeline de aprendizaje diario:
    Evalua predicciones pasadas contra resultados reales → ajusta pesos del modelo.
    """
    try:
        logger.info("learning: iniciando pipeline")
        start = datetime.now(timezone.utc)

        from learner.learning_engine import run_daily_learning
        await run_daily_learning()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("learning: completado en %.1fs", elapsed)

    except Exception as e:
        logger.error("learning: error no controlado — %s", e, exc_info=True)


async def _bg_backtest() -> None:
    """
    Backtesting historico contra las ultimas 2 temporadas.
    Ejecutar UNA SOLA VEZ al inicializar el sistema.
    Puede tardar 30-60 min por rate limit de football-data.org.
    """
    try:
        logger.info("backtest: iniciando — esto puede tardar 30-60 min")
        start = datetime.now(timezone.utc)

        from backtester.backtest import run_backtest
        result = await run_backtest(seasons=2)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info(
            "backtest: completado en %.0fs — accuracy=%.1f%% partidos=%d pesos=%s",
            elapsed,
            result.get("accuracy", 0) * 100,
            result.get("matches_processed", 0),
            result.get("weights_final", {}),
        )

    except Exception as e:
        logger.error("backtest: error no controlado — %s", e, exc_info=True)
