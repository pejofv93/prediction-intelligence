"""
services/sports-agent/collectors/highlightly_loader.py

Carga de Highlightly durante el mes de plan PRO (12-nov-2026). SOLO estadísticas para la web de
Tendencias: no lo lee el motor de apuestas, Polymarket ni nada que use cuotas.

Trabajos (job):
  nt           selecciones masculinas senior de Europa y Sudamérica → /statistics por partido
  women        Liga F y WSL (y las ligas femeninas que se pasen en `leagues`) → /statistics
  maint        mantenimiento diario (sirve con la cuota gratuita): lista por fecha los días que
               falten hasta ayer (como mucho 3) en las competiciones vivas de los tres trabajos y
               pide el detalle de lo terminado, con un tope propio de HL_MAINT_DAILY al día
  women_probe  sondeo: 1 listado + 1 /statistics de Frauen-Bundesliga, Serie A, D1 y Champions
  players      box score (jugadores) de los clubes de las 8 ligas → /box-score por partido
  cups         copas y supercopas de esos países (CUPS, 2020-2026): solo cruces entre dos equipos
               de las 8 ligas (sacados de players) → /statistics. Va después de players.

Cada pasada: 1) lista los partidos de cada (competición, temporada) pendiente, 2) pide el
detalle de los partidos terminados que falten, del más reciente al más antiguo, hasta el tope
de la pasada. Reanudable: un partido hecho no se vuelve a pedir; uno vacío o con error se
reintenta hasta HL_MAX_ATTEMPTS. dry_run (por defecto en el endpoint) solo lista y cuenta: no
escribe nada ni pide detalle.

Selecciones — qué equipos entran:
  - Son selecciones por construcción: solo se listan competiciones de selecciones (clasificación
    del Mundial, Nations League, Euro, Copa América, Mundial y la liga "Friendlies" 9294, que en
    Highlightly es solo de selecciones: los amistosos de clubes van en "Friendlies Clubs" y los
    femeninos en "Friendlies Women").
  - Fuera juveniles y femeninas por el nombre (U17…U23, Olympic, Women, " W"…).
  - Europa y Sudamérica: las selecciones que aparecen en las competiciones propias de cada
    confederación (CORE: clasificación del Mundial de Europa y de Sudamérica, clasificación de la
    Euro y Nations League). En Mundial, Copa América y amistosos se guarda el partido si juega al
    menos una de ellas. La lista sale de los datos, sin mantenerla a mano.

Colecciones (por shared.firestore_client.col, migran con el resto):
  hl_team_stats/{match_id}        nt, women y cups (group = "cups"): partido + estadísticas de
                                  equipo de los dos lados
  hl_matches/{match_id}           players: partido + estado del box score (como highlightly_players)
  hl_match_players/{match_id}     players: un doc por partido con los jugadores de los dos equipos
  hl_load_state/{clave}           avance del listado por (trabajo, competición, temporada) y la
                                  lista de selecciones de Europa y Sudamérica (nt_core_teams)
"""
import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from collectors import highlightly_players as hp

logger = logging.getLogger(__name__)

HL_LOAD_RESERVE = 1000          # lo que la carga deja siempre libre (fichas SP2, pruebas a mano)
HL_LOAD_MAX_PER_RUN = 3000      # tope por pasada aunque quede cuota
HL_LOAD_CONCURRENCY = 5         # peticiones a la vez (el plan PRO admite 12/s)
HL_RELIST_HOURS = 20            # una temporada con partidos sin terminar se vuelve a listar tras 20 h
HL_MAINT_DAILY = 50             # mantenimiento: tope propio al día (de las 100 gratuitas; el resto,
                                # para las fichas SP2 y las pruebas a mano)
HL_MAINT_MAX_DAYS = 3           # mantenimiento: días que recupera como mucho si no corrió

TEAM_STATS, MATCHES, PLAYERS, STATE = "hl_team_stats", "hl_matches", "hl_match_players", "hl_load_state"

# (id de liga, temporada, ¿CORE?) — ids verificados el 12-nov-2026.
NT_CORE = [(28016, 2020), (28016, 2024),          # clasificación del Mundial, Europa
           (817744, 2023),                        # clasificación de la Euro
           (5039, 2020), (5039, 2022), (5039, 2024), (5039, 2026),   # Nations League
           (29718, 2022), (29718, 2026)]          # clasificación del Mundial, Sudamérica
