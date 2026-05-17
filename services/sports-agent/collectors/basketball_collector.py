"""
Collector de baloncesto — NBA via ESPN + Euroleague + ACB via APIs gratuitas.

Fuentes:
  NBA        — ESPN public scoreboard API (sin key)
  EUROLEAGUE — feeds.incrowdsports.com (API oficial gratuita, sin key)
  ACB        — TheSportsDB id=4408 (API pública gratuita, sin key)
"""
import asyncio
import hashlib
import logging
import os
import urllib.request
import json as _json
from datetime import datetime, timedelta, timezone

from collectors.api_sports_client import (
    get_nba_games_espn,
    get_nba_team_stats_espn,
    get_team_stats_bdl,
)
from collectors.stats_processor import (
    build_results_list,
    calculate_form_score,
    detect_streak,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_EUR_GAMES_URL = (
    "https://feeds.incrowdsports.com/provider/euroleague-feeds/v2"
    "/competitions/E/seasons/E2025/games?limit=300"
)
_ACB_NEXT_URL = "https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id=4408"
_ACB_PREV_URL = "https://www.thesportsdb.com/api/v1/json/3/eventspastleague.php?id=4408"
_HTTP_TIMEOUT = 15


def _hash_team_id(code: str) -> int:
    """Convierte código de equipo en entero estable (para Euroleague)."""
    return int(hashlib.md5(code.encode()).hexdigest()[:8], 16) % 900_000 + 100_000


def _eur_status(raw: str) -> str:
    s = (raw or "").lower()
    if s == "result":
        return "FINISHED"
    if s == "live":
        return "LIVE"
    return "SCHEDULED"


def _http_get(url: str) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return _json.loads(r.read().decode())
    except Exception as e:
        logger.warning("_http_get(%s): %s", url, e)
        return None


async def _fetch_euroleague_games() -> list[dict]:
    """Partidos Euroleague desde la API oficial gratuita. Sin key."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _http_get, _EUR_GAMES_URL)
    if not data:
        return []

    games = data.get("data", []) if isinstance(data, dict) else []
    result: list[dict] = []

    for g in games:
        status = _eur_status(g.get("status", ""))
        phase = g.get("phaseType", {}).get("code", "")

        # Incluir: no terminados (upcoming/live) + siempre Final Four
        if status == "FINISHED" and phase != "FF":
            continue

        home = g.get("home", {})
        away = g.get("away", {})
        home_code = home.get("code", "") or home.get("tla", "H")
        away_code = away.get("code", "") or away.get("tla", "A")

        result.append({
            "match_id": f"EUR_{g['identifier']}",
            "home_team_id": _hash_team_id(home_code),
            "away_team_id": _hash_team_id(away_code),
            "home_team_name": home.get("name", home_code),
            "away_team_name": away.get("name", away_code),
            "league": "EUROLEAGUE",
            "sport": "basketball",
            "source": "euroleague_incrowd",
            "match_date": g.get("date", ""),
            "status": status,
            "phase": phase,
        })

    logger.info("_fetch_euroleague_games: %d partidos (no terminados + FF)", len(result))
    return result


async def _fetch_acb_games() -> list[dict]:
    """Próximos partidos ACB desde TheSportsDB (id=4408). Sin key."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _http_get, _ACB_NEXT_URL)
    events = (data or {}).get("events") or []

    result: list[dict] = []
    for e in events:
        home_id_raw = e.get("idHomeTeam")
        away_id_raw = e.get("idAwayTeam")
        try:
            home_id = int(home_id_raw) if home_id_raw else _hash_team_id(e.get("strHomeTeam", "H"))
            away_id = int(away_id_raw) if away_id_raw else _hash_team_id(e.get("strAwayTeam", "A"))
        except (ValueError, TypeError):
            home_id = _hash_team_id(e.get("strHomeTeam", "H"))
            away_id = _hash_team_id(e.get("strAwayTeam", "A"))

        has_score = e.get("intHomeScore") is not None and e.get("intHomeScore") != ""
        status = "FINISHED" if has_score else "SCHEDULED"

        result.append({
            "match_id": f"ACB_{e['idEvent']}",
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_team_name": e.get("strHomeTeam", ""),
            "away_team_name": e.get("strAwayTeam", ""),
            "league": "ACB",
            "sport": "basketball",
            "source": "thesportsdb",
            "match_date": f"{e.get('dateEvent', '')} {e.get('strTime', '')}".strip(),
            "status": status,
        })

    logger.info("_fetch_acb_games: %d partidos próximos ACB", len(result))
    return result


async def collect_basketball_games(days: int = 3) -> list[dict]:
    """
    Recopila partidos de baloncesto — hoy y próximos días.

    NBA:        ESPN public scoreboard API (gratuito, sin clave) — days días consecutivos.
    EUROLEAGUE: feeds.incrowdsports.com API oficial (gratuito, sin clave).
    ACB:        TheSportsDB id=4408 (gratuito, sin clave).
    """
    all_games: list[dict] = []

    # --- NBA via ESPN (hoy + próximos days-1 días para Playoffs) ---
    try:
        _nba_seen: set[str] = set()
        for _day_offset in range(max(1, days)):
            _date_str = (datetime.now(timezone.utc) + timedelta(days=_day_offset)).date().isoformat()
            try:
                day_games = await get_nba_games_espn(date_str=_date_str)
                new_games = [g for g in day_games if g.get("match_id") not in _nba_seen]
                for g in new_games:
                    _nba_seen.add(g["match_id"])
                all_games.extend(new_games)
                if new_games:
                    logger.info("basketball NBA (ESPN %s): %d partidos", _date_str, len(new_games))
            except Exception:
                logger.warning("basketball_collector: error colectando NBA %s", _date_str, exc_info=True)
        if not _nba_seen:
            logger.info("basketball NBA (ESPN): sin partidos en los próximos %d días", days)
    except Exception:
        logger.error("basketball_collector: error colectando NBA via ESPN", exc_info=True)

    # --- EUROLEAGUE via incrowdsports (gratuito) ---
    try:
        eur_games = await _fetch_euroleague_games()
        all_games.extend(eur_games)
    except Exception:
        logger.error("basketball_collector: error colectando Euroleague", exc_info=True)

    # --- ACB via TheSportsDB (gratuito) ---
    try:
        acb_games = await _fetch_acb_games()
        all_games.extend(acb_games)
    except Exception:
        logger.error("basketball_collector: error colectando ACB", exc_info=True)

    logger.info("basketball_collector: %d partidos totales de baloncesto", len(all_games))
    return all_games


async def _fetch_acb_team_last_games(team_id: int) -> list[dict]:
    """Últimos partidos de un equipo ACB desde TheSportsDB eventslast. Sin key."""
    url = f"https://www.thesportsdb.com/api/v1/json/3/eventslast.php?id={team_id}"
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _http_get, url)
    events = (data or {}).get("results") or []
    result: list[dict] = []
    for e in events:
        home_score = e.get("intHomeScore")
        away_score = e.get("intAwayScore")
        if home_score is None or home_score == "" or away_score is None or away_score == "":
            continue
        try:
            h_id_raw = e.get("idHomeTeam")
            a_id_raw = e.get("idAwayTeam")
            h_id = int(h_id_raw) if h_id_raw else _hash_team_id(e.get("strHomeTeam", "H"))
            a_id = int(a_id_raw) if a_id_raw else _hash_team_id(e.get("strAwayTeam", "A"))
            result.append({
                "goals_home": float(home_score),
                "goals_away": float(away_score),
                "home_team_id": h_id,
                "was_home": h_id == team_id,
                "match_date": e.get("dateEvent", ""),
            })
        except (ValueError, TypeError, KeyError):
            continue
    return result


