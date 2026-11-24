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


# ── Mantenimiento: copas (fichas mp-v1-cups) ────────────────────────────────────

class _CupStore(_Store):
    """Almacén con fichas de copa (match_previews_cups) y escritura sin merge."""

    def __init__(self, previews=()):
        super().__init__()
        self.previews = list(previews)

    def cup_previews(self, lo, hi):
        return [p for p in self.previews if lo <= p["kickoff_utc"] <= hi]

    def all_docs(self, coll):
        return list(self.c.get(coll, {}).values())

    def replace(self, coll, docs):
        for k, v in docs.items():
            self.c.setdefault(coll, {})[str(k)] = dict(v)
            self.writes += 1


def _cup_preview(hl_id=50, day="2026-11-12"):
    """Ficha de copa mínima (como la escribe run_cup_previews): una línea fija y una "extra"."""
    blk = {"market": "corners_team", "label": "medido en backtest", "highlight": False,
           "lines": {"over_4_5": {"line": 4.5, "p_model": 0.55, "p_league": 0.5},
                     "over_3_5": {"line": 3.5, "p_model": 0.6, "p_league": 0.55, "extra": True}}}
    return {"match_id": f"ES_{day}_{hl_id}", "div": "SP1", "cup": "Copa del Rey", "cup_id": 122477,
            "country": "ES", "season": "2627", "date": day, "kickoff_utc": f"{day}T19:45:00+00:00",
            "home_team": "Racing", "away_team": "Sporting", "hl_match_id": hl_id, "hash": "h",
            "computed_at": f"{day}T02:00:00+00:00", "data_cutoff": "2026-11-09",
            "stats": {"corners": {"available": True, "lines": {"home": blk}}}}


def _cup_maint_world():
    days, stats, boxes = _maint_world()
    days[(122477, "2026-11-12")] = [_sm(50, "Racing", "Sporting", 1, 2, "2026-11-12", 2026),
                                    _sm(51, "Otro", "Más", 7, 8, "2026-11-12", 2026)]     # sin ficha
    stats[50] = _stats(1, "Racing", 2, "Sporting", corners=(6, 2))
    stats[51] = _stats(7, "Otro", 8, "Más")
    return days, stats, boxes


def test_maint_copas_lista_solo_lo_que_tiene_ficha_y_gradua():
    days, stats, boxes = _cup_maint_world()
    base = _Client(stats=stats, boxes=boxes, days=days)
    _run("maint", base, _maint_store(), max_requests=3000)               # sin fichas de copa
    store = _CupStore([_cup_preview()])
    store.put(L.STATE, {"nt_core_teams": {"ids": [10, 20]}, "maint_last_day": {"day": "2026-11-10"}})
    client = _Client(stats=stats, boxes=boxes, days=days)
    out = _run("maint", client, store, max_requests=3000)
    assert out["ok"]
    # Extra: 1 listado (Copa del Rey, 12-nov) + 1 /statistics del cruce con ficha
    assert len(client.calls) - len(base.calls) == 2 and out["requests"] == len(client.calls)
    assert sum("leagueId=122477&date=2026-11-12" in u for u in client.calls) == 1
    assert not any("leagueId=122477&date=2026-11-11" in u for u in client.calls)   # ese día sin fichas
    assert any(u.endswith("/statistics/50") for u in client.calls)
    assert not any(u.endswith("/statistics/51") for u in client.calls)
    ts = store.c[L.TEAM_STATS]
    assert ts["50"]["group"] == "cups" and ts["50"]["status"] == "done" and "51" not in ts
    g = store.c["match_preview_grades_cups"]["ES_2026-11-12_50"]
    assert g["status"] == "graduada" and [ln["line"] for ln in g["lines"]] == [4.5]   # sin la "extra"
    assert g["lines"][0]["value"] == 6 and g["lines"][0]["y"] == 1
    live = store.c["match_preview_metrics"]["mp-v1-cups_live"]
    assert live["n_graduated"] == 1 and live["n_by_market"] == {"corners_team": 1}
    assert out["cup_grade"]["counts"] == {"graduada": 1} and out["cup_grade"]["star_markets"] == []
    # Al día siguiente: nada de copas que listar ni que graduar otra vez.
    client2 = _Client(stats=stats, boxes=boxes, days=days)
    out2 = _run("maint", client2, store, max_requests=3000, now=NOW + timedelta(days=1))
    assert not any("leagueId=122477" in u for u in client2.calls)
    assert out2["cup_grade"]["counts"] == {} and out2["cup_grade"]["n_graduated"] == 1


