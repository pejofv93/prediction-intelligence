"""Fichas de copa (mp-v1-cups): listado por (copa, día), emparejado de equipos y dry-run sin red."""
import asyncio
from datetime import date, datetime, timedelta, timezone

from analyzers import match_preview as mp
from analyzers import match_preview_cups as mc
from analyzers import match_preview_cups_live as ml
from collectors.highlightly_players import team_similarity

NOW = datetime(2026, 12, 15, 9, 0, tzinfo=timezone.utc)


def test_pendientes_y_poda_del_estado():
    st = {"listed": {f"122477|2026-12-14": "x", f"122477|2026-12-15": "x"},
          "fixtures": [{"date": "2026-12-14"}, {"date": "2026-12-16"}], "id_names": {"map": {"1": "A"}}}
    pr = ml.prune_state(st, NOW)
    assert list(pr["listed"]) == ["122477|2026-12-15"] and pr["fixtures"] == [{"date": "2026-12-16"}]
    assert pr["id_names"] == {"map": {"1": "A"}}
    pend = ml.pending_lists(pr, NOW)
    assert (122477, "2026-12-15") not in pend and (122477, "2026-12-17") in pend
    assert len(pend) == len(ml.LIVE_CUPS) * ml.WINDOW_DAYS - 1
    assert all(lid not in mc.SUPERCUPS for lid, _ in pend)


def test_emparejado_por_id_y_por_parecido():
    tdiv = {("Ath Bilbao", "2627"): "SP1", ("Betis", "2627"): "SP1", ("Leganes", "2627"): "SP2"}
    f = {"country": "ES", "cup": 122477, "date": "2026-12-16", "kickoff_utc": "2026-12-16T20:00:00+00:00",
         "hl_match_id": 9, "hl_home": "Athletic Club", "hl_home_id": 1, "hl_away": "Real Betis", "hl_away_id": 2}
    fx, why = ml.cup_fixture(f, tdiv, "2627", team_similarity, ids={"1": "Ath Bilbao"})
    assert fx["home_team"] == "Ath Bilbao" and fx["away_team"] == "Betis" and fx["div"] == "SP1"
    assert fx["match_id"] == "ES_2026-12-16_9" and fx["cup"] == "Copa del Rey"
    fx2, why2 = ml.cup_fixture({**f, "hl_away": "CD Leganes", "hl_away_id": 3}, tdiv, "2627", team_similarity,
                               ids={"1": "Ath Bilbao", "3": "Leganes"})
    assert fx2 is None and why2 == "distinta división"
    fx3, why3 = ml.cup_fixture({**f, "hl_home": "Athletic Club"}, tdiv, "2627", team_similarity, ids={})
    assert fx3 is None and "sin emparejar" in why3        # sin id y el nombre no se parece lo bastante


def _league(div="SP1", season="2627", teams=("Ath Bilbao", "Betis", "C", "D"), rounds=6, start=date(2026, 8, 15)):
    ms, d = [], start
    for _ in range(rounds):
        for i, h in enumerate(teams):
            a = teams[(i + 1) % len(teams)]
            ms.append({"div": div, "season": season, "date": d.isoformat(), "home": h, "away": a,
                       "h": {"goals": 2, "corners": 6, "cards": 2, "shots": 13, "sot": 5, "fouls": 11},
                       "a": {"goals": 1, "corners": 4, "cards": 2, "shots": 10, "sot": 3, "fouls": 12}})
            d += timedelta(days=1)
    return ms


class _FakeClient:
    async def aclose(self):
        pass


def test_dry_run_sin_red_no_escribe_fichas():
    state = {"listed": {f"{lid}|{d}": "x" for lid in ml.LIVE_CUPS for d in ml.window_days(NOW)},
             "fixtures": [{"hl_match_id": 9, "cup": 122477, "country": "ES", "date": "2026-12-16",
                           "kickoff_utc": "2026-12-16T20:00:00+00:00", "hl_home": "Athletic Club",
                           "hl_home_id": 1, "hl_away": "Real Betis", "hl_away_id": 2}],
             "id_names": {"updated_at": NOW.isoformat(), "map": {"1": "Ath Bilbao", "2": "Betis"}}}
    backtest = {"labels": {"corners_team": mc.LABEL_BACKTEST, "corners_total": mp.LABEL_REF}}
    out = asyncio.run(ml.run_cup_previews(dry_run=True, now=NOW, highlightly=False, state=state,
                                          matches=_league(), backtest=backtest, existing={}))
    assert out["ok"] and out["hl_requests"] == 0 and out["would_write"] == 1
    assert out["counts"] == {"nueva": 1} and out["by_cup"] == {"Copa del Rey": {"nueva": 1}}
    assert out["previews"] == [{"match_id": "ES_2026-12-16_9", "cup": "Copa del Rey"}]
    assert out["state_saved"] is False                    # dry-run sin listado nuevo: no guarda nada


