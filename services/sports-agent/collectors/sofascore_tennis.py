"""
Colector de tenis vía Sofascore — Roland Garros y torneos de tierra.

Qué puede hacer Sofascore para tenis:
  ✓ Resultados por torneo (RG 2025, RG 2024...) con groundType="Red clay"
  ✓ Stats por partido: aces, DFs, 1st serve %, winners, UEs, BPs (via /event/{id}/statistics)
  ✓ Partidos próximos en curso de torneo (next/0, next/1...)
  ✗ Historial cross-torneo por jugador (/player/{id}/events → 404 para tenistas)
  ✗ Rankings ATP/WTA (/rankings/tennis/... → 404)

Uso principal:
  1. Fallback cuando RapidAPI está caído — genera upcoming_matches desde RG draw de Sofascore
  2. Clay form específica de Roland Garros (RG 2025 + 2026 completados)
  3. Enriquece team_stats con clay_rg_win_rate para mejorar señales de tierra
"""
import asyncio
import logging
from datetime import datetime, timezone

from collectors.sofascore_client import (
    TOURNAMENTS,
    fetch_tournament_events,
    normalize_name,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Temporadas Roland Garros disponibles (más recientes primero)
_RG_ATP_SEASONS = TOURNAMENTS["roland_garros_atp"]["seasons"]  # {2026: 85951, 2025: 61364}
_RG_WTA_SEASONS = TOURNAMENTS["roland_garros_wta"]["seasons"]  # {2026: 85953, 2025: 61366}
_RG_ATP_ID = TOURNAMENTS["roland_garros_atp"]["id"]   # 2480
_RG_WTA_ID = TOURNAMENTS["roland_garros_wta"]["id"]   # 2577


async def _fetch_rg_completed_matches(tournament_id: int, season_id: int, max_pages: int = 8) -> list[dict]:
    """
    Obtiene todos los partidos terminados de una temporada de Roland Garros.
    max_pages=8 cubre los ~128 partidos del cuadro individual masculino.
    """
    all_events: list[dict] = []
    for page in range(max_pages):
        events = await fetch_tournament_events(tournament_id, season_id, page=page, direction="last")
        if not events:
            break
        all_events.extend(events)
        # RG tiene ~128 partidos → 8 páginas × 20 los cubren todos
        if len(events) < 15:
            break
    logger.info(
        "sofascore_tennis: RG torneo=%d season=%d → %d partidos terminados (%d páginas buscadas)",
        tournament_id, season_id, len(all_events), page + 1,
    )
    return all_events


def _build_player_clay_stats(events: list[dict]) -> dict[str, dict]:
    """
    Construye historial clay en Roland Garros por jugador.
    Clave: nombre normalizado. Valor: {wins, losses, last_5, rounds_reached}
    """
    stats: dict[str, dict] = {}

    for e in events:
        if (e.get("status") or {}).get("type") != "finished":
            continue
        winner_code = e.get("winnerCode")  # 1=home, 2=away
        ht = e.get("homeTeam") or {}
        at = e.get("awayTeam") or {}
        home_name = normalize_name(ht.get("name") or ht.get("shortName") or "")
        away_name = normalize_name(at.get("name") or at.get("shortName") or "")
        round_name = (e.get("roundInfo") or {}).get("name", "")

        if not home_name or not away_name or winner_code not in (1, 2):
            continue

        for name, won in ((home_name, winner_code == 1), (away_name, winner_code == 2)):
            if name not in stats:
                stats[name] = {"wins": 0, "losses": 0, "results": [], "rounds": set()}
            stats[name]["wins" if won else "losses"] += 1
            stats[name]["results"].append("W" if won else "L")
            if round_name:
                stats[name]["rounds"].add(round_name)

    # Calcular win_rate y last_5 (más recientes primero del torneo = más relevante)
    result: dict[str, dict] = {}
    for name, s in stats.items():
        total = s["wins"] + s["losses"]
        win_rate = round(s["wins"] / total, 4) if total else 0.5
        last_5 = s["results"][:5]
        # form_score ponderado: peso mayor a partidos más recientes en el torneo
        weights = [1.0, 0.8, 0.6, 0.4, 0.2]
        weighted = sum(
            (1.0 if r == "W" else 0.0) * weights[i]
            for i, r in enumerate(last_5[:5])
        )
        total_w = sum(weights[:len(last_5)])
        form = round(weighted / total_w, 4) if total_w else 0.5
        result[name] = {
            "clay_rg_wins": s["wins"],
            "clay_rg_losses": s["losses"],
            "clay_rg_win_rate": win_rate,
            "clay_rg_form": form,
            "clay_rg_matches": total,
            "clay_rg_deepest_round": max(s["rounds"], key=len) if s["rounds"] else "",
        }

    logger.info("sofascore_tennis: historial RG clay → %d jugadores", len(result))
    return result


async def build_rg_clay_cache(include_previous: bool = True) -> dict[str, dict]:
    """
    Construye caché de historial clay en Roland Garros.
    include_previous=True agrega temporada anterior (más contexto histórico).
    Prioridad: temporada actual > temporada anterior.
    """
    combined: dict[str, dict] = {}

    # Temporada anterior (más historia)
    if include_previous:
        prev_season_id = list(_RG_ATP_SEASONS.values())[1] if len(_RG_ATP_SEASONS) > 1 else None
        if prev_season_id:
            prev_events = await _fetch_rg_completed_matches(_RG_ATP_ID, prev_season_id)
            combined.update(_build_player_clay_stats(prev_events))
            await asyncio.sleep(0.5)

    # Temporada actual (sobrescribe si hay datos más recientes)
    current_season_id = list(_RG_ATP_SEASONS.values())[0]
    current_events = await _fetch_rg_completed_matches(_RG_ATP_ID, current_season_id)
    if current_events:
        current_stats = _build_player_clay_stats(current_events)
        combined.update(current_stats)

    return combined


async def enrich_players_with_rg_clay(player_ids: list[str], clay_cache: dict[str, dict] | None = None) -> None:
    """
    Enriquece team_stats de los jugadores dados con datos clay de Roland Garros.
    Si clay_cache es None, lo construye desde Sofascore.
    player_ids: lista de player_id (strings) tal como se guardan en Firestore.
    """
    if clay_cache is None:
        clay_cache = await build_rg_clay_cache()

    if not clay_cache:
        logger.info("sofascore_tennis: clay_cache vacío — sin datos RG disponibles")
        return

    updated = 0
    for pid in player_ids:
        try:
            doc_ref = col("team_stats").document(str(pid))
            doc = doc_ref.get()
            if not doc.exists:
                continue
            data = doc.to_dict()
            player_name = normalize_name(data.get("name") or "")
            if not player_name:
                continue

            clay_stats = clay_cache.get(player_name)
            if not clay_stats:
                # Intento parcial: apellido solamente
                parts = player_name.split()
                if parts:
                    last_name = parts[-1]
                    clay_stats = next(
                        (v for k, v in clay_cache.items() if last_name in k and len(last_name) > 3),
                        None,
                    )

            if clay_stats:
                update = {
                    **clay_stats,
                    "win_rate_clay": clay_stats["clay_rg_win_rate"],
                    "sofascore_clay_updated_at": datetime.now(timezone.utc),
                }
                doc_ref.update(update)
                updated += 1
                logger.info(
                    "sofascore_tennis: enriquecido %s — clay_rg=%d/%d (%.0f%%)",
                    data.get("name"), clay_stats["clay_rg_wins"], clay_stats["clay_rg_matches"],
                    clay_stats["clay_rg_win_rate"] * 100,
                )
        except Exception:
            logger.warning("sofascore_tennis: error enriqueciendo player %s", pid, exc_info=True)

    logger.info("sofascore_tennis: %d/%d jugadores enriquecidos con clay RG", updated, len(player_ids))


async def collect_rg_upcoming_as_fallback(tour: str = "atp") -> list[dict]:
    """
    Obtiene próximos partidos de Roland Garros desde Sofascore.
    Úsalo como fallback cuando RapidAPI devuelve 0 torneos.

    tour: 'atp' o 'wta'
    """
    from collectors.tennis_collector import _fetch_oddsapiio_h2h_odds, _norm_name

    t_id = _RG_ATP_ID if tour == "atp" else _RG_WTA_ID
    season_map = _RG_ATP_SEASONS if tour == "atp" else _RG_WTA_SEASONS
    season_id = list(season_map.values())[0]  # más reciente
    league_code = "ATP_FRENCH_OPEN" if tour == "atp" else "WTA_FRENCH_OPEN"

    # Pre-fetch odds h2h en bulk (1 sola request) para embeber en cada partido
    try:
        odds_by_pair = await _fetch_oddsapiio_h2h_odds()
    except Exception:
        logger.debug("sofascore_tennis: oddsapiio bulk fetch falló — sin odds embebidas")
        odds_by_pair = {}

    result: list[dict] = []
    for page in range(3):
        events = await fetch_tournament_events(t_id, season_id, page=page, direction="next")
        if not events:
            break
        for e in events:
            ht = e.get("homeTeam") or {}
            at = e.get("awayTeam") or {}
            ts = e.get("startTimestamp")
            match_date = (
                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                if ts else ""
            )
            status_type = (e.get("status") or {}).get("type", "notstarted")
            home_id = str(ht.get("id", ""))
            away_id = str(at.get("id", ""))
            if not home_id or not away_id:
                continue
            home_name = ht.get("name", "")
            away_name = at.get("name", "")
            match_doc: dict = {
                "match_id": f"RG_SF_{e['id']}",
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_team": home_name,
                "away_team": away_name,
                "home_team_name": home_name,
                "away_team_name": away_name,
                "league": league_code,
                "sport": "tennis",
                "surface": "clay",
                "tournament": "Roland Garros",
                "source": "sofascore",
                "date": match_date,
                "status": "FINISHED" if status_type == "finished" else "SCHEDULED",
            }
            # Embeber odds si están disponibles (evita fetch HTTP extra en analyze)
            h_norm = _norm_name(home_name)[:8]
            a_norm = _norm_name(away_name)[:8]
            odds_pair = odds_by_pair.get(f"{h_norm}|{a_norm}", {})
            if odds_pair:
                match_doc["home_odds"]      = odds_pair["home_odds"]
                match_doc["away_odds"]      = odds_pair["away_odds"]
                match_doc["odds_bookmaker"] = odds_pair["bookmaker"]
            result.append(match_doc)

    logger.info(
        "sofascore_tennis: %d partidos próximos RG %s (season=%d, con_odds=%d)",
        len(result), tour.upper(), season_id,
        sum(1 for m in result if m.get("home_odds")),
    )
    return result
