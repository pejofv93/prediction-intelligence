"""NBA desde ESPN: scoreboard + summary → doc con la forma de eur_games. Datos inventados, sin red."""
import asyncio
from datetime import date

from collectors import nba_espn as ne


def _stats(fg="40-85", fg3="12-35", ft="18-22", reb="44", oreb="10", ast="25", tov="13", tov_name="totalTurnovers"):
    vals = {"fieldGoalsMade-fieldGoalsAttempted": fg,
            "threePointFieldGoalsMade-threePointFieldGoalsAttempted": fg3,
            "freeThrowsMade-freeThrowsAttempted": ft, "totalRebounds": reb, "offensiveRebounds": oreb,
            "defensiveRebounds": "34", "assists": ast, "steals": "7", "blocks": "5", "fouls": "19",
            tov_name: tov, "fieldGoalPct": "47.1"}
    return [{"name": k, "displayValue": v, "label": "x"} for k, v in vals.items() if v is not None]


def _summary(h=110, a=105, quarters=4, completed=True, stats_home=None):
    def comp(side, tid, abbr, score):
        return {"homeAway": side, "score": str(score), "team": {"id": tid, "abbreviation": abbr},
                "linescores": [{"displayValue": "25"}] * quarters}
    return {"header": {"season": {"type": 2},
                       "competitions": [{"date": "2025-11-02T00:30Z",
                                         "status": {"type": {"completed": completed}},
                                         "competitors": [comp("home", "1", "AAA", h), comp("away", "2", "BBB", a)]}]},
            "boxscore": {"teams": [{"team": {"id": "2"}, "statistics": _stats(fg="38-88")},
                                   {"team": {"id": "1"}, "statistics": stats_home or _stats()}]}}


def test_doc_con_prorroga_y_posesiones():
    doc, why = ne.build_game_doc(_summary(quarters=5), "401", "20251101", "N2025")
    assert why is None
    assert doc["date"] == "2025-11-01" and doc["season"] == "N2025" and doc["game_id"] == "401"
    assert doc["ot_periods"] == 1 and doc["minutes"] == 265
    assert doc["home_code"] == "AAA" and doc["home"]["pts"] == 110 and doc["away"]["pts"] == 105
    # el local es el equipo con id 1 aunque llegue segundo en el boxscore
    assert doc["home"]["fg2a"] == 85 - 35 and doc["away"]["fg2a"] == 88 - 35
    # FGA 85 + 0,44·22 − 10 + 13 = 97,68
    assert doc["home"]["poss_est"] == 97.7
    assert doc["total_pts"] == 215


def test_perdidas_con_nombre_alternativo_y_faltas():
    doc, _ = ne.build_game_doc(_summary(stats_home=_stats(tov_name="turnovers")), "1", "20251101", "N2025")
    assert doc["home"]["tov"] == 13
    _, why = ne.build_game_doc(_summary(stats_home=_stats(ast=None)), "1", "20251101", "N2025")
    assert why == "faltan_ast"
    _, why = ne.build_game_doc(_summary(stats_home=_stats(fg="--")), "1", "20251101", "N2025")
    assert why == "faltan_fg"
    _, why = ne.build_game_doc(_summary(completed=False), "1", "20251101", "N2025")
    assert why == "no_terminado"


def test_el_doc_sirve_al_modelo():
    from analyzers import basket_preview as bp
    doc, _ = ne.build_game_doc(_summary(), "1", "20251101", "N2025")
    games = bp.games_from_docs([doc], "NBA")
    assert len(games) == 1 and games[0]["group"] == "N2025_2025-11-01"


def test_temporadas_y_dias():
    assert ne.season_code(date(2025, 11, 1)) == "N2025"
    assert ne.season_code(date(2026, 3, 1)) == "N2025"
    assert ne.default_seasons(date(2026, 11, 24)) == ["N2022", "N2023", "N2024", "N2025", "N2026"]
    assert ne.eligible_season("N2022", date(2026, 11, 24)) and ne.eligible_season("N2026", date(2026, 11, 24))
    assert not ne.eligible_season("N2021", date(2026, 11, 24))
    assert not ne.eligible_season("N2027", date(2026, 11, 24)) and not ne.eligible_season("E2025")
    days = ne.season_days("N2026", date(2026, 10, 3))
    assert days == ["20261001", "20261002", "20261003"]
    assert len(ne.season_days("N2024", date(2030, 1, 1))) == 273   # 1-oct-2024 … 30-jun-2025


def test_scoreboard_filtra_pretemporada_y_all_star():
    sb = {"events": [
        {"id": "1", "status": {"type": {"completed": True}}, "season": {"type": 2}, "name": "A at B"},
        {"id": "2", "status": {"type": {"completed": True}}, "season": {"type": 1}, "name": "C at D"},
        {"id": "3", "status": {"type": {"completed": True}}, "season": {"type": 2}, "name": "NBA All-Star Game"},
        {"id": "4", "status": {"type": {"completed": False}}, "season": {"type": 3}, "name": "E at F"}]}
    evs = ne.events_from_scoreboard(sb)
    assert [e["id"] for e in evs if ne.wanted(e)] == ["1", "4"]
    assert not ne.day_complete(evs, {"1"}, set())          # el 4 aún no ha terminado
    evs[3]["completed"] = True
    assert ne.day_complete(evs, {"1", "4"}, set())


def test_informe_de_campos_sin_cifras():
    rep = ne.field_report(_summary(stats_home=_stats(ast="--")))
    assert rep[1]["team_index"] == 1
    fields = dict(rep[1]["fields"])
    assert fields["assists"] is False and fields["totalRebounds"] is True


