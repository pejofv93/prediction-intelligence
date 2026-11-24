"""tennis_readonly (F0): imprime solo agregados; ningún nombre de jugador (datos inventados)."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
import tennis_readonly as tr  # noqa: E402


def _fake():
    spec = importlib.util.spec_from_file_location(
        "tennis_fake", ROOT / "services" / "sports-agent" / "tests" / "tennis_fake.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_informe_sin_nombres_de_jugador(capsys):
    docs = _fake().make_docs(weeks=60)
    tr.report(docs)
    out = capsys.readouterr().out
    assert "Inventado" not in out
    assert "== 1." in out and "== 2." in out and "== 3." in out
    assert "ATP United Cup" in out and "ATP Olympics" in out     # torneos fuera (públicos)
    assert "ATP Hard    bo5" in out
