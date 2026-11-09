"""match_fixtures (ficha de partido mp-v1, paso 1): fixtures.csv, upcoming_matches (solo
lectura), qué se pide a Highlightly, presupuesto de la reserva manual, emparejado estricto de
nombres y fusión. Datos INVENTADOS con la estructura real de las fuentes (no se guardan
respuestas reales en el repo); los nombres de equipo son los públicos de cada fuente.
Sin red ni Firestore."""
import asyncio
from datetime import datetime, timezone

from collectors import match_fixtures as mf

HEADER = "Div,Date,Time,HomeTeam,AwayTeam,Referee,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5\n"


def _csv(rows):
    return "﻿" + HEADER + "".join(",".join(r) + "\n" for r in rows)


NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)   # viernes

FDCO = {
    "SP1": {"Ath Madrid", "Real Madrid", "Ath Bilbao", "Barcelona", "Espanol", "Sociedad", "Vallecano", "Betis"},
    "SP2": {"Santander", "Cordoba", "La Coruna", "Huesca", "Sp Gijon", "Sociedad B", "Valladolid"},
    "E0": {"Man City", "Man United", "Newcastle", "Hull", "Nott'm Forest"},
    "I1": {"Inter", "Milan", "Genoa"},
    "F1": {"Paris SG", "Paris FC", "Le Havre", "Le Mans", "Lyon", "Marseille"},
    "P1": {"Sp Lisbon", "Sp Braga", "Benfica", "Guimaraes"},
    "N1": {"PSV Eindhoven", "Feyenoord", "Sparta Rotterdam", "Nijmegen", "For Sittard"},
    "D1": {"FC Koln", "Dortmund", "M'gladbach", "Bayern Munich", "Hamburg"},
}


# ── Hora de Londres ──────────────────────────────────────────────────────────

def test_hora_londres_invierno_y_verano():
    assert mf.uk_to_utc("2026-11-14", "15:00") == datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    assert mf.uk_to_utc("2026-08-22", "15:00") == datetime(2026, 8, 22, 14, 0, tzinfo=timezone.utc)
    # cambio de hora 2026: 29-mar y 25-oct
    assert mf.uk_to_utc("2026-10-24", "20:00").hour == 19
    assert mf.uk_to_utc("2026-10-25", "20:00").hour == 20
    assert mf.uk_to_utc("2026-14-01", "15:00") is None
    assert mf.uk_date(datetime(2026, 8, 22, 23, 30, tzinfo=timezone.utc)) == "2026-08-23"


# ── Emparejado estricto de nombres ───────────────────────────────────────────

def test_map_team_trampas_que_la_subcadena_fallaba():
    cases = [
        ("I1", "FC Internazionale Milano", "Inter"),     # no Milan
        ("I1", "AC Milan", "Milan"),
        ("F1", "Paris Saint-Germain FC", "Paris SG"),    # no Paris FC
        ("F1", "Paris FC", "Paris FC"),                  # no Paris SG
        ("SP1", "RCD Espanyol de Barcelona", "Espanol"),  # no Barcelona
        ("SP1", "FC Barcelona", "Barcelona"),
        ("SP1", "Club Atlético de Madrid", "Ath Madrid"),  # no Real Madrid
        ("SP1", "Real Madrid CF", "Real Madrid"),
        ("SP1", "Athletic Club", "Ath Bilbao"),          # se queda vacío al normalizar
        ("SP1", "Real Sociedad de Fútbol", "Sociedad"),
        ("SP1", "Rayo Vallecano de Madrid", "Vallecano"),
        ("E0", "Manchester City FC", "Man City"),        # no Man United
        ("E0", "Manchester United FC", "Man United"),
        ("E0", "Hull City AFC", "Hull"),
        ("E0", "Nottingham Forest FC", "Nott'm Forest"),
        ("F1", "Le Havre AC", "Le Havre"),
        ("F1", "Olympique Lyonnais", "Lyon"),
        ("P1", "Sporting Clube de Portugal", "Sp Lisbon"),
        ("P1", "Vitória SC", "Guimaraes"),
        ("N1", "PSV", "PSV Eindhoven"),
        ("N1", "Feyenoord Rotterdam", "Feyenoord"),      # no Sparta Rotterdam
        ("N1", "NEC", "Nijmegen"),
        ("D1", "1. FC Köln", "FC Koln"),
        ("D1", "Borussia Dortmund", "Dortmund"),
        ("D1", "Borussia Mönchengladbach", "M'gladbach"),
        ("SP2", "Sporting Gijón", "Sp Gijon"),
        ("SP2", "Real Sociedad B", "Sociedad B"),
        ("SP2", "Racing Santander", "Santander"),
    ]
    for div, src, want in cases:
        assert mf.map_team(src, FDCO[div]) == (want, True), (div, src)


