"""
services/sports-agent/collectors/match_fixtures.py

Ficha de partido de Tendencias (mp-v1) — Paso 1: lista de partidos próximos de las 8 ligas.

SIN USO todavía: ningún endpoint ni job lo llama y no escribe nada en Firestore (lo único que
escribe, solo si se le pasa un QuotaManager, es el contador de cuota de Highlightly). Aislado
del feed actual, de trend_finder y de Polymarket; `upcoming_matches` solo se LEE.

Fuentes, en este orden:
  1. football-data.co.uk/fixtures.csv — gratis, fecha + hora (de Londres) + cuotas. Problemas
     verificados: SP2 llega incompleta (8 de 11 partidos el 8-nov) y el fichero es una foto por
     jornada que no se refresca entre jornadas (el lunes 9-nov aún traía la jornada jugada).
  2. upcoming_matches (football-data.org, lo escribe el colector de Sports; aquí solo lectura):
     cubre PD, PL, SA, BL1, FL1, DED y PPL (SUPPORTED_FOOTBALL_LEAGUES), NO Segunda (SD).
     Se da por buena si el colector la ha refrescado hace menos de UPCOMING_MAX_AGE_H horas.
  3. Highlightly /matches?leagueId=&date=&timezone=Etc/UTC — 1 petición por liga y día, solo
     para los próximos días (por defecto 3, en UTC) y solo lo que 1 y 2 no cubren: en la
     práctica SP2, o cualquier liga si upcoming_matches está caducada. Se paga con la RESERVA
     MANUAL (HL_MANUAL_RESERVE = 10 de las 100 diarias): contador propio
     "highlightly_fixtures" y nunca más de lo que la API dice que queda.
     Verificado 2026-11-09: `date` filtra en la zona de `timezone` (UTC por defecto) y los
     partidos por jugar traen state.description == "Not started".
  Nunca se inventa un partido. Lo que no se pudo comprobar sale en `unchecked` (liga y día).

Nombres de equipo: se guardan los de football-data.co.uk (los de fdco_team_matches, de donde
saldrá el cálculo). El emparejado es estricto: alias conocidos → igualdad → contención de
palabras completas con candidato ÚNICO. Ante la duda NO se empareja: el partido sigue en la
lista con team_matched=False y el nombre sale en `unmatched_teams` (mejor "sin datos" que la
ficha de otro equipo). `team_mapping` lista cada traducción para revisarla.

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
    HL_LEAGUES, HL_MANUAL_RESERVE, HL_QUOTA_API, HighlightlyBudgetExhausted, hl_get,
)
from collectors.team_identity import normalize

logger = logging.getLogger(__name__)

FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
SEASON_CSV_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
HL_FIXTURES_QUOTA_API = "highlightly_fixtures"   # contador propio dentro de la reserva manual
DEFAULT_WINDOW_DAYS = 3
UPCOMING_MAX_AGE_H = 36
_HTTP_TIMEOUT = 30.0

# Partidos de una jornada completa (equipos / 2), 2026/27
ROUND_SIZE: dict[str, int] = {
    "SP1": 10, "SP2": 11, "E0": 10, "I1": 10,
    "F1": 9, "P1": 9, "D1": 9, "N1": 9,
}

# Código de liga de football-data.org (upcoming_matches.league) → división FDCO
FD_LEAGUE_TO_DIV: dict[str, str] = {v: k for k, v in MAIN_DIVS.items()}
# Las que football-data.org da en el plan gratuito (SUPPORTED_FOOTBALL_LEAGUES); SD no
UPCOMING_DIVS = frozenset({"SP1", "E0", "I1", "D1", "F1", "N1", "P1"})

# Cuotas del CSV que se guardan (media del mercado; la de cierre aún no existe antes del partido)
_ODDS_COLUMNS = {"AvgH": "home", "AvgD": "draw", "AvgA": "away",
                 "Avg>2.5": "over_2_5", "Avg<2.5": "under_2_5"}

# Alias: nombre de football-data.org / Highlightly ya normalizado (team_identity.normalize) →
# nombre de football-data.co.uk. Son los casos en que el emparejado estricto falla o, peor,
# acertaría con otro club: "internazionale milano" → Milan, "paris saint germain" → Paris FC,
# "espanyol barcelona" → Barcelona, "madrid" (Club Atlético de Madrid) → Real Madrid.
# Base: _CORNER_STATS_ALIASES de trend_finder (comprobados en BL1/PD/PL/SA/FL1) + los de
# highlightly_players. Los nombres oficiales de DED/PPL no están comprobados en vivo: lo que
# falte saldrá en unmatched_teams del dry-run.
TEAM_ALIASES: dict[str, str] = {
    # España
    "madrid": "Ath Madrid", "espanyol barcelona": "Espanol", "espanyol": "Espanol",
    "racing santander": "Santander", "sporting gijon": "Sp Gijon",
    # Inglaterra
    "manchester city": "Man City", "manchester united": "Man United",
    "nottingham forest": "Nott'm Forest",
    # Italia
    "internazionale milano": "Inter", "internazionale": "Inter",
    # Alemania
    "bayern munchen": "Bayern Munich", "borussia monchengladbach": "M'gladbach",
    "eintracht frankfurt": "Ein Frankfurt", "hamburger": "Hamburg",
    # Francia
    "paris saint germain": "Paris SG", "stade rennais": "Rennes", "stade brestois": "Brest",
    "olympique lyonnais": "Lyon",
    # Portugal
    "sporting clube portugal": "Sp Lisbon", "sporting cp": "Sp Lisbon",
    "sporting clube braga": "Sp Braga", "vitoria": "Guimaraes",
    # Países Bajos
    "nec": "Nijmegen", "fortuna sittard": "For Sittard",
}
# Nombres que la normalización deja vacíos (todas sus palabras son genéricas)
RAW_ALIASES: dict[str, str] = {"athletic club": "Ath Bilbao", "athletic bilbao": "Ath Bilbao"}


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


def _parse_utc(raw) -> Optional[datetime]:
    """ISO con o sin zona, 'YYYY-MM-DD HH:MM' o datetime (Firestore); sin zona = UTC."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw or "").strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def match_id(div: str, uk_date_iso: str, home: str, away: str) -> str:
    return f"{div}_{uk_date_iso}_{slugify(home)}_{slugify(away)}"