async def _fetch_euroleague_history() -> list[dict]:
    """Partidos Euroleague terminados de la temporada actual. Sin key."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _http_get, _EUR_GAMES_URL)
    games = data.get("data", []) if isinstance(data, dict) else []
    result: list[dict] = []
    for g in games:
        if _eur_status(g.get("status", "")) != "FINISHED":
            continue
        home = g.get("home", {})
        away = g.get("away", {})
        home_code = home.get("code", "") or home.get("tla", "H")
        away_code = away.get("code", "") or away.get("tla", "A")
        home_score = home.get("score")
        away_score = away.get("score")
        if home_score is None or away_score is None:
            continue
        try:
            result.append({
                "match_id": f"EUR_{g['identifier']}",
                "home_team_id": _hash_team_id(home_code),
                "away_team_id": _hash_team_id(away_code),
                "home_team_name": home.get("name", home_code),
                "away_team_name": away.get("name", away_code),
                "goals_home": float(home_score),
                "goals_away": float(away_score),
                "match_date": (g.get("date") or "")[:10],
                "status": "FINISHED",
            })
        except (ValueError, TypeError, KeyError):
            continue
    logger.info("_fetch_euroleague_history: %d partidos Euroleague terminados", len(result))
    return result


async def collect_basketball_team_stats(games: list[dict]) -> None:
    """
    Para cada equipo en la lista de partidos, recopila sus últimos partidos
    y guarda team_stats enriquecido en Firestore.

    - Partidos ESPN (source='espn'): usa get_nba_team_stats_espn() — gratuito, sin clave.
    - Partidos api-basketball: usa get_team_stats_bdl() — requiere suscripción activa.
    - Partidos ACB (source='thesportsdb'): usa _fetch_acb_team_last_games() por equipo.
    - Partidos Euroleague (source='euroleague_incrowd'): usa _fetch_euroleague_history().
    """
    teams_seen: set[int] = set()

    # Pre-fetch histórico Euroleague (1 sola petición para ~300 partidos)
    eur_history: list[dict] = []
    if any(g.get("source") == "euroleague_incrowd" for g in games):
        try:
            eur_history = await _fetch_euroleague_history()
        except Exception:
            logger.warning("basketball_collector: error cargando historial Euroleague", exc_info=True)

    for game in games:
        source = game.get("source", "")

        for team_id_key in ("home_team_id", "away_team_id"):
            team_id = game.get(team_id_key)
            if not team_id or team_id in teams_seen:
                continue

            teams_seen.add(team_id)
            try:
                if source in ("thesportsdb", "euroleague_incrowd"):
                    if source == "thesportsdb":
                        team_matches = await _fetch_acb_team_last_games(team_id)
                    else:
                        team_matches = [
                            {
                                "goals_home": m["goals_home"],
                                "goals_away": m["goals_away"],
                                "home_team_id": m["home_team_id"],
                                "was_home": m["home_team_id"] == team_id,
                                "match_date": m.get("match_date", ""),
                            }
                            for m in eur_history
                            if m.get("home_team_id") == team_id or m.get("away_team_id") == team_id
                        ]
                    if not team_matches:
                        logger.debug(
                            "basketball_collector: sin historial para team %d (%s)", team_id, source
                        )
                        continue
                    raw_matches_fmt = team_matches[:10]
                    results = [
                        "win" if (m["goals_home"] > m["goals_away"] and m["was_home"])
                              or (m["goals_away"] > m["goals_home"] and not m["was_home"])
                        else "loss"
                        for m in raw_matches_fmt
                    ]
                    form_score = calculate_form_score(results[:10])
                    streak = detect_streak(results[:10])
                    team_name = game.get(
                        "home_team_name" if team_id_key == "home_team_id" else "away_team_name",
                        f"Team_{team_id}",
                    )
                    doc = {
                        "team_id": team_id,
                        "team_name": team_name,
                        "league": game.get("league"),
                        "sport": "basketball",
                        "last_10": results[:10],
                        "form_score": form_score,
                        "streak": streak,
                        "raw_matches": raw_matches_fmt,
                        "xg_per_game": 0.0,
                        "source": source,
                        "updated_at": datetime.now(timezone.utc),
                    }
                elif source == "espn":
                    # ESPN schedule → raw_matches ya en formato correcto
                    raw_matches_fmt = await get_nba_team_stats_espn(team_id)
                    if not raw_matches_fmt:
                        logger.debug("basketball_collector: ESPN sin partidos completados para team %d", team_id)
                        continue

                    # Form score desde raw_matches ESPN — calculate_form_score espera list[str]
                    results = [
                        "win" if (m["goals_home"] > m["goals_away"] and m["was_home"])
                              or (m["goals_away"] > m["goals_home"] and not m["was_home"])
                        else "loss"
                        for m in raw_matches_fmt
                    ]
                    form_score = calculate_form_score(results[:10])
                    streak = detect_streak(results[:10])

                    # Nombre del equipo desde el game actual
                    if team_id == game.get("home_team_id"):
                        team_name = game.get("home_team_name", f"Team_{team_id}")
                    else:
                        team_name = game.get("away_team_name", f"Team_{team_id}")

                    doc = {
                        "team_id": team_id,
                        "team_name": team_name,
                        "league": game.get("league", "NBA"),
                        "sport": "nba",
                        "last_10": results[:10],
                        "form_score": form_score,
                        "streak": streak,
                        "raw_matches": raw_matches_fmt[:10],
                        "xg_per_game": 0.0,
                        "source": "espn",
                        "updated_at": datetime.now(timezone.utc),
                    }
                else:
                    sport = game.get("sport", "nba")
                    raw = await get_team_stats_bdl(sport, team_id, last_n=10)
                    if not raw:
                        logger.debug("basketball_collector: sin stats para team %d", team_id)
                        continue

                    results = build_results_list(raw, team_id)
                    form_score = calculate_form_score(results[:10])
                    streak = detect_streak(results[:10])

                    team_name = ""
                    for m in raw:
                        if m.get("home_team_id") == team_id:
                            team_name = m.get("home_team_name", "")
                            break
                        elif m.get("away_team_id") == team_id:
                            team_name = m.get("away_team_name", "")
                            break

                    raw_matches_fmt = [
                        {
                            "goals_home": m.get("goals_home") or 0,
                            "goals_away": m.get("goals_away") or 0,
                            "home_team_id": m["home_team_id"],
                            "was_home": m["home_team_id"] == team_id,
                            "match_date": m.get("date", ""),
                        }
                        for m in raw
                        if m.get("goals_home") is not None and m.get("goals_away") is not None
                    ]

                    doc = {
                        "team_id": team_id,
                        "team_name": team_name or f"Team_{team_id}",
                        "league": game.get("league", "NBA"),
                        "sport": sport,
                        "last_10": results[:10],
                        "form_score": form_score,
                        "streak": streak,
                        "raw_matches": raw_matches_fmt,
                        "xg_per_game": 0.0,
                        "updated_at": datetime.now(timezone.utc),
                    }

                col("team_stats").document(f"bball_{team_id}").set(doc)
                logger.info(
                    "basketball_collector: team_stats(%d) %s form=%.1f src=%s partidos=%d",
                    team_id, doc["team_name"], form_score, source or "api", len(raw_matches_fmt),
                )

            except Exception:
                logger.error("basketball_collector: error stats team %d", team_id, exc_info=True)
