"""
services/sports-agent/collectors/highlightly_players.py

Ampliación de Tendencias — Fase 2a: colector de estadísticas por jugador y partido desde
Highlightly (plan gratuito, 100 peticiones/día). SIN USO todavía: ningún endpoint ni job lo
llama. Aislado del feed actual, de trend_finder y de Polymarket.

Fuente verificada 2026-11-08 (17 peticiones de prueba):
  - GET https://soccer.highlightly.net/matches?leagueId=&season=&date=&limit=&offset=
  - GET https://soccer.highlightly.net/box-score/{matchId}  → [{team, players[]}] (2 equipos)
  - cabecera x-ratelimit-requests-remaining; cuota diaria reinicia a las 00:00 UTC.
  - El plan BASIC incluye la temporada anterior. "season" = año de inicio (2026 = 2026/27).
  - El box score NO siempre trae primero al local: se empareja por nombre de equipo.

Campos NO fiables (no se guardan o se marcan):
  - cardsSecondYellow: copia cardsYellow → se ignora; tarjetas = cardsYellow + cardsRed.
  - goalsConceded del portero: sale 0 siempre → no se guarda.
  - shotsTotal sumado por jugador a veces queda POR DEBAJO del CSV de football-data.co.uk
    → shots_suspect por equipo al comparar con la fila del CSV.
  - goalsSaved: coincide con las paradas derivadas en 14/18 equipos; en 4, +1 → se guarda la
    diferencia (saves_vs_derived) para medirla antes de publicar paradas.

Colecciones (por shared.firestore_client.col, migran con el resto):
  hl_matches/{match_id}         estado del partido y del box score (caché: done = no se repide)
  hl_match_players/{match_id}   UN doc por partido con los jugadores de los dos equipos
  hl_collector_state/{div}_{season}  avance del listado de temporada
"""
import logging
import re
import unicodedata
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HL_BASE = "https://soccer.highlightly.net"
HL_QUOTA_API = "highlightly"
HL_DAILY_LIMIT = 100                 # plan gratuito (BASIC)
HL_PRO_DAILY_LIMIT = 7500            # plan PRO, comprado un mes el 12-nov-2026
# Último día (UTC, incluido) del mes de PRO; desde el día siguiente vuelve el límite gratuito.
# Es solo un techo propio: el tope real lo pone la cabecera x-ratelimit-requests-remaining
# (requests_available usa el menor de los dos), así que una fecha tardía no puede gastar de más.
HL_PRO_UNTIL = date(2026, 12, 12)
HL_MANUAL_RESERVE = 10
HL_MAX_ATTEMPTS = 3
HL_PAGE_LIMIT = 100
_HTTP_TIMEOUT = 60.0

# División FDCO → id de liga Highlightly (verificados 2026-11-08)
HL_LEAGUES: dict[str, int] = {
    "SP1": 119924, "SP2": 120775, "I1": 115669, "E0": 33973,
    "F1": 52695, "P1": 80778, "D1": 67162, "N1": 75672,
}

# Campos de jugador que se guardan: nombre en Highlightly → nombre propio
_PLAYER_FIELDS = {
    "shotsTotal": "shots_total",
    "shotsOnTarget": "shots_on_target",
    "fouledOthers": "fouls_committed",
    "fouledByOthers": "fouls_drawn",
    "cardsYellow": "yellows",
    "cardsRed": "reds",
    "goalsSaved": "saves",
    "goalsScored": "goals",
}


# ── Temporadas ──────────────────────────────────────────────────────────────

def hl_season(fdco_season: str) -> int:
    """'2627' → 2026 (Highlightly usa el año de inicio)."""
    return 2000 + int(fdco_season[:2])


# ── Emparejar equipos con football-data.co.uk ───────────────────────────────

_NOISE = {"fc", "cf", "sc", "sv", "ac", "as", "ss", "ssc", "afc", "cd", "ud", "rc", "rcd", "sd",
          "club", "de", "la", "le", "the", "united", "city", "calcio", "football", "futbol", "1", "29"}
# Casos que la normalización no resuelve (nombre Highlightly normalizado → nombre FDCO normalizado)
_ALIASES = {"eintracht frankfurt": "ein frankfurt", "hamburger": "hamburg",
            "racing santander": "santander", "stade brestois": "brest",
            "deportivo coruna": "coruna"}


def normalize_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in _NOISE]
    out = " ".join(tokens)
    return _ALIASES.get(out, out)


