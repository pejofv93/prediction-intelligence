"""tennis_backtest (F1): imprime solo agregados; ningún nombre de jugador (datos inventados)."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
import tennis_backtest as tb  # noqa: E402


def _fake():
    spec = importlib.util.spec_from_file_location(
        "tennis_fake", ROOT / "services" / "sports-agent" / "tests" / "tennis_fake.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_informe_sin_nombres(capsys):
    docs = _fake().make_docs(weeks=90, n_players=16, per_tournament=10)
    tb.report(docs, "2023-06-01")
    out = capsys.readouterr().out
    assert "Inventado" not in out and "Torneo" not in out
    for s in ("== 1.", "== 2.", "== 3.", "== 4a.", "== 4b.", "[tp-v1]", "[regresión]", "[tp-v1 − regresión]"):
        assert s in out, s
    assert "total_games  ALL" in out and "hcp_fav      ATP-bo5" in out
