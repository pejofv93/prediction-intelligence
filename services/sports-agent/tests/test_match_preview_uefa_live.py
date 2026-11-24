"""Fichas de Europa (mp-e1) y su graduación. Datos INVENTADOS (equipos y ligas de mentira). Sin red,
sin Firestore y sin Highlightly (highlightly=False y todo lo que se leería se pasa a mano)."""
import asyncio
import random
from datetime import date, datetime, timedelta, timezone

from analyzers import match_preview as mp
from analyzers import match_preview_cups as mc
from analyzers import match_preview_uefa_live as ul

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
WORDS = ("Norte", "Bahía", "Colina", "Puerto", "Valle", "Ribera")
SP = [f"Zorrovia {w}" for w in WORDS]
BE = [f"Quimeria {w}" for w in WORDS]
IDS = {t: 100 + i for i, t in enumerate(SP + BE)}


def _league():
    rng = random.Random(2)
    out = []
    for season, y0 in (("2526", 2025), ("2627", 2026)):
        for div, ts in (("SP1", SP), ("B1", BE)):
            for i, (h, a) in enumerate((h, a) for h in ts for a in ts if h != a):
                d = (date(y0, 8, 1) + timedelta(days=3 * i)).isoformat()
                if d >= "2026-11-24":
                    continue
                out.append({"div": div, "season": season, "date": d, "home": h, "away": a,
                            "h": {s: rng.randint(1, 9) for s in mp.STATS}, "a": {s: rng.randint(1, 9) for s in mp.STATS}})
    return out


def _side(rng):
    return {"corners": rng.randint(2, 9), "fouls": rng.randint(8, 15), "yellows": rng.randint(0, 4), "reds": None,
            "shots_on": rng.randint(1, 7), "shots_off": rng.randint(1, 7), "shots_blocked": rng.randint(0, 4)}


def _uefa_docs():
    rng = random.Random(4)
    docs = []
    for j in range(60):
        h, a = SP[j % 6], BE[(j * 5) % 6]
        d = (date(2025, 9, 16) + timedelta(days=7 * j)).isoformat()
        docs.append({"match_id": 7000 + j, "group": "uefa", "comp": "CL", "status": "done", "finished": True,
                     "date": d, "home_team": h, "away_team": a, "home_team_id": IDS[h], "away_team_id": IDS[a],
                     "goals_home": 1, "goals_away": 1, "stats": {"home": _side(rng), "away": _side(rng)}})
    return docs


def _state(fixtures, now=NOW):
    return {"listed": {}, "fixtures": fixtures,
            "id_names": {"updated_at": now.isoformat(), "map": {str(v): k for k, v in IDS.items()}}}


def _fx(hl_id, comp, home, away, hid, aid, ko="2026-11-25T20:00:00+00:00"):
    return {"hl_match_id": hl_id, "league_id": 9001, "comp": comp, "date": ko[:10], "kickoff_utc": ko,
            "hl_home": home, "hl_away": away, "hl_home_id": hid, "hl_away_id": aid}


BACKTEST = {"labels": {"corners_team": "medido en backtest"}}


def _previews(fixtures, now=NOW, **kw):
    return asyncio.run(ul.run_uefa_previews(dry_run=True, now=now, highlightly=False, state=_state(fixtures, now),
                                            matches=_league(), uefa_docs=_uefa_docs(), backtest=BACKTEST,
                                            live={}, existing={}, read_hl_matches=lambda: [], lids=[], **kw))


def test_fichas_dry_run_cuenta_y_marca_lo_que_no_tiene_datos():
    fixtures = [_fx(1, "CL", SP[0], BE[1], IDS[SP[0]], IDS[BE[1]]),
                _fx(2, "ECL", SP[2], "Lejania FK", IDS[SP[2]], 999),
                _fx(3, "CL", SP[1], BE[2], IDS[SP[1]], IDS[BE[2]], ko="2026-11-20T20:00:00+00:00")]   # ya jugado
    out = _previews(fixtures)
    assert out["ok"], out["errors"]
    assert not out["state_saved"]
    assert out["fixtures_window"] == 2 and out["would_write"] == 2 and out["counts"] == {"nueva": 2}
    assert out["by_comp"]["ECL"]["equipos sin emparejar"] == 1 and out["by_comp"]["ECL"]["muestra insuficiente"] == 1
    assert out["engine"]["europeos_previos"] == 60 and out["engine"]["con_factor"]
    assert {p["match_id"] for p in out["previews"]} == {"CL_2026-11-25_1", "ECL_2026-11-25_2"}
    assert out["aviso"].startswith("sin ids")           # lids=[]: no se lista nada (ni se pide)


