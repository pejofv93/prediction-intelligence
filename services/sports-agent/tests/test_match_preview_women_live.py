"""mp-w1 paso 4: lista de partidos, fichas "solo referencia", graduación y exportación web."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from analyzers import match_preview as mp
from analyzers import match_preview_store as store
from analyzers import match_preview_women_live as wl
from analyzers import match_preview_women_store as ws
from tests.test_match_preview_women_store import _docs

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def _raw(mid, ko, h, a, finished=False):
    return {"id": mid, "date": ko, "league": {"name": "x"},
            "homeTeam": {"id": h, "name": f"Local {h}"}, "awayTeam": {"id": a, "name": f"Visitante {a}"},
            "state": {"description": "Finished" if finished else "Not started", "score": {"current": None}}}


def test_lista_desde_listado_filtra_terminados_y_ventana():
    listing = [_raw(1, "2026-11-21T18:00:00Z", 12100, 12101),
               _raw(2, "2026-11-19T18:00:00Z", 12102, 12103),                 # ya empezó
               _raw(3, "2026-11-22T18:00:00Z", 12104, 12105, finished=True),
               _raw(4, "2026-12-05T18:00:00Z", 12106, 12107)]                 # más de 7 días
    fx = wl.fixtures_from_listing(listing, 121626, 2026, NOW)
    assert [f["hl_match_id"] for f in fx] == [1]
    f = fx[0]
    assert f["match_id"] == "WLF_2026-11-21_1" and f["home_id"] == "12100" and f["div"] == "WLF"
    assert wl.current_hl_season(NOW) == 2026 and wl.current_hl_season(datetime(2027, 3, 1)) == 2026


def test_tope_diario_y_cabecera():
    from datetime import date
    assert wl.women_fixtures_cap(date(2026, 11, 15)) == 30
    assert wl.women_fixtures_cap(date(2026, 12, 12)) == 30          # último día PRO, incluido
    assert wl.women_fixtures_cap(date(2026, 12, 13)) == 12
    assert wl.women_fixtures_budget(0, None, cap=12) == 12
    assert wl.women_fixtures_budget(10, None, cap=12) == 2
    assert wl.women_fixtures_budget(0, 5, cap=30) == 5
    assert wl.women_fixtures_budget(20, 50, cap=12) == 0
    assert wl.women_fixtures_budget(9, None, cap=30) == 21


def test_dry_run_guarda_el_listado_pero_no_las_fichas(monkeypatch):
    docs = [d for d in _docs() if d["season"] < 2026 or d["date"] < "2026-09-01T030"]
    bt = _backtest(docs)
    fx = _fixture(docs)

    async def fake_list(now, api_key, quota, state, client=None):
        return {"fixtures": [fx], "listed": ["WLF"], "from_state": [], "not_listed": [], "hl_requests": 3,
                "errors": [], "by_div_state": {"WLF": {"listed_at": now.isoformat(), "fixtures": [fx]}}}

    writes = []
    monkeypatch.setattr(wl, "list_women_fixtures", fake_list)
    monkeypatch.setattr(wl, "_set_docs", lambda c, d: writes.append((c, d)))
    s = asyncio.run(wl.run_women_previews(dry_run=True, now=NOW, docs=docs, backtest=bt, existing={}, state={}, live={}))
    assert s["would_write"] == 1 and s["state_saved"]
    assert [c for c, _ in writes] == ["match_previews_state"]          # ninguna ficha
    assert writes[0][1]["women"]["by_div"]["WLF"]["fixtures"] == [fx]

    async def from_state(now, api_key, quota, state, client=None):   # sin listar nada nuevo
        return {"fixtures": [fx], "listed": [], "from_state": ["WLF"], "not_listed": [], "hl_requests": 0,
                "errors": [], "by_div_state": state.get("by_div", {})}

    writes.clear()
    monkeypatch.setattr(wl, "list_women_fixtures", from_state)
    s = asyncio.run(wl.run_women_previews(dry_run=True, now=NOW, docs=docs, backtest=bt, existing={}, state={}, live={}))
    assert not s["state_saved"] and writes == []


def test_sin_clave_usa_lo_guardado_y_no_relista_lo_reciente():
    fresh = (NOW - timedelta(hours=3)).isoformat()
    old = (NOW - timedelta(hours=30)).isoformat()
    fx = {"match_id": "WLF_2026-11-21_1", "kickoff_utc": "2026-11-21T18:00:00+00:00"}
    gone = {"match_id": "WSL_2026-11-19_9", "kickoff_utc": "2026-11-19T18:00:00+00:00"}
    state = {"by_div": {"WLF": {"listed_at": fresh, "fixtures": [fx]},
                        "WSL": {"listed_at": old, "fixtures": [gone]}}}
    out = asyncio.run(wl.list_women_fixtures(NOW, None, None, state))
    assert out["from_state"] == ["WLF"] and "WSL" in out["not_listed"] and out["hl_requests"] == 0
    assert out["fixtures"] == [fx]                                    # lo ya empezado no sale


def _fixture(docs):
    """Un partido futuro entre dos equipos de Liga F con historial en los docs sintéticos."""
    return {"match_id": "WLF_2026-11-21_999", "div": "WLF", "league": "Liga F", "date": "2026-11-21",
            "kickoff_utc": "2026-11-21T18:00:00+00:00", "home_team": "Local", "away_team": "Visitante",
            "home_id": str(121626 * 100), "away_id": str(121626 * 100 + 1), "hl_match_id": 999, "source": "highlightly"}


def _backtest(docs):
    calls = []
    import analyzers.match_preview_women_store as m
    orig = m._set_docs
    m._set_docs = lambda c, d: calls.append(d)
    try:
        asyncio.run(ws.run_women_backtest(dry_run=False, docs=docs))
    finally:
        m._set_docs = orig
    return calls[0]["mp-w1_backtest"]


def test_fichas_solo_referencia_y_resumen_sin_nombres(monkeypatch):
    docs = [d for d in _docs() if d["season"] < 2026 or d["date"] < "2026-09-01T030"]
    bt = _backtest(docs)
    writes = []
    monkeypatch.setattr(wl, "_set_docs", lambda c, d: writes.append((c, d)))
    s = asyncio.run(wl.run_women_previews(dry_run=False, now=NOW, fixtures=[_fixture(docs)], docs=docs,
                                          backtest=bt, existing={}, live={}))
    assert s["ok"] and s["counts"] == {"nueva": 1} and s["written"] == 1
    coll, written = writes[0]
    assert coll == "match_previews_women"
    p = written["WLF_2026-11-21_999"]
    assert p["model_version"] == "mp-w1" and p["group"] == "women" and p["season"] == "2627"
    assert p["teams"]["home"]["team"] == "Local"
    blocks = [b for st in p["stats"].values() if st.get("available") for b in st["lines"].values()]
    assert blocks and all(not b["highlight"] for b in blocks)
    assert all(b["label"] in (mp.LABEL_REF, mp.LABEL_LOW_SAMPLE) for b in blocks)
    # líneas del backtest femenino (goles con 3,5 por equipo), no las de mp-v1
    assert "over_3_5" in p["stats"]["goals"]["lines"]["home"]["lines"]
    txt = json.dumps(s, ensure_ascii=False)
    assert "Local" not in txt and "999" not in txt


def test_sin_backtest_no_hay_fichas():
    s = asyncio.run(wl.run_women_previews(dry_run=True, now=NOW, fixtures=[], docs=[], backtest={}))
    assert not s["ok"] and "mp-w1_backtest" in s["errors"][0]


def _preview(computed_at="2026-11-20T10:00:00+00:00", cutoff="2026-11-15"):
    return {"match_id": "WLF_2026-11-21_999", "div": "WLF", "season": "2627", "date": "2026-11-21",
            "kickoff_utc": "2026-11-21T18:00:00+00:00", "home_team": "Local", "away_team": "Visitante",
            "hl_match_id": 999, "model_version": "mp-w1", "computed_at": computed_at, "data_cutoff": cutoff,
            "hash": "h", "stats": {"corners": {"available": True, "lines": {
                "home": {"market": "corners_team", "label": mp.LABEL_REF, "highlight": False,
                         "lines": {"over_3_5": {"line": 3.5, "p_model": 0.6, "p_league": 0.5}}},
                "away": {"market": "corners_team", "label": mp.LABEL_REF, "highlight": False,
                         "lines": {"over_3_5": {"line": 3.5, "p_model": 0.4, "p_league": 0.5}}},
                "total": {"market": "corners_total", "label": mp.LABEL_REF, "highlight": False,
                          "lines": {"over_8_5": {"line": 8.5, "p_model": 0.5, "p_league": 0.5}}}}}}}


def _result(status="done"):
    side = lambda c: {"corners": c, "fouls": 10, "yellows": 1, "reds": 0, "shots_on": 4, "shots_off": 5,
                      "shots_blocked": 2}
    return {"group": "women", "league_id": 121626, "season": 2026, "date": "2026-11-21", "finished": True,
            "status": status, "home_team_id": 12160, "away_team_id": 12161, "goals_home": 2, "goals_away": 1,
            **({"stats": {"home": side(6), "away": side(2)}} if status == "done" else {})}


def test_graduacion_con_estadisticas_espera_y_sin_resultado(monkeypatch):
    writes = []
    monkeypatch.setattr(wl, "_set_docs", lambda c, d: writes.append((c, d)))
    later = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    s = asyncio.run(wl.run_women_grade(dry_run=False, now=later, previews=[_preview()],
                                       results={"999": _result()}, graded={}, all_grades=[]))
    assert s["counts"] == {"graduada": 1}
    g = writes[0][1]["WLF_2026-11-21_999"]
    assert {(ln["side"], ln["y"]) for ln in g["lines"]} == {("home", 1), ("away", 0), ("total", 0)}
    assert writes[1][0] == "match_preview_metrics" and "mp-w1_live" in writes[1][1]
    s = asyncio.run(wl.run_women_grade(dry_run=True, now=later, previews=[_preview()],
                                       results={"999": _result("pending")}, graded={}, all_grades=[]))
    assert s["counts"] == {"esperando estadísticas": 1}
    s = asyncio.run(wl.run_women_grade(dry_run=True, now=later + timedelta(days=8), previews=[_preview()],
                                       results={}, graded={}, all_grades=[]))
    assert s["counts"] == {"sin resultado": 1}
    s = asyncio.run(wl.run_women_grade(dry_run=True, now=later, graded={}, all_grades=[], results={},
                                       previews=[_preview(computed_at="2026-11-21T19:00:00+00:00")]))
    assert s["counts"] == {"anulada": 1}


def test_web_marca_el_grupo():
    p = {**_preview(), "teams": {}, "stats": {}}
    assert store.web_match(p)["group"] == "men"
    w = store.web_match({**p, "group": "women"})
    assert w["group"] == "women" and w["model_version"] == "mp-w1"


def test_estrella_femenino_solo_con_50_graduadas_en_directo_e_ic95_bajo_0():
    live = {
        "corners_team|ALL": {"market": "corners_team", "div": "ALL", "n_matches": 50, "ci_label": mp.LABEL_EDGE},
        "fouls_team|ALL": {"market": "fouls_team", "div": "ALL", "n_matches": 49, "ci_label": mp.LABEL_EDGE},
        "shots_team|ALL": {"market": "shots_team", "div": "ALL", "n_matches": 80, "ci_label": mp.LABEL_REF},
        "corners_team|WLF": {"market": "corners_team", "div": "WLF", "n_matches": 30, "ci_label": mp.LABEL_EDGE},
    }
    labels = wl.star_labels(live, {"WLF", "WSL"})
    assert labels == {("corners_team", "WLF"): mp.LABEL_EDGE, ("corners_team", "WSL"): mp.LABEL_EDGE}
    assert wl.star_labels({}, {"WLF"}) == {} and wl.star_labels(None, {"WLF"}) == {}
