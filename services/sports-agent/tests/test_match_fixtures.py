"""match_fixtures (ficha de partido mp-v1, paso 1): fixtures.csv, qué se pide a Highlightly,
presupuesto de la reserva manual, emparejado de nombres y fusión. Datos INVENTADOS con la
estructura real de las fuentes (no se guardan respuestas reales en el repo). Sin red ni Firestore."""
import asyncio
from datetime import datetime, timezone

from collectors import match_fixtures as mf

HEADER = "Div,Date,Time,HomeTeam,AwayTeam,Referee,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5\n"


def _csv(rows):
    return "﻿" + HEADER + "".join(",".join(r) + "\n" for r in rows)


NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)   # viernes


# ── Hora de Londres ──────────────────────────────────────────────────────────

def test_hora_londres_invierno_y_verano():
    assert mf.uk_to_utc("2026-11-14", "15:00") == datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    assert mf.uk_to_utc("2026-08-22", "15:00") == datetime(2026, 8, 22, 14, 0, tzinfo=timezone.utc)
    # cambio de hora 2026: 29-mar y 25-oct
    assert mf.uk_to_utc("2026-10-24", "20:00").hour == 19
    assert mf.uk_to_utc("2026-10-25", "20:00").hour == 20
    assert mf.uk_to_utc("2026-14-01", "15:00") is None
    assert mf.uk_date(datetime(2026, 8, 22, 23, 30, tzinfo=timezone.utc)) == "2026-08-23"


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


# ── Qué pedir a Highlightly ──────────────────────────────────────────────────

def _rows(div, n, day="2026-11-14"):
    return [{"div": div, "kickoff_utc": f"{day}T15:00:00+00:00"} for _ in range(n)]


def test_plan_liga_completa_en_su_fecha_no_se_pide():
    days = mf.window_days(NOW, 3)
    assert days == ["2026-11-13", "2026-11-14", "2026-11-15"]
    csv_by_div = {d: _rows(d, mf.ROUND_SIZE[d]) for d in mf.MAIN_DIVS}
    plan = mf.plan_hl_checks(csv_by_div, days)
    # solo los días fuera de lo que cubre el CSV (13 y 15), nunca el 14
    assert {p["day"] for p in plan} == {"2026-11-13", "2026-11-15"}
    assert all(p["reason"] == "fuera_del_csv" for p in plan)


def test_plan_sp2_incompleta_va_primero_en_cada_dia():
    days = ["2026-11-14"]
    csv_by_div = {d: _rows(d, mf.ROUND_SIZE[d]) for d in mf.MAIN_DIVS}
    csv_by_div["SP2"] = _rows("SP2", 8)
    del csv_by_div["N1"]
    plan = mf.plan_hl_checks(csv_by_div, days)
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
FDCO_SP2 = {"Santander", "Cordoba", "La Coruna", "Huesca"}


def test_parse_listado_highlightly():
    out = mf.parse_hl_listing(HL_RAW, "SP2")
    assert [m["hl_match_id"] for m in out] == [501, 502]
    assert out[0]["kickoff"] == datetime(2026, 11, 14, 17, 30, tzinfo=timezone.utc)


def test_map_team_a_nombres_fdco():
    assert mf.map_team("Racing Santander", FDCO_SP2) == ("Santander", True)
    assert mf.map_team("Córdoba CF", FDCO_SP2) == ("Cordoba", True)
    assert mf.map_team("Equipo Nuevo", FDCO_SP2) == ("Equipo Nuevo", False)


def test_fusion_csv_manda_y_highlightly_completa():
    csv_m = mf.parse_fixtures_csv(_csv([
        ["SP2", "14/11/2026", "18:30", "Santander", "Cordoba", "", "1.9", "3.4", "4.2", "2.1", "1.7"]]))
    hl = mf.parse_hl_listing(HL_RAW, "SP2")
    merged, unmatched = mf.merge_fixtures(csv_m, hl, {"SP2": FDCO_SP2})
    assert len(merged) == 2                                  # el 501 es el mismo partido que el CSV
    second, first = merged                                   # ordenados por hora: el de las 11:00 va antes
    assert first["source"] == "fixtures_csv" and first["hl_match_id"] == 501
    assert first["odds"]["over_2_5"] == 2.1
    # el CSV decía 18:30 de Londres; Highlightly, 17:30 UTC → manda Highlightly y se marca
    assert first["kickoff_utc"] == "2026-11-14T17:30:00+00:00"
    assert first["kickoff_csv_utc"] == "2026-11-14T18:30:00+00:00"
    assert second["source"] == "highlightly"
    assert second["match_id"] == "SP2_2026-11-14_la_coruna_equipo_nuevo"
    assert second["team_matched"] is False                   # sigue en la lista, sin datos
    assert unmatched == ["SP2: Equipo Nuevo"]


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
    responde a cualquier liga y día; el CSV de temporada de SP2 da los nombres."""
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
            return _Resp(200, "HomeTeam,AwayTeam\nSantander,Cordoba\nLa Coruna,Huesca\n")
        if "leagueId=120775&date=2026-11-14" in url:
            return _Resp(200, payload={"data": [m for m in HL_RAW["data"] if m["date"].startswith("2026-11-14")]})
        return _Resp(200, payload={"data": []})


def test_build_sin_clave_no_llama_a_highlightly_y_lo_dice(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(mf.httpx, "AsyncClient", _FakeClient)
    out = asyncio.run(mf.build_fixture_list(now=NOW, days=3))
    assert _FakeClient.calls == [mf.FIXTURES_URL]
    assert [m["match_id"] for m in out["matches"]] == ["SP2_2026-11-14_santander_cordoba"]
    assert len(out["unchecked"]) == 24 and {u["why"] for u in out["unchecked"]} == {"sin clave"}


def test_build_con_tope_respeta_prioridad_y_completa_sp2(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(mf.httpx, "AsyncClient", _FakeClient)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(mf.asyncio, "sleep", lambda *_: real_sleep(0))
    out = asyncio.run(mf.build_fixture_list(now=NOW, days=3, api_key="k", max_hl_requests=10))
    assert out["hl_requests"] == 10
    hl_calls = [c for c in _FakeClient.calls if "highlightly" in c]
    # el día más cercano primero: el 13 entero (SP2 la primera), después el 14 con SP2 delante
    assert "leagueId=120775&date=2026-11-13" in hl_calls[0]
    assert "leagueId=120775&date=2026-11-14" in hl_calls[8]
    assert "leagueId=119924&date=2026-11-14" in hl_calls[9]
    ids = [m["match_id"] for m in out["matches"]]
    assert ids == ["SP2_2026-11-14_la_coruna_equipo_nuevo", "SP2_2026-11-14_santander_cordoba"]
    assert out["by_div"]["SP2"] == {"total": 2, "fixtures_csv": 1, "highlightly": 1}
    assert len(out["unchecked"]) == 14 and all(u["why"] == "sin presupuesto" for u in out["unchecked"])
    assert {"div": "SP2", "day": "2026-11-15", "reason": "csv_incompleto", "why": "sin presupuesto"} in out["unchecked"]
    assert out["unmatched_teams"] == ["SP2: Equipo Nuevo"]
