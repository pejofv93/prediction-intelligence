"""
services/sports-agent/collectors/match_fixtures.py

Ficha de partido de Tendencias (mp-v1) — Paso 1: lista de partidos próximos de las 8 ligas.

SIN USO todavía: ningún endpoint ni job lo llama y no escribe nada en Firestore (lo único que
escribe, solo si se le pasa un QuotaManager, es el contador de cuota de Highlightly). Aislado
del feed actual, de trend_finder, de `upcoming` y de Polymarket.

Fuentes, en este orden:
  1. football-data.co.uk/fixtures.csv — gratis, fecha + hora (de Londres) + cuotas. Problemas
     verificados: SP2 llega incompleta (8 de 11 partidos el 8-nov) y el fichero es una foto por
     jornada que no se refresca enseguida (el lunes 9-nov aún traía la jornada ya jugada).
  2. Highlightly /matches?leagueId=&date= — 1 petición por liga y día, SOLO para los próximos
     días (por defecto 3: hoy, mañana y pasado, en UTC) y SOLO donde el CSV no basta:
       - liga cuyo CSV trae menos partidos que una jornada completa (SP2), o
       - día de la ventana que cae fuera de las fechas que cubre el CSV de esa liga.
     Se paga con la RESERVA MANUAL de Highlightly (HL_MANUAL_RESERVE = 10 de las 100 diarias,
     las que el colector de jugadores no puede gastar): contador propio "highlightly_fixtures"
     y nunca más de lo que la propia API dice que queda.
  Nunca se inventa un partido. Lo que no se pudo comprobar por falta de cuota sale en
  `unchecked` (liga y día), a la vista, para que ningún partido se quede sin ficha en silencio.

Nombres de equipo: se guardan los de football-data.co.uk (los mismos de fdco_team_matches, que
es de donde saldrá el cálculo). Un partido de Highlightly se pasa a esos nombres con el mismo
normalizador del colector de jugadores; si un equipo no se reconoce, el partido sigue en la
lista con team_matched=False (su ficha dirá "sin datos", no desaparece).

Id del partido: {div}_{fecha}_{local}_{visitante}, con la fecha de Londres (la del CSV de
resultados) y los nombres en slug, para emparejarlo después con fdco_team_matches.
"""
import asyncio
import csv
import io
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import httpx

from collectors.fdco_team_matches import MAIN_DIVS, _float, _parse_date, slugify
from collectors.highlightly_players import (
    HL_LEAGUES, HL_MANUAL_RESERVE, HL_QUOTA_API, HighlightlyBudgetExhausted, hl_get, team_similarity,
)

logger = logging.getLogger(__name__)

FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
SEASON_CSV_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
HL_FIXTURES_QUOTA_API = "highlightly_fixtures"   # contador propio dentro de la reserva manual
DEFAULT_WINDOW_DAYS = 3
_HTTP_TIMEOUT = 30.0
_TEAM_MIN_SIMILARITY = 0.75

# Partidos de una jornada completa (equipos / 2), 2026/27
ROUND_SIZE: dict[str, int] = {
    "SP1": 10, "SP2": 11, "E0": 10, "I1": 10,
    "F1": 9, "P1": 9, "D1": 9, "N1": 9,
}

# Cuotas del CSV que se guardan (media del mercado; la de cierre aún no existe antes del partido)
_ODDS_COLUMNS = {"AvgH": "home", "AvgD": "draw", "AvgA": "away",
                 "Avg>2.5": "over_2_5", "Avg<2.5": "under_2_5"}


# ── Hora de Londres ↔ UTC (sin depender de tzdata en el contenedor) ─────────────

def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _uk_offset(utc_dt: datetime) -> timedelta:
    """+1 h en horario de verano británico: del último domingo de marzo al último de
    octubre, ambos a la 01:00 UTC."""
    y = utc_dt.year
    start = datetime.combine(_last_sunday(y, 3), time(1), tzinfo=timezone.utc)
    end = datetime.combine(_last_sunday(y, 10), time(1), tzinfo=timezone.utc)
    return timedelta(hours=1) if start <= utc_dt < end else timedelta(0)