def test_maint_copas_dry_run_no_escribe_ni_pide_detalle():
    days, stats, boxes = _cup_maint_world()
    store = _CupStore([_cup_preview()])
    store.put(L.STATE, {"nt_core_teams": {"ids": [10, 20]}, "maint_last_day": {"day": "2026-11-10"}})
    writes = store.writes
    client = _Client(stats=stats, boxes=boxes, days=days)
    out = _run("maint", client, store, dry_run=True, max_requests=3000)
    assert out["ok"] and store.writes == writes and out["would_fetch"] == 5
    assert not any("/statistics/" in u for u in client.calls)
    assert out["cup_grade"]["counts"] == {"esperando estadísticas": 1} and out["cup_grade"]["would_write"] == 0


def test_maint_copas_fallo_de_graduacion_no_tumba_el_mantenimiento():
    days, stats, boxes = _cup_maint_world()

    class _Broken(_CupStore):
        def all_docs(self, coll):
            raise RuntimeError("firestore caído")

    store = _Broken([_cup_preview()])
    store.put(L.STATE, {"nt_core_teams": {"ids": [10, 20]}, "maint_last_day": {"day": "2026-11-10"}})
    out = _run("maint", _Client(stats=stats, boxes=boxes, days=days), store, max_requests=3000)
    assert out["ok"] and out["cup_grade"]["ok"] is False
    assert store.c[L.STATE]["maint_last_day"]["day"] == "2026-11-12"


# ── Copas ──────────────────────────────────────────────────────────────────────

def _cups_store(n_teams=120):
    store = _Store()
    lid = L.hp.HL_LEAGUES["SP1"]
    store.put(L.MATCHES, {f"9{i}": {"league_id": lid, "hl_season": 2026, "home_team_id": i,
                                    "away_team_id": i + 1} for i in range(1, n_teams, 2)})
    return store


def _cups_world():
    routes = {(122477, 2026): [_m(1, "Racing", "Sporting", 1, 2), _m(2, "Racing", "Aldeano", 1, 999),
                               _m(3, "Eibar", "Huesca", 3, 4, desc="Not started")]}
    return routes, {1: _stats(1, "Racing", 2, "Sporting")}


def test_cups_sin_equipos_de_liga_no_lista_nada():
    client = _Client()
    out = _run("cups", client, _cups_store(n_teams=10))
    assert not out["ok"] and "players" in out["errors"][0] and client.calls == []


def test_cups_guarda_solo_cruces_entre_equipos_de_liga_y_pide_lo_terminado():
    routes, stats = _cups_world()
    store, client = _cups_store(), _Client(routes=routes, stats=stats)
    out = _run("cups", client, store)
    assert out["ok"] and out["cup_teams"] == 120
    assert out["fetched"] == {"done": 1, "empty": 0, "error": 0}
    ts = store.c[L.TEAM_STATS]
    assert set(ts) == {"1", "3"} and ts["1"]["group"] == "cups" and ts["1"]["status"] == "done"
    assert ts["3"]["status"] == "pending"                                     # sin jugar
    st = store.c[L.STATE]
    assert len(st["cups_team_ids"]["ids"]) == 120
    assert st["cups_122477_2026"]["all_finished"] is False                   # quedan partidos por jugar
    assert st["cups_122477_2025"]["all_finished"] is True                    # edición pasada vacía: no se relista
    # Al día siguiente no se relistan las ediciones pasadas.
    client2 = _Client(routes=routes, stats=stats)
    _run("cups", client2, store, now=NOW + timedelta(days=1))
    assert all("season=2026" in u for u in client2.calls if "/matches?" in u)


def test_cups_dry_run_no_escribe():
    routes, stats = _cups_world()
    store = _cups_store()
    writes = store.writes
    out = _run("cups", _Client(routes=routes, stats=stats), store, dry_run=True)
    assert out["ok"] and store.writes == writes and out["would_fetch"] == 1