def test_etiqueta_de_copa_sin_estrella():
    hist = mp.History(_league())
    p = mp.predict_match(hist, "SP1", "2627", "2026-12-16", "Ath Bilbao", "Betis",
                         labels=ml.labels_for({"labels": {"corners_team": mc.LABEL_BACKTEST}}, "SP1"))
    blk = p["stats"]["corners"]["lines"]["home"]
    assert blk["market_label"] == "medido en backtest" and blk["highlight"] is False


def test_sin_metricas_no_hay_fichas():
    out = asyncio.run(ml.run_cup_previews(dry_run=True, now=NOW, highlightly=False, state={}, matches=[],
                                          backtest={}))
    assert out["ok"] is False and "no se escriben fichas" in out["errors"][0]


# ── Graduación y estrellas ──────────────────────────────────────────────────────

KO = "2026-12-16T20:00:00+00:00"
NOW_G = datetime(2026, 12, 17, 9, 0, tzinfo=timezone.utc)
BT = {"labels": {"corners_team": mc.LABEL_BACKTEST, "corners_total": mp.LABEL_REF}}


def _preview(computed_at="2026-12-15T16:00:00+00:00", **kw):
    hist = mp.History(_league())
    fx = {"match_id": "ES_2026-12-16_9", "div": "SP1", "cup": "Copa del Rey", "cup_id": 122477, "country": "ES",
          "date": "2026-12-16", "kickoff_utc": KO, "home_team": "Ath Bilbao", "away_team": "Betis",
          "hl_match_id": 9, "source": "highlightly"}
    pred = mp.predict_match(hist, "SP1", "2627", fx["date"], fx["home_team"], fx["away_team"],
                            labels=ml.labels_for(BT, "SP1"))
    content = ml.cup_preview_content(fx, pred, ml.BACKTEST_DOC)
    return {**content, "season": "2627", "hash": "h", "computed_at": computed_at, **kw}


def _hl(**kw):
    d = {"match_id": 9, "group": "cups", "status": "done", "finished": True, "date": "2026-12-16",
         "goals_home": 2, "goals_away": 1,
         "stats": {"home": {"corners": 7, "yellows": 2, "shots_on": 6, "shots_off": 5, "shots_blocked": 2,
                            "fouls": 12},
                   "away": {"corners": 2, "yellows": None, "shots_on": 2, "shots_off": 3, "shots_blocked": 1,
                            "fouls": 10}}}
    d.update(kw)
    return d


class _Writer:
    def __init__(self):
        self.c = {}

    def __call__(self, coll, docs):
        self.c.setdefault(coll, {}).update(docs)


def _grade(dry_run=False, previews=None, results=None, all_grades=None, now=NOW_G, writer=None):
    return asyncio.run(ml.run_cup_grade(dry_run=dry_run, now=now, previews=previews or [_preview()],
                                        results={"9": _hl()} if results is None else results, graded={},
                                        all_grades=all_grades or [], backtest=BT, write=writer or _Writer()))


def test_graduacion_por_mercado_equipo_y_linea_sin_la_extra():
    w = _Writer()
    out = _grade(writer=w)
    assert out["ok"] and out["counts"] == {"graduada": 1} and out["written"] == 1
    assert out["by_cup"] == {"Copa del Rey": {"graduada": 1}} and out["n_graduated"] == 1
    g = w.c[ml.GRADES_C]["ES_2026-12-16_9"]
    assert g["status"] == "graduada" and g["group"] == "cups" and g["country"] == "ES"
    for ln in g["lines"]:
        team, total = mp.LINES[ln["stat"]]
        assert ln["line"] in (total if ln["side"] == "total" else team)      # nunca la línea "extra"
    assert {(ln["stat"], ln["side"]) for ln in g["lines"] if ln["stat"] == "cards"} == {("cards", "home")}
    c = next(ln for ln in g["lines"] if ln["stat"] == "corners" and ln["side"] == "home")
    assert c["value"] == 7 and c["y"] == int(7 > c["line"])
    live = w.c[ml.METRICS][ml.LIVE_DOC]
    assert live["n_graduated"] == 1 and live["n_by_market"]["corners_team"] == 1
    assert live["stars_after"] == 50 and live["star_markets"] == []
    assert live["labels"]["corners_team"] == mc.LABEL_BACKTEST


