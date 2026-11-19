"""
Euroliga en Tendencias — Fase 1: historial de estadísticas de equipo por partido.

Fuentes (gratis, sin clave; uso personal, no comercial):
  feed incrowdsports  → calendario, fecha, códigos de equipo, marcador por cuartos y prórrogas
                        (collectors.basketball_collector._fetch_eur_season_games)
  live.euroleague.net → /api/Boxscore?gamecode={code}&seasoncode={season}: totales de equipo
                        (`totr`). El boxscore NO trae fecha ni código de equipo: salen del feed.
                        Stats[0] es el local; se comprueba con los puntos del feed.

Escribe (solo con dry_run=False):
  eur_games/{season}_{gamecode}   un doc por partido terminado (sin estadísticas de jugadores)
  eur_games_state/{season}        hash por partido → solo se reescribe lo nuevo o cambiado

Prórroga: se guarda `minutes` (200 = sin prórroga, +25 por prórroga) y `ot_periods`; los
puntos de equipo incluyen la prórroga (la graduación la cuenta, como las casas).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from collectors.basketball_collector import (
    _eur_season_code,
    _fetch_eur_season_games,
    _http_get,
)

logger = logging.getLogger(__name__)

BOXSCORE_URL = "https://live.euroleague.net/api/Boxscore?gamecode={code}&seasoncode={season}"
_SLEEP_S = 0.25
_REGULATION_MINUTES = 200   # 5 jugadores × 40 min
_OT_MINUTES = 25            # 5 jugadores × 5 min


def default_seasons(now: Optional[datetime] = None) -> list[str]:
    """Temporada en curso y las dos anteriores: E2024, E2025, E2026 en nov-2026."""
    return [_eur_season_code(now, offset=o) for o in (-2, -1, 0)]


def parse_minutes(raw) -> Optional[int]:
    """'225:00' → 225."""
    try:
        return int(str(raw).split(":")[0])
    except (TypeError, ValueError):
        return None


def ot_from_minutes(minutes: Optional[int]) -> Optional[int]:
    if minutes is None or minutes < _REGULATION_MINUTES:
        return None
    return round((minutes - _REGULATION_MINUTES) / _OT_MINUTES)


def ot_from_feed(game: dict) -> int:
    q = (game.get("home") or {}).get("quarters") or {}
    return sum(1 for i in range(1, 6) if q.get(f"ot{i}") is not None)


def _n(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def team_totals(totr: dict) -> dict:
    fg2m, fg2a = _n(totr.get("FieldGoalsMade2")), _n(totr.get("FieldGoalsAttempted2"))
    fg3m, fg3a = _n(totr.get("FieldGoalsMade3")), _n(totr.get("FieldGoalsAttempted3"))
    ftm, fta = _n(totr.get("FreeThrowsMade")), _n(totr.get("FreeThrowsAttempted"))
    oreb, dreb = _n(totr.get("OffensiveRebounds")), _n(totr.get("DefensiveRebounds"))
    tov = _n(totr.get("Turnovers"))
    return {
        "pts": _n(totr.get("Points")),
        "reb": _n(totr.get("TotalRebounds")), "oreb": oreb, "dreb": dreb,
        "ast": _n(totr.get("Assistances")),
        "fg2m": fg2m, "fg2a": fg2a, "fg3m": fg3m, "fg3a": fg3a, "ftm": ftm, "fta": fta,
        "tov": tov, "stl": _n(totr.get("Steals")), "blk": _n(totr.get("BlocksFavour")),
        "fouls": _n(totr.get("FoulsCommited")),
        # Posesiones estimadas: FGA + 0,44·FTA − OREB + TOV
        "poss_est": round(fg2a + fg3a + 0.44 * fta - oreb + tov, 1),
    }


def build_game_doc(game: dict, box: dict) -> tuple[Optional[dict], Optional[str]]:
    """Partido del feed + boxscore → (doc, None) o (None, motivo)."""
    if str(game.get("status") or "").lower() != "result":
        return None, "no_terminado"   # sin jugar, el boxscore llega con totales a 0
    stats = (box or {}).get("Stats") or []
    if len(stats) != 2 or not all(isinstance(s.get("totr"), dict) for s in stats):
        return None, "boxscore_sin_totales"
    home, away = game.get("home") or {}, game.get("away") or {}
    h_pts, a_pts = _n(home.get("score")), _n(away.get("score"))
    s0, s1 = team_totals(stats[0]["totr"]), team_totals(stats[1]["totr"])
    if (s0["pts"], s1["pts"]) == (h_pts, a_pts):
        h_tot, a_tot = s0, s1
    elif (s1["pts"], s0["pts"]) == (h_pts, a_pts):
        h_tot, a_tot = s1, s0
    else:
        return None, "puntos_no_cuadran"
    minutes = parse_minutes(stats[0]["totr"].get("Minutes"))
    ot = ot_from_minutes(minutes)
    ot_feed = ot_from_feed(game)
    if ot is None:
        ot = ot_feed
    elif ot != ot_feed:
        return None, "prorroga_no_cuadra"
    season = (game.get("season") or {}).get("code") or str(game.get("identifier", "_")).split("_")[0]
    doc = {
        "season": season,
        "gamecode": _n(game.get("code")),
        "date": str(game.get("date") or "")[:10] or None,
        "kickoff_utc": game.get("date"),
        "phase": (game.get("phaseType") or {}).get("code"),
        "round": (game.get("round") or {}).get("round"),
        "home_code": home.get("code"), "away_code": away.get("code"),
        "home_name": home.get("name"), "away_name": away.get("name"),
        "minutes": minutes, "ot_periods": ot,
        "home": h_tot, "away": a_tot,
        "total_pts": h_tot["pts"] + a_tot["pts"],
        "source": "euroleague_boxscore",
    }
    if not doc["date"]:
        return None, "sin_fecha"
    return doc, None


def doc_hash(doc: dict) -> str:
    payload = json.dumps({k: v for k, v in doc.items() if k not in ("updated_at", "fetched_at")},
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def finished(games: list[dict]) -> list[dict]:
    return [g for g in games if str(g.get("status") or "").lower() == "result"]


def _load_state(season: str) -> dict:
    from shared.firestore_client import col
    snap = col("eur_games_state").document(season).get()
    return (snap.to_dict() or {}).get("hashes", {}) if snap.exists else {}


def _write(season: str, docs: dict[str, dict], old: dict) -> int:
    from shared.firestore_client import col, get_client
    new_hashes = {d: doc_hash(doc) for d, doc in docs.items()}
    changed = [d for d, h in new_hashes.items() if old.get(d) != h]
    if not changed:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    client = get_client()
    for i in range(0, len(changed), 400):
        batch = client.batch()
        for doc_id in changed[i:i + 400]:
            batch.set(col("eur_games").document(doc_id), {**docs[doc_id], "fetched_at": now})
        batch.commit()
    col("eur_games_state").document(season).set({"hashes": {**old, **new_hashes}, "updated_at": now})
    return len(changed)


async def run_eur_boxscores(seasons: Optional[list[str]] = None, dry_run: bool = True,
                            max_games: int = 0, only_missing: bool = True) -> dict:
    """
    seasons: códigos 'E2024'… (por defecto las 3 últimas).
    max_games: tope de boxscores por temporada (0 = sin tope; útil en dry-run).
    only_missing: con estado guardado, solo pide boxscores de partidos que no estén en el
                  estado (los terminados no cambian) → el mantenimiento diario pide 0-10.
    """
    seasons = seasons or default_seasons()
    loop = asyncio.get_running_loop()
    summary: dict = {"ok": True, "dry_run": dry_run, "seasons": {}, "errors": [],
                     "requests": {"feed_seasons": 0, "boxscores": 0}}
    for season in seasons:
        s: dict = {"feed_games": 0, "finished": 0, "boxscores": 0, "docs": 0, "written": 0,
                   "ot_games": 0, "skipped": {}, "phases": {}}
        try:
            games = await loop.run_in_executor(None, _fetch_eur_season_games, season)
            summary["requests"]["feed_seasons"] += 1
            s["feed_games"] = len(games)
            done = finished(games)
            s["finished"] = len(done)
            old = {}
            try:
                old = await loop.run_in_executor(None, _load_state, season)
            except Exception as e:  # sin Firestore (p. ej. en local) el dry-run sigue
                logger.warning("eur_boxscores: sin estado de %s (%s)", season, e)
            todo = [g for g in done if not (only_missing and f"{season}_{_n(g.get('code'))}" in old)]
            if max_games:
                todo = todo[:max_games]
            docs: dict[str, dict] = {}
            for g in todo:
                box = await loop.run_in_executor(
                    None, _http_get, BOXSCORE_URL.format(code=_n(g.get("code")), season=season))
                summary["requests"]["boxscores"] += 1
                s["boxscores"] += 1
                doc, why = build_game_doc(g, box) if isinstance(box, dict) else (None, "boxscore_error")
                if doc is None:
                    s["skipped"][why] = s["skipped"].get(why, 0) + 1
                else:
                    docs[f"{season}_{doc['gamecode']}"] = doc
                    s["ot_games"] += 1 if doc["ot_periods"] else 0
                    s["phases"][doc["phase"]] = s["phases"].get(doc["phase"], 0) + 1
                await asyncio.sleep(_SLEEP_S)
            s["docs"] = len(docs)
            if docs:
                pts = [d["total_pts"] for d in docs.values()]
                poss = [(d["home"]["poss_est"] + d["away"]["poss_est"]) / 2 for d in docs.values()]
                s["avg_total_pts"] = round(sum(pts) / len(pts), 1)
                s["avg_poss"] = round(sum(poss) / len(poss), 1)
                dates = sorted(d["date"] for d in docs.values())
                s["first_date"], s["last_date"] = dates[0], dates[-1]
            if not dry_run and docs:
                s["written"] = await loop.run_in_executor(None, _write, season, docs, old)
            else:
                s["written"] = sum(1 for d, doc in docs.items() if old.get(d) != doc_hash(doc))
        except Exception as e:
            logger.error("eur_boxscores: error en %s", season, exc_info=True)
            summary["errors"].append(f"{season}: {e}")
        summary["seasons"][season] = s
    summary["ok"] = not summary["errors"]
    logger.info("eur_boxscores: %s", summary)
    return summary
