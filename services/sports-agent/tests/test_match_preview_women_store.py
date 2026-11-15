"""mp-w1: guardar el backtest (paso 3). Sin Firestore: docs sintéticos y _set_docs simulado."""
import asyncio
import json
import random

from analyzers import match_preview as mp
from analyzers import match_preview_women_store as ws


def _docs(seed=2):
    rnd = random.Random(seed)
    out = []
    for lid in (121626, 38228):
        for y in (2023, 2024, 2025, 2026):
            day = 0
            for i in range(8):
                for j in range(8):
                    if i == j:
                        continue
                    day += 1
                    side = lambda: {"corners": rnd.randint(1, 9), "fouls": rnd.randint(5, 15),
                                    "yellows": rnd.randint(0, 3), "reds": 0, "shots_on": rnd.randint(1, 8),
                                    "shots_off": rnd.randint(1, 8), "shots_blocked": rnd.randint(0, 4)}
                    out.append({"group": "women", "league_id": lid, "season": y, "finished": True,
                                "status": "done", "date": f"{y}-09-01T{day:03d}",
                                "stats": {"home": side(), "away": side(), "complete": True},
                                "home_team_id": lid * 100 + i, "away_team_id": lid * 100 + j,
                                "goals_home": rnd.randint(0, 4), "goals_away": rnd.randint(0, 3)})
    return out


def test_dry_run_no_escribe_y_resumen_solo_agregados(monkeypatch):
    calls = []
    monkeypatch.setattr(ws, "_set_docs", lambda c, d: calls.append((c, d)))
    s = asyncio.run(ws.run_women_backtest(dry_run=True, docs=_docs()))
    assert s["ok"] and s["written"] == 0 and not calls
    assert s["n_matches"] > 0 and set(s["lines"]) == set(mp.STATS)
    txt = json.dumps(s, ensure_ascii=False)
    assert "home_team" not in txt and "12100" not in txt          # ni equipos ni ids


def test_escribe_un_doc_con_lineas_y_dispersion_recuperables(monkeypatch):
    calls = []
    monkeypatch.setattr(ws, "_set_docs", lambda c, d: calls.append((c, d)))
    s = asyncio.run(ws.run_women_backtest(dry_run=False, docs=_docs()))
    assert s["written"] == 1 and calls[0][0] == "match_preview_metrics"
    doc = calls[0][1]["mp-w1_backtest"]
    json.dumps(doc)                                                 # serializable (sin claves tupla)
    lines, disp = ws.params_from_backtest(doc)
    assert lines["goals"] == ((0.5, 1.5, 2.5, 3.5), (1.5, 2.5, 3.5, 4.5))
    assert all(k[0] in mp.STATS and k[1] in ("team", "total") for k in disp)
    assert ws.params_from_backtest(None) is None


def test_sin_filas_no_guarda(monkeypatch):
    monkeypatch.setattr(ws, "_set_docs", lambda c, d: (_ for _ in ()).throw(AssertionError("no debe escribir")))
    s = asyncio.run(ws.run_women_backtest(dry_run=False, docs=[]))
    assert not s["ok"] and s.get("written", 0) == 0