NT_OTHER = [(4188, 2020), (4188, 2024),           # Euro
            (8443, 2021), (8443, 2024),           # Copa América
            (1635, 2022), (1635, 2026),           # Mundial
            *[(9294, y) for y in range(2020, 2027)]]   # amistosos de selecciones
WOMEN_BASE = [(lid, y) for lid in (121626, 38228) for y in (2023, 2024, 2025, 2026)]   # Liga F, WSL
WOMEN_PROBE = {70566: "Frauen-Bundesliga", 119073: "Serie A Women", 55248: "Feminine Division 1",
               1014325: "UEFA Women's Champions League"}
# Ediciones de cada liga extra. La Champions femenina cambió de id con el formato de liga (2025):
# 2023 y 2024 están en 447559 (15 campos: córners, tarjetas, remates y faltas; sin xG).
WOMEN_EXTRA_SEASONS = {1014325: [(447559, 2023), (447559, 2024), (1014325, 2025), (1014325, 2026)]}
PLAYER_SEASONS = ("2425", "2526", "2627")
# Mantenimiento: competiciones vivas. Se lista por fecha, así que las temporadas nuevas de estas
# ligas entran solas. Las fases finales (Euro, Copa América, Mundial) se añaden a mano cuando toquen.
MAINT_NT = [(28016, True), (817744, True), (5039, True), (29718, True), (9294, False)]
# Copas y supercopas de los países de las 8 ligas (ids verificados el 14-nov-2026). Solo se guardan
# los cruces entre dos equipos que ya tenemos en las ligas (hl_matches de players), con estadísticas
# de equipo: para fichas de copa "solo referencia" y para medir la diferencia con la liga.
CUPS = {122477: "Copa del Rey", 473940: "Supercopa de España",
        39079: "FA Cup", 41632: "League Cup", 450112: "Community Shield",
        117371: "Coppa Italia", 466281: "Supercoppa",
        69715: "DFB-Pokal", 450963: "DFL-Supercup",
        56950: "Coupe de France", 448410: "Trophée des Champions",
        82480: "Taça de Portugal", 83331: "Taça da Liga", 468834: "Supertaça",
        77374: "KNVB Beker", 462877: "Johan Cruijff Schaal"}
CUP_SEASONS = (2026, 2025, 2024, 2023, 2022, 2021, 2020)   # la más reciente primero
CUP_CURRENT_SEASON = 2026
CUP_TEAMS_MAX_DAYS = 7          # la lista de equipos de liga se recalcula como mucho cada 7 días
CUP_TEAMS_MIN = 100             # menos equipos = players sin cargar: no se lista nada

_YOUTH_WOMEN = re.compile(r"\bU\s?-?\d{2}\b|\bunder[- ]?\d{2}\b|olympic|women|femenin|f[ée]minin|frauen|\bW$",
                          re.IGNORECASE)

# displayName de /statistics → campo propio
_STAT_FIELDS = {"Corners": "corners", "Fouls": "fouls", "Yellow cards": "yellows", "Red cards": "reds",
                "Shots on target": "shots_on", "Shots off target": "shots_off",
                "Blocked shots": "shots_blocked", "Goalkeeper saves": "saves", "Offsides": "offsides"}
_CORE_FIELDS = ("corners", "yellows", "shots_on", "shots_off")


# ── Puros ──────────────────────────────────────────────────────────────────────

def is_senior_men(name: str) -> bool:
    return bool(name) and not _YOUTH_WOMEN.search(name)


def parse_score(score) -> tuple[Optional[int], Optional[int]]:
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)", str(score or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def listing_meta(m: dict, group: str, league_id: int, season: int) -> dict:
    state = m.get("state") or {}
    desc = state.get("description") or ""
    hg, ag = parse_score((state.get("score") or {}).get("current"))
    home, away = m.get("homeTeam") or {}, m.get("awayTeam") or {}
    return {"match_id": m.get("id"), "group": group, "league_id": league_id,
            "league": (m.get("league") or {}).get("name"), "season": season, "hl_season": season,
            "date": (m.get("date") or "")[:10], "kickoff_utc": m.get("date"),
            "home_team": home.get("name"), "home_team_id": home.get("id"),
            "away_team": away.get("name"), "away_team_id": away.get("id"),
            "finished": desc.startswith("Finished"), "state": desc,
            "goals_home": hg, "goals_away": ag, "status": "pending", "attempts": 0}