# ── Europa (CL, EL, ECL): sondeo y carga ───────────────────────────────────────

_UEFA_FAKE = {"CL": [(9001, 2026), (9001, 2025)], "EL": [(9002, 2026)], "ECL": [(9003, 2026)]}


def test_uefa_seasons_ids_del_sondeo():
    # Sondeo del 24-nov-2026: un id por competición; 2020/21 → actual, la ECL desde 2021/22
    assert L.uefa_missing() == []
    assert {c: {i for i, _ in v} for c, v in L.UEFA_SEASONS.items()} ==         {"CL": {2486}, "EL": {3337}, "ECL": {722432}}
    assert [y for _, y in L.UEFA_SEASONS["CL"]] == list(range(2020, 2027))
    assert [y for _, y in L.UEFA_SEASONS["ECL"]] == list(range(2021, 2027))
    comps = L._comps("uefa", None)
    assert comps[:3] == [(2486, 2026, True), (3337, 2026, True), (722432, 2026, True)]
    assert len(comps) == 7 + 7 + 6


def test_uefa_sin_ids_falla_claro_y_no_pide_nada(monkeypatch):
    monkeypatch.setattr(L, "UEFA_SEASONS", {"CL": [], "EL": [], "ECL": []})
    client = _Client()
    out = _run("uefa", client, _Store())
    assert not out["ok"] and client.calls == [] and out["requests"] == 0
    assert "UEFA_SEASONS" in out["errors"][0] and "CL/EL/ECL" in out["errors"][0]
    monkeypatch.setattr(L, "UEFA_SEASONS", {"CL": [(1, 2026)], "EL": [], "ECL": [(3, 2026)]})
    assert L.uefa_missing() == ["EL"]


def test_uefa_comps_orden_reciente_primero(monkeypatch):
    monkeypatch.setattr(L, "UEFA_SEASONS", _UEFA_FAKE)
    assert [(l, y) for l, y, _ in L._comps("uefa", None)] == [(9001, 2026), (9002, 2026), (9003, 2026), (9001, 2025)]
    assert L.uefa_comp_of(9002) == "EL" and L.uefa_comp_of(122477) is None


def _uefa_world():
    m1 = _m(1, "Aland FC", "Qarabeta", 10, 99, date="2025-10-01")            # equipo de liga sin datos: dentro
    m1["round"] = "League phase - 2"
    routes = {(9001, 2025): [m1, _m(2, "Bravia", "Zeta", 20, 98, date="2025-07-22")],   # previa: dentro
              (9002, 2026): [_m(3, "Cetia", "Dosia", 30, 31, date="2026-11-27", desc="Not started", score=None)]}
    stats = {1: _stats(10, "Aland FC", 99, "Qarabeta"), 2: _stats(20, "Bravia", 98, "Zeta")}
    return routes, stats


def test_uefa_guarda_todo_sin_filtrar_equipos_y_estado_por_clave_uefa(monkeypatch):
    monkeypatch.setattr(L, "UEFA_SEASONS", _UEFA_FAKE)
    routes, stats = _uefa_world()
    store = _Store()
    out = _run("uefa", _Client(routes, stats), store)
    assert out["ok"] and out["fetched"] == {"done": 2, "empty": 0, "error": 0}
    ts = store.c[L.TEAM_STATS]
    assert set(ts) == {"1", "2", "3"} and {d["group"] for d in ts.values()} == {"uefa"}
    assert ts["1"]["comp"] == "CL" and ts["1"]["round"] == "League phase - 2" and ts["2"]["round"] is None
    assert ts["3"]["comp"] == "EL" and ts["3"]["status"] == "pending"
    st = store.c[L.STATE]
    assert st["uefa_9001_2025"]["all_finished"] is True
    assert st["uefa_9003_2026"]["all_finished"] is False          # temporada en curso vacía: se relista
    assert st["uefa_9002_2026"]["all_finished"] is False


def test_uefa_dry_run_no_escribe_ni_pide_detalle(monkeypatch):
    monkeypatch.setattr(L, "UEFA_SEASONS", _UEFA_FAKE)
    routes, stats = _uefa_world()
    store, client = _Store(), _Client(routes, stats)
    out = _run("uefa", client, store, dry_run=True)
    assert out["ok"] and store.writes == 0 and out["would_fetch"] == 2
    assert not any("/statistics/" in u for u in client.calls)