def test_anulada_sin_resultado_esperando_y_dry_run():
    w = _Writer()
    out = _grade(dry_run=True, writer=w)
    assert out["would_write"] == 1 and w.c == {}
    out = _grade(previews=[_preview(computed_at="2026-12-16T21:00:00+00:00")], writer=w)
    assert out["counts"] == {"anulada": 1}
    out = _grade(results={}, writer=_Writer())
    assert out["counts"] == {"esperando estadísticas": 1} and out["written"] == 0
    out = _grade(results={"9": _hl(status="pending")}, now=NOW_G + timedelta(days=8), writer=_Writer())
    assert out["counts"] == {"sin resultado": 1}
    # empezado hace menos de 6 h: todavía no se mira
    out = _grade(now=datetime(2026, 12, 16, 23, 0, tzinfo=timezone.utc), writer=_Writer())
    assert out["counts"] == {} and out["written"] == 0


def test_estrella_al_llegar_a_50_cruces_con_ic_bajo_cero():
    # 60 cruces ya graduados en los que el modelo acierta claramente más que la liga en corners_team
    old = [{"status": "graduada", "match_id": f"ES_{i}", "season": "2627", "country": "ES",
            "lines": [{"market": "corners_team", "line": 4.5, "y": i % 2, "p_league": 0.5,
                       "p_model": 0.8 if i % 2 else 0.2}]} for i in range(60)]
    w = _Writer()
    out = _grade(all_grades=old, writer=w)
    assert out["star_markets"] == ["corners_team"] and out["n_graduated"] == 61
    live = w.c[ml.METRICS][ml.LIVE_DOC]
    assert live["labels"]["corners_team"] == mp.LABEL_EDGE and live["labels"]["corners_total"] == mp.LABEL_REF
    # Y la ficha siguiente sale con estrella (highlight) en ese mercado.
    hist = mp.History(_league())
    p = mp.predict_match(hist, "SP1", "2627", "2026-12-20", "Ath Bilbao", "Betis",
                         labels=ml.labels_for(BT, "SP1", live))
    assert p["stats"]["corners"]["lines"]["home"]["highlight"] is True
    assert p["stats"]["corners"]["lines"]["total"]["highlight"] is False
    # Con 49 no.
    w2 = _Writer()
    _grade(all_grades=old[:48], writer=w2)
    assert w2.c[ml.METRICS][ml.LIVE_DOC]["labels"]["corners_team"] == mc.LABEL_BACKTEST


def test_fichas_leen_las_estrellas_del_directo():
    state = {"listed": {f"{lid}|{d}": "x" for lid in ml.LIVE_CUPS for d in ml.window_days(NOW)},
             "fixtures": [{"hl_match_id": 9, "cup": 122477, "country": "ES", "date": "2026-12-16",
                           "kickoff_utc": KO, "hl_home": "Athletic Club", "hl_home_id": 1,
                           "hl_away": "Real Betis", "hl_away_id": 2}],
             "id_names": {"updated_at": NOW.isoformat(), "map": {"1": "Ath Bilbao", "2": "Betis"}}}
    live = {"metrics": {"corners_team|ALL": {"market": "corners_team", "div": "ALL", "n_matches": 50,
                                             "ci_label": mp.LABEL_EDGE}}}
    a = asyncio.run(ml.run_cup_previews(dry_run=True, now=NOW, highlightly=False, state=state,
                                        matches=_league(), backtest=BT, existing={}))
    b = asyncio.run(ml.run_cup_previews(dry_run=True, now=NOW, highlightly=False, state=state,
                                        matches=_league(), backtest=BT, existing={}, live=live))
    assert a["ok"] and b["ok"] and a["would_write"] == b["would_write"] == 1
