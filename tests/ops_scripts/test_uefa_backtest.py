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
                if div == "SP1":
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
                     "home_team_id": ids[h], "away_team_id": 9000 + j, "goals_home": rng.randint(0, 3),
                     "goals_away": rng.randint(0, 3), "stats": {"home": side(), "away": side()}})
    return docs, league, matches


def test_report_solo_agregados(capsys, monkeypatch):
    monkeypatch.setattr(ub.mu, "EVAL_SEASONS", ("2526",))
    monkeypatch.setattr(ub.mu, "MIN_FIT", 20)
    docs, league, matches = _world()
    out = ub.report(docs, league, matches, n_boot=100)
    text = capsys.readouterr().out
    assert "Zorrovia" not in text and "Quimeria" not in text and "QUIMERIA" not in text
    assert "5000" not in text                                                # ni ids de partido
    assert set(out["labels"]), text
    assert set(out["labels"].values()) <= {"medido en backtest", "solo referencia"}
    assert out["counts"]["uefa_utilizables"] == 80
    assert "[e1_vs_comp · ambos]" in text and "[e1_vs_v1 · todos]" in text and "Factor de liga" in text