class _ProbeClient(_Client):
    """Como _Client, más /leagues?leagueName=… (ligas inventadas)."""

    LEAGUES = {"UEFA Champions League": [{"id": 9001, "name": "UEFA Champions League", "country": {"name": "World"},
                                     "seasons": [{"season": 2024}, {"season": 2025}]},
                                    {"id": 9101, "name": "UEFA Champions League Qualification"},
                                    {"id": 9201, "name": "UEFA Champions League Women"}],
               "UEFA Europa League": [{"id": 9002, "name": "UEFA Europa League", "seasons": [2025]}],
               "Champions League": [], "Europa League": [], "Conference League": []}

    async def get(self, url, headers=None):
        m = re.search(r"/leagues\?leagueName=([^&]+)", url)
        if m:
            from urllib.parse import unquote
            self.calls.append(url)
            return _Resp(200, {"data": self.LEAGUES.get(unquote(m.group(1)), [])})
        return await super().get(url, headers)


def test_uefa_probe_como_mucho_9_y_solo_nombres_de_campo():
    lst = [_m(100 + i, "Aland", "Bravia", 10, 20, date="2025-07-15" if i < 150 else "2025-10-01") for i in range(300)]
    client = _ProbeClient({(9001, 2025): lst, (9002, 2025): lst[150:]},
                          stats={100: _stats(10, "Aland", 20, "Bravia")})
    out = _run("uefa_probe", client, _Store())
    assert out["ok"] and len(client.calls) <= 9 and out["requests"] == len(client.calls)
    cl, el, ecl = out["probe"]
    assert cl["main_id"] == 9001 and cl["qualifying_ids"] == [9101]
    assert [c["id"] for c in cl["candidates"]] == [9001, 9101]                 # sin la femenina
    assert cl["candidates"][0]["seasons"] == [2024, 2025] and cl["candidates"][0]["country"] == "World"
    assert cl["matches_2025"] == 300 and cl["previas_mismo_id_probable"] is True
    assert cl["jul_ago_in_page"] == 100 and cl["finished_in_page"] == 100
    assert "Corners" in cl["stats"]["field_names"] and cl["stats"]["complete"]
    assert all(isinstance(v, str) for v in cl["stats"]["field_names"])        # nombres, no valores
    assert el["matches_2025"] == 150 and el["previas_mismo_id_probable"] is False
    assert el["stats"] is not None and "field_names" in el["stats"]
    assert ecl["main_id"] is None and "matches_2025" not in ecl
    # ECL: prueba los dos nombres exactos y no encuentra nada
    assert sum("leagueName=UEFA%20Europa%20Conference" in u or "leagueName=UEFA%20Conference" in u
               for u in client.calls) == 2


def test_uefa_probe_ids_primero_si_el_presupuesto_es_corto():
    client = _ProbeClient({(9001, 2025): [], (9002, 2025): []})
    out = _run("uefa_probe", client, _Store(), max_requests=4)
    assert out["requests"] == 4 and all("/leagues?" in u for u in client.calls)
    assert out["probe"][0]["main_id"] == 9001 and out["probe"][1]["main_id"] == 9002


# ── Mantenimiento: Europa (fichas mp-e1) ─────────────────────────────────────────

class _UefaStore(_CupStore):
    def __init__(self, uefa=()):
        super().__init__()
        self.uefa = list(uefa)

    def uefa_previews(self, lo, hi):
        return [p for p in self.uefa if lo <= p["kickoff_utc"] <= hi]


def _uefa_preview(hl_id, comp, lid, day="2026-12-15"):
    blk = {"market": "corners_team", "label": "medido en backtest", "highlight": False,
           "lines": {"over_4_5": {"line": 4.5, "p_model": 0.55, "p_league": 0.5}}}
    return {"match_id": f"{comp}_{day}_{hl_id}", "div": comp, "comp": comp, "league_id": lid, "season": "2627",
            "date": day, "kickoff_utc": f"{day}T20:00:00+00:00", "home_team": "Aland FC", "away_team": "Qarabeta",
            "hl_match_id": hl_id, "hash": "h", "computed_at": f"{day}T02:00:00+00:00", "data_cutoff": "2026-12-10",
            "low_sample": False, "stats": {"corners": {"available": True, "lines": {"home": blk}}}}