# ── Nombres → football-data.co.uk ──────────────────────────────────────────────

def _raw_key(name: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def map_team(name: str, fdco_teams: set[str]) -> tuple[str, bool]:
    """
    Nombre de football-data.co.uk para un nombre de otra fuente, o (nombre original, False).
    alias → igualdad tras normalizar → contención de palabras completas (en un sentido o en
    el otro) con candidato único. Dos o más candidatos = sin emparejar.
    """
    alias = RAW_ALIASES.get(_raw_key(name))
    n = normalize(name)
    alias = alias or TEAM_ALIASES.get(n)
    if alias:
        return (alias, True) if alias in fdco_teams else (name, False)
    if not n:
        return name, False
    norm = {t: normalize(t) for t in fdco_teams}
    exact = [t for t, nt in norm.items() if nt == n]
    if len(exact) == 1:
        return exact[0], True
    words = set(n.split())
    cands = [t for t, nt in norm.items() if nt and (set(nt.split()) <= words or words <= set(nt.split()))]
    return (cands[0], True) if len(cands) == 1 else (name, False)


def _same_team(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return _raw_key(a) == _raw_key(b)
    wa, wb = set(na.split()), set(nb.split())
    return wa <= wb or wb <= wa


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
            "source": "fixtures_csv", "team_matched": True, "hl_match_id": None, "fd_match_id": None,
            "odds": {k: v for k, v in odds.items() if v is not None} or None,
        })
    return out


