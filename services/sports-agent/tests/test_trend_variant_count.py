"""Reinicio del recuento de variantes en sombra por fecha. Sin red."""
from analyzers import trend_variants as tv


def _row(rule, result, gen):
    r = {"rule_id": rule, "result": result, "shadow": True}
    if gen:
        r["generated_at"] = gen
    return r


def test_solo_cuenta_desde_la_fecha(monkeypatch):
    monkeypatch.setattr(tv, "TREND_VARIANT_COUNT_SINCE", "2026-11-01")
    rows = [_row("btts.B6", "hit", None),                          # antiguo, sin fecha
            _row("btts.B6", "hit", "2026-10-31T13:00:00+00:00"),   # antes del reinicio
            _row("btts.B6", "miss", "2026-11-01T14:23:00+00:00"),
            _row("btts.B6", "hit", "2026-11-03T14:00:00+00:00")]
    assert tv._shadow_tally(rows, "btts.B6") == (1, 2)


def test_nada_listo_tras_el_reinicio(monkeypatch):
    monkeypatch.setattr(tv, "TREND_VARIANT_COUNT_SINCE", "2026-11-01")
    old = [_row("btts.B6", "hit", None) for _ in range(40)] + [_row("btts.v1", "miss", None) for _ in range(40)]
    st = {s["variant"]: s for s in tv.promotion_status(old, {})}
    assert st["B6"]["n"] == 0 and st["B6"]["status"] == "testing"


def test_h4_en_sombra_junto_a_h5():
    ids = [v["id"] for v in tv.SHADOW_VARIANTS["handicap"]]
    assert ids == ["v1", "H5", "H4"]
    import importlib.util, pathlib
    p = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "trend_variant_backtest.py"
    spec = importlib.util.spec_from_file_location("bt", p); bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    menu_h4 = next(v for v in bt.MENU["handicap"] if v["id"] == "H4")
    live_h4 = next(v for v in tv.SHADOW_VARIANTS["handicap"] if v["id"] == "H4")
    assert live_h4["conds"] == menu_h4["conds"]   # condiciones copiadas literalmente del menú
