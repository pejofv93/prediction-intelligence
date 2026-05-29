"""
Enriquecedor de fútbol vía Sofascore — Champions League y ligas europeas.

Qué aporta vs football-data.org (free tier):
  ✓ Historial de equipo con más partidos (20 por página, sin límite duro de 10)
  ✓ hasXg flag por partido — identifica partidos con datos xG reales
  ✓ Cubre ligas no disponibles en football-data.org free (Bundesliga segunda división, etc.)
  ✓ Champions League con datos de equipo completos (no solo partidos del grupo)
  ✗ xG absoluto requiere call adicional por partido (/event/{id}/statistics)
     → implementado con límite de 5 partidos por equipo para no sobrecargar

Estrategia de ID mapping:
  football-data.org team_id ≠ Sofascore team_id.
  Solución: fetch de standings de CL → tabla {nombre_normalizado: sf_id}.
  Para equipos que no aparecen en CL standings, se intenta búsqueda por nombre.
"""
import asyncio
import logging
from datetime import datetime, timezone

from collectors.sofascore_client import (
    TOURNAMENTS,
    fetch_team_events,
    fetch_tournament_standings,
    fetch_event_statistics,
    normalize_name,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_CL_TOURNAMENT_ID = TOURNAMENTS["champions_league"]["id"]    # 7
_CL_SEASON_2425   = TOURNAMENTS["champions_league"]["seasons"][2425]   # 61644
_CL_SEASON_2526   = TOURNAMENTS["champions_league"]["seasons"][2526]   # 76953

# Caché en proceso (evita re-fetch del mismo equipo en la misma ejecución)
_SF_TEAM_MAP: dict[str, int] = {}       # {nombre_normalizado: sf_team_id}
_SF_TEAM_MAP_LOADED = False


async def _load_cl_team_map(season_id: int | None = None) -> dict[str, int]:
    """
    Construye {nombre_normalizado → sofascore_team_id} desde standings de CL.
    Intenta temporada actual (2526) y anterior (2425) para máxima cobertura.
    """
    global _SF_TEAM_MAP, _SF_TEAM_MAP_LOADED
    if _SF_TEAM_MAP_LOADED:
        return _SF_TEAM_MAP

    seasons = [_CL_SEASON_2526, _CL_SEASON_2425] if season_id is None else [season_id]
    for sid in seasons:
        rows = await fetch_tournament_standings(_CL_TOURNAMENT_ID, sid)
        for row in rows:
            team = row.get("team") or {}
            name = team.get("name") or team.get("shortName") or ""
            sf_id = team.get("id")
            if name and sf_id:
                _SF_TEAM_MAP[normalize_name(name)] = int(sf_id)
                # También indexar shortName si distinto
                short = team.get("shortName") or ""
                if short and normalize_name(short) != normalize_name(name):
                    _SF_TEAM_MAP[normalize_name(short)] = int(sf_id)
        if _SF_TEAM_MAP:
            break

    _SF_TEAM_MAP_LOADED = True
    logger.info("sofascore_football: CL team map cargado — %d equipos", len(_SF_TEAM_MAP))
    return _SF_TEAM_MAP


def _find_sf_team_id(team_name: str, team_map: dict[str, int]) -> int | None:
    """Busca Sofascore team ID por nombre (exacto → apellido → parcial)."""
    norm = normalize_name(team_name)
    if norm in team_map:
        return team_map[norm]
    # Coincidencia parcial: alguna palabra del nombre del equipo
    parts = [p for p in norm.split() if len(p) > 3]
    for part in parts:
        for key, sf_id in team_map.items():
            if part in key:
                return sf_id
    return None


async def _fetch_team_xg_from_sofascore(sf_team_id: int, max_xg_matches: int = 5) -> dict:
    """
    Obtiene últimos eventos del equipo + xG de los primeros max_xg_matches partidos
    que tienen hasXg=True. Limita las llamadas adicionales para no sobrecargar.
    """
    events = await fetch_team_events(sf_team_id, page=0)
    if not events:
        return {}

    xg_for_list: list[float] = []
    xg_against_list: list[float] = []
    xg_calls = 0

    for e in events:
        if xg_calls >= max_xg_matches:
            break
        if not e.get("hasXg"):
            continue
        event_id = e.get("id")
        if not event_id:
            continue

        try:
            stats_data = await fetch_event_statistics(event_id)
            stats_groups = stats_data.get("statistics", [])
            for group in stats_groups:
                if group.get("period") != "ALL":
                    continue
                for item in group.get("groups", []):
                    for stat in item.get("statisticsItems", []):
                        if stat.get("name", "").lower() in ("expected goals", "xg", "xgoals"):
                            try:
                                home_val = float(stat.get("home") or 0)
                                away_val = float(stat.get("away") or 0)
                                # Determinar si el equipo es local o visitante
                                is_home = e.get("homeTeam", {}).get("id") == sf_team_id
                                xg_for_list.append(home_val if is_home else away_val)
                                xg_against_list.append(away_val if is_home else home_val)
                            except (TypeError, ValueError):
                                pass
            xg_calls += 1
            await asyncio.sleep(0.2)  # cortesía mínima
        except Exception:
            logger.debug("sofascore_football: error fetch xG event %d", event_id, exc_info=True)

    result: dict = {}
    if xg_for_list:
        result["sf_xg_for"] = round(sum(xg_for_list) / len(xg_for_list), 3)
        result["sf_xg_against"] = round(sum(xg_against_list) / len(xg_against_list), 3)
        result["sf_xg_matches"] = len(xg_for_list)

    # Form score básico de los 20 últimos eventos (sin xG calls adicionales)
    wins = losses = draws = 0
    for e in events[:20]:
        status_type = (e.get("status") or {}).get("type", "")
        if status_type != "finished":
            continue
        wc = e.get("winnerCode")
        is_home = e.get("homeTeam", {}).get("id") == sf_team_id
        if wc == 0:
            draws += 1
        elif (wc == 1 and is_home) or (wc == 2 and not is_home):
            wins += 1
        else:
            losses += 1

    total = wins + losses + draws
    if total > 0:
        result["sf_form_score"] = round(((wins * 3 + draws) / (total * 3)) * 100, 1)
        result["sf_form_matches"] = total
        result["sf_form_wins"] = wins
        result["sf_form_losses"] = losses
        result["sf_form_draws"] = draws

    return result


async def enrich_cl_teams_sofascore(games: list[dict]) -> None:
    """
    Para partidos de Champions League, enriquece team_stats en Firestore
    con datos adicionales de Sofascore (form_score basado en 20 partidos + xG real).

    Sólo actúa sobre partidos con league='CL' para minimizar calls.
    """
    cl_games = [g for g in games if g.get("league") in ("CL", "UEFA Champions League", "CHAMPIONS_LEAGUE")]
    if not cl_games:
        return

    team_map = await _load_cl_team_map()
    if not team_map:
        logger.warning("sofascore_football: CL team map vacío — sin enriquecimiento")
        return

    enriched_teams: set[int] = set()

    for game in cl_games:
        for side in ("home", "away"):
            team_id = game.get(f"{side}_team_id")
            team_name = game.get(f"{side}_team", game.get(f"{side}_team_name", ""))
            if not team_id or not team_name:
                continue

            if team_id in enriched_teams:
                continue

            sf_id = _find_sf_team_id(team_name, team_map)
            if not sf_id:
                logger.debug(
                    "sofascore_football: sin Sofascore ID para '%s' — skip", team_name
                )
                continue

            try:
                xg_form = await _fetch_team_xg_from_sofascore(sf_id)
                if xg_form:
                    xg_form["sf_team_id"] = sf_id
                    xg_form["sf_updated_at"] = datetime.now(timezone.utc)
                    col("team_stats").document(str(team_id)).set(xg_form, merge=True)
                    logger.info(
                        "sofascore_football: %s (sf_id=%d) enriquecido — "
                        "sf_form=%.1f%% xG_for=%.2f xG_against=%.2f",
                        team_name, sf_id,
                        xg_form.get("sf_form_score", 0),
                        xg_form.get("sf_xg_for", 0),
                        xg_form.get("sf_xg_against", 0),
                    )
                    enriched_teams.add(team_id)
            except Exception:
                logger.warning(
                    "sofascore_football: error enriqueciendo %s", team_name, exc_info=True
                )

    logger.info(
        "sofascore_football: %d/%d equipos CL enriquecidos con Sofascore",
        len(enriched_teams), len({g.get("home_team_id") for g in cl_games} | {g.get("away_team_id") for g in cl_games}) - 1,
    )
