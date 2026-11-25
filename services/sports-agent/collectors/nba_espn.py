"""
NBA en Tendencias — historial de estadísticas de equipo por partido desde ESPN (sin clave).

Fuentes (públicas, sin clave):
  scoreboard → site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
               partidos del día (id, terminado, tipo de temporada)
  summary    → site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={id}
               header.competitions[0].competitors[]: homeAway, score, linescores (cuartos+prórrogas)
               boxscore.teams[].statistics[]: {name, displayValue} por equipo
  Nombres de campo de ESPN que se usan (A CONFIRMAR con la sonda scripts/ops/espn_nba_probe.py
  antes de cargar nada): fieldGoalsMade-fieldGoalsAttempted, threePointFieldGoalsMade-
  threePointFieldGoalsAttempted, freeThrowsMade-freeThrowsAttempted ("40-85"), totalRebounds,
  offensiveRebounds, defensiveRebounds, assists, steals, blocks, totalTurnovers (o turnovers),
  fouls. Un partido sin alguno de los imprescindibles se salta con su motivo.

Escribe (solo con dry_run=False):
  nba_games/{season}_{event_id}   un doc por partido terminado, con la forma de eur_games
                                  (home/away con pts, reb, ast, fg3m, poss_est…), sin jugadores
  nba_games_state/{season}        hash por partido y días ya completos → no se repiden

Temporada: 'N2025' = 2025-26 (año de inicio, como los códigos de Euroliga). Entran la regular,
el play-in y los playoffs; fuera la pretemporada y el All-Star. La fecha del doc es la del día
de EE. UU. del scoreboard (un partido a las 02:00 UTC es del día anterior allí): las jornadas
del backtest son días.
Prórroga: ot_periods = cuartos de más de 4; los puntos la incluyen (como gradúan las casas).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={day}"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={event}"
_HTTP_TIMEOUT_S = 20
_SLEEP_S = 0.3
TIME_BUDGET_S = 1500          # Cloud Run corta a 1800 s: lo que no da tiempo queda para la siguiente
DRY_RUN_MAX_DAYS = 5          # en dry-run, sin max_days, solo unos días: es una prueba, no la carga
DAILY_MAX_DAYS = 30           # pasada diaria: tope de días (de más reciente a más antiguo)
FIRST_SEASON = 2022           # la carga empieza en 2022-23 (N2022): antes, ni se pide
SEASON_TYPES = {2, 3, 5}      # regular, playoffs, play-in (1 = pretemporada, 4 = All-Star)
SEASON_START = (10, 1)        # la temporada se recorre del 1-oct al 30-jun
SEASON_END = (6, 30)
# Partidos que no se juegan ese día: no impiden dar el día por completo (si no, se repediría
# siempre). Si se juegan otro día, salen en el scoreboard de ese otro día.
_NOT_PLAYED = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_FORFEIT"}

STAT_NAMES = {
    "fg": "fieldGoalsMade-fieldGoalsAttempted",
    "fg3": "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
    "ft": "freeThrowsMade-freeThrowsAttempted",
    "reb": "totalRebounds", "oreb": "offensiveRebounds", "dreb": "defensiveRebounds",
    "ast": "assists", "stl": "steals", "blk": "blocks", "fouls": "fouls",
}
_TOV_NAMES = ("totalTurnovers", "turnovers")
_REQUIRED = ("fg", "fg3", "ft", "reb", "oreb", "ast")


# ── Temporadas y días ───────────────────────────────────────────────────────────

def season_code(d: date) -> str:
    start = d.year if d.month >= 8 else d.year - 1
    return f"N{start}"


def default_seasons(today: Optional[date] = None) -> list[str]:
    """De N2022 (2022-23) a la actual: N2022…N2026 en nov-2026."""
    today = today or datetime.now(timezone.utc).date()
    start = int(season_code(today)[1:])
    return [f"N{y}" for y in range(FIRST_SEASON, start + 1)]


def eligible_season(season: str, today: Optional[date] = None) -> bool:
    """'N2022'… hasta la temporada actual."""
    if len(season) != 5 or season[0] != "N" or not season[1:].isdigit():
        return False
    today = today or datetime.now(timezone.utc).date()
    return FIRST_SEASON <= int(season[1:]) <= int(season_code(today)[1:])


def season_days(season: str, until: date) -> list[str]:
    """Días YYYYMMDD de la temporada hasta `until` (incluido), del 1-oct al 30-jun."""
    y = int(season[1:])
    d, end = date(y, *SEASON_START), min(date(y + 1, *SEASON_END), until)
    out = []
    while d <= end:
        out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


# ── Puros: scoreboard y summary ────────────────────────────────────────────────

def events_from_scoreboard(sb: dict) -> list[dict]:
    """[{id, completed, season_type, name}] de los partidos del día."""
    out = []
    for ev in (sb or {}).get("events") or []:
        st = ((ev.get("status") or {}).get("type") or {})
        out.append({"id": str(ev.get("id") or ""), "completed": bool(st.get("completed")),
                    "status": st.get("name") or "",
                    "season_type": (ev.get("season") or {}).get("type"),
                    "name": ev.get("name") or ""})
    return [e for e in out if e["id"]]


def wanted(ev: dict) -> bool:
    return ev["season_type"] in SEASON_TYPES and "all-star" not in ev["name"].lower()


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _made_att(v) -> tuple[Optional[int], Optional[int]]:
    parts = str(v or "").split("-")
    if len(parts) != 2:
        return None, None
    return _int(parts[0]), _int(parts[1])


def team_stats(stats: list[dict]) -> tuple[Optional[dict], Optional[str]]:
    """statistics[] de un equipo → (totales, None) o (None, motivo)."""
    by = {s.get("name"): s.get("displayValue") for s in stats or [] if s.get("name")}
    fgm, fga = _made_att(by.get(STAT_NAMES["fg"]))
    fg3m, fg3a = _made_att(by.get(STAT_NAMES["fg3"]))
    ftm, fta = _made_att(by.get(STAT_NAMES["ft"]))
    vals = {k: _int(by.get(STAT_NAMES[k])) for k in ("reb", "oreb", "dreb", "ast", "stl", "blk", "fouls")}
    tov = next((_int(by.get(n)) for n in _TOV_NAMES if _int(by.get(n)) is not None), None)
    got = {"fg": fga, "fg3": fg3a, "ft": fta, "reb": vals["reb"], "oreb": vals["oreb"], "ast": vals["ast"]}
    missing = [k for k in _REQUIRED if got[k] is None]
    if missing or tov is None:
        return None, "faltan_" + "_".join(missing + ([] if tov is not None else ["tov"]))
    return {
        "reb": vals["reb"], "oreb": vals["oreb"], "dreb": vals["dreb"], "ast": vals["ast"],
        "fg2m": fgm - fg3m, "fg2a": fga - fg3a, "fg3m": fg3m, "fg3a": fg3a, "ftm": ftm, "fta": fta,
        "tov": tov, "stl": vals["stl"], "blk": vals["blk"], "fouls": vals["fouls"],
        # Posesiones estimadas: FGA + 0,44·FTA − OREB + TOV (la misma fórmula que eur_games)
        "poss_est": round(fga + 0.44 * fta - vals["oreb"] + tov, 1),
    }, None


def build_game_doc(summary: dict, event_id: str, us_day: str, season: str) -> tuple[Optional[dict], Optional[str]]:
    """summary de ESPN → (doc de nba_games, None) o (None, motivo)."""
    comp = (((summary or {}).get("header") or {}).get("competitions") or [{}])[0]
    if not (((comp.get("status") or {}).get("type") or {}).get("completed", True)):
        return None, "no_terminado"
    sides = {}
    for c in comp.get("competitors") or []:
        if c.get("homeAway") in ("home", "away"):
            sides[c["homeAway"]] = c
    if set(sides) != {"home", "away"}:
        return None, "sin_local_visitante"
    pts = {k: _int(sides[k].get("score")) for k in sides}
    if None in pts.values():
        return None, "sin_marcador"
    box = {}
    for t in ((summary.get("boxscore") or {}).get("teams") or []):
        tid = str((t.get("team") or {}).get("id") or "")
        side = next((k for k, c in sides.items() if str((c.get("team") or {}).get("id") or "") == tid), None)
        if side:
            box[side] = t
    if set(box) != {"home", "away"}:
        return None, "boxscore_sin_equipos"
    tot = {}
    for k in ("home", "away"):
        st, why = team_stats(box[k].get("statistics"))
        if st is None:
            return None, why
        tot[k] = {"pts": pts[k], **st}
    periods = max(len(sides[k].get("linescores") or []) for k in sides)
    ot = max(0, periods - 4)
    team = {k: (sides[k].get("team") or {}) for k in sides}
    d = us_day
    doc = {
        "season": season, "game_id": str(event_id),
        "date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "kickoff_utc": comp.get("date"),
        "season_type": (((summary.get("header") or {}).get("season") or {}).get("type")),
        "home_code": team["home"].get("abbreviation"), "away_code": team["away"].get("abbreviation"),
        "home_team_id": team["home"].get("id"), "away_team_id": team["away"].get("id"),
        "ot_periods": ot, "minutes": 240 + 25 * ot,
        "home": tot["home"], "away": tot["away"],
        "total_pts": pts["home"] + pts["away"], "source": "espn_summary",
    }
    if not doc["home_code"] or not doc["away_code"]:
        return None, "sin_codigo_equipo"
    return doc, None


def field_report(summary: dict) -> list[dict]:
    """Para la sonda: por equipo (índice, sin nombre) los nombres de campo y si vienen rellenos."""
    out = []
    for i, t in enumerate(((summary or {}).get("boxscore") or {}).get("teams") or []):
        fields = []
        for s in t.get("statistics") or []:
            v = s.get("displayValue")
            fields.append((s.get("name"), v not in (None, "", "--", "-")))
        out.append({"team_index": i, "fields": fields})
    return out


def doc_hash(doc: dict) -> str:
    payload = json.dumps({k: v for k, v in doc.items() if k not in ("updated_at", "fetched_at")},
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def day_complete(events: list[dict], stored: set[str], skipped_final: set[str]) -> bool:
    """Un día está hecho si todos sus partidos que interesan están terminados y guardados (o
    descartados por un motivo que no cambia al repetir)."""
    evs = [e for e in events if wanted(e) and e.get("status") not in _NOT_PLAYED]
    return all(e["completed"] and (e["id"] in stored or e["id"] in skipped_final) for e in evs)


# ── Red ────────────────────────────────────────────────────────────────────────

def http_get_json(url: str) -> tuple[Optional[dict], Optional[int], float]:
    """(json|None, código HTTP|None, segundos)."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as r:
            data = json.loads(r.read().decode())
            return (data if isinstance(data, dict) else None), r.status, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return None, e.code, time.monotonic() - t0
    except Exception as e:
        logger.warning("nba_espn: %s en %s", type(e).__name__, url)
        return None, None, time.monotonic() - t0