# ── 2. upcoming_matches (solo lectura) ─────────────────────────────────────────

def parse_upcoming_docs(docs: list[dict]) -> list[dict]:
    """Docs de upcoming_matches de las 7 ligas → partidos en bruto (nombres de football-data.org)."""
    out = []
    for u in docs:
        div = FD_LEAGUE_TO_DIV.get(u.get("league") or "")
        if div not in UPCOMING_DIVS or u.get("status") not in ("SCHEDULED", "TIMED"):
            continue
        if (u.get("sport") or "football") != "football":
            continue
        ko = _parse_utc(u.get("match_date") or u.get("date"))
        home, away = (u.get("home_team") or "").strip(), (u.get("away_team") or "").strip()
        if ko and home and away:
            out.append({"div": div, "kickoff": ko, "src_home": home, "src_away": away,
                        "source": "football_data_org", "fd_match_id": u.get("match_id"), "hl_match_id": None})
    return out


def upcoming_fresh(docs: list[dict], now: datetime, max_age_h: int = UPCOMING_MAX_AGE_H) -> bool:
    """El colector de Sports reescribe toda la ventana de 7 días de golpe: si algún doc de
    estas ligas se escribió hace menos de max_age_h, la colección está viva (aunque una liga
    no tenga partidos, p. ej. en parón de selecciones)."""
    newest = None
    for u in docs:
        if FD_LEAGUE_TO_DIV.get(u.get("league") or "") not in UPCOMING_DIVS:
            continue
        c = _parse_utc(u.get("collected_at"))
        if c and (newest is None or c > newest):
            newest = c
    return newest is not None and (now - newest) <= timedelta(hours=max_age_h)


def _read_upcoming_docs() -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    leagues = sorted(MAIN_DIVS[d] for d in UPCOMING_DIVS)
    q = col("upcoming_matches").where(filter=FieldFilter("league", "in", leagues))
    return [d.to_dict() or {} for d in q.stream()]


# ── 3. Qué pedir a Highlightly ─────────────────────────────────────────────────

def window_days(now: datetime, days: int = DEFAULT_WINDOW_DAYS) -> list[str]:
    """Días UTC de la ventana: hoy y los siguientes (days en total)."""
    return [(now.date() + timedelta(days=i)).isoformat() for i in range(max(1, days))]


def plan_hl_checks(csv_by_div: dict[str, list[dict]], days: list[str],
                   already_checked: frozenset = frozenset(),
                   covered_divs: frozenset = frozenset()) -> list[dict]:
    """
    (liga, día) a comprobar en Highlightly, en orden de prioridad: primero el día más cercano;
    dentro del día, primero las ligas cuyo CSV está incompleto. covered_divs (las de
    upcoming_matches cuando está al día) no se piden. Una liga con la jornada completa en el
    CSV solo se da por cubierta en los días que abarca ese CSV.
    """
    plan = []
    for div in MAIN_DIVS:
        if div in covered_divs:
            continue
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


def parse_hl_listing(raw, div: str) -> list[dict]:
    """Elementos de /matches → partidos en bruto. Descarta los que no traen hora o equipos."""
    data = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    out = []
    for m in data:
        ko = _parse_utc(m.get("date") or "")
        home = ((m.get("homeTeam") or {}).get("name") or "").strip()
        away = ((m.get("awayTeam") or {}).get("name") or "").strip()
        if not (ko and home and away):
            continue
        out.append({"div": div, "kickoff": ko, "src_home": home, "src_away": away, "source": "highlightly",
                    "hl_match_id": m.get("id"), "fd_match_id": None,
                    "state": ((m.get("state") or {}).get("description") or "")})
    return out


# ── 4. Fusión ──────────────────────────────────────────────────────────────────