def test_map_team_ante_la_duda_no_empareja():
    assert mf.map_team("Equipo Nuevo", FDCO["SP2"]) == ("Equipo Nuevo", False)
    # dos candidatos por contención → ninguno
    assert mf.map_team("Real", {"Real Madrid", "Real Betis"}) == ("Real", False)
    # alias cuyo destino no es de esa liga → sin emparejar, no se fuerza
    assert mf.map_team("Manchester City FC", FDCO["SP1"]) == ("Manchester City FC", False)


# ── fixtures.csv ─────────────────────────────────────────────────────────────

def test_parse_fixtures_filtra_ligas_y_guarda_cuotas():
    text = _csv([
        ["SP2", "14/11/2026", "18:30", "Racing Santander", "Cordoba", "", "1.9", "3.4", "4.2", "2.1", "1.7"],
        ["E1", "14/11/2026", "15:00", "Leeds", "Hull", "", "2", "3", "4", "2", "1.8"],      # no es de las 8
        ["SP1", "15/11/2026", "", "Sevilla", "Betis", "", "", "", "", "", ""],              # sin hora
    ])
    out = mf.parse_fixtures_csv(text)
    assert len(out) == 1
    m = out[0]
    assert m["match_id"] == "SP2_2026-11-14_racing_santander_cordoba"
    assert m["kickoff_utc"] == "2026-11-14T18:30:00+00:00"
    assert m["odds"] == {"home": 1.9, "draw": 3.4, "away": 4.2, "over_2_5": 2.1, "under_2_5": 1.7}
    assert m["source"] == "fixtures_csv" and m["team_matched"]


# ── upcoming_matches ─────────────────────────────────────────────────────────

def _up(league, home, away, ko, status="TIMED", collected="2026-11-12T14:50:00+00:00", **kw):
    return {"match_id": f"fd_{home[:3]}", "league": league, "home_team": home, "away_team": away,
            "match_date": ko, "status": status, "sport": "football", "collected_at": collected, **kw}


def test_parse_upcoming_filtra_ligas_estado_y_deporte():
    docs = [
        _up("PD", "FC Barcelona", "Real Madrid CF", "2026-11-14T20:00:00Z"),
        _up("CL", "Arsenal FC", "PSV", "2026-11-14T20:00:00Z"),                     # no es de las 8
        _up("PL", "Hull City AFC", "Manchester City FC", "2026-11-14T15:00:00Z", status="FINISHED"),
        _up("SA", "AC Milan", "Genoa CFC", "2026-11-14 17:00"),                     # formato sin zona
        _up("DED", "PSV", "NEC", datetime(2026, 11, 15, 13, 30, tzinfo=timezone.utc)),
        _up("PD", "X", "Y", "2026-11-14T20:00:00Z", sport="basketball"),
    ]
    out = mf.parse_upcoming_docs(docs)
    assert [(o["div"], o["src_home"]) for o in out] == [("SP1", "FC Barcelona"), ("I1", "AC Milan"), ("N1", "PSV")]
    assert out[1]["kickoff"] == datetime(2026, 11, 14, 17, 0, tzinfo=timezone.utc)
    assert all(o["source"] == "football_data_org" for o in out)