def team_similarity(a: str, b: str) -> float:
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return 0.0
    if na == nb or na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def match_fdco_row(hl_home: str, hl_away: str, fdco_rows_same_day: list[dict],
                   min_score: float = 0.75) -> Optional[dict]:
    """Fila CSV (mismo día y división) cuyos equipos se parecen más; None si ninguna convence."""
    best, best_score = None, 0.0
    for r in fdco_rows_same_day:
        score = min(team_similarity(hl_home, r.get("HomeTeam", "")),
                    team_similarity(hl_away, r.get("AwayTeam", "")))
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= min_score else None


# ── Box score → documento ───────────────────────────────────────────────────

def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse_box_score(raw, meta: dict) -> Optional[dict]:
    """
    raw: respuesta de /box-score/{id} (lista de 2 equipos, o dict con 'data').
    meta: {match_id, div, season, date, home_team, away_team, home_team_id, away_team_id}.
    Devuelve el doc de hl_match_players o None si el box score viene vacío o incompleto.
    """
    data = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    if len(data) != 2:
        return None
    teams = []
    for t in data:
        team = t.get("team") or {}
        tid, tname = team.get("id"), team.get("name") or ""
        if tid is not None and tid in (meta.get("home_team_id"), meta.get("away_team_id")):
            side = "home" if tid == meta.get("home_team_id") else "away"
        else:
            side = "home" if team_similarity(tname, meta["home_team"]) >= team_similarity(tname, meta["away_team"]) else "away"
        players, sums = [], {v: 0 for v in _PLAYER_FIELDS.values()}
        for p in t.get("players") or []:
            minutes = _int(p.get("minutesPlayed"))
            if minutes <= 0:
                continue
            st = p.get("statistics") or {}
            row = {"player_id": p.get("id"), "name": p.get("name"), "position": p.get("position"),
                   "is_substitute": bool(p.get("isSubstitute")), "minutes": minutes,
                   "rating": p.get("matchRating")}
            for src, dst in _PLAYER_FIELDS.items():
                row[dst] = _int(st.get(src))
                sums[dst] += row[dst]
            row["cards"] = row["yellows"] + row["reds"]
            players.append(row)
        teams.append({"team_id": tid, "team": tname, "side": side, "players": players, "totals": sums})
    if {t["side"] for t in teams} != {"home", "away"}:
        return None
    teams.sort(key=lambda t: 0 if t["side"] == "home" else 1)
    return {
        "match_id": meta["match_id"], "div": meta["div"], "season": meta["season"], "date": meta["date"],
        "home_team": meta["home_team"], "away_team": meta["away_team"], "teams": teams,
        "source": "highlightly", "unreliable_fields_dropped": ["cardsSecondYellow", "goalsConceded"],
        "fdco_match_key": None, "checks": None,
    }


def compare_with_fdco(doc: dict, fdco_row: dict, match_key: Optional[str] = None) -> dict:
    """
    Marca diferencias con la fila del CSV (que manda para mercados de equipo):
      shots_suspect: suma de remates por jugador < remates del equipo en el CSV
      sot_diff / fouls_diff / cards_diff: Highlightly − CSV por equipo
      saves_vs_derived: goalsSaved − (a puerta del rival − goles encajados) del CSV
    """
    checks = {}
    for t in doc["teams"]:
        me, op = ("H", "A") if t["side"] == "home" else ("A", "H")
        tot = t["totals"]
        csv_shots = _int(fdco_row.get(f"{me}S"))
        derived_saves = max(0, _int(fdco_row.get(f"{op}ST")) - _int(fdco_row.get(f"FT{op}G")))
        checks[t["side"]] = {
            "shots_suspect": tot["shots_total"] < csv_shots,
            "shots_diff": tot["shots_total"] - csv_shots,
            "sot_diff": tot["shots_on_target"] - _int(fdco_row.get(f"{me}ST")),
            "fouls_diff": tot["fouls_committed"] - _int(fdco_row.get(f"{me}F")),
            "cards_diff": (tot["yellows"] + tot["reds"]) - (_int(fdco_row.get(f"{me}Y")) + _int(fdco_row.get(f"{me}R"))),
            "saves_vs_derived": tot["saves"] - derived_saves,
        }
    return {**doc, "fdco_match_key": match_key, "checks": checks}


# ── Reparto del presupuesto ─────────────────────────────────────────────────

