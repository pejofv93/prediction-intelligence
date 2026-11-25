"""ACB con Highlightly baloncesto: cuota propia, parser tolerante y carga reanudable.
Datos inventados; ni red (httpx.MockTransport) ni Firestore (almacén en memoria)."""
import asyncio
from datetime import date, datetime, timezone

import httpx
import pytest

from collectors import highlightly_basket as hb

NOW = datetime(2026, 11, 25, 6, 0, tzinfo=timezone.utc)


def _stats(ast=18, three=("3-point field goals made", 9)):
    vals = [("Total Rebounds", 36), ("Offensive Rebounds", 10), ("Defensive Rebounds", 26),
            ("Assists", ast), ("Turnovers", 12), ("Steals", 7), ("Blocks", 2), ("Personal Fouls", 20),
            ("Field Goals Made", 30), ("Field Goals Attempted", 65), (three[0].title(), three[1]),
            ("3-Point Field Goals Attempted", 27), ("Free Throws", "15/20")]
    return [{"displayName": k, "value": v} for k, v in vals if v is not None]


def _match(mid, day, finished=True, score="85 - 80", hid=1, aid=2):
    return {"id": mid, "date": f"{day}T18:00:00Z", "round": 7,
            "homeTeam": {"id": hid, "name": "Local Inventado"}, "awayTeam": {"id": aid, "name": "Visitante Inventado"},
            "state": {"description": "Finished" if finished else "Not started",
                      "score": {"current": score if finished else None}}}


class Store:
    def __init__(self):
        self.data = {hb.GAMES: {}, hb.STATE: {}}

    def get(self, coll, doc_id):
        return self.data[coll].get(str(doc_id))

    def existing(self, coll, ids):
        return {str(i): self.data[coll][str(i)] for i in ids if str(i) in self.data[coll]}

    def put(self, coll, docs):
        for k, v in docs.items():
            self.data[coll][str(k)] = {**self.data[coll].get(str(k), {}), **v}

    def by_league_season(self, coll, league_id, season):
        return [d for d in self.data[coll].values() if d.get("league_id") == league_id and d.get("hl_season") == season]


def _client(matches_by_season, stats=None, calls=None):
    def handler(req: httpx.Request):
        assert req.headers["x-rapidapi-key"] == "clave-falsa"
        assert req.url.host == "basketball.highlightly.net"
        if calls is not None:
            calls.append(str(req.url.path))
        if req.url.path == "/matches":
            season = int(req.url.params["season"])
            data = matches_by_season.get(season, [])
            return httpx.Response(200, json={"data": data, "pagination": {"totalCount": len(data)}},
                                  headers={"x-ratelimit-requests-remaining": "90"})
        if req.url.path.startswith("/statistics/"):
            return httpx.Response(200, json=stats if stats is not None else [
                {"team": {"id": 2, "name": "Visitante Inventado"}, "statistics": _stats(ast=15)},
                {"team": {"id": 1, "name": "Local Inventado"}, "statistics": _stats()}])
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_temporadas_y_cuota_propia():
    assert hb.current_season(date(2026, 11, 24)) == 2026
    assert hb.current_season(date(2027, 3, 1)) == 2026
    assert hb.default_seasons(date(2026, 11, 24)) == [2026, 2025, 2024, 2023]
    assert hb.HLB_QUOTA_API == "highlightly_basketball"
    from collectors import highlightly_players as hp
    assert hb.HLB_QUOTA_API != hp.HL_QUOTA_API and hb.HLB_BASE != hp.HL_BASE
    assert hb.requests_available(20, None) == 70
    assert hb.requests_available(20, 30) == 20         # manda la API si dice menos
    assert hb.requests_available(95, None) == 0


def test_parser_con_pares_y_alias():
    meta = {"home_team_id": 1, "away_team_id": 2, "pts_home": 85, "pts_away": 80}
    raw = [{"team": {"id": 2}, "statistics": _stats(ast=15)}, {"team": {"id": 1}, "statistics": _stats()}]
    p = hb.parse_statistics(raw, meta)
    assert p["complete"] and p["home"]["ast"] == 18 and p["away"]["ast"] == 15
    assert p["home"]["ftm"] == 15 and p["home"]["fta"] == 20 and p["home"]["fg3m"] == 9
    # FGA 65 + 0,44·20 − 10 + 12 = 75,8
    assert p["home"]["poss_est"] == 75.8 and p["total_pts"] == 165
    assert p["raw_stats"]["home"]["Assists"] == 18           # en bruto, para remapear sin pedir
    raw2 = [{"team": {"id": 2}, "statistics": _stats(ast=None)}, {"team": {"id": 1}, "statistics": _stats()}]
    p2 = hb.parse_statistics(raw2, meta)
    assert p2 is not None and not p2["complete"] and "home" not in p2
    assert hb.parse_statistics([], meta) is None