# ── Firestore ──────────────────────────────────────────────────────────────────

def _load_state(season: str) -> dict:
    from shared.firestore_client import col
    snap = col("nba_games_state").document(season).get()
    return (snap.to_dict() or {}) if snap.exists else {}


def _write(season: str, docs: dict[str, dict], state: dict) -> int:
    from shared.firestore_client import col, get_client
    old = state.get("hashes", {})
    new_hashes = {d: doc_hash(doc) for d, doc in docs.items()}
    changed = [d for d, h in new_hashes.items() if old.get(d) != h]
    now = datetime.now(timezone.utc).isoformat()
    client = get_client()
    for i in range(0, len(changed), 400):
        batch = client.batch()
        for doc_id in changed[i:i + 400]:
            batch.set(col("nba_games").document(doc_id), {**docs[doc_id], "fetched_at": now})
        batch.commit()
    col("nba_games_state").document(season).set({
        "hashes": {**old, **new_hashes}, "days_done": sorted(state.get("days_done", [])),
        "skipped_final": sorted(state.get("skipped_final", [])), "updated_at": now})
    return len(changed)


# Motivos que no cambian al repetir la petición (el partido no se vuelve a pedir)
_FINAL_SKIPS = {"sin_local_visitante", "sin_codigo_equipo"}