def uk_to_utc(date_iso: str, hhmm: str) -> Optional[datetime]:
    """'2026-11-14', '15:00' (hora de Londres) → datetime UTC."""
    try:
        hh, mm = (int(x) for x in (hhmm or "").strip().split(":")[:2])
        local = datetime.combine(date.fromisoformat(date_iso), time(hh, mm), tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    # Primero se supone invierno (UTC = Londres) y se corrige si esa hora cae en verano.
    return local - _uk_offset(local)


def uk_date(utc_dt: datetime) -> str:
    return (utc_dt + _uk_offset(utc_dt)).date().isoformat()


def _parse_utc(raw: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def match_id(div: str, uk_date_iso: str, home: str, away: str) -> str:
    return f"{div}_{uk_date_iso}_{slugify(home)}_{slugify(away)}"


# ── 1. fixtures.csv ────────────────────────────────────────────────────────────

def parse_fixtures_csv(text: str, divs=tuple(MAIN_DIVS)) -> list[dict]:
    """Filas de las ligas pedidas → partidos. Fecha y hora vienen en hora de Londres."""
    out = []
    for r in csv.DictReader(io.StringIO((text or "").lstrip("﻿"))):
        div = (r.get("Div") or "").strip()
        if div not in divs:
            continue
        d = _parse_date(r.get("Date", ""))
        home, away = (r.get("HomeTeam") or "").strip(), (r.get("AwayTeam") or "").strip()
        ko = uk_to_utc(d, r.get("Time", "")) if d else None
        if not (d and home and away and ko):
            logger.warning("match_fixtures: fila de fixtures.csv ilegible: %s", {k: r.get(k) for k in ("Div", "Date", "Time", "HomeTeam", "AwayTeam")})
            continue
        odds = {dst: _float(r.get(src)) for src, dst in _ODDS_COLUMNS.items()}
        out.append({
            "match_id": match_id(div, d, home, away), "div": div, "league": MAIN_DIVS[div],
            "date": d, "kickoff_utc": ko.isoformat(), "home_team": home, "away_team": away,
            "source": "fixtures_csv", "team_matched": True, "hl_match_id": None,
            "odds": {k: v for k, v in odds.items() if v is not None} or None,
        })
    return out


# ── 2. Qué pedir a Highlightly ─────────────────────────────────────────────────

def window_days(now: datetime, days: int = DEFAULT_WINDOW_DAYS) -> list[str]:
    """Días UTC de la ventana: hoy y los siguientes (days en total)."""
    return [(now.date() + timedelta(days=i)).isoformat() for i in range(max(1, days))]


def plan_hl_checks(csv_by_div: dict[str, list[dict]], days: list[str],
                   already_checked: frozenset = frozenset()) -> list[dict]:
    """
    (liga, día) a comprobar en Highlightly, en orden de prioridad: primero el día más cercano;
    dentro del día, primero las ligas cuyo CSV está incompleto. Una liga con la jornada
    completa en el CSV solo se da por cubierta en los días que abarca ese CSV.
    """
    plan = []
    for div in MAIN_DIVS:
        rows = csv_by_div.get(div, [])
        complete = len(rows) >= ROUND_SIZE[div]
        utc_days = {r["kickoff_utc"][:10] for r in rows}
        span = (min(utc_days), max(utc_days)) if utc_days else None
        for day in days:
            if (div, day) in already_checked:
                continue
            if complete and span and span[0] <= day <= span[1]:
                continue
            reason = ("csv_incompleto" if rows and not complete
                      else "csv_sin_liga" if not rows else "fuera_del_csv")
            plan.append({"div": div, "day": day, "reason": reason})
    prio = {"csv_incompleto": 0, "csv_sin_liga": 1, "fuera_del_csv": 1}
    plan.sort(key=lambda p: (p["day"], prio[p["reason"]], list(MAIN_DIVS).index(p["div"])))
    return plan


def fixtures_budget(used_fixtures_today: int, remaining_reported: Optional[int],
                    reserve: int = HL_MANUAL_RESERVE) -> int:
    """Peticiones de hoy para la lista: lo que queda de la reserva manual, y nunca más de lo
    que la API dice que le queda a la clave (la gastan también el colector y las pruebas)."""
    left = reserve - int(used_fixtures_today or 0)
    if remaining_reported is not None:
        left = min(left, int(remaining_reported))
    return max(0, left)


# ── 3. Respuesta de Highlightly → partidos ─────────────────────────────────────

def parse_hl_listing(raw, div: str) -> list[dict]:
    """Elementos de /matches → {hl_match_id, kickoff, home, away, state}. Descarta los que no
    traen hora o equipos."""
    data = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    out = []
    for m in data:
        ko = _parse_utc(m.get("date") or "")
        home = ((m.get("homeTeam") or {}).get("name") or "").strip()
        away = ((m.get("awayTeam") or {}).get("name") or "").strip()
        if not (ko and home and away):
            continue
        out.append({"hl_match_id": m.get("id"), "div": div, "kickoff": ko, "hl_home": home, "hl_away": away,
                    "state": ((m.get("state") or {}).get("description") or "")})
    return out


def map_team(hl_name: str, fdco_teams: set[str]) -> tuple[str, bool]:
    """Nombre de football-data.co.uk más parecido; si ninguno convence, el de Highlightly."""
    best, score = None, 0.0
    for t in fdco_teams:
        s = team_similarity(hl_name, t)
        if s > score:
            best, score = t, s
    return (best, True) if best and score >= _TEAM_MIN_SIMILARITY else (hl_name, False)


def _same_match(a: dict, b: dict) -> bool:
    return (a["div"] == b["div"]
            and abs((_parse_utc(a["kickoff_utc"]) - _parse_utc(b["kickoff_utc"])).total_seconds()) <= 36 * 3600
            and team_similarity(a["home_team"], b["home_team"]) >= _TEAM_MIN_SIMILARITY
            and team_similarity(a["away_team"], b["away_team"]) >= _TEAM_MIN_SIMILARITY)


def merge_fixtures(csv_matches: list[dict], hl_matches: list[dict],
                   fdco_teams_by_div: dict[str, set[str]]) -> tuple[list[dict], list[str]]:
    """
    CSV primero (trae cuotas); de Highlightly se añade solo lo que el CSV no tiene. Si el CSV
    y Highlightly no coinciden en la hora, manda Highlightly (el CSV puede ser una foto vieja) y
    se marca. Devuelve (partidos, equipos de Highlightly sin emparejar).
    """
    merged = [dict(m) for m in csv_matches]
    unmatched: list[str] = []
    for h in hl_matches:
        teams = fdco_teams_by_div.get(h["div"], set())
        home, ok_h = map_team(h["hl_home"], teams)
        away, ok_a = map_team(h["hl_away"], teams)
        for name, ok in ((h["hl_home"], ok_h), (h["hl_away"], ok_a)):
            if not ok:
                unmatched.append(f"{h['div']}: {name}")
        d = uk_date(h["kickoff"])
        cand = {
            "match_id": match_id(h["div"], d, home, away), "div": h["div"], "league": MAIN_DIVS[h["div"]],
            "date": d, "kickoff_utc": h["kickoff"].isoformat(), "home_team": home, "away_team": away,
            "source": "highlightly", "team_matched": ok_h and ok_a, "hl_match_id": h["hl_match_id"],
            "hl_home": h["hl_home"], "hl_away": h["hl_away"], "odds": None,
        }
        twin = next((m for m in merged if _same_match(m, cand)), None)
        if twin is None:
            merged.append(cand)
            continue
        twin["hl_match_id"] = h["hl_match_id"]
        if twin["kickoff_utc"] != cand["kickoff_utc"]:
            twin["kickoff_csv_utc"] = twin["kickoff_utc"]
            twin["kickoff_utc"] = cand["kickoff_utc"]
            twin["kickoff_source"] = "highlightly"
    merged.sort(key=lambda m: (m["kickoff_utc"], m["div"], m["home_team"]))
    return merged, sorted(set(unmatched))


def in_window(m: dict, now: datetime, days: list[str]) -> bool:
    ko = _parse_utc(m["kickoff_utc"])
    return ko is not None and ko > now and ko.date().isoformat() <= days[-1]


# ── Ejecución (lectura; sin escribir partidos) ─────────────────────────────────

async def _get_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        resp = await client.get(url)
    except Exception:
        logger.error("match_fixtures: error de red en %s", url, exc_info=True)
        return None
    if resp.status_code != 200:
        logger.warning("match_fixtures: HTTP %d en %s", resp.status_code, url)
        return None
    return resp.text


async def _fdco_teams(client: httpx.AsyncClient, div: str, season: str) -> set[str]:
    text = await _get_text(client, SEASON_CSV_URL.format(season=season, div=div))
    if not text:
        return set()
    rows = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return {(r.get(k) or "").strip() for r in rows for k in ("HomeTeam", "AwayTeam")} - {""}


def _fixtures_quota_state(quota) -> tuple[int, Optional[int]]:
    """(usadas hoy por la lista, lo que la API dijo que le queda a la clave)."""
    from shared.api_quota_manager import daily_period
    mine = quota._get_doc(f"{HL_FIXTURES_QUOTA_API}_{daily_period(HL_FIXTURES_QUOTA_API)}")
    total = quota._get_doc(f"{HL_QUOTA_API}_{daily_period(HL_QUOTA_API)}")
    return mine.get("used", 0), total.get("remaining_reported")


async def build_fixture_list(now: Optional[datetime] = None, days: int = DEFAULT_WINDOW_DAYS,
                             api_key: Optional[str] = None, quota=None, season: Optional[str] = None,
                             already_checked: frozenset = frozenset(),
                             max_hl_requests: Optional[int] = None) -> dict:
    """
    Lista de partidos de la ventana. No escribe partidos en ningún sitio.
      api_key None      → solo fixtures.csv; todo lo que habría que mirar en Highlightly sale
                          en `unchecked`.
      quota             → QuotaManager: presupuesto = reserva manual (contador propio) y
                          cabecera de la API. Sin quota hace falta max_hl_requests (prueba manual).
      already_checked   → (liga, día) ya comprobados hoy, para no repetir peticiones.
    """
    from collectors.fdco_team_matches import current_season_end_year, season_code

    now = now or datetime.now(timezone.utc)
    season = season or season_code(current_season_end_year(now.date()))
    days_list = window_days(now, days)
    summary: dict = {"window_days": days_list, "matches": [], "by_div": {}, "hl_requests": 0,
                     "hl_checked": [], "unchecked": [], "unmatched_teams": [], "errors": []}

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        text = await _get_text(client, FIXTURES_URL)
        csv_all = parse_fixtures_csv(text) if text else []
        if text is None:
            summary["errors"].append("fixtures.csv no descargado: todo depende de Highlightly")
        csv_by_div: dict[str, list[dict]] = {}
        for m in csv_all:
            csv_by_div.setdefault(m["div"], []).append(m)
        summary["csv_rows_by_div"] = {d: len(csv_by_div.get(d, [])) for d in MAIN_DIVS}

        plan = plan_hl_checks(csv_by_div, days_list, already_checked)
        if api_key and plan:
            if quota is not None:
                used, remaining = _fixtures_quota_state(quota)
                budget = fixtures_budget(used, remaining)
            else:
                budget = max(0, min(int(max_hl_requests or 0), HL_MANUAL_RESERVE))
            todo, rest = plan[:budget], plan[budget:]
        else:
            todo, rest = [], plan
        summary["unchecked"] = [{**p, "why": "sin presupuesto" if api_key else "sin clave"} for p in rest]

        hl_matches: list[dict] = []
        teams_cache: dict[str, set[str]] = {}
        for i, p in enumerate(todo):
            path = f"/matches?leagueId={HL_LEAGUES[p['div']]}&date={p['day']}&limit=100"
            try:
                raw = await hl_get(client, api_key, path, quota=quota)
            except HighlightlyBudgetExhausted:
                summary["unchecked"].extend({**q, "why": "cuota de la API agotada"} for q in todo[i:])
                break
            summary["hl_requests"] += 1
            if quota is not None:
                quota.track_call(HL_FIXTURES_QUOTA_API)
            if raw is None:
                summary["unchecked"].append({**p, "why": "error de Highlightly"})
                continue
            summary["hl_checked"].append(p)
            found = parse_hl_listing(raw, p["div"])
            if found and p["div"] not in teams_cache:
                teams_cache[p["div"]] = await _fdco_teams(client, p["div"], season)
                if not teams_cache[p["div"]]:
                    summary["errors"].append(f"{p['div']}: sin CSV de temporada para emparejar nombres")
            hl_matches.extend(found)
            await asyncio.sleep(0.2)

    merged, unmatched = merge_fixtures(csv_all, hl_matches, teams_cache)
    matches = [m for m in merged if in_window(m, now, days_list)]
    summary["matches"] = matches
    summary["unmatched_teams"] = unmatched
    for m in matches:
        c = summary["by_div"].setdefault(m["div"], {"total": 0, "fixtures_csv": 0, "highlightly": 0})
        c["total"] += 1
        c[m["source"]] += 1
    summary["unchecked"].sort(key=lambda p: (p["day"], p["div"]))
    logger.info("match_fixtures: %d partidos en %s; Highlightly %d peticiones; sin comprobar %d (liga, día)",
                len(matches), days_list, summary["hl_requests"], len(summary["unchecked"]))
    return summary
