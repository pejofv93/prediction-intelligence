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
