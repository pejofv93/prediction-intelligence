"""Mínimo de puntuación por partido y tramos informativos 5,5-7 / 7+. Sin red."""
from analyzers import trend_finder as tf
from analyzers.trend_weekly import score_band_line


def _c(mid, market, score, team="A"):
    return {"match_id": mid, "league": "PD", "match_date": "2026-11-05T20:00:00",
            "home_team": f"H{mid}", "away_team": f"A{mid}", "home_team_id": None,
            "away_team_id": None, "team": team, "pattern_type": "series",
            "market": market, "score": score}


def test_rank_and_cap_respeta_minimo(monkeypatch):
    monkeypatch.setattr(tf, "TREND_MIN_FIXTURE_SCORE", 5.5)
    cands = [_c("1", "btts", 3.0), _c("1", "handicap", 2.6),   # 5,6 → entra
             _c("2", "btts", 3.0), _c("2", "handicap", 2.4)]   # 5,4 → fuera
    out = tf.rank_and_cap(cands)
    assert [f["match_id"] for f in out] == ["1"]


def test_tramos_por_puntuacion():
    rows = [
        {"signal_id": "a", "fixture_total_score": 5.6, "result": "hit"},
        {"signal_id": "b", "fixture_total_score": 6.9, "result": "miss"},
        {"signal_id": "c", "fixture_total_score": 7.0, "result": "hit"},
        {"signal_id": "d", "result": "hit"},                       # puntuación desde trend_signals
        {"signal_id": "e", "fixture_total_score": 4.0, "result": "hit"},  # fuera de tramos
        {"signal_id": "f", "result": "miss"},                      # sin puntuación conocida
    ]
    line = score_band_line("Total", rows, {"d": 9.2})
    assert line == "Total: 5,5-7 1/2 (50%) · 7+ 2/2 (100%)"