async def run_nba_espn(seasons: Optional[list[str]] = None, dry_run: bool = True, max_days: int = 0,
                       only_missing: bool = True, time_budget_s: float = TIME_BUDGET_S,
                       today: Optional[date] = None, fetch=None, state_loader=None, writer=None,
                       daily: bool = False) -> dict:
    """
    seasons: 'N2025'… (por defecto de N2022 a la actual).
    max_days: tope de días por llamada (0 = sin tope; en dry-run, DRY_RUN_MAX_DAYS).
    only_missing: con estado, se saltan los días ya completos y los partidos ya guardados.
    daily: pasada diaria — solo la temporada de ayer, siempre only_missing, días de más reciente
           a más antiguo (ayer primero aunque quede historial por cargar) y tope DAILY_MAX_DAYS.
           Fuera de temporada (jul-sep) no pide nada.
    dry_run: descarga, calcula y cuenta; no escribe nada.
    fetch/state_loader/writer: inyectables para los tests (por defecto red y Firestore).
    """
    fetch = fetch or http_get_json
    state_loader = state_loader or _load_state
    writer = writer or _write
    today = today or datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    if daily:
        seasons = seasons or [season_code(yesterday)]
        only_missing = True
        max_days = max_days or DAILY_MAX_DAYS
    seasons = seasons or default_seasons(today)
    if dry_run and not max_days:
        max_days = DRY_RUN_MAX_DAYS
    deadline = time.monotonic() + time_budget_s
    loop = asyncio.get_running_loop()
    summary: dict = {"ok": True, "dry_run": dry_run, "daily": daily, "seasons": {}, "errors": [],
                     "requests": {"scoreboard": 0, "summary": 0, "http_errors": 0},
                     "out_of_time": False, "days_left": 0}
    days_used = 0
    for season in seasons:
        s = {"days": 0, "days_skipped": 0, "empty_days": 0, "events": 0, "docs": 0, "written": 0,
             "ot_games": 0, "by_type": {}, "skipped": {}, "days_left": 0}
        try:
            state = {}
            try:
                state = await loop.run_in_executor(None, state_loader, season)
            except Exception as e:      # sin Firestore (en local) el dry-run sigue
                logger.warning("nba_espn: sin estado de %s (%s)", season, type(e).__name__)
            state.setdefault("hashes", {})
            done_days = set(state.get("days_done", [])) if only_missing else set()
            skipped_final = set(state.get("skipped_final", []))
            stored = {k.split("_", 1)[1] for k in state["hashes"]} if only_missing else set()
            todo = [d for d in season_days(season, yesterday) if d not in done_days]
            if daily:
                todo.reverse()
            s["days_skipped"] = len(season_days(season, yesterday)) - len(todo)
            docs: dict[str, dict] = {}
            for i, day in enumerate(todo):
                if (max_days and days_used >= max_days) or time.monotonic() > deadline:
                    summary["out_of_time"] = time.monotonic() > deadline
                    s["days_left"] = len(todo) - i
                    break
                days_used += 1
                sb, code, _ = await loop.run_in_executor(None, fetch, SCOREBOARD_URL.format(day=day))
                summary["requests"]["scoreboard"] += 1
                if sb is None or not isinstance(sb.get("events"), list):
                    # sin respuesta o sin la lista de partidos: el día NO se da por hecho (que un
                    # «0 partidos» raro de ESPN no deje huecos permanentes)
                    summary["requests"]["http_errors"] += 1
                    continue
                events = events_from_scoreboard(sb)
                s["days"] += 1
                s["empty_days"] += 0 if any(wanted(e) for e in events) else 1
                for ev in events:
                    if not wanted(ev) or not ev["completed"] or ev["id"] in stored or ev["id"] in skipped_final:
                        continue
                    s["events"] += 1
                    sm, code, _ = await loop.run_in_executor(None, fetch, SUMMARY_URL.format(event=ev["id"]))
                    summary["requests"]["summary"] += 1
                    if sm is None:
                        summary["requests"]["http_errors"] += 1
                        s["skipped"]["http"] = s["skipped"].get("http", 0) + 1
                        continue
                    doc, why = build_game_doc(sm, ev["id"], day, season)
                    if doc is None:
                        s["skipped"][why] = s["skipped"].get(why, 0) + 1
                        if why in _FINAL_SKIPS:
                            skipped_final.add(ev["id"])
                        continue
                    docs[f"{season}_{ev['id']}"] = doc
                    stored.add(ev["id"])
                    st_key = str(doc.get("season_type"))
                    s["by_type"][st_key] = s["by_type"].get(st_key, 0) + 1
                    s["ot_games"] += 1 if doc["ot_periods"] else 0
                    await asyncio.sleep(_SLEEP_S)
                if day_complete(events, stored, skipped_final):
                    done_days.add(day)
                await asyncio.sleep(_SLEEP_S)
            s["docs"] = len(docs)
            if docs:
                pts = [d["total_pts"] for d in docs.values()]
                s["avg_total_pts"] = round(sum(pts) / len(pts), 1)
                s["avg_poss"] = round(sum((d["home"]["poss_est"] + d["away"]["poss_est"]) / 2
                                          for d in docs.values()) / len(docs), 1)
            state["days_done"], state["skipped_final"] = done_days, skipped_final
            if not dry_run and (docs or done_days):
                s["written"] = await loop.run_in_executor(None, writer, season, docs, state)
            else:
                s["written"] = sum(1 for k, d in docs.items() if state["hashes"].get(k) != doc_hash(d))
        except Exception as e:
            logger.error("nba_espn: error en %s", season, exc_info=True)
            summary["errors"].append(f"{season}: {type(e).__name__}")
        summary["seasons"][season] = s
        summary["days_left"] += s["days_left"]
    summary["ok"] = not summary["errors"]
    logger.info("nba_espn: %s", summary)
    return summary