def test_tras_el_pro_solo_champions_y_europa_league():
    fixtures = [_fx(1, "CL", SP[0], BE[1], IDS[SP[0]], IDS[BE[1]], ko="2026-12-17T20:00:00+00:00"),
                _fx(2, "ECL", SP[2], BE[3], IDS[SP[2]], IDS[BE[3]], ko="2026-12-17T20:00:00+00:00")]
    out = _previews(fixtures, now=datetime(2026, 12, 16, 12, 0, tzinfo=timezone.utc))
    assert out["ok"] and not out["state_saved"]
    assert out["fixtures_window"] == 1 and {p["comp"] for p in out["previews"]} == {"CL"}
    assert ul.live_comps(date(2026, 12, 12)) == ("CL", "EL", "ECL") and ul.live_comps(date(2026, 12, 13)) == ("CL", "EL")
    seasons = {"CL": [(9001, 2026), (9001, 2025)], "EL": [(9002, 2026)], "ECL": [(9003, 2026)]}
    assert ul.live_league_ids(NOW, seasons) == [(9001, "CL"), (9002, "EL"), (9003, "ECL")]
    assert ul.live_league_ids(datetime(2026, 12, 20, tzinfo=timezone.utc), seasons) == [(9001, "CL"), (9002, "EL")]
    assert ul.fixtures_cap(date(2026, 12, 12)) == ul.HL_UEFA_FIXTURES_DAILY_PRO
    assert ul.fixtures_cap(date(2026, 12, 13)) == ul.HL_UEFA_FIXTURES_DAILY


def test_sin_backtest_no_hay_fichas():
    out = asyncio.run(ul.run_uefa_previews(dry_run=True, now=NOW, highlightly=False, backtest={}, live={}))
    assert not out["ok"] and "mp-e1_backtest" in out["errors"][0]


def _preview_doc(hl_id=1, low=False, day="2026-11-20"):
    blk = {"market": "corners_team", "label": "medido en backtest", "highlight": False,
           "lines": {"over_4_5": {"line": 4.5, "p_model": 0.6, "p_league": 0.5}}}
    return {"match_id": f"CL_{day}_{hl_id}", "div": "CL", "comp": "CL", "season": "2627", "date": day,
            "kickoff_utc": f"{day}T20:00:00+00:00", "home_team": SP[0], "away_team": BE[0], "hl_match_id": hl_id,
            "hash": "h", "computed_at": f"{day}T02:00:00+00:00", "data_cutoff": "2026-11-10", "low_sample": low,
            "model_version": "mp-e1", "stats": {"corners": {"available": True, "lines": {"home": blk}}}}


def test_graduacion_y_estrella_con_la_regla_de_copas():
    rng = random.Random(9)
    stats = {"home": {**_side(rng), "corners": 7}, "away": _side(rng)}
    result = {"status": "done", "finished": True, "date": "2026-11-20", "goals_home": 2, "goals_away": 0,
              "stats": stats}
    written = {}
    out = asyncio.run(ul.run_uefa_grade(
        dry_run=False, now=NOW, previews=[_preview_doc(1), _preview_doc(2, low=True)],
        results={"1": result, "2": result}, graded={}, all_grades=[], backtest=BACKTEST,
        write=lambda coll, docs: written.setdefault(coll, {}).update(docs)))
    assert out["ok"] and out["counts"] == {"graduada": 2}
    g = written[ul.GRADES_U]["CL_2026-11-20_1"]
    assert g["status"] == "graduada" and g["country"] == "CL" and g["lines"][0]["y"] == 1
    live = written["match_preview_metrics"][ul.LIVE_DOC]
    assert live["n_graduated"] == 1                   # la de muestra insuficiente no cuenta para estrellas
    assert live["labels"]["corners_team"] == "medido en backtest" and live["star_markets"] == []
    # Estrella: 50 graduados con IC95 en directo bajo 0 y el mercado medido en backtest.
    met = {"corners_team|ALL": {"market": "corners_team", "div": "ALL", "n_matches": 50, "ci_label": mp.LABEL_EDGE}}
    assert ul.labels_for(BACKTEST, {"metrics": met})["corners_team"] == mp.LABEL_EDGE
    met["corners_team|ALL"]["n_matches"] = mc.STARS_AFTER_LIVE - 1
    assert ul.labels_for(BACKTEST, {"metrics": met})["corners_team"] == "medido en backtest"