def keep_nt(meta: dict, core_ids: set, is_core_comp: bool) -> bool:
    if not (is_senior_men(meta["home_team"] or "") and is_senior_men(meta["away_team"] or "")):
        return False
    return is_core_comp or meta["home_team_id"] in core_ids or meta["away_team_id"] in core_ids


def parse_team_stats(raw, meta: dict) -> Optional[dict]:
    """/statistics/{id} → {home:{…}, away:{…}, n_fields, complete} o None si viene vacío."""
    data = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    if len(data) != 2:
        return None
    sides = {}
    for t in data:
        team = t.get("team") or {}
        tid, tname = team.get("id"), team.get("name") or ""
        if tid is not None and tid in (meta["home_team_id"], meta["away_team_id"]):
            side = "home" if tid == meta["home_team_id"] else "away"
        else:
            side = ("home" if hp.team_similarity(tname, meta["home_team"] or "")
                    >= hp.team_similarity(tname, meta["away_team"] or "") else "away")
        vals = {x.get("displayName"): x.get("value") for x in t.get("statistics") or []}
        row = {dst: _num(vals.get(src)) for src, dst in _STAT_FIELDS.items()}
        row["n_fields"] = len(vals)
        sides[side] = row
    if set(sides) != {"home", "away"}:
        return None
    complete = all(sides[s][f] is not None for s in sides for f in _CORE_FIELDS)
    return {"home": sides["home"], "away": sides["away"], "complete": complete,
            "n_fields": min(sides["home"]["n_fields"], sides["away"]["n_fields"])}


def _num(v) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def next_status(parsed_ok: bool, attempts: int) -> tuple[str, int]:
    attempts += 1
    if parsed_ok:
        return "done", attempts
    return ("empty" if attempts >= hp.HL_MAX_ATTEMPTS else "error"), attempts


def to_fetch(docs: list[dict], n: int) -> list[dict]:
    """Terminados sin hacer, del más reciente al más antiguo, máximo n."""
    ok = [d for d in docs if d.get("finished") and d.get("status") in ("pending", "error")
          and d.get("attempts", 0) < hp.HL_MAX_ATTEMPTS]
    ok.sort(key=lambda d: d.get("date") or "", reverse=True)
    return ok[:max(0, n)]


def needs_listing(state: Optional[dict], now: datetime) -> bool:
    if not state or not state.get("listed_at"):
        return True
    if state.get("all_finished"):
        return False
    listed = datetime.fromisoformat(state["listed_at"])
    return (now - listed).total_seconds() >= HL_RELIST_HOURS * 3600


# ── Almacén (Firestore) ────────────────────────────────────────────────────────

class FirestoreStore:
    def get(self, coll: str, doc_id: str) -> Optional[dict]:
        from shared.firestore_client import col
        snap = col(coll).document(str(doc_id)).get()
        return snap.to_dict() if snap.exists else None

    def existing(self, coll: str, ids: list) -> dict:
        from shared.firestore_client import col, get_client
        refs = [col(coll).document(str(i)) for i in ids]
        out = {}
        for k in range(0, len(refs), 300):
            for snap in get_client().get_all(refs[k:k + 300]):
                if snap.exists:
                    out[snap.id] = snap.to_dict()
        return out

    def put(self, coll: str, docs: dict) -> None:
        from shared.firestore_client import col, get_client
        items = list(docs.items())
        for k in range(0, len(items), 400):
            batch = get_client().batch()
            for doc_id, doc in items[k:k + 400]:
                batch.set(col(coll).document(str(doc_id)), doc, merge=True)
            batch.commit()

    def by_league_season(self, coll: str, league_id: int, season) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        from shared.firestore_client import col
        q = (col(coll).where(filter=FieldFilter("league_id", "==", league_id))
             .where(filter=FieldFilter("hl_season", "==", season)))
        return [s.to_dict() for s in q.stream()]


# ── Carga ──────────────────────────────────────────────────────────────────────

