"""
sports-agent — FastAPI service
Endpoints: /run-collect /run-enrich /run-analyze /run-learning /run-backtest /clear-odds-cache /health /status
Todos los endpoints /run-* devuelven 202 Accepted inmediatamente.
El trabajo real se ejecuta en background (asyncio.create_task).
Cloud Run timeout=1800s para /run-collect (puede tardar hasta 15min con rate limit football-data.org).
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="sports-agent")

CLOUD_RUN_TOKEN = os.environ.get("CLOUD_RUN_TOKEN", "")

# Timestamps de ultima ejecucion (en memoria — se pierden al reiniciar)
_status: dict = {"last_collect": None, "last_enrich": None, "last_analyze": None}

# Estado del backtest de calidad (backtest_engine) — persiste en memoria
_backtest_status: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,   # dict con métricas al completar
    "error": None,
}


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


@app.get("/api/quota")
async def api_quota() -> dict:
    """Estado de cuotas de todas las APIs externas (diarias y mensuales)."""
    from shared.api_quota_manager import quota
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "daily": quota.get_quota_status(),
        "monthly": quota.get_monthly_status(),
        "odds_sources_exhausted": quota.all_monthly_exhausted(["the_odds_api", "oddspapi"]),
    }


@app.post("/admin/reset-quota/{api_name}", dependencies=[Depends(verify_token)])
async def admin_reset_quota(api_name: str) -> dict:
    """
    Resetea remaining_reported de una API mensual en Firestore.
    Útil cuando la cuota se renueva pero el flag persiste del mes anterior.
    """
    from shared.api_quota_manager import quota, _this_month
    month = _this_month()
    key = f"{api_name}_monthly_{month}"
    try:
        quota._col().document(key).set({"remaining_reported": None}, merge=True)
        logger.info("admin_reset_quota: %s reseteado para %s", api_name, month)
        return {"ok": True, "api": api_name, "key": key, "month": month}
    except Exception as e:
        logger.error("admin_reset_quota: error reseteando %s — %s", api_name, e)
        raise HTTPException(status_code=500, detail=str(e))


async def _stream_job(coro_func, job_name: str):
    """
    Ejecuta coro_func() manteniendo la conexion HTTP abierta hasta completion.
    Emite 'ping Xs' cada 30s para que Cloud Run no mate el proceso (min-instances=0).
    El caller recibe 200 con stream de texto; la ultima linea es 'done Xs' o 'error: ...'.
    """
    task = asyncio.create_task(coro_func())
    start = datetime.now(timezone.utc)
    yield f"started: {job_name}\n"

    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
        except asyncio.TimeoutError:
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            yield f"ping {elapsed:.0f}s\n"
        except Exception as exc:
            yield f"error: {exc}\n"
            return

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    exc = task.exception()
    if exc:
        yield f"error: {exc}\n"
    else:
        yield f"done {elapsed:.0f}s\n"


@app.post("/run-collect", dependencies=[Depends(verify_token)])
async def run_collect() -> StreamingResponse:
    """Sincrono: mantiene conexion hasta completion (~10min con rate limit). Pings cada 30s."""
    return StreamingResponse(_stream_job(_bg_collect, "collect"), media_type="text/plain")


@app.post("/run-enrich", dependencies=[Depends(verify_token)])
async def run_enrich() -> StreamingResponse:
    """Sincrono: mantiene conexion hasta completion. Pings cada 30s."""
    return StreamingResponse(_stream_job(_bg_enrich, "enrich"), media_type="text/plain")


@app.get("/api/fixtures-sample", dependencies=[Depends(verify_token)])
async def fixtures_sample(date: str = "", n: int = 2) -> dict:
    """Debug: devuelve los primeros N fixtures de OddsPapi v4 para inspeccionar estructura."""
    from analyzers.corners_bookings import _fetch_fixtures_for_date
    from datetime import date as _date, timedelta
    target = _date.fromisoformat(date) if date else _date.today()
    week_end = target + timedelta(days=7)
    fixtures = await _fetch_fixtures_for_date(target, to_date=week_end)
    return {"total": len(fixtures), "sample": fixtures[:n]}


@app.post("/run-analyze", dependencies=[Depends(verify_token)])
async def run_analyze() -> StreamingResponse:
    """Sincrono: mantiene conexion hasta completion. Pings cada 30s."""
    return StreamingResponse(_stream_job(_bg_analyze, "analyze"), media_type="text/plain")


@app.post("/run-learning", dependencies=[Depends(verify_token)])
async def run_learning() -> StreamingResponse:
    """Sincrono: mantiene conexion hasta completion. Pings cada 30s."""
    return StreamingResponse(_stream_job(_bg_learning, "learning"), media_type="text/plain")


@app.post("/run-backtest", dependencies=[Depends(verify_token)])
async def run_backtest() -> JSONResponse:
    """202 inmediato → background: backtest histórico de calidad (backtest_engine).
    Puede tardar 30-60 min. Consultar progreso en GET /backtest/status."""
    if _backtest_status["running"]:
        return JSONResponse(
            status_code=409,
            content={"status": "already_running", "started_at": _backtest_status["started_at"]},
        )
    asyncio.create_task(_bg_sports_backtest())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "sports-backtest"})


@app.get("/backtest/status")
async def backtest_status() -> JSONResponse:
    """Estado actual del backtest de calidad (sin auth — solo lectura)."""
    return JSONResponse(content={
        "running": _backtest_status["running"],
        "started_at": _backtest_status["started_at"],
        "finished_at": _backtest_status["finished_at"],
        "result": _backtest_status["result"],
        "error": _backtest_status["error"],
    })


@app.post("/run-production-backtest", dependencies=[Depends(verify_token)])
async def run_production_backtest() -> JSONResponse:
    """
    202 → background: backtest sobre predictions resueltas de producción.
    Calcula accuracy por liga/mercado/edge/confianza y actualiza model_weights/current.
    Llamado automáticamente los lunes desde weekly-report.yml, después del reporte.
    """
    asyncio.create_task(_bg_production_backtest())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "production-backtest"})


async def _bg_production_backtest() -> None:
    try:
        from learner.backtest_engine import run_production_backtest as _run
        result = await _run()
        logger.info("production-backtest: completado — %s", result)
    except Exception as e:
        logger.error("production-backtest: error — %s", e, exc_info=True)


@app.post("/run-arb", dependencies=[Depends(verify_token)])
async def run_arb() -> JSONResponse:
    """202 → background: lee cuotas de Firestore, detecta arb, envía alertas Telegram."""
    asyncio.create_task(_bg_arb())
    return JSONResponse(status_code=202, content={"status": "accepted", "job": "arb"})


@app.post("/run-fdco-collect", dependencies=[Depends(verify_token)])
async def run_fdco_collect() -> StreamingResponse:
    """
    Descarga stats de corners/tarjetas de football-data.co.uk para todas las ligas.
    Guarda promedios por equipo en Firestore team_corner_stats.
    Ejecutar periódicamente (recomendado: cada 24h tras los partidos del día).
    """
    return StreamingResponse(_stream_job(_bg_fdco_collect, "fdco-collect"), media_type="text/plain")


@app.post("/clear-odds-cache", dependencies=[Depends(verify_token)])
async def clear_odds_cache() -> dict:
    """
    Limpia los cachés en memoria de odds-api.io (_EVENT_CACHE, _SPORT_EVENTS_CACHE, _SPORTS_CACHE).
    Útil para forzar reintento inmediato tras un rate limit 429 sin esperar el TTL.
    Requiere X-Cloud-Token. El caché es solo en memoria — se limpia solo en cold starts.
    """
    from collectors.odds_apiio_client import clear_caches
    result = clear_caches()
    return {"ok": True, **result}


@app.get("/test-nba", dependencies=[Depends(verify_token)])
async def test_nba() -> dict:
    """
    Verifica que api-basketball.p.rapidapi.com responde.
    Llama GET /games?date=hoy y devuelve status + primeros 2 partidos.
    """
    from collectors.api_sports_client import _request, _current_basketball_season, NBA_LEAGUE_ID
    today = __import__("datetime").date.today().isoformat()
    host = "api-basketball.p.rapidapi.com"
    season = _current_basketball_season()
    data = await _request(host, "/games", {"date": today, "league": NBA_LEAGUE_ID, "season": season})
    if data is None:
        return {"ok": False, "error": "sin respuesta — verificar key o host"}
    games = data.get("response", [])
    return {
        "ok": True,
        "host": host,
        "total": len(games),
        "sample": games[:2],
    }


@app.get("/test-tennis", dependencies=[Depends(verify_token)])
async def test_tennis() -> dict:
    """
    Verifica que tennisapi1.p.rapidapi.com responde.
    Llama /tournaments y devuelve status + primeros 2 torneos.
    """
    from collectors.tennis_collector import _request as tennis_request
    data = await tennis_request("/atp/tournaments")
    if data is None:
        return {"ok": False, "error": "sin respuesta — verificar key o host en tennisapi1"}
    results = data.get("results", data.get("tournaments", data if isinstance(data, list) else []))
    return {
        "ok": True,
        "host": "tennisapi1.p.rapidapi.com",
        "endpoint": "/atp/tournaments",
        "total": len(results) if isinstance(results, list) else "?",
        "sample": results[:2] if isinstance(results, list) else results,
    }


@app.get("/api/corners-signals")
async def get_corners_signals(league: str = "PD", days: int = 3) -> dict:
    """
    Devuelve señales de corners/tarjetas generadas en los últimos N días.
    Parámetros: league (código interno, e.g. PD), days (1-7).
    """
    from shared.firestore_client import col
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min(days, 7))).isoformat()
    try:
        docs = (
            col("corners_signals")
            .where(filter=FieldFilter("generated_at", ">=", cutoff))
            .limit(50)
            .stream()
        )
        results = [d.to_dict() for d in docs]
        return {"count": len(results), "signals": results}
    except Exception as e:
        return {"count": 0, "signals": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _bg_collect() -> None:
    """
    Pipeline de recoleccion completo.
    Orden: deportes rápidos primero para garantizar que no los bloquee
    el rate-limit de football-data.org (10 req/min, ~20-25 min para 88 partidos).
    """
    try:
        logger.info("collect: iniciando pipeline")
        start = datetime.now(timezone.utc)

        # --- 1. Baloncesto (rápido, sin rate-limit severo) ---
        await _collect_basketball_enhanced()

        # --- 2. Tenis (rápido) ---
        await _collect_tennis()

        # --- 3. Otros deportes (Basketball API, NFL, etc.) ---
        await _collect_other_sports()

        # --- 4. Fútbol extra (AllSports: NL, WCQ, Argentina, Copa Sud) ---
        await _collect_allsports_football()

        # --- 5. Fútbol europeo (football-data.org — lento, rate-limit 10 req/min) ---
        await _collect_football()

        # --- 6. Clasificaciones domésticas (flags motivacionales para MOTIVATION_CHECK) ---
        await _collect_standings()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        _status["last_collect"] = datetime.now(timezone.utc).isoformat()
        logger.info("collect: completado en %.1fs", elapsed)

        # Limpieza de partidos FINISHED >48h (no bloquea el pipeline si falla)
        try:
            deleted = await _cleanup_stale_upcoming()
            if deleted:
                logger.info("collect.cleanup: %d partidos FINISHED eliminados de upcoming_matches", deleted)
        except Exception as exc:
            logger.warning("collect.cleanup: error no crítico — %s", exc)

    except Exception as e:
        logger.error("collect: error no controlado — %s", e, exc_info=True)


async def _cleanup_stale_upcoming() -> int:
    """
    Elimina de upcoming_matches partidos que ya no son relevantes:
    1. FINISHED/PAUSED con match_date > 48h de antigüedad (evaluados por learning engine)
    2. SCHEDULED/TIMED con match_date > 48h de antigüedad (partidos ya jugados sin actualizar
       status — ocurre con ligas eliminadas de SUPPORTED_FOOTBALL_LEAGUES como CLI/PPL, y con
       partidos de fases eliminatorias cuyo status no se actualiza al no re-colectarse)
    Devuelve el número de documentos eliminados.
    """
    from shared.firestore_client import col

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    deleted = 0

    # Paso 1: FINISHED/PAUSED (comportamiento original)
    try:
        finished_docs = list(
            col("upcoming_matches")
            .where(filter=FieldFilter("status", "in", ["FINISHED", "PAUSED"]))
            .stream()
        )
    except Exception as exc:
        logger.warning("_cleanup_stale_upcoming: error leyendo FINISHED/PAUSED — %s", exc)
        finished_docs = []

    # Paso 2: SCHEDULED/TIMED con fecha pasada (stale sin actualizar)
    try:
        scheduled_docs = list(
            col("upcoming_matches")
            .where(filter=FieldFilter("status", "in", ["SCHEDULED", "TIMED"]))
            .stream()
        )
    except Exception as exc:
        logger.warning("_cleanup_stale_upcoming: error leyendo SCHEDULED — %s", exc)
        scheduled_docs = []

    all_docs = finished_docs + scheduled_docs

    for doc in all_docs:
        data = doc.to_dict()
        match_date = data.get("match_date")
        if not match_date:
            continue
        if isinstance(match_date, str):
            try:
                from datetime import datetime as _dt
                match_date = _dt.fromisoformat(match_date.replace("Z", "+00:00"))
            except ValueError:
                continue
        if hasattr(match_date, "tzinfo") and match_date.tzinfo is None:
            match_date = match_date.replace(tzinfo=timezone.utc)
        if match_date < cutoff:
            try:
                doc.reference.delete()
                deleted += 1
            except Exception as exc:
                logger.warning("_cleanup_stale_upcoming: error borrando %s — %s", doc.id, exc)

    return deleted


async def _is_stats_fresh(collection: str, doc_id: str, ttl_hours: int) -> bool:
    """True si el doc existe en Firestore y updated_at tiene menos de ttl_hours."""
    from shared.firestore_client import col
    try:
        doc = col(collection).document(doc_id).get()
        if not doc.exists:
            return False
        updated_at = doc.to_dict().get("updated_at")
        if not updated_at:
            return False
        if hasattr(updated_at, "tzinfo") and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated_at) < timedelta(hours=ttl_hours)
    except Exception:
        return False


async def _collect_football() -> None:
    """
    Recolecta partidos de futbol europeo via football-data.org.
    Caché TTL 6h: si team_stats o h2h_data tienen menos de 6h → omite llamada API.
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

    team_ids_seen: set[int] = set()
    h2h_pairs_seen: set[tuple[int, int]] = set()
    skipped_teams = 0
    skipped_h2h = 0

    for match in matches:
        home_id = match.get("home_team_id")
        away_id = match.get("away_team_id")

        if not home_id or not away_id:
            continue

        # Stats equipo local
        if home_id not in team_ids_seen:
            try:
                if await _is_stats_fresh("team_stats", str(home_id), ttl_hours=6):
                    logger.debug("collect.football: team_stats(%d) cache vigente — omitiendo", home_id)
                    skipped_teams += 1
                else:
                    raw_home = await get_team_stats(home_id)
                    await save_team_stats(home_id, raw_home)
                team_ids_seen.add(home_id)
            except Exception:
                logger.error("collect.football: error stats equipo %d", home_id, exc_info=True)

        # Stats equipo visitante
        if away_id not in team_ids_seen:
            try:
                if await _is_stats_fresh("team_stats", str(away_id), ttl_hours=6):
                    logger.debug("collect.football: team_stats(%d) cache vigente — omitiendo", away_id)
                    skipped_teams += 1
                else:
                    raw_away = await get_team_stats(away_id)
                    await save_team_stats(away_id, raw_away)
                team_ids_seen.add(away_id)
            except Exception:
                logger.error("collect.football: error stats equipo %d", away_id, exc_info=True)

        # H2H (par canonico ordenado)
        pair = (min(home_id, away_id), max(home_id, away_id))
        if pair not in h2h_pairs_seen:
            try:
                pair_key = f"{pair[0]}_{pair[1]}"
                if await _is_stats_fresh("h2h_data", pair_key, ttl_hours=6):
                    logger.debug("collect.football: h2h(%s) cache vigente — omitiendo", pair_key)
                    skipped_h2h += 1
                else:
                    h2h_matches = await get_h2h(home_id, away_id)
                    await save_h2h(home_id, away_id, h2h_matches)
                h2h_pairs_seen.add(pair)
            except Exception:
                logger.error(
                    "collect.football: error H2H %d vs %d", home_id, away_id, exc_info=True
                )

    logger.info(
        "collect.football: %d equipos (%d cache), %d H2H (%d cache)",
        len(team_ids_seen), skipped_teams, len(h2h_pairs_seen), skipped_h2h,
    )

    # Actualizar resultados de partidos terminados (últimos 30 días)
    finished: list[dict] = []
    try:
        from collectors.football_api import get_finished_matches
        from collectors.firestore_writer import update_finished_matches
        finished = await get_finished_matches(days_back=30)
        updated = await update_finished_matches(finished)
        logger.info("RESULTS_UPDATE: %d partidos actualizados a FINISHED", updated)
    except Exception:
        logger.error("collect.football: error actualizando resultados FINISHED", exc_info=True)

    # Actualizar ELO ratings desde partidos terminados con resultado conocido
    try:
        from enrichers.elo_rating import update_all_elos
        elo_matches = [
            {
                "home_team_id": m["home_team_id"],
                "away_team_id": m["away_team_id"],
                "result": (
                    "HOME_WIN" if m.get("goals_home", 0) > m.get("goals_away", 0)
                    else "AWAY_WIN" if m.get("goals_away", 0) > m.get("goals_home", 0)
                    else "DRAW"
                ),
                "date": m.get("date", ""),
            }
            for m in finished
            if m.get("goals_home") is not None and m.get("goals_away") is not None
        ]
        if elo_matches:
            await update_all_elos(elo_matches)
            logger.info("ELO_UPDATE: %d partidos procesados", len(elo_matches))
        else:
            logger.info("ELO_UPDATE: sin partidos con resultado conocido")
    except Exception:
        logger.error("collect.football: error actualizando ELO ratings", exc_info=True)


