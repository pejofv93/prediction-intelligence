"""uefa_backtest: de punta a punta con datos inventados; el log solo lleva agregados (ni equipos ni
partidos). Sin red ni Firestore."""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ops"))
import uefa_backtest as ub  # noqa: E402

STATS = ("goals", "corners", "cards", "shots", "sot", "fouls")


def _world():
    rng = random.Random(5)
    words = ("Norte", "Bahía", "Colina", "Puerto", "Valle", "Ribera")
    teams = {"SP1": [f"Zorrovia {w}" for w in words], "B1": [f"Quimeria {w}" for w in words]}
    ids = {t: 1000 + i for i, t in enumerate(teams["SP1"] + teams["B1"])}
    matches, league = [], []
    for season, y0 in (("2425", 2024), ("2526", 2025)):
        for div, ts in teams.items():
            for i, (h, a) in enumerate((h, a) for h in ts for a in ts if h != a):
                d = (date(y0, 8, 10) + timedelta(days=5 * i)).isoformat()
                matches.append({"div": div, "season": season, "date": d, "home": h, "away": a,
                                "h": {s: rng.randint(1, 9) for s in STATS}, "a": {s: rng.randint(1, 9) for s in STATS}})
                # SP1 en hl_matches (mapa de antes); B1 solo en hl_league_listing (mapa nuevo)
                league.append({"div": div, "date": d, "home_team": h, "away_team": a,
                               "home_team_id": ids[h], "away_team_id": ids[a]})
    docs = []
    for j in range(80):
        h, a = teams["SP1"][j % 6], teams["B1"][(j * 5) % 6]
        d = (date(2024 + j // 40, 9, 20) + timedelta(days=3 * (j % 40))).isoformat()
        side = lambda: {"corners": rng.randint(2, 9), "fouls": rng.randint(8, 15), "yellows": rng.randint(0, 4),  # noqa: E731
                        "reds": None, "shots_on": rng.randint(1, 7), "shots_off": rng.randint(1, 7),
                        "shots_blocked": rng.randint(0, 4)}
        docs.append({"match_id": 5000 + j, "group": "uefa", "comp": "CL" if j % 2 else "ECL", "status": "done",
                     "finished": True, "date": d, "home_team": h, "away_team": a.upper(),
                     "home_team_id": ids[h], "away_team_id": ids[a], "goals_home": rng.randint(0, 3),
                     "goals_away": rng.randint(0, 3), "stats": {"home": side(), "away": side()}})
    # Otro club con el mismo nombre que uno de SP1 (como el Inter de la Conference): id propio sin mapa.
    docs.append({**docs[-1], "match_id": 5999, "comp": "ECL", "away_team": "ZORROVIA NORTE", "away_team_id": 7777})
    return docs, league, matches


def test_report_solo_agregados(capsys, monkeypatch):
    monkeypatch.setattr(ub.mu, "EVAL_SEASONS", ("2526",))
    monkeypatch.setattr(ub.mu, "MIN_FIT", 20)
    docs, league, matches = _world()
    league_old = [m for m in league if m["div"] == "SP1"]
    out = ub.report(docs, league, matches, n_boot=100, league_old=league_old)
    text = capsys.readouterr().out
    assert "Zorrovia" not in text and "Quimeria" not in text and "QUIMERIA" not in text
    assert "5000" not in text                                                # ni ids de partido
    assert set(out["labels"]), text
    assert set(out["labels"].values()) <= {"ventaja medida", "solo referencia"}
    assert out["eval_seasons"] == ["2526"] and out["clean_seasons"] == ["2526"] and set(out["by_season"]) == {"2526"}
    assert "Por temporada" in text and "temporada 2526" in text
    assert out["recent_seasons"] == ["2526"] and set(out["recent"]) == set(out["labels"])
    assert out["counts"]["uefa_utilizables"] == 81
    # Auditoría: los 6 de B1 iban por nombre y el id confirma el mismo club; el "ZORROVIA NORTE" de la
    # ECL era otro club (falso) y ahora se queda sin emparejar; los 6 de SP1, igual por id.
    a = out["audit"]
    assert a["por_nombre_ahora_igual"] == 6 and a["falso_otro_club"] == 1 and a["id_igual"] == 6
    assert [r["hl"] for r in out["audit_detail"] if r["cat"] == "falso_otro_club"] == ["ZORROVIA NORTE"]
    assert "Auditoría del emparejamiento" in text and "ZORROVIA" not in text
    assert "[e1_vs_comp · ambos]" in text and "[e1_vs_v1 · todos]" in text and "Factor de liga" in text
