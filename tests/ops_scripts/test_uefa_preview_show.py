"""uefa_preview_show: prueba sin escribir con el backtest de Europa en memoria. Datos inventados; ni
Firestore ni red (todo lo que leería se sustituye)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import uefa_preview_show as sh  # noqa: E402
from test_uefa_backtest import _world  # noqa: E402


def _shift(docs, league, matches, years):
    """Mueve el mundo inventado `years` años adelante (para que la temporada actual tenga datos)."""
    def mv(d):
        return d.replace(year=d.year + years)
    from datetime import date
    for m in matches + league:
        m["date"] = mv(date.fromisoformat(m["date"])).isoformat()
    for m in matches:
        m["season"] = f"{int(m['season'][:2]) + years:02d}{int(m['season'][2:]) + years:02d}"
    for d in docs:
        d["date"] = mv(date.fromisoformat(d["date"])).isoformat()


def test_prueba_sin_escribir_saca_fichas_de_la_jornada_y_no_escribe(monkeypatch, tmp_path, capsys):
    docs, league, matches = _world()
    _shift(docs, league, matches, 2)                         # 2526-2627 → como ahora
    now = datetime.now(timezone.utc)
    ko = (now + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    far = ko + timedelta(days=12)
    for i, (k, comp) in enumerate(((ko, "CL"), (ko + timedelta(days=1), "EL"), (far, "CL"))):
        docs.append({"match_id": 7000 + i, "group": "uefa", "comp": comp, "status": "pending", "finished": False,
                     "date": k.date().isoformat(), "kickoff_utc": k.isoformat(), "league_id": 2486,
                     "home_team": "Zorrovia Norte", "home_team_id": 1000, "away_team": "Zorrovia Bahía",
                     "away_team_id": 1001})
    monkeypatch.setattr(sh.mu, "EVAL_SEASONS", ("2627",))
    monkeypatch.setattr(sh.mu, "MIN_FIT", 20)

    class _Snap:
        def __init__(self, d):
            self._d = d

        def to_dict(self):
            return self._d
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proyecto-de-prueba")
    import shared.firestore_client as fc
    monkeypatch.setattr(fc, "iter_all", lambda name, where=None: [_Snap(d) for d in (docs if name == "hl_team_stats" else league)])

    async def fake_history(last_end):
        return matches, []
    monkeypatch.setattr(sh.ub, "_history", fake_history)
    monkeypatch.setattr(sh.ul, "_get_doc", lambda *a: (_ for _ in ()).throw(AssertionError("no debe leer docs")))
    monkeypatch.setattr(sh.ul, "_get_many", lambda *a: (_ for _ in ()).throw(AssertionError("no debe leer fichas")))
    out = tmp_path / "fichas.txt"
    monkeypatch.setenv("OUT_FILE", str(out))
    monkeypatch.delenv("DIAS", raising=False)
    monkeypatch.delenv("DESDE", raising=False)
    rc = sh.main()
    log = capsys.readouterr().out
    text = out.read_text(encoding="utf-8")
    assert rc == 0, log
    assert "escrituras bloqueadas: 0" in log
    assert "Zorrovia" not in log                              # el log público, sin equipos
    assert text.count("\n== ") == 2                           # solo la jornada (el de dentro de 12 días no)
    assert "Zorrovia Norte – Zorrovia Bahía" in text and "factor de liga" in text


def test_ventana_jornada_y_dias():
    from datetime import date
    fx = [{"date": d} for d in ("2026-12-08", "2026-12-09", "2026-12-10", "2026-12-11", "2026-12-22")]
    w, lo, hi = sh.pick_window(fx, None, 0)
    assert (lo, hi, len(w)) == ("2026-12-08", "2026-12-10", 3)
    w, lo, hi = sh.pick_window(fx, date(2026, 12, 9), 0)
    assert (lo, hi, len(w)) == ("2026-12-09", "2026-12-11", 3)
    w, lo, hi = sh.pick_window(fx, date(2026, 12, 9), 14)
    assert (lo, hi, len(w)) == ("2026-12-09", "2026-12-22", 4)
    assert sh.pick_window([], None, 0) == ([], "", "")