async def _collect_standings() -> None:
    """
    Recolecta clasificaciones de ligas domésticas (PL, PD, BL1, SA, FL1) via football-data.org.
    Escribe standings/{league_code} en Firestore con flags motivacionales para MOTIVATION_CHECK.
    Solo ligas domésticas con ≥8 equipos — skip silencioso para CL/EL/WC (torneos sin tabla).
    TTL implícito: corre junto con el collect cada 6h.
    """
    from collectors.football_api import get_standings
    from collectors.firestore_writer import save_standings
    from shared.config import SUPPORTED_FOOTBALL_LEAGUES

    # Solo ligas domésticas — CL/EL/ECL/WC no tienen standings de liga clásicos
    domestic_leagues = {k: v for k, v in SUPPORTED_FOOTBALL_LEAGUES.items()
                        if k not in ("CL", "EL", "ECL", "EC", "WC")}

    logger.info("collect.standings: %d ligas domésticas", len(domestic_leagues))
    for league_code, league_id in domestic_leagues.items():
        try:
            raw = await get_standings(league_id)
            if raw:
                await save_standings(league_code, raw)
            else:
                logger.warning("collect.standings: sin datos para %s (id=%d)", league_code, league_id)
        except Exception:
            logger.error("collect.standings: error en %s", league_code, exc_info=True)

    logger.info("collect.standings: completado")