def test_upcoming_fresca_o_caducada():
    fresh = [_up("PL", "A", "B", "2026-11-14T15:00:00Z", collected="2026-11-12T14:50:00+00:00")]
    assert mf.upcoming_fresh(fresh, NOW)                                         # 18 h
    stale = [_up("PL", "A", "B", "2026-11-14T15:00:00Z", collected="2026-11-11T14:50:00+00:00")]
    assert not mf.upcoming_fresh(stale, NOW)                                     # 42 h
    uefa = [_up("CL", "A", "B", "2026-11-14T15:00:00Z")]
    assert not mf.upcoming_fresh(uefa, NOW)                                      # solo cuentan las 7 ligas
    assert not mf.upcoming_fresh([], NOW)


# ── Qué pedir a Highlightly ──────────────────────────────────────────────────

def _rows(div, n, day="2026-11-14"):
    return [{"div": div, "kickoff_utc": f"{day}T15:00:00+00:00"} for _ in range(n)]


def test_plan_liga_completa_en_su_fecha_no_se_pide():
    days = mf.window_days(NOW, 3)
    assert days == ["2026-11-13", "2026-11-14", "2026-11-15"]
    csv_by_div = {d: _rows(d, mf.ROUND_SIZE[d]) for d in mf.MAIN_DIVS}
    plan = mf.plan_hl_checks(csv_by_div, days)
    assert {p["day"] for p in plan} == {"2026-11-13", "2026-11-15"}
    assert all(p["reason"] == "fuera_del_csv" for p in plan)


def test_plan_con_upcoming_al_dia_solo_queda_sp2():
    plan = mf.plan_hl_checks({}, mf.window_days(NOW, 3), covered_divs=mf.UPCOMING_DIVS)
    assert [(p["div"], p["day"]) for p in plan] == [("SP2", "2026-11-13"), ("SP2", "2026-11-14"), ("SP2", "2026-11-15")]


def test_plan_sp2_incompleta_va_primero_en_cada_dia():
    csv_by_div = {d: _rows(d, mf.ROUND_SIZE[d]) for d in mf.MAIN_DIVS}
    csv_by_div["SP2"] = _rows("SP2", 8)
    del csv_by_div["N1"]
    plan = mf.plan_hl_checks(csv_by_div, ["2026-11-14"])
    assert [(p["div"], p["reason"]) for p in plan] == [("SP2", "csv_incompleto"), ("N1", "csv_sin_liga")]


def test_plan_salta_lo_ya_comprobado():
    plan = mf.plan_hl_checks({}, ["2026-11-14"], already_checked=frozenset({("SP1", "2026-11-14")}))
    assert "SP1" not in {p["div"] for p in plan} and len(plan) == 7


def test_presupuesto_reserva_manual():
    assert mf.fixtures_budget(0, None) == 10
    assert mf.fixtures_budget(4, None) == 6
    assert mf.fixtures_budget(0, 3) == 3          # la API manda si le queda menos
    assert mf.fixtures_budget(12, 50) == 0


# ── Highlightly → partidos ───────────────────────────────────────────────────

HL_RAW = {"data": [
    {"id": 501, "date": "2026-11-14T17:30:00.000Z", "league": {"id": 120775},
     "homeTeam": {"id": 1, "name": "Racing Santander"}, "awayTeam": {"id": 2, "name": "Córdoba CF"},
     "state": {"description": "Not started"}},
    {"id": 502, "date": "2026-11-14T11:00:00.000Z", "league": {"id": 120775},
     "homeTeam": {"id": 3, "name": "Deportivo La Coruña"}, "awayTeam": {"id": 4, "name": "Equipo Nuevo"},
     "state": {"description": "Not started"}},
    {"id": 503, "date": "", "homeTeam": {"name": "X"}, "awayTeam": {"name": "Y"}},   # sin hora → fuera
]}