def hl_daily_limit(today: Optional[date] = None) -> int:
    """Límite diario propio: el de PRO hasta HL_PRO_UNTIL (incluido), el gratuito después."""
    today = today or datetime.now(timezone.utc).date()
    return HL_PRO_DAILY_LIMIT if today <= HL_PRO_UNTIL else HL_DAILY_LIMIT


def requests_available(used_counter: int, remaining_reported: Optional[int],
                       limit: Optional[int] = None, reserve: int = HL_MANUAL_RESERVE) -> int:
    """Peticiones que el colector puede gastar hoy: el menor de lo que dice el contador propio y
    lo que dice la API (la misma clave la gastan las pruebas manuales), menos la reserva."""
    left = (hl_daily_limit() if limit is None else limit) - used_counter
    if remaining_reported is not None:
        left = min(left, int(remaining_reported))
    return max(0, left - reserve)


def split_budget(available: int, maintenance_needed: int) -> tuple[int, int]:
    """(mantenimiento, carga): el mantenimiento va primero; la carga se queda el resto."""
    maint = min(available, maintenance_needed)
    return maint, available - maint


def pick_backfill(pending: list[dict], n: int, current_season: str) -> list[dict]:
    """
    Partidos pendientes a pedir hoy, máximo n: primero la temporada actual, de más reciente a
    más antiguo; después la anterior. Fuera los que ya agotaron intentos.
    """
    ok = [m for m in pending if m.get("box_status") in (None, "pending", "error")
          and m.get("attempts", 0) < HL_MAX_ATTEMPTS]
    ok.sort(key=lambda m: (m.get("season") != current_season, _neg_date(m.get("date", ""))))
    return ok[:max(0, n)]


def _neg_date(d: str) -> str:
    # orden descendente por fecha ISO sin convertir: complemento de cada dígito
    return "".join(chr(ord("9") - ord(c) + ord("0")) if c.isdigit() else c for c in d)


def next_box_status(doc: Optional[dict], finished: bool, attempts: int) -> tuple[str, int]:
    """Estado tras un intento: done (se guarda y no se repide), pending (aún no terminado) o
    empty/error, con reintento hasta HL_MAX_ATTEMPTS."""
    if not finished:
        return "pending", attempts
    if doc is not None:
        return "done", attempts + 1
    return ("empty" if attempts + 1 >= HL_MAX_ATTEMPTS else "error"), attempts + 1


def match_meta_from_listing(m: dict, div: str, fdco_season: str) -> dict:
    """Elemento de /matches → doc base de hl_matches."""
    state = (m.get("state") or {})
    return {
        "match_id": m.get("id"), "div": div, "season": fdco_season,
        "hl_league_id": (m.get("league") or {}).get("id"),
        "date": (m.get("date") or "")[:10],
        "home_team": (m.get("homeTeam") or {}).get("name"), "home_team_id": (m.get("homeTeam") or {}).get("id"),
        "away_team": (m.get("awayTeam") or {}).get("name"), "away_team_id": (m.get("awayTeam") or {}).get("id"),
        "finished": state.get("description") == "Finished",
        "score": (state.get("score") or {}).get("current"),
        "box_status": None, "attempts": 0,
    }


# ── Cliente con cuota propia ────────────────────────────────────────────────

class HighlightlyBudgetExhausted(Exception):
    pass


async def hl_get(client: httpx.AsyncClient, api_key: str, path: str, quota=None) -> Optional[object]:
    """
    GET con control de cuota (contador 'highlightly' del QuotaManager + cabecera de la API).
    No se llama si no queda presupuesto. Devuelve el JSON o None si falla.
    """
    if quota is not None:
        from shared.api_quota_manager import daily_period
        doc = quota._get_doc(f"{HL_QUOTA_API}_{daily_period(HL_QUOTA_API)}")
        if requests_available(doc.get("used", 0), doc.get("remaining_reported"), reserve=0) <= 0:
            raise HighlightlyBudgetExhausted()
    try:
        resp = await client.get(HL_BASE + path, headers={"x-rapidapi-key": api_key})
    except Exception:
        logger.error("highlightly: error de red en %s", path, exc_info=True)
        return None
    if quota is not None:
        quota.track_call(HL_QUOTA_API, resp.headers.get("x-ratelimit-requests-remaining"))
    if resp.status_code == 429:
        raise HighlightlyBudgetExhausted()
    if resp.status_code != 200:
        logger.warning("highlightly: HTTP %d en %s", resp.status_code, path)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("highlightly: respuesta no JSON en %s", path)
        return None