async def _collect_allsports_football() -> None:
    """Recolecta fútbol de selecciones y sudamérica via AllSportsApi."""
    try:
        from collectors.allsports_client import get_upcoming_matches
        from collectors.firestore_writer import save_upcoming_matches

        if not os.environ.get("FOOTBALL_RAPID_API_KEY"):
            logger.warning("collect.allsports: FOOTBALL_RAPID_API_KEY no configurada")
            return

        matches = await get_upcoming_matches(days=7)
        if matches:
            await save_upcoming_matches(matches)
            logger.info("collect.allsports: %d partidos guardados", len(matches))
        else:
            logger.info("collect.allsports: sin partidos próximos")
    except Exception:
        logger.error("collect.allsports: error no controlado", exc_info=True)


async def _collect_tennis() -> None:
    """Recolecta partidos de tenis ATP/WTA via Tennis API."""
    try:
        from collectors.tennis_collector import collect_tennis_matches
        from collectors.firestore_writer import save_upcoming_matches

        if not os.environ.get("FOOTBALL_RAPID_API_KEY"):
            logger.warning("collect.tennis: FOOTBALL_RAPID_API_KEY no configurada")
            return

        matches = await collect_tennis_matches(days=7)
        if matches:
            await save_upcoming_matches(matches)
            logger.info("collect.tennis: %d partidos guardados", len(matches))
        else:
            logger.info("collect.tennis: sin torneos activos o sin partidos")
    except Exception:
        logger.error("collect.tennis: error no controlado", exc_info=True)


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

    # NBA se recoge via ESPN en _collect_basketball_enhanced — saltar aquí para evitar 403
    _SKIP_IN_OTHER = {"nba", "basketball"}

    for sport_type, sport_name in SUPPORTED_SPORTS_APISPORTS.items():
        if sport_name.lower() in _SKIP_IN_OTHER:
            logger.debug("collect.other_sports: %s omitido (manejado por basketball_enhanced via ESPN)", sport_name)
            continue
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


