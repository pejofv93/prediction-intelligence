"""Fase 1 Euroliga: boxscore + feed → doc (local/visitante, prórroga, posesiones). Sin red."""
import asyncio
from datetime import datetime, timezone

from collectors import euroleague_boxscores as eb


def _totr(pts, minutes="200:00", fg2=(20, 40), fg3=(10, 30), ft=(15, 20), oreb=10, dreb=25, tov=12):
    return {"Minutes": minutes, "Points": pts,
            "FieldGoalsMade2": fg2[0], "FieldGoalsAttempted2": fg2[1],
            "FieldGoalsMade3": fg3[0], "FieldGoalsAttempted3": fg3[1],
            "FreeThrowsMade": ft[0], "FreeThrowsAttempted": ft[1],
            "OffensiveRebounds": oreb, "DefensiveRebounds": dreb, "TotalRebounds": oreb + dreb,
            "Assistances": 18, "Steals": 6, "Turnovers": tov, "BlocksFavour": 2,
            "BlocksAgainst": 3, "FoulsCommited": 20, "FoulsReceived": 21, "Valuation": 90}


def _game(code=397, h=90, a=94, ot1=10, status="result"):
    q = {"q1": 20, "q2": 22, "q3": 20, "q4": 18, "ot1": ot1, "ot2": None, "ot3": None,
         "ot4": None, "ot5": None}
    return {"identifier": f"E2025_{code}", "code": code, "date": "2026-05-01T18:00:00.000Z",
            "status": status, "season": {"code": "E2025"}, "phaseType": {"code": "PO"},
            "round": {"round": 41},
            "home": {"code": "ZAL", "name": "Zalgiris Kaunas", "score": h, "quarters": q},
            "away": {"code": "ULK", "name": "Fenerbahce Beko Istanbul", "score": a, "quarters": q}}


def _box(p0, p1, minutes="225:00"):
    return {"Stats": [{"Team": "ZALGIRIS KAUNAS", "totr": _totr(p0, minutes)},
                      {"Team": "FENERBAHCE", "totr": _totr(p1, minutes)}]}


def test_partido_con_prorroga():
    doc, why = eb.build_game_doc(_game(), _box(90, 94))
    assert why is None
    assert doc["season"] == "E2025" and doc["gamecode"] == 397 and doc["date"] == "2026-05-01"
    assert doc["minutes"] == 225 and doc["ot_periods"] == 1
    assert doc["home_code"] == "ZAL" and doc["home"]["pts"] == 90 and doc["away"]["pts"] == 94
    assert doc["total_pts"] == 184
    # FGA 70 + 0,44·20 − 10 + 12 = 80,8
    assert doc["home"]["poss_est"] == 80.8
    assert "PlayersStats" not in str(doc)


def test_sin_prorroga_y_orden_invertido():
    doc, why = eb.build_game_doc(_game(h=94, a=90, ot1=None), _box(90, 94, "200:00"))
    assert why is None and doc["ot_periods"] == 0
    assert doc["home"]["pts"] == 94 and doc["away"]["pts"] == 90   # Stats venía al revés


def test_descartes():
    assert eb.build_game_doc(_game(), {"Stats": []}) == (None, "boxscore_sin_totales")
    assert eb.build_game_doc(_game(), _box(80, 81)) == (None, "puntos_no_cuadran")
    assert eb.build_game_doc(_game(ot1=None), _box(90, 94, "225:00")) == (None, "prorroga_no_cuadra")
    # Sin jugar: el feed da 0-0 y el boxscore totales a 0 → no es un partido
    assert eb.build_game_doc(_game(h=0, a=0, ot1=None, status="confirmed"),
                             _box(0, 0, "0:00")) == (None, "no_terminado")


def test_minutos_y_temporadas():
    assert eb.parse_minutes("250:00") == 250 and eb.ot_from_minutes(250) == 2
    assert eb.parse_minutes(None) is None and eb.ot_from_minutes(None) is None
    assert eb.default_seasons(datetime(2026, 11, 19, tzinfo=timezone.utc)) == ["E2024", "E2025", "E2026"]
    assert eb.default_seasons(datetime(2027, 3, 1, tzinfo=timezone.utc)) == ["E2024", "E2025", "E2026"]


def test_hash_ignora_marcas_de_tiempo():
    doc, _ = eb.build_game_doc(_game(), _box(90, 94))
    assert eb.doc_hash(doc) == eb.doc_hash(dict(doc, fetched_at="x", updated_at="y"))


def test_dry_run_sin_escribir_y_solo_lo_que_falta(monkeypatch):
    games = [_game(code=1, h=90, a=94), _game(code=2, h=94, a=90), _game(code=3, status="confirmed")]
    monkeypatch.setattr(eb, "_fetch_eur_season_games", lambda season: games)
    monkeypatch.setattr(eb, "_load_state", lambda season: {"E2025_1": "abc"})
    calls = []
    monkeypatch.setattr(eb, "_http_get", lambda url: calls.append(url) or _box(94, 90))
    monkeypatch.setattr(eb, "_SLEEP_S", 0)
    monkeypatch.setattr(eb, "_write", lambda *a: (_ for _ in ()).throw(AssertionError("escribió")))
    out = asyncio.run(eb.run_eur_boxscores(["E2025"], dry_run=True))
    s = out["seasons"]["E2025"]
    assert out["ok"] and s["finished"] == 2 and s["boxscores"] == 1 and s["docs"] == 1
    assert len(calls) == 1 and "gamecode=2" in calls[0]
    assert s["written"] == 1 and out["requests"] == {"feed_seasons": 1, "boxscores": 1}