class _Budget:
    def __init__(self, n: int):
        self.left = n
        self.used = 0

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        self.used += 1
        return True


async def _list_all(client, api_key, quota, budget: _Budget, league_id: int, season: int) -> Optional[list]:
    return await _list_pages(client, api_key, quota, budget, f"leagueId={league_id}&season={season}")


async def _list_day(client, api_key, quota, budget: _Budget, league_id: int, day: str) -> Optional[list]:
    return await _list_pages(client, api_key, quota, budget, f"leagueId={league_id}&date={day}")


async def _list_pages(client, api_key, quota, budget: _Budget, query: str) -> Optional[list]:
    out, offset = [], 0
    while True:
        if not budget.take():
            return None
        raw = await hp.hl_get(client, api_key, f"/matches?{query}"
                              f"&limit={hp.HL_PAGE_LIMIT}&offset={offset}", quota=quota)
        if raw is None:
            return None
        page = raw.get("data", []) if isinstance(raw, dict) else []
        out.extend(page)
        total = ((raw.get("pagination") or {}).get("totalCount") if isinstance(raw, dict) else None) or 0
        offset += hp.HL_PAGE_LIMIT
        if not page or offset >= total:
            return out


def _comps(job: str, leagues: Optional[list]) -> list[tuple[int, int, bool]]:
    if job == "nt":
        return [(l, y, True) for l, y in NT_CORE] + [(l, y, False) for l, y in NT_OTHER]
    if job == "women":
        extra = [c for l in (leagues or []) if l in WOMEN_PROBE
                 for c in WOMEN_EXTRA_SEASONS.get(l, [(l, y) for y in (2023, 2024, 2025, 2026)])]
        return [(l, y, True) for l, y in WOMEN_BASE + extra]
    if job == "players":
        return [(lid, hp.hl_season(s), True) for lid in hp.HL_LEAGUES.values() for s in PLAYER_SEASONS]
    if job == "cups":
        return [(lid, y, True) for y in CUP_SEASONS for lid in CUPS]
    raise ValueError(f"job desconocido: {job}")


