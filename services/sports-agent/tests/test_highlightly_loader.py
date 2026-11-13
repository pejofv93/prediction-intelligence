"""Cargador de Highlightly (mes PRO): filtros de selecciones, estadísticas de equipo, topes,
dry-run sin escrituras y reanudación. Datos INVENTADOS: no se meten respuestas reales de
Highlightly en el repositorio (términos de uso). Sin red ni Firestore."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

from collectors import highlightly_loader as L

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _m(mid, home, away, hid, aid, date="2025-10-10", desc="Finished", score="2 - 1", league="X"):
    return {"id": mid, "date": f"{date}T19:45:00Z", "league": {"name": league},
            "homeTeam": {"id": hid, "name": home}, "awayTeam": {"id": aid, "name": away},
            "state": {"description": desc, "score": {"current": score}}}


def _stats(hid, home, aid, away, corners=(5, 3), complete=True):
    def side(tid, name, c):
        st = [{"displayName": "Corners", "value": c}, {"displayName": "Fouls", "value": 11},
              {"displayName": "Yellow cards", "value": 2}, {"displayName": "Red cards", "value": 0},
              {"displayName": "Shots on target", "value": 4}, {"displayName": "Blocked shots", "value": 2},
              {"displayName": "Goalkeeper saves", "value": 3}]
        if complete:
            st.append({"displayName": "Shots off target", "value": 6})
        return {"team": {"id": tid, "name": name}, "statistics": st}
    return [side(aid, away, corners[1]), side(hid, home, corners[0])]   # visitante primero a propósito


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body, {"x-ratelimit-requests-remaining": "7000"}

    def json(self):
        return self._body


class _Client:
    """routes: {(league, season): [partidos]}, stats: {id: respuesta}, boxes: {id: respuesta}."""

    def __init__(self, routes=None, stats=None, boxes=None, page=100, days=None):
        self.routes, self.stats, self.boxes, self.page, self.calls = routes or {}, stats or {}, boxes or {}, page, []
        self.days = days or {}      # {(league, "AAAA-MM-DD"): [partidos]}

    async def get(self, url, headers=None):
        self.calls.append(url)
        m = re.search(r"/matches\?leagueId=(\d+)&date=([\d-]+)&limit=\d+&offset=(\d+)", url)
        if m:
            lst = self.days.get((int(m.group(1)), m.group(2)), [])
            off = int(m.group(3))
            return _Resp(200, {"data": lst[off:off + 100], "pagination": {"totalCount": len(lst)}})
        m = re.search(r"/matches\?leagueId=(\d+)&season=(\d+)&limit=\d+&offset=(\d+)", url)
        if m:
            lst = self.routes.get((int(m.group(1)), int(m.group(2))), [])
            off = int(m.group(3)) // L.hp.HL_PAGE_LIMIT * self.page
            return _Resp(200, {"data": lst[off:off + self.page], "pagination": {"totalCount": len(lst)}})
        m = re.search(r"/(statistics|box-score)/(\d+)", url)
        if m:
            src = self.stats if m.group(1) == "statistics" else self.boxes
            body = src.get(int(m.group(2)))
            return _Resp(200, body if body is not None else [])
        return _Resp(404)

    async def aclose(self):
        pass


class _Quota:
    def _get_doc(self, key):
        return {"used": 0, "remaining_reported": None}

    def track_call(self, api, remaining):
        pass


class _Store:
    def __init__(self):
        self.c, self.writes = {}, 0

    def get(self, coll, doc_id):
        return self.c.get(coll, {}).get(str(doc_id))

    def existing(self, coll, ids):
        d = self.c.get(coll, {})
        return {str(i): d[str(i)] for i in ids if str(i) in d}

    def put(self, coll, docs):
        for k, v in docs.items():
            self.c.setdefault(coll, {}).setdefault(str(k), {}).update(v)
            self.writes += 1

    def by_league_season(self, coll, league_id, season):
        return [d for d in self.c.get(coll, {}).values()
                if d.get("league_id") == league_id and d.get("hl_season") == season]


def _run(job, client, store, dry_run=False, max_requests=500, **kw):
    return asyncio.run(L.run_hl_load(job, dry_run=dry_run, max_requests=max_requests, now=kw.pop("now", NOW),
                                     api_key="k", quota=_Quota(), store=store, client=client, **kw))


# ── Puros ──────────────────────────────────────────────────────────────────────

def test_senior_masculino_por_nombre():
    for ok in ("Spain", "Bosnia & Herzegovina", "Wales", "Kosovo National Team"):
        assert L.is_senior_men(ok), ok
    for no in ("Spain U21", "Japan U-23", "Brazil Women", "Spain W", "France Olympic", "Iran U23", "England Under 19"):
        assert not L.is_senior_men(no), no


def test_parse_team_stats_por_id_aunque_venga_el_visitante_primero():
    meta = L.listing_meta(_m(1, "Aland", "Bravia", 10, 20), "nt", 28016, 2024)
    p = L.parse_team_stats(_stats(10, "Aland", 20, "Bravia", corners=(7, 2)), meta)
    assert p["home"]["corners"] == 7 and p["away"]["corners"] == 2 and p["complete"]
    inc = L.parse_team_stats(_stats(10, "Aland", 20, "Bravia", complete=False), meta)
    assert inc["complete"] is False and inc["home"]["shots_off"] is None
    assert L.parse_team_stats([], meta) is None
    assert meta["goals_home"] == 2 and meta["goals_away"] == 1 and meta["finished"]


def test_to_fetch_recientes_primero_y_sin_agotados():
    docs = [{"match_id": 1, "finished": True, "status": "pending", "date": "2025-01-01"},
            {"match_id": 2, "finished": True, "status": "error", "attempts": 1, "date": "2025-06-01"},
            {"match_id": 3, "finished": True, "status": "error", "attempts": 3, "date": "2025-07-01"},
            {"match_id": 4, "finished": False, "status": "pending", "date": "2025-08-01"},
            {"match_id": 5, "finished": True, "status": "done", "date": "2025-09-01"}]
    assert [d["match_id"] for d in L.to_fetch(docs, 5)] == [2, 1]


def test_needs_listing():
    assert L.needs_listing(None, NOW)
    assert not L.needs_listing({"listed_at": NOW.isoformat(), "all_finished": True}, NOW + timedelta(days=9))
    assert not L.needs_listing({"listed_at": NOW.isoformat(), "all_finished": False}, NOW + timedelta(hours=5))
    assert L.needs_listing({"listed_at": NOW.isoformat(), "all_finished": False}, NOW + timedelta(hours=21))


# ── Selecciones ────────────────────────────────────────────────────────────────

def _nt_world():
    core = [_m(1, "Aland", "Bravia", 10, 20, date="2025-10-10"),
            _m(2, "Bravia", "Aland", 20, 10, date="2025-11-15"),
            _m(3, "Aland U21", "Bravia U21", 11, 21)]                       # juveniles: fuera
    friendlies = [_m(101, "Aland", "Zeta", 10, 99, date="2026-03-20"),     # una de CORE: dentro
                  _m(102, "Zeta", "Omega", 99, 98),                        # ninguna de CORE: fuera
                  _m(103, "Aland Women", "Zeta Women", 12, 97),            # femenino: fuera
                  _m(104, "Bravia", "Zeta", 20, 99, desc="Not started", score=None)]
    routes = {(28016, 2024): core, (9294, 2026): friendlies}
    stats = {1: _stats(10, "Aland", 20, "Bravia"), 2: _stats(20, "Bravia", 10, "Aland"),
             101: _stats(10, "Aland", 99, "Zeta")}
    return routes, stats


def test_nt_dry_run_cuenta_filtra_y_no_escribe():
    routes, stats = _nt_world()
    store, client = _Store(), _Client(routes, stats)
    out = _run("nt", client, store, dry_run=True)
    assert out["ok"] and store.writes == 0
    assert not any("/statistics/" in u for u in client.calls)
    by = {(r["league_id"], r["season"]): r for r in out["comps"]}
    assert by[(28016, 2024)]["kept"] == 2                     # sin los U21
    assert by[(9294, 2026)]["kept"] == 2                      # 101 y 104
    assert out["core_teams"] == 2 and out["would_fetch"] == 3
    assert out["requests"] == sum(1 for u in client.calls)


def test_nt_escribe_pide_detalle_y_no_repite():
    routes, stats = _nt_world()
    store, client = _Store(), _Client(routes, stats)
    out = _run("nt", client, store)
    assert out["ok"] and out["fetched"] == {"done": 3, "empty": 0, "error": 0}
    docs = store.c[L.TEAM_STATS]
    assert set(docs) == {"1", "2", "101", "104"}
    assert docs["1"]["status"] == "done" and docs["1"]["stats"]["home"]["corners"] == 5
    assert docs["104"]["status"] == "pending" and "stats" not in docs["104"]
    assert store.c[L.STATE]["nt_core_teams"]["ids"] == [10, 20]
    # Segunda pasada: nada nuevo que pedir; las temporadas terminadas no se vuelven a listar.
    client2 = _Client(routes, stats)
    out2 = _run("nt", client2, store)
    assert not any("/statistics/" in u for u in client2.calls)
    assert not any("leagueId=28016&season=2024" in u for u in client2.calls)
    assert any("leagueId=9294&season=2026" in u for u in client2.calls) is False   # < 20 h
    assert out2["fetched"] == {"done": 0, "empty": 0, "error": 0}


def test_nt_partido_que_termina_despues_se_pide():
    routes, stats = _nt_world()
    store = _Store()
    _run("nt", _Client(routes, stats), store)
    routes[(9294, 2026)][3] = _m(104, "Bravia", "Zeta", 20, 99, desc="Finished", score="1 - 1")
    stats[104] = _stats(20, "Bravia", 99, "Zeta")
    out = _run("nt", _Client(routes, stats), store, now=NOW + timedelta(hours=21))
    assert store.c[L.TEAM_STATS]["104"]["status"] == "done"
    assert out["fetched"]["done"] == 1


def test_estadisticas_vacias_reintenta_y_al_tercero_vacio():
    routes, stats = _nt_world()
    del stats[2]
    store = _Store()
    for i in range(3):
        _run("nt", _Client(routes, stats), store, now=NOW + timedelta(hours=21 * i))
    d = store.c[L.TEAM_STATS]["2"]
    assert d["status"] == "empty" and d["attempts"] == 3


def test_tope_de_peticiones_para_y_sigue_en_la_siguiente_pasada():
    routes, stats = _nt_world()
    store, client = _Store(), _Client(routes, stats)
    out = _run("nt", client, store, max_requests=3)
    assert out["requests"] == 3 and len(client.calls) == 3
    # 3 listados (una página por competición, las CORE primero) y ningún detalle
    assert all("/matches?" in u for u in client.calls)


# ── Jugadores y sondeo femenino ────────────────────────────────────────────────

def test_players_guarda_box_score_y_consulta_por_hl_season():
    lid = L.hp.HL_LEAGUES["SP2"]
    routes = {(lid, 2025): [_m(500, "Racing", "Sporting", 1, 2, date="2026-05-01")]}
    box = [{"team": {"id": t, "name": n}, "players": [
        {"id": 7, "name": "X", "minutesPlayed": 90, "statistics": {"shotsTotal": 2, "cardsYellow": 1}}]}
        for t, n in ((1, "Racing"), (2, "Sporting"))]
    store = _Store()
    out = _run("players", _Client(routes, boxes={500: box}), store)
    assert out["ok"] and out["fetched"]["done"] == 1
    assert store.c[L.MATCHES]["500"]["status"] == "done" and store.c[L.MATCHES]["500"]["hl_season"] == 2025
    assert store.c[L.PLAYERS]["500"]["teams"][0]["side"] == "home"
    assert store.by_league_season(L.MATCHES, lid, 2025)


def test_women_probe_gasta_como_mucho_8():
    routes = {(70566, 2025): [_m(900, "Wolfsburg Women", "Essen", 1, 2)]}
    client = _Client(routes, stats={900: _stats(1, "Wolfsburg Women", 2, "Essen", complete=False)})
    out = _run("women_probe", client, _Store())
    assert len(client.calls) <= 8 and out["requests"] == len(client.calls)
    row = next(r for r in out["probe"] if r["league_id"] == 70566)
    assert row["matches_2025"] == 1 and row["stats"]["complete"] is False and "shots_off" in row["stats"]["missing"]


def test_paginacion_de_100_en_100():
    many = [_m(2000 + i, "Aland", "Bravia", 10, 20, date=f"2025-{1 + i % 12:02d}-01") for i in range(150)]
    client = _Client({(28016, 2020): many})
    out = _run("nt", client, _Store(), dry_run=True)
    pages = [u for u in client.calls if "leagueId=28016&season=2020" in u]
    assert len(pages) == 2 and pages[1].endswith("offset=100")
    assert next(r for r in out["comps"] if (r["league_id"], r["season"]) == (28016, 2020))["kept"] == 150


# ── Champions femenina y mantenimiento diario ──────────────────────────────────

def test_champions_femenina_2023_2024_con_el_id_antiguo():
    comps = {(l, y) for l, y, _ in L._comps("women", [1014325])}
    assert {(447559, 2023), (447559, 2024), (1014325, 2025), (1014325, 2026)} <= comps
    assert (1014325, 2023) not in comps and (1014325, 2024) not in comps


def test_maint_days():
    assert L.maint_days(None, NOW) == ["2026-11-10", "2026-11-11", "2026-11-12"]
    assert L.maint_days("2026-11-11", NOW) == ["2026-11-12"]
    assert L.maint_days("2026-11-12", NOW) == []
    assert L.maint_days("2026-10-01", NOW) == ["2026-11-10", "2026-11-11", "2026-11-12"]


def _sm(mid, home, away, hid, aid, day, season, desc="Finished"):
    m = _m(mid, home, away, hid, aid, date=day, desc=desc)
    m["league"]["season"] = season
    return m


def _maint_world():
    lid = L.hp.HL_LEAGUES["SP2"]
    days = {(5039, "2026-11-12"): [_sm(1, "Aland", "Bravia", 10, 20, "2026-11-12", 2026)],
            (9294, "2026-11-12"): [_sm(2, "Aland", "Zeta", 10, 99, "2026-11-12", 2026),
                                   _sm(3, "Yota", "Zeta", 98, 99, "2026-11-12", 2026)],     # sin núcleo
            (121626, "2026-11-11"): [_sm(4, "Barcelona", "Madrid CFF", 30, 31, "2026-11-11", 2026)],
            (lid, "2026-11-12"): [_sm(5, "Racing", "Sporting", 1, 2, "2026-11-12", 2026),
                                  _sm(6, "Eibar", "Huesca", 3, 4, "2026-11-12", 2026, desc="Not started")]}
    stats = {1: _stats(10, "Aland", 20, "Bravia"), 2: _stats(10, "Aland", 99, "Zeta"),
             4: _stats(30, "Barcelona", 31, "Madrid CFF")}
    box = [{"team": {"id": t, "name": n}, "players": [
        {"id": 7, "name": "X", "minutesPlayed": 90, "statistics": {"shotsTotal": 2}}]}
        for t, n in ((1, "Racing"), (2, "Sporting"))]
    return days, stats, {5: box}


def _maint_store():
    store = _Store()
    store.put(L.STATE, {"nt_core_teams": {"ids": [10, 20]}, "maint_last_day": {"day": "2026-11-10"}})
    return store


def test_maint_lista_por_fecha_guarda_y_pide_lo_terminado():
    days, stats, boxes = _maint_world()
    store, client = _maint_store(), _Client(stats=stats, boxes=boxes, days=days)
    out = _run("maint", client, store, max_requests=3000)
    assert out["ok"] and out["days"] == ["2026-11-11", "2026-11-12"]
    assert out["budget"] == L.HL_MAINT_DAILY
    assert not any("season=" in u for u in client.calls if "/matches?" in u)     # solo por fecha
    assert out["fetched"] == {"done": 4, "empty": 0, "error": 0}
    assert store.c[L.TEAM_STATS]["1"]["status"] == "done" and store.c[L.TEAM_STATS]["1"]["hl_season"] == 2026
    assert "3" not in store.c[L.TEAM_STATS]                                         # ninguna selección núcleo
    assert store.c[L.TEAM_STATS]["4"]["group"] == "women"
    assert store.c[L.MATCHES]["5"]["status"] == "done" and store.c[L.MATCHES]["6"]["status"] == "pending"
    assert store.c[L.STATE]["maint_last_day"]["day"] == "2026-11-12"
    assert store.c[L.STATE]["maint_2026-11-13"]["used"] == out["requests"] == len(client.calls)
    # Otra vez el mismo día: no hay días que listar y el tope es lo que quede de los 50.
    client2 = _Client(stats=stats, boxes=boxes, days=days)
    out2 = _run("maint", client2, store, max_requests=3000)
    assert out2["days"] == [] and client2.calls == []
    assert out2["budget"] == L.HL_MAINT_DAILY - out["requests"]


def test_maint_tope_deja_pendientes_para_la_pasada_siguiente():
    days, stats, boxes = _maint_world()
    store = _maint_store()
    n_list = 2 * len(L._maint_comps(None))          # dos días, una página por competición
    out = _run("maint", _Client(stats=stats, boxes=boxes, days=days), store, max_requests=n_list + 1)
    assert out["fetched"]["done"] == 1
    assert len(store.c[L.STATE]["maint_backlog"]["items"]) == 3
    # Al día siguiente: lista solo el día nuevo y termina lo que quedó.
    out2 = _run("maint", _Client(stats=stats, boxes=boxes, days=days), store, max_requests=3000,
                now=NOW + timedelta(days=1))
    assert out2["days"] == ["2026-11-13"] and out2["fetched"]["done"] == 3
    assert store.c[L.STATE]["maint_backlog"]["items"] == []


def test_maint_sin_presupuesto_para_listar_no_avanza_el_dia():
    days, stats, boxes = _maint_world()
    store = _maint_store()
    out = _run("maint", _Client(stats=stats, boxes=boxes, days=days), store, max_requests=5)
    assert out["requests"] == 5
    assert store.c[L.STATE]["maint_last_day"]["day"] == "2026-11-10"


def test_maint_dry_run_no_escribe():
    days, stats, boxes = _maint_world()
    store = _maint_store()
    writes = store.writes
    out = _run("maint", _Client(stats=stats, boxes=boxes, days=days), store, dry_run=True, max_requests=3000)
    assert out["ok"] and store.writes == writes and out["would_fetch"] == 4
