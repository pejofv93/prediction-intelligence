"""Jugadores en las fichas (pp-v1): modelo puro. Datos INVENTADOS (no se guardan respuestas reales de
Highlightly en el repo, por sus términos). Sin red ni Firestore."""
import random
from datetime import date, timedelta

from analyzers import player_preview as pp


def _pl(pid, pos, start, minutes, shots=0, sot=0, goals=0, assists=0):
    return {"player_id": pid, "name": f"Jugador {pid}", "position": pos, "is_substitute": not start,
            "minutes": minutes, "shots_total": shots, "shots_on_target": sot, "goals": goals, "assists": assists}


def _doc(mid, day, season, home, away, hp, ap, fv=2):
    return {"match_id": mid, "div": "SP1", "season": season, "date": day, "fields_version": fv,
            "teams": [{"team_id": home, "team": f"Equipo {home}", "side": "home", "players": hp},
                      {"team_id": away, "team": f"Equipo {away}", "side": "away", "players": ap}]}


def _season_docs(season="2526", start=date(2025, 8, 15), rounds=12, seed=3):
    """4 equipos; en cada equipo un delantero (x01) que remata mucho, un medio (x02) y un suplente
    (x03) que casi nunca sale de titular."""
    rng = random.Random(seed)
    pairs = [(1, 2), (3, 4), (1, 3), (2, 4), (1, 4), (2, 3)]
    out, mid = [], 0
    for r in range(rounds):
        day = (start + timedelta(days=7 * r)).isoformat()
        for h, a in pairs[(2 * r) % 6:(2 * r) % 6 + 2]:
            mid += 1

            def side(t):
                fw_shots = rng.randint(2, 5)
                return [_pl(t * 100 + 1, "Forward", True, 90, fw_shots, rng.randint(0, fw_shots), rng.randint(0, 1)),
                        _pl(t * 100 + 2, "Midfielder", True, 80, rng.randint(0, 2), 0, 0, rng.randint(0, 1)),
                        _pl(t * 100 + 3, "Midfielder", False, 15, rng.randint(0, 1))]
            out.append(_doc(f"{season}-{mid}", day, season, h, a, side(h), side(a)))
    return out


def test_remates_fuera_y_asistencias_segun_version():
    st = pp.row_stats({"shots_total": 3, "shots_on_target": 1, "goals": 1, "assists": 1}, 2)
    assert st == {"shots": 3, "sot": 1, "off": 2, "goals": 1, "assists": 1}
    assert pp.row_stats({"shots_total": 1, "shots_on_target": 2}, 2)["off"] == 0
    assert pp.row_stats({"shots_total": 3, "shots_on_target": 1, "assists": 0}, 1)["assists"] is None


def test_poisson_y_linea_principal():
    assert abs(pp.poisson_over(1.0, 0.5) - (1 - 0.36788)) < 1e-4
    assert pp.poisson_over(0.0, 0.5) == 0.0
    assert pp.main_line({0.5: 0.8, 1.5: 0.45, 2.5: 0.2}) == 1.5


def test_quien_juega_por_minutos_recientes():
    games = pp.games_from_docs(_season_docs())
    h = pp.History()
    for g in games:
        h.update(g)
    assert h.candidates(1) == [101, 102]                     # el suplente (103) no
    pred = h.predict("SP1", "2526", "home", 1, 2, 101)
    assert pred["pos"] == "Forward" and pred["exp_min"] == 90 and not pred["low_sample"]
    assert pred["last_date"] == max(g["date"] for g in games if 1 in (g["home"]["team_id"], g["away"]["team_id"]))
    fw, mf = pred["stats"]["shots"]["lam"], h.predict("SP1", "2526", "home", 1, 2, 102)["stats"]["shots"]["lam"]
    assert fw > mf > 0


def test_asistencias_sin_dato_en_docs_antiguos():
    docs = [{**d, "fields_version": 1} for d in _season_docs()]
    h = pp.History()
    for g in pp.games_from_docs(docs):
        h.update(g)
    pred = h.predict("SP1", "2526", "home", 1, 2, 101)
    assert "assists" not in pred["stats"] and "shots" in pred["stats"]


def test_backtest_walk_forward_solo_temporadas_evaluadas():
    docs = _season_docs("2425", date(2024, 8, 15), seed=1) + _season_docs("2526", date(2025, 8, 15), seed=2)
    rows_p, rows_pos, counts = pp.backtest_rows(pp.games_from_docs(docs))
    assert rows_p and len(rows_p) == len(rows_pos)
    assert {r[0] for r in rows_p} <= set(pp.EVAL_SEASONS)
    assert {r[2] for r in rows_p} == set(pp.MARKETS)
    assert all(0 <= r[5] <= 1 and 0 <= r[6] <= 1 and r[4] in (0, 1) for r in rows_p)
    assert counts["evaluados"] > 0 and "Jugador" not in repr(counts)


def test_traspasado_solo_cuenta_con_su_equipo_actual():
    docs = _season_docs()
    # el delantero 301 pasa al equipo 1 en los dos últimos partidos del equipo 1 (titular): aún tiene
    # apariciones con el equipo 3 entre sus 3 últimas → no sale en el 1 hasta jugar 3 con él
    games = pp.games_from_docs(docs)
    t1 = [g for g in games if 1 in (g["home"]["team_id"], g["away"]["team_id"])]
    for g in t1[-2:]:
        side = "home" if g["home"]["team_id"] == 1 else "away"
        g[side]["players"].append({"pid": 301, "name": "Jugador 301", "pos": "Forward", "start": True, "min": 90,
                                   "st": {"shots": 3, "sot": 1, "off": 2, "goals": 0, "assists": 0}})
    h = pp.History()
    for g in games:
        h.update(g)
    assert h.plays(1, 301) and not h.same_team(301, 1) and 301 not in h.candidates(1)
    assert {a[2] for a in h.recent_apps(301)} == {1, 3}                # aparece con los dos equipos