def test_carga_dry_run_y_reanudable(monkeypatch):
    monkeypatch.setattr(ne, "_SLEEP_S", 0)
    calls = []

    def fetch(url):
        calls.append(url)
        if "scoreboard" in url:
            day = url.split("dates=")[1]
            evs = [{"id": f"{day}1", "status": {"type": {"completed": True}}, "season": {"type": 2}, "name": "x"}]
            return {"events": evs}, 200, 0.01
        return _summary(), 200, 0.01

    written = {}

    def writer(season, docs, state):
        written[season] = (dict(docs), {**state})
        return len(docs)

    run = lambda **kw: asyncio.run(ne.run_nba_espn(  # noqa: E731
        seasons=["N2026"], today=date(2026, 10, 4), fetch=fetch, writer=writer, **kw))
    s = run(dry_run=True, state_loader=lambda season: {})
    assert s["ok"] and s["dry_run"] and not written
    assert s["seasons"]["N2026"]["docs"] == 3 and s["requests"] == {"scoreboard": 3, "summary": 3, "http_errors": 0}

    s = run(dry_run=False, max_days=2, state_loader=lambda season: {})
    docs, state = written["N2026"]
    assert set(docs) == {"N2026_202610011", "N2026_202610021"}
    assert state["days_done"] == {"20261001", "20261002"} and s["days_left"] == 1

    # segunda pasada con ese estado: solo el día que faltaba
    calls.clear()
    saved = {"hashes": {k: ne.doc_hash(v) for k, v in docs.items()},
             "days_done": sorted(state["days_done"])}
    s = run(dry_run=False, state_loader=lambda season: dict(saved))
    assert [c for c in calls if "scoreboard" in c] == [ne.SCOREBOARD_URL.format(day="20261003")]
    assert s["seasons"]["N2026"]["docs"] == 1


def _ev(eid, completed=True, stype=2, status="STATUS_FINAL"):
    return {"id": eid, "status": {"type": {"completed": completed, "name": status}},
            "season": {"type": stype}, "name": "x"}


def test_aplazado_no_bloquea_el_dia():
    evs = ne.events_from_scoreboard({"events": [_ev("1"), _ev("2", completed=False, status="STATUS_POSTPONED")]})
    assert ne.day_complete(evs, {"1"}, set())
    evs = ne.events_from_scoreboard({"events": [_ev("1"), _ev("2", completed=False, status="STATUS_SCHEDULED")]})
    assert not ne.day_complete(evs, {"1"}, set())


def _fetcher(monkeypatch, scoreboards):
    monkeypatch.setattr(ne, "_SLEEP_S", 0)
    calls = []

    def fetch(url):
        calls.append(url)
        if "scoreboard" in url:
            sb = scoreboards(url.split("dates=")[1])
            return sb, (200 if sb is not None else 503), 0.01
        return _summary(), 200, 0.01
    return fetch, calls


def test_scoreboard_sin_lista_no_da_el_dia_por_hecho(monkeypatch):
    # 1-oct sin clave events (respuesta rara), 2-oct sin partidos (vacío legítimo), 3-oct caído
    fetch, _ = _fetcher(monkeypatch, lambda d: {"20261001": {"leagues": []}, "20261002": {"events": []}}.get(d))
    written = {}

    def writer(season, docs, state):
        written[season] = state
        return 0
    s = asyncio.run(ne.run_nba_espn(seasons=["N2026"], today=date(2026, 10, 4), dry_run=False, fetch=fetch,
                                    state_loader=lambda season: {}, writer=writer))
    assert written["N2026"]["days_done"] == {"20261002"}
    assert s["requests"]["http_errors"] == 2 and s["seasons"]["N2026"]["empty_days"] == 1


def test_pasada_diaria_temporada_actual_de_ayer_hacia_atras(monkeypatch):
    fetch, calls = _fetcher(monkeypatch, lambda d: {"events": [_ev(d + "1")]})
    s = asyncio.run(ne.run_nba_espn(daily=True, dry_run=True, max_days=2, today=date(2026, 11, 25),
                                    fetch=fetch, state_loader=lambda season: {}))
    assert list(s["seasons"]) == ["N2026"] and s["daily"]
    assert [c.split("dates=")[1] for c in calls if "scoreboard" in c] == ["20261124", "20261123"]
    assert s["seasons"]["N2026"]["by_type"] == {"2": 2}


def test_pasada_diaria_fuera_de_temporada_no_pide_nada(monkeypatch):
    fetch, calls = _fetcher(monkeypatch, lambda d: {"events": []})
    for today in (date(2026, 8, 15), date(2026, 9, 30)):
        s = asyncio.run(ne.run_nba_espn(daily=True, dry_run=False, today=today, fetch=fetch,
                                        state_loader=lambda season: {}, writer=lambda *a: 0))
        assert s["ok"] and list(s["seasons"]) == ["N2026"]
    # julio: la temporada que acaba (N2025) ya cargada entera → 0 peticiones
    done = {"days_done": ne.season_days("N2025", date(2026, 7, 31))}
    s = asyncio.run(ne.run_nba_espn(daily=True, dry_run=False, today=date(2026, 7, 10), fetch=fetch,
                                    state_loader=lambda season: dict(done), writer=lambda *a: 0))
    assert list(s["seasons"]) == ["N2025"]
    assert calls == []


def test_endpoint_rechaza_temporadas_fuera_de_rango():
    from fastapi.testclient import TestClient
    import main
    main.app.dependency_overrides[main.verify_token] = lambda: None
    try:
        r = TestClient(main.app).post("/run-nba-espn?seasons=N2021")
        assert r.status_code == 400
    finally:
        main.app.dependency_overrides.clear()
