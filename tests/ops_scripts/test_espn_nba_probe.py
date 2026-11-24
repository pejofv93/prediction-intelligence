"""espn_nba_probe: el log solo lleva campos, HTTP y tiempos; ni equipos ni cifras. Sin red."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT))
import espn_nba_probe as probe  # noqa: E402


def _summary():
    stats = [{"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "41-87"},
             {"name": "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "displayValue": "13-37"},
             {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "19-23"},
             {"name": "totalRebounds", "displayValue": "47"}, {"name": "offensiveRebounds", "displayValue": "11"},
             {"name": "assists", "displayValue": "29"}, {"name": "totalTurnovers", "displayValue": "14"},
             {"name": "fouls", "displayValue": "--"}]

    def comp(side, tid, abbr, score):
        return {"homeAway": side, "score": score, "team": {"id": tid, "abbreviation": abbr,
                                                           "displayName": "Equipo Secreto " + abbr},
                "linescores": [{}] * 4}
    return {"header": {"competitions": [{"status": {"type": {"completed": True}},
                                         "competitors": [comp("home", "1", "QQQ", "123"),
                                                         comp("away", "2", "ZZZ", "119")]}]},
            "boxscore": {"teams": [{"team": {"id": "1", "abbreviation": "QQQ"}, "statistics": stats},
                                   {"team": {"id": "2", "abbreviation": "ZZZ"}, "statistics": stats}]}}


def test_log_sin_equipos_ni_cifras(monkeypatch, capsys):
    def fake(url):
        if "scoreboard" in url:
            return {"events": [{"id": f"9{i}", "status": {"type": {"completed": True}},
                                "season": {"type": 2}, "name": "ZZZ at QQQ"} for i in range(2)]}, 200, 0.12
        return _summary(), 200, 0.2
    monkeypatch.setattr(probe.ne, "http_get_json", fake)
    assert probe.main() == 0
    out = capsys.readouterr().out
    assert "assists" in out and "fouls (vacío)" in out and "HTTP 200" in out and "doc OK" in out
    for secret in ("QQQ", "ZZZ", "Secreto", "123", "119", "41-87", "47"):
        assert secret not in out
    assert out.count("summary partido") == 3