def test_nombres_reales_de_la_acb():
    # sonda del 25-nov-2026: 14 campos, «Successful X» = anotados y «X» = intentados
    raw = {"Successful Field Goals": 30, "Field Goals": 64, "Successful 3 Pointers": 9,
           "3 Pointers": 26, "Successful Free Throws": 15, "Free Throws": 20, "Assists": 18,
           "Rebounds": 35, "Offensive Rebounds": 10, "Defensive Rebounds": 25, "Steals": 7,
           "Blocks": 2, "Turnovers": 12, "Personal Fouls": 19}
    st = hb.map_stats(raw)
    assert (st["fgm"], st["fga"], st["fg3m"], st["fg3a"], st["ftm"], st["fta"]) == (30, 64, 9, 26, 15, 20)
    assert (st["reb"], st["oreb"], st["dreb"], st["ast"], st["tov"], st["fouls"]) == (35, 10, 25, 18, 12, 19)
    b = hb.team_block(84, st)
    assert b is not None and b["poss_est"] == round(64 + 0.44 * 20 - 10 + 12, 1)
    assert hb.ACB_LEAGUE_ID == 100351


def test_anotados_mayor_que_intentados_deja_el_par_vacio():
    st = hb.map_stats({"Successful 3 Pointers": 12, "3 Pointers": 9})
    assert st["fg3m"] is None and st["fg3a"] is None


def test_el_doc_completo_sirve_al_modelo():
    from analyzers import basket_preview as bp
    meta = hb.listing_meta(_match(10, "2026-10-05"), 99, 2026)
    p = hb.parse_statistics([{"team": {"id": 1}, "statistics": _stats()},
                             {"team": {"id": 2}, "statistics": _stats()}], meta)
    games = bp.games_from_docs([{**meta, **p}], "ACB")
    assert len(games) == 1 and games[0]["season"] == "A2026" and games[0]["home"] == "1"


def test_sin_league_id_ni_clave_no_pide_nada(monkeypatch):
    monkeypatch.setattr(hb, "ACB_LEAGUE_ID", None)
    s = asyncio.run(hb.run_hlb_load(league_id=None, api_key="x", store=Store(), quota=object()))
    assert not s["ok"] and "league_id" in s["errors"][0]
    s = asyncio.run(hb.run_hlb_load(league_id=5, api_key="", store=Store(), quota=object()))
    assert not s["ok"] and "HIGHLIGHTLY_API_KEY" in s["errors"][0]


def _run(store, client, **kw):
    async def go():
        async with client:
            return await hb.run_hlb_load(league_id=99, now=NOW, api_key="clave-falsa", quota=None,
                                         store=store, client=client, **kw)
    return asyncio.run(go())


class FakeQuota:
    """Contador en memoria con la interfaz que usa el colector (sin Firestore)."""
    def __init__(self):
        self.calls = 0

    def _get_doc(self, key):
        assert key.startswith("highlightly_basketball_")
        return {"used": self.calls, "remaining_reported": None}

    def track_call(self, api, remaining):
        assert api == "highlightly_basketball"
        self.calls += 1


@pytest.fixture(autouse=True)
def _fake_quota(monkeypatch):
    fq = FakeQuota()
    monkeypatch.setattr("shared.api_quota_manager.QuotaManager", lambda: fq)
    return fq


def test_carga_dry_run_reanudable_y_por_orden():
    seasons = {2026: [_match(1, "2026-10-05"), _match(2, "2026-11-20"), _match(3, "2026-12-20", finished=False)],
               2025: [_match(11, "2026-01-10"), _match(12, "2026-04-10")]}
    store = Store()

    s = _run(store, _client(seasons), dry_run=True, seasons=[2026, 2025])
    assert s["ok"] and s["requests"] == 2 and s["would_fetch"] == 4
    assert store.data[hb.GAMES] == {} and store.data[hb.STATE] == {}
    assert s["budget"] == 90                  # 100 − 10 de reserva

    calls = []
    s = _run(store, _client(seasons, calls=calls), dry_run=False, seasons=[2026, 2025], max_requests=5)
    # 2 listados + 3 detalles: primero la temporada actual (más reciente antes), luego la anterior
    assert s["requests"] == 5 and s["fetched"] == {"done_complete": 3}
    assert [c for c in calls if c.startswith("/statistics/")] == ["/statistics/2", "/statistics/1", "/statistics/12"]
    assert store.data[hb.GAMES]["2"]["status"] == "done" and store.data[hb.GAMES]["11"]["status"] == "pending"
    assert store.data[hb.GAMES]["3"]["finished"] is False

    # siguiente pasada (menos de 20 h): sin listar de nuevo; solo el que faltaba
    calls.clear()
    s = _run(store, _client(seasons, calls=calls), dry_run=False, seasons=[2026, 2025])
    assert calls == ["/statistics/11"] and s["requests"] == 1


def test_429_corta_la_pasada():
    def handler(req):
        return httpx.Response(429)
    store = Store()
    s = _run(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)), dry_run=False,
             seasons=[2026], max_requests=10)
    assert not s["ok"] and "agotada" in s["errors"][0]
