"""match_preview_store (ficha de partido mp-v1, paso 3): congelado 2 h antes, recálculo solo si
cambia el hash, graduación contra el resultado, anulación y dry-run sin escribir. Sin red ni
Firestore: todo se inyecta."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from analyzers import match_preview as mp
from analyzers import match_preview_store as st
from tests.test_match_preview import _league, _m

NOW = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)


def _fixture(home="A", away="B", ko=NOW + timedelta(days=1), **kw):
    d = ko.date().isoformat()
    return {"match_id": f"SP1_{d}_{home.lower()}_{away.lower()}", "div": "SP1", "league": "PD", "date": d,
            "kickoff_utc": ko.isoformat(), "home_team": home, "away_team": away, "source": "fixtures_csv",
            "team_matched": True, "odds": None, **kw}


def _fixtures(*fx, **extra):
    return {"matches": list(fx), "team_mapping": [], "unmatched_teams": [], "hl_checked": [], **extra}


def _run(**kw):
    kw.setdefault("matches", _league(rounds=4))
    kw.setdefault("labels_doc", {"metrics": {}})
    kw.setdefault("existing", {})
    return asyncio.run(st.run_match_previews(dry_run=True, now=NOW, **kw))


def test_decide_nueva_igual_cambia_congelada_tarde():
    f = _fixture()
    c = st.preview_content(f, {"x": 1}, None)
    assert st.decide(c, None, NOW) == "nueva"
    same = {"hash": st.content_hash(c), "freeze_at": st.freeze_at(f["kickoff_utc"]).isoformat()}
    assert st.decide(c, same, NOW) == "igual"
    assert st.decide(st.preview_content(f, {"x": 2}, None), same, NOW) == "cambia"
    late = NOW + timedelta(days=1) - timedelta(minutes=90)            # 1 h 30 min antes del inicio
    assert st.decide(st.preview_content(f, {"x": 2}, None), same, late) == "congelada"
    assert st.decide(c, None, late) == "tarde"
    assert st.decide(c, None, NOW + timedelta(days=2)) == "empezado"


def test_hora_adelantada_congela_con_la_mas_temprana():
    f = _fixture(ko=NOW + timedelta(hours=3))                          # ahora empieza 3 h después
    c = st.preview_content(f, {"x": 1}, None)
    old = {"hash": "otro", "freeze_at": (NOW + timedelta(hours=10)).isoformat()}
    assert st.decide(c, old, NOW + timedelta(hours=1, minutes=30)) == "congelada"


def test_hash_ignora_marcas_de_tiempo():
    f = _fixture()
    c = st.preview_content(f, {"x": 1}, "mp-v1_backtest")
    assert st.content_hash(c) == st.content_hash(dict(c))
    assert "computed_at" not in c


def test_dry_run_calcula_y_no_escribe(monkeypatch):
    monkeypatch.setattr(st, "_set_docs", lambda *a, **k: pytest.fail("dry-run no escribe"))
    out = _run(fixtures=_fixtures(_fixture()))
    assert out["would_write"] == 1 and out["counts"] == {"nueva": 1}
    p = out["previews"][0]
    assert p["action"] == "nueva" and p["low_sample"] is False and "shots" in p["exp_total"]
    assert any("sin" in e for e in out["errors"]) is False


def test_sin_etiquetas_del_backtest_avisa_y_todo_es_referencia(monkeypatch):
    monkeypatch.setattr(st, "_get_doc", lambda *a: None)
    out = asyncio.run(st.run_match_previews(dry_run=True, now=NOW, fixtures=_fixtures(_fixture()),
                                            matches=_league(rounds=4), existing={}))
    assert any("solo referencia" in e for e in out["errors"])
    assert out["previews"][0]["highlighted"] == []


def test_equipo_sin_emparejar_sale_como_sin_ficha():
    f = _fixture(home="Zzz", team_matched=False, src_home="Zzz FC", src_away="B")
    out = _run(fixtures=_fixtures(f, unmatched_teams=["SP1: Zzz FC (highlightly)"]))
    assert out["counts"] == {"sin_ficha_nombre": 1} and out["would_write"] == 0
    assert out["unmatched_teams"] == ["SP1: Zzz FC (highlightly)"]


def test_escritura_real_guarda_ficha_y_estado(monkeypatch):
    written = {}
    monkeypatch.setattr(st, "_set_docs", lambda coll, docs: written.setdefault(coll, {}).update(docs))
    f = _fixture()
    out = asyncio.run(st.run_match_previews(
        dry_run=False, now=NOW, fixtures=_fixtures(f, hl_checked=[{"div": "SP2", "day": "2026-12-31"}]),
        matches=_league(rounds=4), labels_doc={"metrics": {}}, existing={}, state={}))
    assert out["written"] == 1
    doc = written[st.PREVIEWS][f["match_id"]]
    assert doc["freeze_at"] == (NOW + timedelta(days=1, hours=-2)).isoformat()
    assert doc["computed_at"] == NOW.isoformat() and doc["n_writes"] == 1 and doc["model_version"] == "mp-v1"
    assert written[st.STATE]["previews"]["hl_checked_at"] == {"SP2|2026-12-31": NOW.isoformat()}
    # segunda pasada con lo mismo → igual, no escribe
    written.clear()
    out = asyncio.run(st.run_match_previews(
        dry_run=False, now=NOW + timedelta(hours=1), fixtures=_fixtures(f), matches=_league(rounds=4),
        labels_doc={"metrics": {}}, existing={f["match_id"]: doc}, state={}))
    assert out["counts"] == {"igual": 1} and st.PREVIEWS not in written or not written[st.PREVIEWS]


def test_revision_ded_ppl_marca_metodo_y_huecos():
    fx = _fixtures(
        {**_fixture(home="Twente", away="Ajax"), "div": "N1"},
        team_mapping=[{"div": "N1", "source": "football_data_org", "name": "NEC", "fdco": "Nijmegen"},
                      {"div": "N1", "source": "football_data_org", "name": "AFC Ajax", "fdco": "Ajax"},
                      {"div": "N1", "source": "football_data_org", "name": "PSV", "fdco": "PSV Eindhoven"}],
        unmatched_teams=["N1: FC Twente '65 (football_data_org)", "SP2: X (highlightly)"])
    rv = st._mapping_review(fx)
    methods = {m["name"]: m["method"] for m in rv["N1"]["team_mapping"]}
    assert methods["NEC"] == "alias" and methods["AFC Ajax"] == "igual"
    assert methods["PSV"].startswith("contención")
    assert rv["N1"]["unmatched_teams"] == ["N1: FC Twente '65 (football_data_org)"]
    assert rv["N1"]["fdco_in_window_not_translated"] == ["Twente"]
    assert rv["P1"]["team_mapping"] == []


# ── Graduación ─────────────────────────────────────────────────────────────────

def _frozen_preview(computed_at=NOW, kickoff=NOW + timedelta(days=1)):
    hist = mp.History(_league(rounds=4))
    f = _fixture(ko=kickoff)
    pred = mp.predict_match(hist, "SP1", "2627", f["date"], "A", "B")
    return {**st.preview_content(f, pred, None), "computed_at": computed_at.isoformat(), "hash": "h"}


def test_graduacion_por_linea_y_metricas():
    p = _frozen_preview()
    res = _m("SP1", "2627", p["date"], "A", "B", {"shots": 15, "goals": 2}, {"shots": 9, "goals": 0})
    later = NOW + timedelta(days=2)
    out = asyncio.run(st.run_match_preview_grade(dry_run=True, now=later, previews=[p], matches=[res],
                                                 graded={}, all_grades=[]))
    assert out["counts"] == {"graduada": 1} and out["would_write"] == 1
    assert "shots_team|ALL" in out["live_metrics"]
    g = st.grade_preview(p, res)
    shots_home = [ln for ln in g["lines"] if ln["market"] == "shots_team" and ln["side"] == "home"]
    assert {ln["line"]: ln["y"] for ln in shots_home} == {8.5: 1, 10.5: 1, 12.5: 1, 14.5: 1}
    tot = [ln for ln in g["lines"] if ln["market"] == "goals_total"]
    assert {ln["line"]: ln["y"] for ln in tot} == {1.5: 1, 2.5: 0, 3.5: 0}
    ln = shots_home[0]
    assert ln["brier_model"] == pytest.approx((ln["p_model"] - 1) ** 2, abs=1e-5)


def test_resultado_a_un_dia_vale_y_a_dos_no():
    p = _frozen_preview()
    d1 = (datetime.fromisoformat(p["date"]) + timedelta(days=1)).date().isoformat()
    d2 = (datetime.fromisoformat(p["date"]) + timedelta(days=2)).date().isoformat()
    by = {("SP1", "A", "B"): [_m("SP1", "2627", d1, "A", "B")]}
    assert st.find_result(p, by) is not None
    assert st.find_result(p, {("SP1", "A", "B"): [_m("SP1", "2627", d2, "A", "B")]}) is None


def test_anulada_si_se_escribio_tras_el_inicio_y_sin_resultado_a_los_7_dias():
    late = _frozen_preview(computed_at=NOW + timedelta(days=1, minutes=5))
    assert st.validity(late) == "ficha escrita después del inicio"
    ok = _frozen_preview()
    assert st.validity(ok) is None
    out = asyncio.run(st.run_match_preview_grade(dry_run=True, now=NOW + timedelta(days=3), previews=[ok],
                                                 matches=[], graded={}, all_grades=[]))
    assert out["counts"] == {"esperando CSV": 1} and out["would_write"] == 0
    out = asyncio.run(st.run_match_preview_grade(dry_run=True, now=NOW + timedelta(days=9), previews=[ok],
                                                 matches=[], graded={}, all_grades=[]))
    assert out["counts"] == {"sin resultado": 1}


def test_rows_from_grades_formato_de_backtest():
    g = {"status": "graduada", "season": "2627", "div": "SP1", "match_id": "m",
         "lines": [{"market": "shots_team", "line": 10.5, "y": 1, "p_league": 0.5, "p_model": 0.7}]}
    assert st.rows_from_grades([g, {"status": "anulada"}]) == [("2627", "SP1", "shots_team", 10.5, 1, 0.5, 0.7, "m")]


def test_backtest_falla_entero_si_falta_un_csv(monkeypatch):
    async def fake_load(season, client=None):
        return [], ["SP1_2526"]
    monkeypatch.setattr(st, "load_matches", fake_load)
    out = asyncio.run(st.run_match_preview_backtest(dry_run=True, now=NOW))
    assert out["ok"] is False and out["seasons"] == ["2526", "2627"]


def test_backtest_dry_run_con_partidos():
    ms = _league(season="2526", rounds=4, strong="A")
    for m in ms:
        m["date"] = m["date"].replace("2026", "2025")
    out = asyncio.run(st.run_match_preview_backtest(dry_run=True, now=NOW, matches=ms))
    assert out["ok"] and out["seasons"] == ["2526", "2627"] and out["n_rows"] > 0 and out["written"] == 0


def test_highlightly_no_repite_lo_mirado_en_20_h_y_poda_a_48_h():
    state = {"hl_checked_at": {"SP2|2026-12-31": (NOW - timedelta(hours=10)).isoformat(),
                               "SP2|2026-12-30": (NOW - timedelta(hours=21)).isoformat(),
                               "SP2|2026-12-28": (NOW - timedelta(hours=50)).isoformat(),
                               "roto": "no es fecha"}}
    assert st.recent_checks(state, NOW) == frozenset({("SP2", "2026-12-31")})
    merged = st.merge_checks(state, [{"div": "SP2", "day": "2027-01-01"}], NOW)
    assert set(merged) == {"SP2|2026-12-31", "SP2|2026-12-30", "SP2|2027-01-01"}
    assert merged["SP2|2027-01-01"] == NOW.isoformat()


def test_hl_divs_limita_el_plan_de_highlightly(monkeypatch):
    from collectors import match_fixtures as mf
    async def no_text(client, url):
        return None
    async def no_teams(client, div, season):
        return set()
    monkeypatch.setattr(mf, "_get_text", no_text)
    monkeypatch.setattr(mf, "_fdco_teams", no_teams)
    out = asyncio.run(mf.build_fixture_list(now=NOW, days=2, api_key=None, upcoming_docs=[],
                                            hl_divs=frozenset({"SP2"})))
    why = {(p["div"], p["why"]) for p in out["unchecked"]}
    assert ("SP2", "sin clave") in why and ("SP1", "fuera de hl_divs") in why
    assert not any(p["div"] != "SP2" and p["why"] == "sin clave" for p in out["unchecked"])


# ── Exportación para la web ──────────────────────────────────────────────────

def _stored(ko, home="A", away="B", **kw):
    f = _fixture(home, away, ko=ko, hl_match_id=123, src_home="Src A", odds={"over_2_5": 1.9}, **kw)
    pred = mp.predict_match(mp.History(_league(rounds=4)), "SP1", "2627", f["date"], home, away)
    return {**st.preview_content(f, pred, "mp-v1_backtest"), "hash": "h", "computed_at": NOW.isoformat()}


def test_web_export_ventana_orden_y_sin_datos_de_origen():
    docs = [_stored(NOW + timedelta(days=2), "C", "D"),
            _stored(NOW + timedelta(days=1)),
            _stored(NOW - timedelta(hours=1), "B", "A"),     # empezado hace 1 h: sigue
            _stored(NOW - timedelta(hours=3), "D", "C"),     # hace 3 h: fuera
            _stored(NOW + timedelta(days=8), "A", "C")]      # más de 7 días: fuera
    out = st.web_export(docs, NOW)
    assert [m["home"] for m in out["matches"]] == ["B", "A", "C"]
    flat = json.dumps(out)
    for leak in ("hl_match_id", "src_home", "Src A", "odds", "p_market", "hash", "params"):
        assert leak not in flat


def test_web_match_esperados_lineas_y_muestra():
    m = st.web_match(_stored(NOW + timedelta(days=1)))
    g = m["stats"]["goals"]
    assert g["available"] and len(g["exp"]) == 3
    assert abs(g["exp"][2] - (g["exp"][0] + g["exp"][1])) < 0.01
    assert set(g["blocks"]) == {"home", "away", "total"}
    lines = g["blocks"]["total"]["lines"]
    assert [ln[0] for ln in lines] == list(mp.LINES["goals"][1])          # ordenadas
    assert all(0 <= ln[1] <= 1 for ln in lines)                           # p_model
    assert g["blocks"]["home"]["label"] in (mp.LABEL_EDGE, mp.LABEL_REF, mp.LABEL_LOW_SAMPLE)
    assert set(m["sample"]) == {"home", "away"} and m["sample"]["home"]["n_cur"] >= 1
