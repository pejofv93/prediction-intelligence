"""basket_backtest: informe de solo agregados con partidos inventados (sin Firestore)."""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT))
import basket_backtest as bb  # noqa: E402


def _games(seasons=("E2024", "E2025")):
    rnd = random.Random(3)
    teams = [f"EQUIPO{i}" for i in range(6)]

    def side():
        return {"pts": 80 + rnd.randint(-15, 15), "reb": 35 + rnd.randint(-6, 6),
                "ast": 18 + rnd.randint(-5, 5), "fg3m": 10 + rnd.randint(-4, 4), "poss_est": 75.0}

    out = []
    for si, season in enumerate(seasons):
        r = 0
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                r += 1
                day = f"{2024 + si}-{1 + r // 28:02d}-{1 + r % 28:02d}"
                out.append({"season": season, "date": day, "phase": "RS", "round": r,
                            "home_code": h, "away_code": a, "ot_periods": 0,
                            "home": side(), "away": side()})
    return out


def test_informe_solo_agregados():
    games = bb.bp.games_from_docs(_games(), "EUR")
    text = "\n".join(bb.run(games, "EUR"))
    assert "calentamiento E2024" in text and "evaluadas ['E2025']" in text
    assert "media de la liga" in text and "media del equipo" in text
    assert "pts_total" in text and "fg3m_team" in text
    assert "EQUIPO" not in text           # ni un nombre de equipo en el log público


def test_una_temporada_no_basta():
    games = bb.bp.games_from_docs(_games(seasons=("E2025",)), "EUR")
    try:
        bb.run(games, "EUR")
    except ValueError as e:
        assert "2 temporadas" in str(e)
    else:
        raise AssertionError("debía fallar")


def test_orden_de_temporadas_numerico():
    games = [{"season": s} for s in ("A2025", "A2023", "A2024")]
    assert bb.split_seasons(games) == ("A2023", ["A2024", "A2025"])
