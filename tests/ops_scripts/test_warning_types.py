"""warning_types: los avisos se cuentan por la plantilla del código, sin nombres ni cifras."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ops"))
import warning_types as w  # noqa: E402

SRC = '''
import logging
logger = logging.getLogger(__name__)
def f(league, x):
    logger.warning("understat(%s season=%s): fetch falló", league, x, exc_info=True)
    logger.warning(f"cuota rara en {league}: {x:.2f}")
    logger.warning("sin cuotas para " + league)
'''


def _src(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text(SRC, encoding="utf-8")
    return p


def test_plantillas_y_clasificacion(tmp_path):
    t = w.templates(str(_src(tmp_path)))
    assert [s for _, s, _ in t] == ["understat({…} season={…}): fetch falló",
                                    "cuota rara en {…}: {…}", "sin cuotas para {…}"]
    assert w.classify("cuota rara en Arsenal FC: 2.10", t)[1] == "cuota rara en {…}: {…}"
    assert w.classify("otra cosa con Arsenal", t) is None


def _e(ts, msg):
    return {"timestamp": ts, "textPayload": f"{ts[:10]} {ts[11:19]},1 analyzers.x WARNING {msg}"}


def test_salida_sin_datos_y_ultima_pasada(tmp_path, capsys, monkeypatch):
    src = _src(tmp_path)
    logs = tmp_path / "logs.json"
    logs.write_text(json.dumps([
        _e("2026-11-23T19:01:00.5Z", "sin cuotas para Viejo"),
        _e("2026-11-24T01:01:00.5Z", "understat(EPL season=2026): fetch falló\n"
           "Traceback (most recent call last):\n  File x\nHTTPStatusError: 403 Arsenal"),
        _e("2026-11-24T01:02:00Z", "cuota rara en Getafe: 3.5"),
        _e("2026-11-24T01:03:00Z", "algo con Osasuna"),
    ]), encoding="utf-8")
    monkeypatch.setenv("LOGS_FILE", str(logs))
    assert w.main(["x", f"analyzers.x={src}"]) == 0
    out = capsys.readouterr().out
    assert "3 avisos, 3 tipos" in out
    assert "understat({…} season={…}): fetch falló [excepción HTTPStatusError]" in out
    assert "sin plantilla" in out
    for name in ("Viejo", "Arsenal", "Getafe", "Osasuna", "EPL", "2026)", "3.5", "403"):
        assert name not in out, name