async def _collect_basketball_enhanced() -> None:
    """Recopila partidos NBA/Euroleague y guarda team_stats enriquecido."""
    try:
        from collectors.basketball_collector import collect_basketball_games, collect_basketball_team_stats
        from collectors.firestore_writer import save_upcoming_matches

        games = await collect_basketball_games()
        if games:
            await save_upcoming_matches(games)
            await collect_basketball_team_stats(games)
            logger.info("collect.basketball_enhanced: %d partidos procesados", len(games))
        else:
            logger.info("collect.basketball_enhanced: sin partidos hoy")
    except Exception:
        logger.error("collect.basketball_enhanced: error no controlado", exc_info=True)


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


def _dedup_signals_for_match(base_match_id: str, signals: list[dict]) -> None:
    """
    Mantiene máximo 2 señales por partido: mejor 1X2 + mejor mercado alternativo.
    Las señales ya están guardadas en Firestore; borra las extras.
    """
    if len(signals) <= 2:
        return

    h2h_signals = [s for s in signals if s.get("market_type") in ("h2h", None, "")]
    alt_signals  = [s for s in signals if s.get("market_type") not in ("h2h", None, "")]

    to_delete: list[str] = []

    if len(h2h_signals) > 1:
        h2h_signals.sort(key=lambda s: float(s.get("ev", s.get("edge", 0))), reverse=True)
        to_delete += [s.get("match_id", "") for s in h2h_signals[1:]]

    if len(alt_signals) > 1:
        alt_signals.sort(key=lambda s: float(s.get("ev", s.get("edge", 0))), reverse=True)
        to_delete += [s.get("match_id", "") for s in alt_signals[1:]]

    for doc_id in to_delete:
        if not doc_id:
            continue
        try:
            from shared.firestore_client import col
            col("predictions").document(doc_id).delete()
            logger.debug("dedup: eliminada señal extra %s de %s", doc_id, base_match_id)
        except Exception as e:
            logger.warning("dedup: error eliminando %s — %s", doc_id, e)

    if to_delete:
        logger.info("dedup(%s): %d señales eliminadas, quedan 2 máx", base_match_id, len(to_delete))


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

        # Leer enriched_matches usando db.get_all() para SOLO los IDs que necesitamos.
        # ANTES: list(col("enriched_matches").stream()) leía 5000+ docs históricos → lento.
        # AHORA: db.get_all(refs) = 1 RPC con los N IDs del día → N × 1ms en vez de 5000 × Xms.
        try:
            from datetime import timedelta as _td
            from shared.firestore_client import get_client as _get_fs_client2
            from shared.config import COLLECTION_PREFIX as _COL_PFX

            _now_utc = datetime.now(timezone.utc)
            _cutoff_24h = (_now_utc + _td(days=7)).isoformat()
            _today_str = _now_utc.date().isoformat()
            _fs = _get_fs_client2()

            upcoming_docs_raw = list(
                col("upcoming_matches")
                    .where(filter=FieldFilter("status", "in", ["SCHEDULED", "TIMED"]))
                    .stream()
            )
            # Solo IDs de partidos en las próximas 24h
            upcoming_ids_48h: set[str] = set()
            for d in upcoming_docs_raw:
                data = d.to_dict()
                mid = str(data.get("match_id", ""))
                if not mid:
                    continue
                md = data.get("match_date") or data.get("date")
                if md is None:
                    upcoming_ids_48h.add(mid)
                    continue
                if isinstance(md, str):
                    md_str = md[:10]
                elif hasattr(md, "date"):
                    md_str = md.date().isoformat()
                else:
                    md_str = str(md)[:10]
                if _today_str <= md_str <= _cutoff_24h[:10]:
                    upcoming_ids_48h.add(mid)

            # db.get_all(): 1 sola RPC para N documentos específicos (sin leer toda la colección)
            enriched_refs = [
                _fs.collection(f"{_COL_PFX}enriched_matches").document(mid)
                for mid in upcoming_ids_48h
            ]
            docs_raw = list(_fs.get_all(enriched_refs)) if enriched_refs else []
            docs = [d for d in docs_raw if d.exists]
            logger.info(
                "analyze: %d enriched (7d) de %d upcoming SCHEDULED (get_all %d refs)",
                len(docs), len(upcoming_ids_48h), len(enriched_refs),
            )
        except Exception:
            logger.error("analyze: error leyendo enriched_matches", exc_info=True)
            return

        weights_version = 0
        try:
            from analyzers.value_bet_engine import _get_weights_version
            weights_version = _get_weights_version()
        except Exception:
            pass

        signals_generated = 0

        # --- Pre-fetch odds en paralelo (cubre TODAS las fuentes HTTP) ---
        # Sin esto: N ligas × 15s secuencial → timeout. Con esto: ~15s en paralelo.
        try:
            from analyzers.value_bet_engine import _ODDS_SPORT_MAP, _get_league_events
            from analyzers.football_markets import _fetch_oddspapi_league, _ODDSPAPI_LEAGUE_MAP as _OP_MAP
            from analyzers.basketball_analyzer import _fetch_basketball_odds
            from analyzers.tennis_analyzer import _fetch_tennis_odds, _TENNIS_SPORT_KEYS
            from analyzers.corners_bookings import _fetch_fixtures_for_date
            from datetime import date as _date_pf

            _now = datetime.now(timezone.utc)
            _today = _date_pf.today()
            _active_leagues = {d.to_dict().get("league", "") for d in docs}
            # Solo ligas de fútbol con mapeado en OddsPapi (las demás harían un request sin filtro)
            _active_football_leagues = _active_leagues & set(_OP_MAP.keys())

            # Torneos de tenis con partidos programados
            try:
                _tennis_raw = list(
                    col("upcoming_matches")
                    .where(filter=FieldFilter("sport", "==", "tennis"))
                    .where(filter=FieldFilter("status", "in", ["SCHEDULED", "TIMED"]))
                    .limit(100)
                    .stream()
                )
                _tennis_sks = {
                    _TENNIS_SPORT_KEYS.get(
                        d.to_dict().get("tournament", "") or d.to_dict().get("league", "")
                    )
                    for d in _tennis_raw
                } - {None}
            except Exception:
                _tennis_sks = set()

            from datetime import timedelta as _td_pf
            _week_end = _today + _td_pf(days=7)

            # Pre-fetch odds-api.io (primaria): una coroutine por liga activa
            from shared.config import ODDSAPIIO_KEY as _ODDSAPIIO_KEY
            _oaio_coros = []
            if _ODDSAPIIO_KEY:
                from collectors.odds_apiio_client import get_league_odds as _get_oaio_odds
                _oaio_coros = [_get_oaio_odds(lg) for lg in _active_leagues if lg]

            from analyzers.value_bet_engine import _has_upcoming_matches_for_league as _has_upcoming
            _prefetch_coros = (
                # odds-api.io: fuente primaria — pre-fetch todas las ligas activas
                _oaio_coros
                # The Odds API: secundaria — solo ligas con partidos en próximas 48h
                + [_get_league_events(sk, "prefetch", _now)
                   for lg, sk in _ODDS_SPORT_MAP.items()
                   if lg in _active_leagues and _has_upcoming(lg, 48)]
                # OddsPapi v4: fixtures de hoy (corners/bookings del día)
                + [_fetch_fixtures_for_date(_today)]
                + [_fetch_fixtures_for_date(_today, to_date=_week_end)]
                # The Odds API: baloncesto (NBA + Euroleague)
                + [_fetch_basketball_odds("basketball_nba"),
                   _fetch_basketball_odds("basketball_euroleague")]
                # The Odds API: torneos de tenis activos
                + [_fetch_tennis_odds(sk, "prefetch") for sk in _tennis_sks]
            )
            logger.info("analyze: pre-fetching %d coroutines en paralelo", len(_prefetch_coros))
            await asyncio.gather(*_prefetch_coros, return_exceptions=True)
            logger.info("analyze: pre-fetch completado — todas las fuentes en cache")
        except Exception:
            logger.warning("analyze: error en pre-fetch odds — continuando sin cache", exc_info=True)

        # --- Fútbol (via enriched_matches con Poisson) ---
        from analyzers.player_props import generate_player_props_signals
        from analyzers.corners_bookings import generate_corners_signals, save_signals
        from analyzers.value_bet_engine import _ODDS_SPORT_MAP, _FOOTBALL_SPORT_KEYS
        from datetime import date as _date

        # Diagnóstico: ligas en enriched_matches vs. ligas con cobertura de señales extra
        _leagues_all    = {d.to_dict().get("league", "?") for d in docs}
        _leagues_extra  = {lg for lg in _leagues_all if _ODDS_SPORT_MAP.get(lg, "") in _FOOTBALL_SPORT_KEYS}
        _leagues_skip   = _leagues_all - _leagues_extra
        logger.info(
            "analyze: ligas en enriched=%s | con señales extra=%s | sin cobertura=%s",
            sorted(_leagues_all), sorted(_leagues_extra), sorted(_leagues_skip),
        )

        for doc in docs:
            enriched = doc.to_dict()
            # FIX1: generate_signal y mercados auxiliares son exclusivos de fútbol.
            # NBA/basketball pasan por generate_basketball_signals() — procesarlos aquí
            # también causaba doble-análisis con ensemble inflado (Timberwolves EV+106%).
            _doc_sport = enriched.get("sport", "football")
            if _doc_sport not in ("football", "soccer", ""):
                logger.info(
                    "analyze: sport=%s — skipping generate_signal loop (no es fútbol) [%s]",
                    _doc_sport, enriched.get("match_id"),
                )
                continue
            try:
                signals = await generate_signal(enriched)
                signals_generated += len(signals)
                # Deduplicar: incluye también señales de football_markets (btts, ah, etc.)
                # que generate_signal guarda internamente sin retornarlas.
                base_match_id = str(enriched.get("match_id", ""))
                if base_match_id:
                    try:
                        from shared.firestore_client import col as _col_dedup
                        all_match_docs = list(
                            _col_dedup("predictions")
                            .where(filter=FieldFilter("match_id", ">=", base_match_id))
                            .where(filter=FieldFilter("match_id", "<=", base_match_id + ""))
                            .stream()
                        )
                        all_signal_dicts = [d.to_dict() for d in all_match_docs if d.to_dict()]
                        _dedup_signals_for_match(base_match_id, all_signal_dicts)
                    except Exception as _dedup_e:
                        logger.debug("dedup: error leyendo predictions para %s — %s", base_match_id, _dedup_e)
            except Exception:
                logger.error(
                    "analyze: error en generate_signal para %s",
                    enriched.get("match_id"), exc_info=True,
                )

            # Player props (goleador + asistente)
            try:
                prop_sigs = await generate_player_props_signals(enriched, weights_version)
                signals_generated += len(prop_sigs)
            except Exception:
                logger.error("analyze: error player_props %s", enriched.get("match_id"), exc_info=True)

            # Corners + bookings 1X2
            try:
                match_date_raw = enriched.get("match_date")
                if hasattr(match_date_raw, "date"):
                    match_date_d = match_date_raw.date()
                elif isinstance(match_date_raw, str):
                    from datetime import date as _date2
                    match_date_d = _date2.fromisoformat(match_date_raw[:10])
                else:
                    match_date_d = _date.today()
                cb_sigs = await generate_corners_signals(
                    home_team  = enriched.get("home_team", ""),
                    away_team  = enriched.get("away_team", ""),
                    league     = enriched.get("league", ""),
                    match_date = match_date_d,
                )
                if cb_sigs:
                    await save_signals(cb_sigs, enriched.get("match_id", ""), enriched)
                signals_generated += len(cb_sigs)
            except Exception:
                logger.error("analyze: error corners_bookings %s", enriched.get("match_id"), exc_info=True)

        # --- Tenis — solo partidos en las próximas 48h ---
        try:
            from analyzers.tennis_analyzer import generate_tennis_signals
            tennis_docs = [
                d for d in upcoming_docs_raw
                if d.to_dict().get("sport") == "tennis"
                and str(d.to_dict().get("match_id", "")) in upcoming_ids_48h
            ]
            logger.info("analyze: %d partidos de tenis (48h) a analizar", len(tennis_docs))
            for tdoc in tennis_docs:
                match = tdoc.to_dict()
                try:
                    sigs = await generate_tennis_signals(match, weights_version)
                    signals_generated += len(sigs)
                except Exception:
                    logger.error("analyze: error tennis %s", match.get("match_id"), exc_info=True)
        except Exception:
            logger.error("analyze: error cargando tennis_analyzer", exc_info=True)

        # --- Baloncesto — solo partidos en las próximas 48h ---
        try:
            from analyzers.basketball_analyzer import generate_basketball_signals
            bball_docs = [
                d for d in upcoming_docs_raw
                if d.to_dict().get("sport") in ("nba", "basketball")
                and str(d.to_dict().get("match_id", "")) in upcoming_ids_48h
            ]
            logger.info("analyze: %d partidos de baloncesto (48h) a analizar", len(bball_docs))
            for bdoc in bball_docs:
                game = bdoc.to_dict()
                try:
                    sigs = await generate_basketball_signals(game, weights_version)
                    signals_generated += len(sigs)
                    if sigs:
                        _dedup_signals_for_match(str(game.get("match_id", "")), sigs)
                except Exception:
                    logger.error("analyze: error basketball %s", game.get("match_id"), exc_info=True)
        except Exception:
            logger.error("analyze: error cargando basketball_analyzer", exc_info=True)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        _status["last_analyze"] = datetime.now(timezone.utc).isoformat()

        # Diagnóstico odds-api.io — log al final donde Cloud Logging no dropa
        try:
            from collectors.odds_apiio_client import _EVENT_CACHE as _oaio_ev_cache, _SPORTS_CACHE as _oaio_sp_cache
            from shared.config import ODDSAPIIO_KEY as _oaio_key
            _oaio_summary = {
                lg: f"{len(e['events'])}{'(err)' if e.get('error') else ''}"
                for lg, e in _oaio_ev_cache.items()
            }
            logger.info(
                "analyze[diag]: oddsapiio key_set=%s sports_cached=%d event_cache=%s",
                bool(_oaio_key), len(_oaio_sp_cache), _oaio_summary,
            )
        except Exception as _diag_e:
            logger.warning("analyze[diag]: error leyendo oaio cache — %s", _diag_e)

        logger.info(
            "analyze: %d senales generadas de %d enriquecidos en %.1fs",
            signals_generated, len(docs), elapsed,
        )

        # Arbitrage detection — corre al final de cada analyze en background
        # para no bloquear el streaming response ni penalizar el tiempo de analyze.
        asyncio.create_task(_bg_arb())
        logger.info("analyze: arbitrage detector lanzado en background")

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

        # Limpiar FINISHED >48h después de que el learning haya evaluado resultados
        try:
            deleted = await _cleanup_stale_upcoming()
            if deleted:
                logger.info("learning.cleanup: %d partidos FINISHED eliminados de upcoming_matches", deleted)
        except Exception as exc:
            logger.warning("learning.cleanup: error no crítico — %s", exc)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("learning: completado en %.1fs", elapsed)

    except Exception as e:
        logger.error("learning: error no controlado — %s", e, exc_info=True)


