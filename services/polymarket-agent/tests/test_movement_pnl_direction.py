"""update_trade_result liquida BUY_NO a la cuota del NO también en polymarket_movement."""
import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize("source", ["polymarket", "polymarket_movement"])
@pytest.mark.parametrize("sel,result,mp_yes,expected", [
    ("BUY_NO", "win", 0.25, 0.165),    # 1/(1-0.25)=1.33 (2 decimales) → 0.5·0.33
    ("BUY_YES", "win", 0.25, 1.5),     # 1/0.25=4 → 0.5·3
    ("BUY_NO", "loss", 0.25, -0.5),
])
def test_movement_settles_on_traded_side(monkeypatch, source, sel, result, mp_yes, expected):
    import shared.shadow_engine as se
    data = {"source": source, "selection": sel, "virtual_stake": 0.5, "odds": 4.0,
            "signal_data": {"market_price_yes": mp_yes}}
    ref = MagicMock()
    ref.get.return_value = MagicMock(exists=True, to_dict=lambda: data)
    monkeypatch.setattr(se, "col", lambda name: MagicMock(document=lambda _id: ref))
    monkeypatch.setattr(se, "_calc_bankroll_after", lambda: 50.0)
    asyncio.run(se.update_trade_result("t1", result))
    upd = ref.update.call_args[0][0]
    assert upd["pnl_virtual"] == pytest.approx(expected, abs=0.001)
    assert upd["result"] == result