def _same_match(a: dict, b: dict) -> bool:
    return (a["div"] == b["div"]
            and abs((_parse_utc(a["kickoff_utc"]) - _parse_utc(b["kickoff_utc"])).total_seconds()) <= 36 * 3600
            and _same_team(a["home_team"], b["home_team"]) and _same_team(a["away_team"], b["away_team"]))


def merge_fixtures(csv_matches: list[dict], others: list[dict],
                   fdco_teams_by_div: dict[str, set[str]]) -> tuple[list[dict], list[str], list[dict]]:
    """
    CSV primero (trae cuotas); después upcoming_matches y Highlightly, en ese orden, solo con
    lo que falta. Si la hora no coincide con el CSV, manda la fuente en vivo y se marca.
    Devuelve (partidos, nombres sin emparejar, traducciones de nombres para revisar).
    """
    merged = [dict(m) for m in csv_matches]
    unmatched: set[str] = set()
    mapping: dict[tuple, str] = {}
    for o in others:
        teams = fdco_teams_by_div.get(o["div"], set())
        home, ok_h = map_team(o["src_home"], teams)
        away, ok_a = map_team(o["src_away"], teams)
        for src, dst, ok in ((o["src_home"], home, ok_h), (o["src_away"], away, ok_a)):
            if ok:
                mapping[(o["div"], o["source"], src)] = dst
            else:
                unmatched.add(f"{o['div']}: {src} ({o['source']})")
        d = uk_date(o["kickoff"])
        cand = {
            "match_id": match_id(o["div"], d, home, away), "div": o["div"], "league": MAIN_DIVS[o["div"]],
            "date": d, "kickoff_utc": o["kickoff"].isoformat(), "home_team": home, "away_team": away,
            "source": o["source"], "team_matched": ok_h and ok_a,
            "hl_match_id": o.get("hl_match_id"), "fd_match_id": o.get("fd_match_id"),
            "src_home": o["src_home"], "src_away": o["src_away"], "odds": None,
        }
        twin = next((m for m in merged if _same_match(m, cand)), None)
        if twin is None:
            merged.append(cand)
            continue
        for k in ("hl_match_id", "fd_match_id"):
            if cand[k] is not None and twin.get(k) is None:
                twin[k] = cand[k]
        if twin["source"] == "fixtures_csv" and twin["kickoff_utc"] != cand["kickoff_utc"] \
                and "kickoff_source" not in twin:
            twin["kickoff_csv_utc"] = twin["kickoff_utc"]
            twin["kickoff_utc"] = cand["kickoff_utc"]
            twin["kickoff_source"] = o["source"]
    merged.sort(key=lambda m: (m["kickoff_utc"], m["div"], m["home_team"]))
    mapping_list = [{"div": k[0], "source": k[1], "name": k[2], "fdco": v} for k, v in sorted(mapping.items())]
    return merged, sorted(unmatched), mapping_list


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
                             max_hl_requests: Optional[int] = None,
                             upcoming_docs: Optional[list[dict]] = None,
                             hl_divs: Optional[frozenset] = None) -> dict:
    """
    Lista de partidos de la ventana. No escribe partidos en ningún sitio.
      api_key None      → sin Highlightly; lo que habría que mirar ahí sale en `unchecked`.
      quota             → QuotaManager: presupuesto = reserva manual (contador propio) y
                          cabecera de la API, y como mucho max_hl_requests si se pasa. Sin quota
                          hace falta max_hl_requests (prueba manual).
      already_checked   → (liga, día) ya comprobados hoy, para no repetir peticiones.
      upcoming_docs     → docs de upcoming_matches (tests); None = leerlos de Firestore.
      hl_divs           → si se pasa, Highlightly solo para esas ligas; el resto del plan sale
                          en `unchecked` ("fuera de hl_divs").
    """
    from collectors.fdco_team_matches import current_season_end_year, season_code

    now = now or datetime.now(timezone.utc)
    season = season or season_code(current_season_end_year(now.date()))
    days_list = window_days(now, days)
    summary: dict = {"window_days": days_list, "matches": [], "by_div": {}, "hl_requests": 0,
                     "hl_checked": [], "unchecked": [], "unmatched_teams": [], "team_mapping": [],
                     "upcoming_fresh": False, "errors": []}

    if upcoming_docs is None:
        try:
            upcoming_docs = _read_upcoming_docs()
        except Exception as e:
            logger.error("match_fixtures: error leyendo upcoming_matches", exc_info=True)
            summary["errors"].append(f"upcoming_matches no leída: {e}")
            upcoming_docs = []
    fresh = upcoming_fresh(upcoming_docs, now)
    summary["upcoming_fresh"] = fresh
    upcoming = parse_upcoming_docs(upcoming_docs) if fresh else []
    if upcoming_docs and not fresh:
        summary["errors"].append(f"upcoming_matches sin refrescar en {UPCOMING_MAX_AGE_H} h: no se usa")

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        text = await _get_text(client, FIXTURES_URL)
        csv_all = parse_fixtures_csv(text) if text else []
        if text is None:
            summary["errors"].append("fixtures.csv no descargado")
        csv_by_div: dict[str, list[dict]] = {}
        for m in csv_all:
            csv_by_div.setdefault(m["div"], []).append(m)
        summary["csv_rows_by_div"] = {d: len(csv_by_div.get(d, [])) for d in MAIN_DIVS}

        plan = plan_hl_checks(csv_by_div, days_list, already_checked,
                              covered_divs=UPCOMING_DIVS if fresh else frozenset())
        skipped = []
        if hl_divs is not None:
            skipped = [{**p, "why": "fuera de hl_divs"} for p in plan if p["div"] not in hl_divs]
            plan = [p for p in plan if p["div"] in hl_divs]
        if api_key and plan:
            if quota is not None:
                used, remaining = _fixtures_quota_state(quota)
                budget = fixtures_budget(used, remaining)
                if max_hl_requests is not None:
                    budget = min(budget, max(0, int(max_hl_requests)))
            else:
                budget = max(0, min(int(max_hl_requests or 0), HL_MANUAL_RESERVE))
            todo, rest = plan[:budget], plan[budget:]
        else:
            todo, rest = [], plan
        summary["unchecked"] = [{**p, "why": "sin presupuesto" if api_key else "sin clave"} for p in rest] + skipped

        hl_matches: list[dict] = []
        for i, p in enumerate(todo):
            path = f"/matches?leagueId={HL_LEAGUES[p['div']]}&date={p['day']}&timezone=Etc/UTC&limit=100"
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
            hl_matches.extend(parse_hl_listing(raw, p["div"]))
            await asyncio.sleep(0.2)

        others = upcoming + hl_matches
        teams_by_div: dict[str, set[str]] = {}
        for div in sorted({o["div"] for o in others}):
            teams_by_div[div] = await _fdco_teams(client, div, season)
            if not teams_by_div[div]:
                summary["errors"].append(f"{div}: sin CSV de temporada para emparejar nombres")

    merged, unmatched, mapping = merge_fixtures(csv_all, others, teams_by_div)
    matches = [m for m in merged if in_window(m, now, days_list)]
    summary["matches"] = matches
    summary["unmatched_teams"] = unmatched
    summary["team_mapping"] = mapping
    for m in matches:
        c = summary["by_div"].setdefault(m["div"], {"total": 0, "fixtures_csv": 0,
                                                     "football_data_org": 0, "highlightly": 0})
        c["total"] += 1
        c[m["source"]] += 1
    summary["unchecked"].sort(key=lambda p: (p["day"], p["div"]))
    logger.info("match_fixtures: %d partidos en %s; upcoming %s; Highlightly %d peticiones; "
                "sin comprobar %d (liga, día); sin emparejar %d",
                len(matches), days_list, "al día" if fresh else "NO usada", summary["hl_requests"],
                len(summary["unchecked"]), len(unmatched))
    return summary
