"""basket_preview_show: prueba sin escribir con el backtest en memoria. Datos inventados; ni
Firestore ni red (todo lo que leería se sustituye)."""
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT))
import basket_preview_show as sh  # noqa: E402

TEAMS = [("ZRV", "Zorrovia Norte"), ("QMR", "Quimeria Valle"), ("PRT", "Puerto Ficticio"),
         ("CLN", "Colina Inventada"), ("BAH", "Bahía Imaginaria"), ("RIB", "Ribera de Mentira")]
NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)


def _docs():
    rng = random.Random(11)

    def side():
        return {"pts": 80 + rng.randint(-12, 12), "reb": 35 + rng.randint(-5, 5), "ast": 18 + rng.randint(-4, 4),
                "fg3m": 10 + rng.randint(-3, 3), "poss_est": 74.0 + rng.randint(-3, 3)}
    out = []
    for season, start in (("E2024", date(2024, 10, 1)), ("E2025", date(2025, 10, 1)), ("E2026", date(2026, 10, 1))):
        pairs = [(h, a) for h in TEAMS for a in TEAMS if h != a]
        for r in range(len(pairs) // 3):
            day = start + timedelta(days=4 * r)
            if day >= NOW.date():
                break
            for j, (h, a) in enumerate(pairs[r * 3:(r + 1) * 3]):
                out.append({"season": season, "gamecode": 100 * r + j, "date": day.isoformat(), "phase": "RS",
                            "round": r + 1, "home_code": h[0], "away_code": a[0], "home_name": h[1],
                            "away_name": a[1], "ot_periods": 0, "home": side(), "away": side()})
    return out


def test_prueba_sin_escribir_saca_fichas_y_no_escribe(monkeypatch, tmp_path, capsys):
    docs = _docs()
    feed = [{"identifier": "E2026_801", "code": 801, "season": {"code": "E2026"}, "date": "2026-11-26T19:00:00.000Z",
             "status": "confirmed", "phaseType": {"code": "RS"}, "round": {"round": 9},
             "home": {"code": "ZRV", "name": "Zorrovia Norte"}, "away": {"code": "QMR", "name": "Quimeria Valle"}}]
    writes = []
    monkeypatch.setattr(sh.bb, "load_docs", lambda league: docs)
    monkeypatch.setattr(sh.bl, "_fetch_feed", lambda season: feed)
    monkeypatch.setattr(sh.bl, "_read_games", lambda seasons, coll="eur_games": [d for d in docs if d["season"] in seasons])
    monkeypatch.setattr(sh.bl, "_get_many", lambda coll, ids: {})
    monkeypatch.setattr(sh.bl, "_set_docs", lambda c, d: writes.append(c))
    monkeypatch.setattr(sh.bl, "_get_doc", lambda *a: (_ for _ in ()).throw(AssertionError("no debe leer el doc")))
    real_run = sh.bl.run_basket_previews
    monkeypatch.setattr(sh.bl, "run_basket_previews", lambda **kw: real_run(now=NOW, **kw))
    out = tmp_path / "fichas.txt"
    monkeypatch.setenv("OUT_FILE", str(out))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proyecto-de-prueba")
    assert sh.main() == 0
    log = capsys.readouterr().out
    text = out.read_text(encoding="utf-8")
    assert not writes                                         # nada escrito
    assert "Zorrovia Norte – Quimeria Valle" in text and "puntos: modelo" in text
    assert "Telegram" in text and "JSON de la web" in text
    assert "Zorrovia" not in log and "would_write" in log     # el log público, sin equipos