async def _bg_sports_backtest() -> None:
    """
    Backtest de calidad histórico (backtest_engine.run_full_backtest).
    Llama a API-Football para 5 ligas × 3 temporadas, calcula ROI/sharpe/CLV,
    auto-calibra thresholds y guarda en Firestore backtest_results.
    Puede tardar 30-60 min — retorna 202 inmediatamente.
    """
    global _backtest_status
    _backtest_status["running"] = True
    _backtest_status["started_at"] = datetime.now(timezone.utc).isoformat()
    _backtest_status["finished_at"] = None
    _backtest_status["result"] = None
    _backtest_status["error"] = None

    try:
        logger.info("sports-backtest: iniciando backtest histórico de calidad")
        start = datetime.now(timezone.utc)

        api_key = os.environ.get("FOOTBALL_RAPID_API_KEY", "")
        from backtester.backtest_engine import run_full_backtest
        summary = await run_full_backtest(api_key=api_key)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("sports-backtest: completado en %.0fs — %s", elapsed, summary)
        _backtest_status["result"] = summary
        _backtest_status["finished_at"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        logger.error("sports-backtest: error no controlado — %s", e, exc_info=True)
        _backtest_status["error"] = str(e)
        _backtest_status["finished_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        _backtest_status["running"] = False


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


async def _bg_arb() -> None:
    """
    Pipeline de detección de arbitraje:
    1. Lee enriched_matches con odds_current != None de las últimas 24h.
    2. Formatea para arbitrage_detector.detect_and_store_arbitrage.
    3. Envía alerta Telegram por cada arb encontrado.
    """
    try:
        logger.info("arb: iniciando detección de arbitraje")
        from shared.firestore_client import col
        from collectors.arbitrage_detector import detect_and_store_arbitrage, format_arb_telegram

        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        try:
            # Firestore no soporta != con None — filtrar por status en Python
            raw_docs = list(
                col("enriched_matches")
                .where(filter=FieldFilter("status", "in", ["SCHEDULED", "TIMED"]))
                .stream()
            )
        except Exception as e:
            logger.warning("arb: error leyendo enriched_matches — %s", e)
            raw_docs = []

        # Filtrar por las últimas 24h y que tengan odds_current (en Python)
        enriched_docs: list[dict] = []
        for d in raw_docs:
            data = d.to_dict()
            match_date = data.get("match_date") or data.get("updated_at") or ""
            if isinstance(match_date, str) and match_date >= cutoff_24h:
                enriched_docs.append(data)
            elif hasattr(match_date, "isoformat") and match_date.isoformat() >= cutoff_24h:
                enriched_docs.append(data)

        logger.info("arb: %d partidos enriquecidos con cuotas en las últimas 24h", len(enriched_docs))

        # Construir estructura markets para el detector
        markets: list[dict] = []
        for enriched in enriched_docs:
            odds_current = enriched.get("odds_current")
            if not odds_current or not isinstance(odds_current, dict):
                continue

            bookmakers: list[dict] = []
            # odds_current puede ser {bookmaker: {home, draw, away}} o lista
            if isinstance(odds_current, dict):
                for bm_name, bm_odds in odds_current.items():
                    if isinstance(bm_odds, dict):
                        bookmakers.append({
                            "name": bm_name,
                            "home_odds": bm_odds.get("home") or bm_odds.get("1") or 0,
                            "draw_odds": bm_odds.get("draw") or bm_odds.get("X") or 0,
                            "away_odds": bm_odds.get("away") or bm_odds.get("2") or 0,
                        })

            if not bookmakers:
                continue

            markets.append({
                "match": f"{enriched.get('home_team', '')} vs {enriched.get('away_team', '')}",
                "home": enriched.get("home_team", ""),
                "away": enriched.get("away_team", ""),
                "league": enriched.get("league", ""),
                "bookmakers": bookmakers,
            })

        arbs = await detect_and_store_arbitrage(markets)

        from collectors.arbitrage_detector import build_arb_prediction
        from shared.firestore_client import col as _col_arb
        telegram_url = os.environ.get("TELEGRAM_BOT_URL", "")
        cloud_run_token = os.environ.get("CLOUD_RUN_TOKEN", "")
        sent = 0
        for idx, arb in enumerate(arbs):
            try:
                # Guardar en predictions con market_type=ARBITRAGE
                match_id_arb = f"arb_{int(datetime.now(timezone.utc).timestamp())}_{idx}"
                pred = build_arb_prediction(arb, match_id_arb)
                try:
                    _col_arb("predictions").document(pred["match_id"]).set(pred)
                except Exception as _fe:
                    logger.warning("arb: error guardando prediction arb — %s", _fe)

                # Enviar alerta Telegram con formato 💎
                message = format_arb_telegram(arb)
                if telegram_url:
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(
                            f"{telegram_url}/send-alert",
                            headers={"x-cloud-token": cloud_run_token},
                            json={"type": "arbitrage", "data": {"message": message, "arb": pred}},
                        )
                    sent += 1
            except Exception as e:
                logger.warning("arb: error procesando arb — %s", e)

        logger.info("arb: %d arbs encontrados, %d alertas Telegram enviadas", len(arbs), sent)

    except Exception as e:
        logger.error("arb: error no controlado — %s", e, exc_info=True)


async def _bg_fdco_collect() -> None:
    """
    Descarga stats de corners/tarjetas de football-data.co.uk para todas las ligas.
    Un CSV por liga (~300KB) — sin cuota de API, sin coste.
    """
    try:
        logger.info("fdco-collect: iniciando descarga de stats corners/tarjetas")
        from collectors.fdco_collector import run_all_leagues

        results = await run_all_leagues(season_year=2025)
        total_teams = sum(results.values())
        logger.info(
            "fdco-collect: completado — %d equipos guardados en %d ligas: %s",
            total_teams, len(results), results,
        )
    except Exception as e:
        logger.error("fdco-collect: error no controlado — %s", e, exc_info=True)