def test_parse_listado_highlightly():
    out = mf.parse_hl_listing(HL_RAW, "SP2")
    assert [m["hl_match_id"] for m in out] == [501, 502]
    assert out[0]["kickoff"] == datetime(2026, 11, 14, 17, 30, tzinfo=timezone.utc)
    assert out[0]["state"] == "Not started"


def test_fusion_csv_manda_y_las_demas_completan():
    csv_m = mf.parse_fixtures_csv(_csv([
        ["SP2", "14/11/2026", "18:30", "Santander", "Cordoba", "", "1.9", "3.4", "4.2", "2.1", "1.7"]]))
    hl = mf.parse_hl_listing(HL_RAW, "SP2")
    merged, unmatched, mapping = mf.merge_fixtures(csv_m, hl, {"SP2": FDCO["SP2"]})
    assert len(merged) == 2                                  # el 501 es el mismo partido que el CSV
    second, first = merged                                   # ordenados por hora: el de las 11:00 va antes
    assert first["source"] == "fixtures_csv" and first["hl_match_id"] == 501
    assert first["odds"]["over_2_5"] == 2.1
    # el CSV decía 18:30 de Londres; Highlightly, 17:30 UTC → manda la fuente en vivo y se marca
    assert first["kickoff_utc"] == "2026-11-14T17:30:00+00:00"
    assert first["kickoff_csv_utc"] == "2026-11-14T18:30:00+00:00"
    assert first["kickoff_source"] == "highlightly"
    assert second["source"] == "highlightly"
    assert second["match_id"] == "SP2_2026-11-14_la_coruna_equipo_nuevo"
    assert second["team_matched"] is False                   # sigue en la lista, sin datos
    assert unmatched == ["SP2: Equipo Nuevo (highlightly)"]
    assert {"div": "SP2", "source": "highlightly", "name": "Córdoba CF", "fdco": "Cordoba"} in mapping


def test_fusion_upcoming_antes_que_highlightly():
    up = mf.parse_upcoming_docs([_up("PD", "FC Barcelona", "Real Madrid CF", "2026-11-14T20:00:00Z")])
    hl = [{"div": "SP1", "kickoff": datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc), "src_home": "Barcelona",
           "src_away": "Real Madrid", "source": "highlightly", "hl_match_id": 9, "fd_match_id": None}]
    merged, unmatched, _ = mf.merge_fixtures([], up + hl, {"SP1": FDCO["SP1"]})
    assert len(merged) == 1 and not unmatched
    m = merged[0]
    assert m["source"] == "football_data_org" and m["hl_match_id"] == 9 and m["fd_match_id"] == "fd_FC "
    assert m["match_id"] == "SP1_2026-11-14_barcelona_real_madrid"


def test_ventana_quita_jugados_y_lejanos():
    days = mf.window_days(NOW, 3)
    mk = lambda ko: {"kickoff_utc": ko}
    assert not mf.in_window(mk("2026-11-13T08:00:00+00:00"), NOW, days)   # ya empezado
    assert mf.in_window(mk("2026-11-13T19:00:00+00:00"), NOW, days)
    assert mf.in_window(mk("2026-11-15T23:00:00+00:00"), NOW, days)
    assert not mf.in_window(mk("2026-11-16T12:00:00+00:00"), NOW, days)


# ── Ejecución completa con red simulada ──────────────────────────────────────

class _Resp:
    def __init__(self, status, text="", payload=None, headers=None):
        self.status_code, self.text, self._payload, self.headers = status, text, payload, headers or {}

    def json(self):
        return self._payload


class _FakeClient:
    """fixtures.csv con SP2 incompleta (1 partido) y el resto de ligas sin partidos; Highlightly
    solo trae algo para SP2 el día 14; los CSV de temporada dan los nombres de FDCO."""
    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeClient.calls.append(url)
        if url == mf.FIXTURES_URL:
            return _Resp(200, _csv([["SP2", "14/11/2026", "18:30", "Santander", "Cordoba", "", "1.9", "3.4", "4.2", "2.1", "1.7"]]))
        if "mmz4281" in url:
            div = url.rsplit("/", 1)[1].split(".")[0]
            return _Resp(200, "HomeTeam,AwayTeam\n" + "".join(f"{t},X\n" for t in FDCO.get(div, ())))
        if "leagueId=120775&date=2026-11-14" in url:
            return _Resp(200, payload={"data": [m for m in HL_RAW["data"] if m["date"].startswith("2026-11-14")]})
        return _Resp(200, payload={"data": []})