async def run_hl_load(job: str, dry_run: bool = True, max_requests: int = HL_LOAD_MAX_PER_RUN,
                      leagues: Optional[list] = None, now: Optional[datetime] = None,
                      api_key: Optional[str] = None, quota=None, store=None,
                      client: Optional[httpx.AsyncClient] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    api_key = api_key if api_key is not None else os.environ.get("HIGHLIGHTLY_API_KEY")
    summary = {"job": job, "dry_run": dry_run, "comps": [], "fetched": {}, "errors": [], "requests": 0}
    if not api_key:
        summary["errors"].append("faltan HIGHLIGHTLY_API_KEY")
        summary["ok"] = False
        return summary
    if quota is None:
        from shared.api_quota_manager import QuotaManager, daily_period
        quota = QuotaManager()
        qd = quota._get_doc(f"{hp.HL_QUOTA_API}_{daily_period(hp.HL_QUOTA_API)}")
        avail = hp.requests_available(qd.get("used", 0), qd.get("remaining_reported"),
                                      reserve=hp.HL_MANUAL_RESERVE if job == "maint" else HL_LOAD_RESERVE)
    else:
        avail = max_requests
    store = store or FirestoreStore()
    if job == "maint":     # tope propio del día, aunque se lance varias veces
        avail = min(avail, max(0, HL_MAINT_DAILY - _maint_used(store, now)))
    budget = _Budget(min(max_requests, HL_LOAD_MAX_PER_RUN, avail))
    summary["budget"] = budget.left
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=hp._HTTP_TIMEOUT)
    try:
        if job == "women_probe":
            summary["probe"] = await _probe(client, api_key, quota, budget)
        elif job == "maint":
            await _maint(dry_run, leagues, now, api_key, quota, store, client, budget, summary)
        else:
            await _load(job, dry_run, leagues, now, api_key, quota, store, client, budget, summary)
    except hp.HighlightlyBudgetExhausted:
        summary["errors"].append("cuota de Highlightly agotada (429 o contador)")
    except Exception as e:
        logger.error("hl_load(%s): error", job, exc_info=True)
        summary["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        if own_client:
            await client.aclose()
    summary["requests"] = budget.used
    summary["ok"] = not summary["errors"]
    return summary


async def _load(job, dry_run, leagues, now, api_key, quota, store, client, budget, summary):
    coll = MATCHES if job == "players" else TEAM_STATS
    core_doc = (store.get(STATE, "nt_core_teams") or {}) if job == "nt" else {}
    core_ids = set(core_doc.get("ids", []))
    if job == "cups":     # en copas, core_ids = equipos de las 8 ligas
        core_ids = cup_team_ids(store, now, dry_run)
        summary["cup_teams"] = len(core_ids)
        if len(core_ids) < CUP_TEAMS_MIN:
            summary["errors"].append(f"solo {len(core_ids)} equipos de liga: carga players antes que cups")
            return
    pending_all: list[dict] = []
    for league_id, season, is_core in _comps(job, leagues):
        key = f"{job}_{league_id}_{season}"
        st = store.get(STATE, key)
        row = {"league_id": league_id, "season": season}
        listing_needed = needs_listing(st, now)
        if listing_needed:
            listing = await _list_all(client, api_key, quota, budget, league_id, season)
            if listing is None:
                row["listed"] = False
                summary["comps"].append(row)
                break     # sin presupuesto o error: se sigue en la próxima pasada (CORE va primero)
            row["listed"] = True
            docs = _ingest(job, listing, league_id, season, is_core, core_ids, store, dry_run, row)
            if not dry_run:
                store.put(STATE, {key: {"listed_at": now.isoformat(), "all_finished": row["all_finished"],
                                        "kept": row["kept"]}})
            row.pop("all_finished")
        else:
            row["listed"] = "ya listada"
            docs = store.by_league_season(coll, league_id, season)
        pend = _pending(docs)
        row["pending_detail"] = len(pend)
        pending_all.extend(pend)
        summary["comps"].append(row)
    if job == "nt" and not dry_run and core_ids != set(core_doc.get("ids", [])):
        store.put(STATE, {"nt_core_teams": {"ids": sorted(core_ids), "updated_at": now.isoformat()}})
    summary["core_teams"] = len(core_ids) if job == "nt" else None
    summary["pending_detail"] = len(pending_all)
    if dry_run:
        summary["would_fetch"] = min(len(pending_all), budget.left)
        return
    await _fetch_details(job, to_fetch(pending_all, budget.left), api_key, quota, store, client, budget, summary)


def _ingest(job, listing, league_id, season, is_core, core_ids: set, store, dry_run, row: dict) -> list[dict]:
    """Listado de una competición → docs (nuevos, ya guardados y los que acaban de terminar).
    Guarda los nuevos salvo en dry-run y rellena los recuentos en row. En las CORE amplía core_ids."""
    coll = MATCHES if job == "players" else TEAM_STATS
    if job == "players":
        div = next(d for d, lid in hp.HL_LEAGUES.items() if lid == league_id)
        fdco = f"{season % 100:02d}{(season + 1) % 100:02d}"
        metas = []
        for m in listing:
            mm = hp.match_meta_from_listing(m, div, fdco)
            mm.update({"league_id": league_id, "hl_season": season, "status": "pending", "attempts": 0,
                       "finished": ((m.get("state") or {}).get("description") or "").startswith("Finished")})
            metas.append(mm)
    else:
        metas = [listing_meta(m, job, league_id, season) for m in listing]
        if job == "nt":
            if is_core:
                core_ids |= {i for x in metas for i in (x["home_team_id"], x["away_team_id"])
                             if i is not None and is_senior_men(
                                 x["home_team"] if i == x["home_team_id"] else x["away_team"])}
            metas = [x for x in metas if keep_nt(x, core_ids, is_core)]
        elif job == "cups":
            metas = [x for x in metas if x["home_team_id"] in core_ids and x["away_team_id"] in core_ids]
    known = store.existing(coll, [x["match_id"] for x in metas if x["match_id"] is not None])
    new = {str(x["match_id"]): x for x in metas
           if x["match_id"] is not None and str(x["match_id"]) not in known}
    # Un partido ya guardado que acaba de terminar: se marca para pedirlo.
    for x in metas:
        k = known.get(str(x["match_id"]))
        if k and x["finished"] and not k.get("finished"):
            new[str(x["match_id"])] = {**k, "finished": True, "goals_home": x.get("goals_home"),
                                       "goals_away": x.get("goals_away"), "state": x.get("state")}
    row.update({"listed_total": len(listing), "kept": len(metas),
                "finished": sum(1 for x in metas if x["finished"]), "new": len(new),
                "all_finished": bool(metas) and all(x["finished"] for x in metas)})
    if job == "cups":     # se mira el listado entero: una edición pasada sin cruces nuestros no se relista
        fin = [((m.get("state") or {}).get("description") or "").startswith("Finished") for m in listing]
        row["all_finished"] = all(fin) if fin else season < CUP_CURRENT_SEASON
    if not dry_run:
        store.put(coll, new)
    docs = []
    for x in metas:
        mid = str(x["match_id"])
        docs.append(new[mid] if mid in new else {**x, **known[mid]} if mid in known else x)
    return docs


def _pending(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("finished") and d.get("status") in ("pending", "error")
            and d.get("attempts", 0) < hp.HL_MAX_ATTEMPTS]


def cup_team_ids(store, now: datetime, dry_run: bool) -> set:
    """Equipos de las 8 ligas (ids de Highlightly) sacados de hl_matches de players, guardados en
    hl_load_state/cups_team_ids y recalculados como mucho cada CUP_TEAMS_MAX_DAYS."""
    doc = store.get(STATE, "cups_team_ids") or {}
    if doc.get("ids") and doc.get("updated_at") and \
            now - datetime.fromisoformat(doc["updated_at"]) < timedelta(days=CUP_TEAMS_MAX_DAYS):
        return set(doc["ids"])
    ids = set()
    for lid in hp.HL_LEAGUES.values():
        for s in PLAYER_SEASONS:
            for d in store.by_league_season(MATCHES, lid, hp.hl_season(s)):
                ids |= {d.get("home_team_id"), d.get("away_team_id")}
    ids.discard(None)
    if not dry_run and len(ids) >= CUP_TEAMS_MIN:
        store.put(STATE, {"cups_team_ids": {"ids": sorted(ids), "updated_at": now.isoformat()}})
    return ids


# ── Mantenimiento ──────────────────────────────────────────────────────────────

def maint_days(last_day: Optional[str], now: datetime) -> list[str]:
    """Días a listar: del siguiente al último hecho hasta ayer, como mucho HL_MAINT_MAX_DAYS."""
    yesterday = (now - timedelta(days=1)).date()
    first = yesterday - timedelta(days=HL_MAINT_MAX_DAYS - 1)
    if last_day:
        first = max(first, date.fromisoformat(last_day) + timedelta(days=1))
    return [(first + timedelta(days=i)).isoformat() for i in range((yesterday - first).days + 1)]


def _maint_comps(leagues) -> list[tuple[str, int, bool]]:
    women = [121626, 38228] + [l for l in (leagues or []) if l in WOMEN_PROBE]
    return ([("nt", l, core) for l, core in MAINT_NT] + [("women", l, True) for l in women]
            + [("players", l, True) for l in hp.HL_LEAGUES.values()])


def _maint_used(store, now: datetime) -> int:
    return (store.get(STATE, f"maint_{now.date().isoformat()}") or {}).get("used", 0)


async def _maint(dry_run, leagues, now, api_key, quota, store, client, budget, summary):
    """Lista por fecha los días pendientes y pide el detalle de lo terminado, lo más reciente
    primero. Lo que no quepa queda en hl_load_state/maint_backlog para la pasada siguiente."""
    core_ids = set((store.get(STATE, "nt_core_teams") or {}).get("ids", []))
    last = (store.get(STATE, "maint_last_day") or {}).get("day")
    days = maint_days(last, now)
    pend: dict = {"nt": {}, "women": {}, "players": {}}
    for it in (store.get(STATE, "maint_backlog") or {}).get("items", []):
        coll = MATCHES if it["job"] == "players" else TEAM_STATS
        pend[it["job"]].update(store.existing(coll, [it["match_id"]]))
    done_day = last
    for day in days:
        complete = True
        for job, league_id, is_core in _maint_comps(leagues):
            listing = await _list_day(client, api_key, quota, budget, league_id, day)
            if listing is None:      # sin presupuesto o error: ese día se repite en la pasada siguiente
                complete = False
                break
            by_season: dict = {}
            for m in listing:
                by_season.setdefault((m.get("league") or {}).get("season"), []).append(m)
            for season, ms in by_season.items():
                if season is None:
                    continue
                row = {"job": job, "league_id": league_id, "season": int(season), "day": day}
                docs = _ingest(job, ms, league_id, int(season), is_core, core_ids, store, dry_run, row)
                row.pop("all_finished")
                pend[job].update({str(d["match_id"]): d for d in _pending(docs)})
                summary["comps"].append(row)
        if not complete:
            break
        done_day = day
    pend = {job: {k: d for k, d in v.items() if _pending([d])} for job, v in pend.items()}
    summary["days"] = days
    summary["pending_detail"] = sum(len(v) for v in pend.values())
    if dry_run:
        summary["would_fetch"] = min(summary["pending_detail"], budget.left)
        return
    fetched = {"done": 0, "empty": 0, "error": 0}
    for job in ("nt", "women", "players"):
        picks = to_fetch(list(pend[job].values()), budget.left)
        if not picks:
            continue
        sub: dict = {}
        await _fetch_details(job, picks, api_key, quota, store, client, budget, sub)
        for k, v in sub["fetched"].items():
            fetched[k] += v
        got = {str(d["match_id"]) for d in picks}
        pend[job] = {k: d for k, d in pend[job].items() if k not in got}
    summary["fetched"] = fetched
    store.put(STATE, {
        "maint_backlog": {"items": [{"job": j, "match_id": k} for j in pend for k in pend[j]],
                          "updated_at": now.isoformat()},
        f"maint_{now.date().isoformat()}": {"used": _maint_used(store, now) + budget.used}})
    if done_day and done_day != last:
        store.put(STATE, {"maint_last_day": {"day": done_day}})


async def _fetch_details(job, picks, api_key, quota, store, client, budget, summary):
    coll = MATCHES if job == "players" else TEAM_STATS
    sem = asyncio.Semaphore(HL_LOAD_CONCURRENCY)
    counts = {"done": 0, "empty": 0, "error": 0}
    upd, extra = {}, {}

    async def one(d):
        async with sem:
            if not budget.take():
                return
            path = f"/box-score/{d['match_id']}" if job == "players" else f"/statistics/{d['match_id']}"
            raw = await hp.hl_get(client, api_key, path, quota=quota)
            if job == "players":
                parsed = hp.parse_box_score(raw, d) if raw is not None else None
            else:
                parsed = parse_team_stats(raw, d) if raw is not None else None
            status, attempts = next_status(parsed is not None, d.get("attempts", 0))
            counts[status] += 1
            if job == "players":
                upd[str(d["match_id"])] = {"status": status, "attempts": attempts}
                if parsed:
                    extra[str(d["match_id"])] = parsed
            else:
                upd[str(d["match_id"])] = {"status": status, "attempts": attempts,
                                           **({"stats": parsed} if parsed else {})}

    await asyncio.gather(*(one(d) for d in picks))
    store.put(coll, upd)
    if extra:
        store.put(PLAYERS, extra)
    summary["fetched"] = counts


async def _probe(client, api_key, quota, budget: _Budget) -> list[dict]:
    out = []
    for league_id, name in WOMEN_PROBE.items():
        row = {"league_id": league_id, "name": name}
        listing = None
        if budget.take():
            raw = await hp.hl_get(client, api_key, f"/matches?leagueId={league_id}&season=2025&limit=100&offset=0",
                                  quota=quota)
            if isinstance(raw, dict):
                listing = raw.get("data", [])
                row["matches_2025"] = (raw.get("pagination") or {}).get("totalCount")
        fin = [m for m in (listing or []) if ((m.get("state") or {}).get("description") or "").startswith("Finished")]
        row["finished_in_page"] = len(fin)
        if fin and budget.take():
            meta = listing_meta(fin[0], "women", league_id, 2025)
            raw = await hp.hl_get(client, api_key, f"/statistics/{meta['match_id']}", quota=quota)
            p = parse_team_stats(raw, meta) if raw is not None else None
            row["stats"] = None if p is None else {"complete": p["complete"], "n_fields": p["n_fields"],
                                                   "missing": [f for f in _STAT_FIELDS.values()
                                                               if p["home"][f] is None]}
        out.append(row)
    return out
