"""
Enriquecedor de fútbol vía Sofascore — todas las competiciones europeas + Brasil + mujeres.

Qué aporta vs football-data.org (free tier):
  ✓ xG real por partido (hasXg=true en 82% de PL/CL/La Liga)
  ✓ Historial de 20 partidos (vs máximo 10 del free tier)
  ✓ Cubre EL, ECL (mismos IDs que CL) sin restricciones
  ✓ Liga Femenina, WSL, UCL Femenina, Brasileirao (no en football-data.org free)

Estrategia de ID mapping:
  football-data.org team_id ≠ Sofascore team_id.
  Solución: standings de cada competición → {nombre_norm: sf_id}.
  Los nombres se normalizan (minúsculas, sin acentos) para fuzzy match.

Ligas cubiertas por el enriquecedor:
  CL, EL, ECL — standings disponibles las dos temporadas (2425, 2526)
  PL, PD, BL1, SA, FL1 — idem
  WSL, LIGA_F, WCL — fútbol femenino
  BSA — Brasileirao
"""
import asyncio
import logging
from datetime import datetime, timezone

from collectors.sofascore_client import (
    TOURNAMENTS,
    LEAGUE_CODE_TO_TOURNAMENT,
    fetch_team_events,
    fetch_tournament_standings,
    fetch_event_statistics,
    normalize_name,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Competiciones de las que cargamos el team map (en orden → primero cargado tiene prioridad)
_MAP_SOURCES: list[str] = [
    "champions_league", "europa_league", "conference_league",
    "premier_league", "laliga", "bundesliga", "serie_a", "ligue_1",
    "wsl", "liga_f", "women_cl", "brasileirao",
]

# Liga codes que el enriquecedor procesa (todas las que tenemos en Sofascore)
_ENRICHABLE_LEAGUE_CODES: set[str] = {
    "CL", "EL", "ECL",
    "PL", "PD", "BL1", "SA", "FL1",
    "WSL", "LIGA_F", "WCL", "BSA",
    # Alias que pueden aparecer en los game dicts
    "UEFA Champions League", "UEFA Europa League", "UEFA Conference League",
    "CHAMPIONS_LEAGUE", "EUROPA_LEAGUE", "CONFERENCE_LEAGUE",
}

# Caché global de proceso: {nombre_norm: sf_team_id}
_SF_TEAM_MAP: dict[str, int] = {}
_SF_TEAM_MAP_LOADED = False


async def _load_multi_league_team_map() -> dict[str, int]:
    """
    Construye {nombre_normalizado → sofascore_team_id} cargando standings de
    todas las competiciones en _MAP_SOURCES. Resultado cacheado en proceso.
    """
    global _SF_TEAM_MAP, _SF_TEAM_MAP_LOADED
    if _SF_TEAM_MAP_LOADED:
        return _SF_TEAM_MAP

    for key in _MAP_SOURCES:
        t = TOURNAMENTS.get(key)
        if not t:
            continue
        t_id = t["id"]
        # Intentar temporada más reciente, luego anterior
        season_ids = sorted(t.get("seasons", {}).values(), reverse=True)
        for sid in season_ids[:2]:
            try:
                rows = await fetch_tournament_standings(t_id, sid)
                added = 0
                for row in rows:
                    team = row.get("team") or {}
                    sf_id = team.get("id")
                    if not sf_id:
                        continue
                    for name_field in ("name", "shortName"):
                        raw_name = team.get(name_field) or ""
                        if raw_name:
                            _SF_TEAM_MAP[normalize_name(raw_name)] = int(sf_id)
                            added += 1
                if added:
                    logger.debug(
                        "sofascore_football: mapa %s (season %d) → %d entradas",
                        key, sid, added,
                    )
                    break  # temporada OK, no necesitamos la anterior
            except Exception:
                logger.debug("sofascore_football: error cargando %s season %d", key, sid, exc_info=True)
        await asyncio.sleep(0.15)  # cortesía entre llamadas de standings

    _SF_TEAM_MAP_LOADED = True
    logger.info("sofascore_football: team map cargado — %d equipos de %d competiciones",
                len(_SF_TEAM_MAP), len(_MAP_SOURCES))
    return _SF_TEAM_MAP


def _find_sf_team_id(team_name: str, team_map: dict[str, int]) -> int | None:
    """Búsqueda por nombre exacto → palabra clave → parcial."""
    norm = normalize_name(team_name)
    if norm in team_map:
        return team_map[norm]
    # Palabras significativas del nombre (>3 chars)
    parts = [p for p in norm.split() if len(p) > 3]
    for part in parts:
        for key, sf_id in team_map.items():
            if part in key:
                return sf_id
    return None


async def _fetch_team_xg_and_form(sf_team_id: int, max_xg_matches: int = 5) -> dict:
    """
    Últimos ~20 eventos de un equipo:
      - Form score ponderado (20 partidos, W=3 D=1 L=0)
      - xG medio real de los primeros max_xg_matches con hasXg=True
    """
    events = await fetch_team_events(sf_team_id, page=0)
    if not events:
        return {}

    # --- xG real (requiere call por partido) ---
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
            for group in stats_data.get("statistics", []):
                if group.get("period") != "ALL":
                    continue
                for item in group.get("groups", []):
                    for stat in item.get("statisticsItems", []):
                        if stat.get("name", "").lower() in ("expected goals", "xg", "xgoals"):
                            try:
                                h_val = float(stat.get("home") or 0)
                                a_val = float(stat.get("away") or 0)
                                is_home = (e.get("homeTeam") or {}).get("id") == sf_team_id
                                xg_for_list.append(h_val if is_home else a_val)
                                xg_against_list.append(a_val if is_home else h_val)
                            except (TypeError, ValueError):
                                pass
            xg_calls += 1
            await asyncio.sleep(0.15)
        except Exception:
            logger.debug("sofascore_football: xG event %d error", event_id, exc_info=True)

    # --- Form score de 20 partidos ---
    wins = losses = draws = 0
    for e in events[:20]:
        if (e.get("status") or {}).get("type") != "finished":
            continue
        wc = e.get("winnerCode")
        is_home = (e.get("homeTeam") or {}).get("id") == sf_team_id
        if wc == 0:
            draws += 1
        elif (wc == 1 and is_home) or (wc == 2 and not is_home):
            wins += 1
        else:
            losses += 1

    result: dict = {}
    total = wins + losses + draws
    if total > 0:
        result.update({
            "sf_form_score": round(((wins * 3 + draws) / (total * 3)) * 100, 1),
            "sf_form_matches": total,
            "sf_form_wins": wins,
            "sf_form_losses": losses,
            "sf_form_draws": draws,
        })
    if xg_for_list:
        result.update({
            "sf_xg_for": round(sum(xg_for_list) / len(xg_for_list), 3),
            "sf_xg_against": round(sum(xg_against_list) / len(xg_against_list), 3),
            "sf_xg_matches": len(xg_for_list),
        })

    return result


async def enrich_teams_sofascore(games: list[dict]) -> None:
    """
    Enriquece team_stats en Firestore con datos Sofascore para todos los partidos
    cuya liga esté en _ENRICHABLE_LEAGUE_CODES.

    Guarda (merge=True): sf_form_score, sf_xg_for, sf_xg_against, sf_team_id, sf_updated_at
    Estos campos los usa data_enricher.py para ajustar el modelo Poisson con xG real.
    """
    target_games = [g for g in games if g.get("league") in _ENRICHABLE_LEAGUE_CODES]
    if not target_games:
        return

    team_map = await _load_multi_league_team_map()
    if not team_map:
        logger.warning("sofascore_football: team map vacío — sin enriquecimiento")
        return

    enriched: set[int] = set()

    for game in target_games:
        for side in ("home", "away"):
            team_id = game.get(f"{side}_team_id")
            team_name = game.get(f"{side}_team", game.get(f"{side}_team_name", ""))
            if not team_id or not team_name or team_id in enriched:
                continue

            sf_id = _find_sf_team_id(team_name, team_map)
            if not sf_id:
                logger.debug("sofascore_football: sin SF ID para '%s'", team_name)
                continue

            try:
                data = await _fetch_team_xg_and_form(sf_id)
                if data:
                    data["sf_team_id"] = sf_id
                    data["sf_updated_at"] = datetime.now(timezone.utc)
                    col("team_stats").document(str(team_id)).set(data, merge=True)
                    logger.info(
                        "sofascore_football: %s (%s sf=%d) "
                        "form=%.0f%% xG_for=%.2f xG_ag=%.2f matches=%d",
                        team_name, game.get("league", ""), sf_id,
                        data.get("sf_form_score", 0),
                        data.get("sf_xg_for", 0),
                        data.get("sf_xg_against", 0),
                        data.get("sf_xg_matches", 0),
                    )
                    enriched.add(team_id)
            except Exception:
                logger.warning("sofascore_football: error enriqueciendo %s", team_name, exc_info=True)

    logger.info(
        "sofascore_football: %d equipos enriquecidos (%d ligas cubiertas)",
        len(enriched), len({g.get("league") for g in target_games}),
    )


async def collect_sofascore_native_games(
    league_keys: list[str] | None = None,
) -> list[dict]:
    """
    Obtiene próximos partidos de ligas NO cubiertas por football-data.org
    (WSL, Liga F, UCL Femenina, Brasileirao) directamente desde Sofascore.

    league_keys: lista de claves de TOURNAMENTS a procesar.
                 Por defecto: WSL, Liga F, UCL Femenina, Brasileirao.
    """
    if league_keys is None:
        league_keys = ["wsl", "liga_f", "women_cl", "brasileirao"]

    from collectors.sofascore_client import fetch_tournament_events

    result: list[dict] = []
    for key in league_keys:
        t = TOURNAMENTS.get(key)
        if not t:
            continue
        t_id = t["id"]
        league_code = t.get("league_code", key.upper())
        season_id = max(t.get("seasons", {}).values())

        try:
            for page in range(2):
                events = await fetch_tournament_events(t_id, season_id, page=page, direction="next")
                if not events:
                    break
                for e in events:
                    ht = e.get("homeTeam") or {}
                    at = e.get("awayTeam") or {}
                    ts = e.get("startTimestamp")
                    match_date = (
                        datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                        if ts else ""
                    )
                    status_type = (e.get("status") or {}).get("type", "notstarted")
                    try:
                        result.append({
                            "match_id": f"{key.upper()}_SF_{e['id']}",
                            "home_team_id": int(ht["id"]),
                            "away_team_id": int(at["id"]),
                            "home_team": ht.get("name", ""),
                            "away_team": at.get("name", ""),
                            "league": league_code,
                            "sport": "football",
                            "source": "sofascore",
                            "match_date": match_date,
                            "status": "FINISHED" if status_type == "finished" else "SCHEDULED",
                        })
                    except (KeyError, TypeError):
                        continue
            logger.info("sofascore_football: %s → %d partidos próximos", key, len(result))
        except Exception:
            logger.warning("sofascore_football: error recogiendo %s", key, exc_info=True)

    return result