UPCOMING_OK = [
    _up("PD", "FC Barcelona", "Real Madrid CF", "2026-11-14T20:00:00Z"),
    _up("PL", "Manchester City FC", "Hull City AFC", "2026-11-15T16:30:00Z"),
    _up("PL", "Newcastle United FC", "Manchester United FC", "2026-11-17T20:00:00Z"),   # fuera de ventana
]


def _patch(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(mf.httpx, "AsyncClient", _FakeClient)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(mf.asyncio, "sleep", lambda *_: real_sleep(0))


def test_build_con_upcoming_al_dia_solo_pide_sp2(monkeypatch):
    _patch(monkeypatch)
    out = asyncio.run(mf.build_fixture_list(now=NOW, days=3, api_key="k", max_hl_requests=10,
                                            upcoming_docs=UPCOMING_OK))
    assert out["upcoming_fresh"] is True
    hl_calls = [c for c in _FakeClient.calls if "highlightly" in c]
    assert out["hl_requests"] == 3 and all("leagueId=120775" in c and "timezone=Etc/UTC" in c for c in hl_calls)
    ids = [m["match_id"] for m in out["matches"]]
    assert ids == ["SP2_2026-11-14_la_coruna_equipo_nuevo", "SP2_2026-11-14_santander_cordoba",
                   "SP1_2026-11-14_barcelona_real_madrid", "E0_2026-11-15_man_city_hull"]
    assert out["by_div"]["SP2"] == {"total": 2, "fixtures_csv": 1, "football_data_org": 0, "highlightly": 1}
    assert out["by_div"]["E0"]["football_data_org"] == 1
    assert out["unchecked"] == []
    assert out["unmatched_teams"] == ["SP2: Equipo Nuevo (highlightly)"]


def test_build_upcoming_caducada_vuelve_al_plan_completo(monkeypatch):
    _patch(monkeypatch)
    stale = [dict(d, collected_at="2026-11-10T14:50:00+00:00") for d in UPCOMING_OK]
    out = asyncio.run(mf.build_fixture_list(now=NOW, days=3, api_key="k", max_hl_requests=10,
                                            upcoming_docs=stale))
    assert out["upcoming_fresh"] is False
    assert any("sin refrescar" in e for e in out["errors"])
    hl_calls = [c for c in _FakeClient.calls if "highlightly" in c]
    # el día más cercano primero: el 13 entero (SP2 la primera), después el 14 con SP2 delante
    assert out["hl_requests"] == 10
    assert "leagueId=120775&date=2026-11-13" in hl_calls[0]
    assert "leagueId=120775&date=2026-11-14" in hl_calls[8]
    assert "leagueId=119924&date=2026-11-14" in hl_calls[9]
    assert len(out["unchecked"]) == 14 and all(u["why"] == "sin presupuesto" for u in out["unchecked"])
    assert {"div": "SP2", "day": "2026-11-15", "reason": "csv_incompleto", "why": "sin presupuesto"} in out["unchecked"]
    assert all(m["source"] != "football_data_org" for m in out["matches"])


def test_build_sin_clave_no_llama_a_highlightly_y_lo_dice(monkeypatch):
    _patch(monkeypatch)
    out = asyncio.run(mf.build_fixture_list(now=NOW, days=3, upcoming_docs=UPCOMING_OK))
    assert not any("highlightly" in c for c in _FakeClient.calls)
    assert [(u["div"], u["why"]) for u in out["unchecked"]] == [("SP2", "sin clave")] * 3
    assert len(out["matches"]) == 3