AFTER_PRO = datetime(2026, 12, 16, 12, 0, tzinfo=timezone.utc)


def _uefa_maint(monkeypatch, cap=20):
    monkeypatch.setattr(L, "UEFA_SEASONS", _UEFA_FAKE)
    monkeypatch.setattr(L, "HL_UEFA_MAINT_DAILY", cap)
    day = "2026-12-15"
    days = {(9001, day): [_sm(60 + i, "Aland FC", "Qarabeta", 10, 99, day, 2026) for i in range(3)],
            (9003, day): [_sm(70, "Bravia", "Zeta", 20, 98, day, 2026)]}
    stats = {60 + i: _stats(10, "Aland FC", 99, "Qarabeta", corners=(6, 2)) for i in range(3)}
    stats[70] = _stats(20, "Bravia", 98, "Zeta")
    store = _UefaStore([_uefa_preview(60 + i, "CL", 9001) for i in range(3)] + [_uefa_preview(70, "ECL", 9003)])
    store.put(L.STATE, {"nt_core_teams": {"ids": [10, 20]}, "maint_last_day": {"day": "2026-12-14"}})
    return store, _Client(stats=stats, days=days)


def test_maint_europa_tras_el_pro_solo_cl_y_el_con_ficha_y_gradua(monkeypatch):
    store, client = _uefa_maint(monkeypatch)
    out = _run("maint", client, store, max_requests=3000, now=AFTER_PRO)
    assert out["ok"] and out["uefa_budget"] == 20
    assert sum("leagueId=9001&date=2026-12-15" in u for u in client.calls) == 1
    assert not any("leagueId=9003" in u for u in client.calls)                 # la Conference no
    assert {u.rsplit("/", 1)[1] for u in client.calls if "/statistics/" in u} == {"60", "61", "62"}
    assert store.c[L.TEAM_STATS]["60"]["group"] == "uefa" and store.c[L.TEAM_STATS]["60"]["comp"] == "CL"
    assert store.c[L.STATE]["maint_2026-12-16"]["uefa_used"] == 4
    g = out["uefa_grade"]
    assert g["counts"] == {"graduada": 3, "esperando estadísticas": 1}           # la de la Conference
    assert store.c["match_preview_grades_uefa"]["CL_2026-12-15_60"]["status"] == "graduada"


def test_maint_europa_tope_propio_por_fecha_tras_el_pro(monkeypatch):
    store, client = _uefa_maint(monkeypatch, cap=2)
    out = _run("maint", client, store, max_requests=3000, now=AFTER_PRO)
    assert out["ok"] and sum(1 for u in client.calls if "leagueId=9001" in u or "/statistics/6" in u) == 2
    assert store.c[L.STATE]["maint_2026-12-16"]["uefa_used"] == 2
    assert sum(1 for it in store.c[L.STATE]["maint_backlog"]["items"] if it["job"] == "uefa") == 2
    # Otra pasada el mismo día: el tope de Europa ya está gastado; lo demás sigue.
    client2 = _Client(stats=client.stats, days=client.days)
    out2 = _run("maint", client2, store, max_requests=3000, now=AFTER_PRO)
    assert out2["uefa_budget"] == 0 and not any("/statistics/6" in u for u in client2.calls)


def test_maint_europa_con_pro_las_tres_y_sin_tope_propio(monkeypatch):
    assert L.uefa_maint_cap(datetime(2026, 12, 12).date()) is None
    assert L.uefa_maint_cap(datetime(2026, 12, 13).date()) == L.HL_UEFA_MAINT_DAILY
    prev = [_uefa_preview(1, "CL", 9001, "2026-11-12"), _uefa_preview(2, "ECL", 9003, "2026-11-12")]
    assert L.uefa_targets(prev, ["2026-11-12"], NOW.date()) == {"2026-11-12": {9001: {1}, 9003: {2}}}
    assert L.uefa_targets(prev, ["2026-11-12"], AFTER_PRO.date()) == {"2026-11-12": {9001: {1}}}
