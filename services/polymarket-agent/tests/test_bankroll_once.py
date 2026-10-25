"""update_trade_result con bankroll_state: una sola lectura y mismo bankroll que releer."""
import asyncio, copy
from unittest.mock import MagicMock

import pytest

TRADES = {
    "a": {"source": "polymarket", "selection": "BUY_YES", "virtual_stake": 1.0,
          "signal_data": {"market_price_yes": 0.25}},                       # pendiente → win +3
    "b": {"source": "polymarket_movement", "selection": "BUY_NO", "virtual_stake": 0.5,
          "signal_data": {"market_price_yes": 0.25}},                       # pendiente → win +0.165
    "c": {"source": "polymarket", "selection": "BUY_NO", "virtual_stake": 2.0, "result": "win",
          "pnl_virtual": 1.5, "signal_data": {"market_price_yes": 0.6}},    # corrección win → loss
    "d": {"source": "sports", "selection": "X", "virtual_stake": 1.0, "odds": 2.0,
          "result": "loss", "pnl_virtual": -1.0},                           # cerrado → void
    "e": {"source": "sports", "result": "win", "pnl_virtual": 4.0},         # no se toca
}
ORDER = [("a", "win"), ("c", "loss"), ("b", "win"), ("d", "void")]


class _Doc:
    def __init__(self, store, k): self.store, self.id = store, k
    exists = property(lambda self: self.id in self.store)
    def get(self): return self
    def to_dict(self): return copy.deepcopy(self.store[self.id])
    def update(self, u): self.store[self.id].update(u)


def _run(monkeypatch, use_state):
    import shared.shadow_engine as se
    store = copy.deepcopy(TRADES)
    reads = []
    monkeypatch.setattr(se, "col", lambda n: MagicMock(document=lambda k: _Doc(store, k)))
    monkeypatch.setattr(se, "iter_all", lambda n, **kw: reads.append(1) or [_Doc(store, k) for k in store])
    st = {} if use_state else None
    for k, r in ORDER:
        asyncio.run(se.update_trade_result(k, r, bankroll_state=st))
    return store, len(reads), st


def test_same_snapshots_and_final(monkeypatch):
    old, reads_old, _ = _run(monkeypatch, False)
    new, reads_new, st = _run(monkeypatch, True)
    assert reads_old == len(ORDER) and reads_new == 1
    for k, _ in ORDER:
        assert new[k]["bankroll_after"] == old[k]["bankroll_after"]
        assert new[k]["pnl_virtual"] == old[k]["pnl_virtual"]
    final = round(50 + sum(t["pnl_virtual"] for t in old.values() if t.get("result")), 4)
    # 50 + 3 + 0.165 − 2 + 0 + 4
    assert final == pytest.approx(55.165) and st["running"] == pytest.approx(final)


def test_missing_trade_leaves_state(monkeypatch):
    import shared.shadow_engine as se
    monkeypatch.setattr(se, "col", lambda n: MagicMock(document=lambda k: MagicMock(get=lambda: MagicMock(exists=False))))
    st = {"running": 10.0}
    asyncio.run(se.update_trade_result("zz", "win", bankroll_state=st))
    assert st == {"running": 10.0}
