"""player_backtest: informe de solo agregados con box scores inventados (sin Firestore)."""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent" / "tests"))
sys.path.insert(0, str(ROOT))
import player_backtest as pb  # noqa: E402
from test_player_preview import _season_docs  # noqa: E402


def _docs():
    return (_season_docs("2324", date(2023, 8, 15), seed=1) + _season_docs("2425", date(2024, 8, 15), seed=2)
            + _season_docs("2526", date(2025, 8, 15), seed=4) + _season_docs("2627", date(2026, 8, 15), seed=5))


def test_informe_y_documento_solo_agregados():
    docs = _docs()
    out = pb.compute(pb.pp.games_from_docs(docs))
    text = "\n".join(pb.coverage(docs) + out["report"])
    assert "Etiquetas" in text and "temporadas recientes juntas" in text
    assert "Jugador " not in text and "Equipo " not in text                 # nada de nombres
    doc = pb.backtest_doc(out, "2026-11-28T00:00:00+00:00")
    assert set(doc["labels"]) == set(pb.pp.MARKETS) and doc["recent_seasons"] == ["2526", "2627"]
    assert "Jugador " not in repr(doc) and "Equipo " not in repr(doc)
    for mk, lab in doc["labels"].items():          # una ⭐ nunca con diferencia > 0 en las recientes
        if lab == pb.LABEL_EDGE:
            assert all(doc["recent"][k][mk]["delta"] <= 0 for k in ("vs_jugador", "vs_posicion"))
