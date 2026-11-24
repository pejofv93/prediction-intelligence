"""
services/sports-agent/collectors/highlightly_basket.py

ACB en Tendencias: estadísticas de equipo por partido desde Highlightly BALONCESTO (plan gratuito,
100 peticiones/día). SOLO estadísticas para la web de Tendencias: no lo lee el motor de apuestas,
Polymarket ni nada que use cuotas. Sin uso hasta que el usuario active el plan de baloncesto y la
sonda (scripts/ops/hlb_acb_probe.py) confirme el id de la liga, el formato de temporada y los
nombres de los campos.

Fuente (documentación pública, highlightly.net/documentation/basketball):
  base https://basketball.highlightly.net · cabecera x-rapidapi-key (la MISMA clave que fútbol)
  GET /leagues?leagueName=ACB          → id de la liga
  GET /matches?leagueId=&season=&limit=&offset=   → partidos de la temporada (paginado)
  GET /statistics/{matchId}            → estadísticas de equipo (como en fútbol: lista de 2
                                         equipos con statistics[] {displayName, value})
  Nombres de campo y formato de temporada SIN VERIFICAR: se guardan los valores en bruto
  (raw_stats) para poder remapear sin volver a pedir nada.

Cuota PROPIA, separada de la de fútbol: contador "highlightly_basketball" del QuotaManager
(api_quotas/highlightly_basketball_{fecha}), 100 al día, con HLB_RESERVE libres para pruebas a
mano. Además manda la cabecera x-ratelimit-requests-remaining de la API (el menor de los dos).

Colecciones (por shared.firestore_client.col; NUNCA al repo ni a logs públicos):
  hlb_games/{match_id}         partido + estado del detalle + estadísticas (forma de eur_games:
                               home/away con pts, reb, ast, fg3m, poss_est… cuando están todas)
  hlb_load_state/{clave}       avance del listado por (liga, temporada)

Carga reanudable: un partido hecho no se repide; uno vacío o con error se reintenta hasta
HL_MAX_ATTEMPTS. Primero la temporada actual (del más reciente al más antiguo), luego las
anteriores: así el mantenimiento diario va primero y el histórico (3 temporadas) llena lo que sobre.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from collectors import highlightly_loader as hl

logger = logging.getLogger(__name__)

HLB_BASE = "https://basketball.highlightly.net"
HLB_QUOTA_API = "highlightly_basketball"
HLB_DAILY_LIMIT = 100             # plan gratuito de baloncesto
HLB_RESERVE = 10                  # libres cada día para sondas y pruebas a mano
HLB_PAGE_LIMIT = 100
HLB_MAX_ATTEMPTS = 3
HLB_HISTORY_SEASONS = 3           # la actual y las 3 anteriores
_HTTP_TIMEOUT = 60.0

# Id de la ACB en Highlightly: SALE DE LA SONDA (leagues?leagueName=ACB). None = sin confirmar;
# mientras tanto el endpoint exige league_id.
ACB_LEAGUE_ID: Optional[int] = None
LEAGUE_CODE = "ACB"

GAMES, STATE = "hlb_games", "hlb_load_state"

# displayName (en minúsculas) → campo propio. Candidatos; la sonda dice cuáles vienen.
_STAT_ALIASES: dict[str, tuple[str, ...]] = {
    "reb": ("total rebounds", "rebounds"),
    "oreb": ("offensive rebounds",),
    "dreb": ("defensive rebounds",),
    "ast": ("assists",),
    "stl": ("steals",),
    "blk": ("blocks",),
    "tov": ("turnovers", "total turnovers"),
    "fouls": ("personal fouls", "fouls"),
    "fgm": ("field goals made",), "fga": ("field goals attempted",),
    "fg3m": ("3-point field goals made", "three pointers made", "3pt made", "3 points made",
             "three-pointers made", "3-pointers made"),
    "fg3a": ("3-point field goals attempted", "three pointers attempted", "3pt attempted",
             "3 points attempted", "three-pointers attempted", "3-pointers attempted"),
    "ftm": ("free throws made",), "fta": ("free throws attempted",),
}
# Campos «hechos/intentados» en un solo valor ("35/70" o "35-70")
_PAIR_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("fgm", "fga"): ("field goals", "field goals made/attempted"),
    ("fg3m", "fg3a"): ("3-point field goals", "three pointers", "3pt", "3 points", "three-pointers"),
    ("ftm", "fta"): ("free throws", "free throws made/attempted"),
}
_NEEDED = ("reb", "oreb", "ast", "tov", "fga", "fg3m", "fta")


# ── Temporadas y cuota ─────────────────────────────────────────────────────────

def current_season(today: Optional[date] = None) -> int:
    """Año de inicio de la temporada en curso (la ACB va de septiembre a junio): 2026 en nov-2026."""
    today = today or datetime.now(timezone.utc).date()
    return today.year if today.month >= 8 else today.year - 1


def default_seasons(today: Optional[date] = None) -> list[int]:
    """La actual primero y después las HLB_HISTORY_SEASONS anteriores."""
    cur = current_season(today)
    return [cur - i for i in range(HLB_HISTORY_SEASONS + 1)]


def season_code(season: int) -> str:
    return f"A{season}"


def requests_available(used: int, remaining_reported: Optional[int], limit: int = HLB_DAILY_LIMIT,
                       reserve: int = HLB_RESERVE) -> int:
    """El menor de lo que dice el contador propio y lo que dice la API, menos la reserva."""
    left = limit - used
    if remaining_reported is not None:
        left = min(left, int(remaining_reported))
    return max(0, left - reserve)


class BasketBudgetExhausted(Exception):
    pass


async def hlb_get(client: httpx.AsyncClient, api_key: str, path: str, quota=None) -> Optional[object]:
    """GET con la cuota de baloncesto (contador propio + cabecera). 429 → BasketBudgetExhausted."""
    if quota is not None:
        from shared.api_quota_manager import daily_period
        doc = quota._get_doc(f"{HLB_QUOTA_API}_{daily_period(HLB_QUOTA_API)}")
        if requests_available(doc.get("used", 0), doc.get("remaining_reported"), reserve=0) <= 0:
            raise BasketBudgetExhausted()
    try:
        resp = await client.get(HLB_BASE + path, headers={"x-rapidapi-key": api_key})
    except Exception as e:
        logger.error("highlightly_basket: error de red (%s)", type(e).__name__)
        return None
    if quota is not None:
        quota.track_call(HLB_QUOTA_API, resp.headers.get("x-ratelimit-requests-remaining"))
    if resp.status_code == 429:
        raise BasketBudgetExhausted()
    if resp.status_code != 200:
        logger.warning("highlightly_basket: HTTP %d", resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("highlightly_basket: respuesta no JSON")
        return None


# ── Puros ──────────────────────────────────────────────────────────────────────

def is_finished(state: dict) -> bool:
    d = ((state or {}).get("description") or "").lower()
    return d.startswith("finished") or "after over" in d or d in ("ended", "ft", "aot")


def listing_meta(m: dict, league_id: int, season: int) -> dict:
    state = m.get("state") or {}
    hp_, ap_ = hl.parse_score((state.get("score") or {}).get("current"))
    home, away = m.get("homeTeam") or {}, m.get("awayTeam") or {}
    return {"match_id": m.get("id"), "league": LEAGUE_CODE, "league_id": league_id,
            "hl_season": season, "season": season_code(season),
            "date": (m.get("date") or "")[:10], "kickoff_utc": m.get("date"),
            "home_team": home.get("name"), "home_team_id": home.get("id"),
            "away_team": away.get("name"), "away_team_id": away.get("id"),
            "home_code": str(home.get("id")) if home.get("id") is not None else None,
            "away_code": str(away.get("id")) if away.get("id") is not None else None,
            "round": m.get("round"), "finished": is_finished(state), "state": state.get("description"),
            "state_raw": state, "pts_home": hp_, "pts_away": ap_, "status": "pending", "attempts": 0}


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*%?\s*", str(v))
    return float(m.group(1)) if m else None


def _pair(v) -> tuple[Optional[float], Optional[float]]:
    m = re.fullmatch(r"\s*(\d+)\s*[/-]\s*(\d+)\s*", str(v or ""))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def map_stats(raw: dict) -> dict:
    """{displayName: value} → campos propios (None si no vienen)."""
    low = {str(k).strip().lower(): v for k, v in (raw or {}).items()}
    out: dict[str, Optional[float]] = {k: None for k in _STAT_ALIASES}
    for dst, names in _STAT_ALIASES.items():
        for n in names:
            if n in low and _num(low[n]) is not None:
                out[dst] = _num(low[n])
                break
    for (made, att), names in _PAIR_ALIASES.items():
        if out[made] is not None and out[att] is not None:
            continue
        for n in names:
            mm, aa = _pair(low.get(n))
            if mm is not None:
                out[made], out[att] = mm, aa
                break
    return out


def team_block(pts: Optional[int], st: dict) -> Optional[dict]:
    """Bloque home/away con la forma de eur_games, o None si falta algo imprescindible."""
    if pts is None or any(st.get(k) is None for k in _NEEDED):
        return None
    i = lambda k: int(st[k]) if st.get(k) is not None else None  # noqa: E731
    return {"pts": int(pts), "reb": i("reb"), "oreb": i("oreb"), "dreb": i("dreb"), "ast": i("ast"),
            "fg3m": i("fg3m"), "fg3a": i("fg3a"), "fgm": i("fgm"), "fga": i("fga"),
            "ftm": i("ftm"), "fta": i("fta"), "tov": i("tov"), "stl": i("stl"), "blk": i("blk"),
            "fouls": i("fouls"),
            "poss_est": round(st["fga"] + 0.44 * st["fta"] - st["oreb"] + st["tov"], 1)}


def parse_statistics(raw, meta: dict) -> Optional[dict]:
    """/statistics/{id} → campos a fusionar en el doc de hlb_games, o None si viene vacío.
    complete = los dos equipos con todo lo que usa bp-v1 (home/away con la forma de eur_games)."""
    data = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    if len(data) != 2:
        return None
    sides: dict[str, dict] = {}
    for t in data:
        team = t.get("team") or {}
        tid = team.get("id")
        if tid is not None and tid in (meta.get("home_team_id"), meta.get("away_team_id")):
            side = "home" if tid == meta.get("home_team_id") else "away"
        else:
            from collectors.highlightly_players import team_similarity
            name = team.get("name") or ""
            side = ("home" if team_similarity(name, meta.get("home_team") or "")
                    >= team_similarity(name, meta.get("away_team") or "") else "away")
        sides[side] = {str(x.get("displayName")): x.get("value") for x in t.get("statistics") or []
                       if x.get("displayName")}
    if set(sides) != {"home", "away"}:
        return None
    out = {"raw_stats": sides, "n_fields": min(len(sides["home"]), len(sides["away"]))}
    h = team_block(meta.get("pts_home"), map_stats(sides["home"]))
    a = team_block(meta.get("pts_away"), map_stats(sides["away"]))
    out["complete"] = h is not None and a is not None
    if out["complete"]:
        out.update({"home": h, "away": a, "total_pts": h["pts"] + a["pts"],
                    "ot_periods": meta.get("ot_periods") or 0})
    return out


def to_fetch(docs: list[dict], n: int, current: int) -> list[dict]:
    """Terminados sin hacer: primero la temporada actual, cada una del más reciente al más
    antiguo; máximo n."""
    ok = [d for d in docs if d.get("finished") and d.get("status") in ("pending", "error")
          and d.get("attempts", 0) < HLB_MAX_ATTEMPTS]
    ok.sort(key=lambda d: (d.get("hl_season") != current, -(d.get("hl_season") or 0),
                           hl.hp._neg_date(d.get("date") or "")))
    return ok[:max(0, n)]


# ── Carga ──────────────────────────────────────────────────────────────────────

async def _list_season(client, api_key, quota, budget, league_id: int, season: int) -> Optional[list]:
    out, offset = [], 0
    while True:
        if not budget.take():
            return None
        raw = await hlb_get(client, api_key, f"/matches?leagueId={league_id}&season={season}"
                            f"&limit={HLB_PAGE_LIMIT}&offset={offset}", quota=quota)
        if raw is None:
            return None
        page = raw.get("data", []) if isinstance(raw, dict) else []
        out.extend(page)
        total = ((raw.get("pagination") or {}).get("totalCount") if isinstance(raw, dict) else None) or 0
        offset += HLB_PAGE_LIMIT
        if not page or offset >= total:
            return out


def _ingest(listing: list, league_id: int, season: int, store, dry_run: bool, row: dict) -> list[dict]:
    metas = [listing_meta(m, league_id, season) for m in listing]
    metas = [x for x in metas if x["match_id"] is not None]
    known = store.existing(GAMES, [x["match_id"] for x in metas])
    new = {str(x["match_id"]): x for x in metas if str(x["match_id"]) not in known}
    for x in metas:      # ya guardado que acaba de terminar: se marca para pedirlo
        k = known.get(str(x["match_id"]))
        if k and x["finished"] and not k.get("finished"):
            new[str(x["match_id"])] = {**k, "finished": True, "pts_home": x["pts_home"],
                                       "pts_away": x["pts_away"], "state": x["state"],
                                       "state_raw": x["state_raw"]}
    row.update({"listed": len(listing), "finished": sum(1 for x in metas if x["finished"]),
                "new": len(new), "all_finished": bool(metas) and all(x["finished"] for x in metas)})
    if not dry_run and new:
        store.put(GAMES, new)
    return [new.get(str(x["match_id"])) or {**x, **known.get(str(x["match_id"]), {})} for x in metas]


async def run_hlb_load(league_id: Optional[int] = None, seasons: Optional[list[int]] = None,
                       dry_run: bool = True, max_requests: int = HLB_DAILY_LIMIT,
                       now: Optional[datetime] = None, api_key: Optional[str] = None, quota=None,
                       store=None, client: Optional[httpx.AsyncClient] = None) -> dict:
    """
    league_id: id de la ACB en Highlightly (por defecto ACB_LEAGUE_ID, que sale de la sonda).
    seasons: años de inicio (por defecto la actual y las 3 anteriores).
    dry_run: lista y cuenta (gasta las páginas del listado, ≤ 4 por temporada); no escribe nada
             ni pide detalle.
    """
    now = now or datetime.now(timezone.utc)
    league_id = league_id or ACB_LEAGUE_ID
    seasons = seasons or default_seasons(now.date())
    api_key = api_key if api_key is not None else os.environ.get("HIGHLIGHTLY_API_KEY")
    summary: dict = {"league": LEAGUE_CODE, "dry_run": dry_run, "seasons": [], "errors": [],
                     "requests": 0, "fetched": {}}
    if not api_key:
        summary["errors"].append("falta HIGHLIGHTLY_API_KEY")
    if not league_id:
        summary["errors"].append("falta league_id de la ACB (sale de la sonda hlb-acb-probe)")
    if summary["errors"]:
        summary["ok"] = False
        return summary
    if quota is None:
        from shared.api_quota_manager import QuotaManager, daily_period
        quota = QuotaManager()
        qd = quota._get_doc(f"{HLB_QUOTA_API}_{daily_period(HLB_QUOTA_API)}")
        avail = requests_available(qd.get("used", 0), qd.get("remaining_reported"))
    else:
        avail = max_requests
    store = store or hl.FirestoreStore()
    budget = hl._Budget(max(0, min(max_requests, avail)))
    summary["budget"] = budget.left
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    try:
        pending: list[dict] = []
        for season in seasons:
            key = f"acb_{league_id}_{season}"
            st = store.get(STATE, key)
            row = {"season": season}
            if hl.needs_listing(st, now):
                listing = await _list_season(client, api_key, quota, budget, league_id, season)
                if listing is None:
                    row["listed"] = False
                    summary["seasons"].append(row)
                    break        # sin presupuesto o error: sigue en la próxima pasada
                docs = _ingest(listing, league_id, season, store, dry_run, row)
                if not dry_run:
                    store.put(STATE, {key: {"listed_at": now.isoformat(),
                                            "all_finished": row["all_finished"], "kept": len(docs)}})
                row.pop("all_finished")
            else:
                row["listed"] = "ya listada"
                docs = store.by_league_season(GAMES, league_id, season)
            pend = [d for d in docs if d.get("finished") and d.get("status") in ("pending", "error")
                    and d.get("attempts", 0) < HLB_MAX_ATTEMPTS]
            row["pending_detail"] = len(pend)
            pending.extend(pend)
            summary["seasons"].append(row)
        summary["pending_detail"] = len(pending)
        if dry_run:
            summary["would_fetch"] = min(len(pending), budget.left)
        else:
            await _fetch_details(to_fetch(pending, budget.left, current_season(now.date())),
                                 api_key, quota, store, client, budget, summary)
    except BasketBudgetExhausted:
        summary["errors"].append("cuota de Highlightly baloncesto agotada (429 o contador)")
    except Exception as e:
        logger.error("hlb_load: error", exc_info=True)
        summary["errors"].append(type(e).__name__)
    finally:
        if own_client:
            await client.aclose()
    summary["requests"] = budget.used
    summary["ok"] = not summary["errors"]
    return summary


async def _fetch_details(todo: list[dict], api_key, quota, store, client, budget, summary) -> None:
    counts = summary["fetched"]
    for d in todo:
        if not budget.take():
            break
        raw = await hlb_get(client, api_key, f"/statistics/{d['match_id']}", quota=quota)
        parsed = parse_statistics(raw, d) if raw is not None else None
        status, attempts = hl.next_status(parsed is not None, d.get("attempts", 0))
        upd = {"status": status, "attempts": attempts,
               "fetched_at": datetime.now(timezone.utc).isoformat(), **(parsed or {})}
        store.put(GAMES, {str(d["match_id"]): upd})
        key = status if parsed is None else ("done_complete" if parsed["complete"] else "done_incomplete")
        counts[key] = counts.get(key, 0) + 1
