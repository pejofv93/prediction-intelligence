"""BUY_NO en sombra: no ocupa el slot de envío y el resolver la gradúa sin notificar,
sin tocar poly_predictions y sin buscar shadow_trade."""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

import polymarket_resolver as pr
from alert_engine import _shadow_opposite, _slot_decision

T0 = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def test_shadow_no_cuenta_como_envio_para_el_slot():
    h = [{"sent_at": T0, "direction": "BUY_NO", "market_price_yes": 0.7, "edge": -0.2,
          "status": "shadow", "alert_key": "m_BUY_NO"}]
    # El slot de envío no la cuenta; la protección contra la contraria va aparte.
    assert _slot_decision(h, "BUY_NO", 0.7, -0.2, T0 + timedelta(hours=1))[0] == "first"
    assert _shadow_opposite(h, "BUY_YES")["alert_key"] == "m_BUY_NO"
    assert _shadow_opposite(h, "BUY_NO") is None


class _Doc:
    def __init__(self, store, doc_id):
        self.store, self.doc_id = store, doc_id

    def update(self, data):
        self.store.setdefault(self.doc_id, {}).update(data)


class _Col:
    def __init__(self, name, writes):
        self.name, self.writes = name, writes

    def document(self, doc_id):
        return _Doc(self.writes.setdefault(self.name, {}), doc_id)

    def where(self, *a, **k):
        raise AssertionError(f"no debe consultar {self.name} para una sombra")


def _run(alerts, gamma, monkeypatch):
    writes, notified = {}, []
    monkeypatch.setattr(pr, "_load_pending_alerts", lambda: alerts)

    async def _gamma(ids):
        return gamma
    monkeypatch.setattr(pr, "_fetch_gamma_markets", _gamma)
    monkeypatch.setattr(pr, "col", lambda name: _Col(name, writes))

    async def _notify(*a, **k):
        notified.append(a)
    monkeypatch.setattr(pr, "_send_resolution_notification", _notify)

    async def _cycle(_):
        return {}
    fake_scanner = types.SimpleNamespace(
        record_gamma_cycle=_cycle, reset_gamma_calls=lambda: None,
        _parse_gamma_ts=lambda v: T0)
    monkeypatch.setitem(sys.modules, "scanner", fake_scanner)
    out = asyncio.run(pr.resolve_closed_markets())
    return out, writes, notified


def test_resolver_gradua_la_sombra_sin_notificar_ni_shadow_trade(monkeypatch):
    alerts = [("m1_BUY_NO", {"market_id": "m1", "status": "shadow", "recommendation": "BUY_NO",
                             "sent_at": T0 - timedelta(days=1)})]
    gamma = {"m1": {"closed": True, "outcomePrices": ["0.0", "1.0"], "closedTime": "x"}}
    out, writes, notified = _run(alerts, gamma, monkeypatch)
    doc = writes["alerts_sent"]["m1_BUY_NO"]
    assert doc["result"] == "win" and doc["outcome"] == "NO"
    assert doc["shadow_synced"] is True and doc["shadow_trade_id"] is None
    assert "poly_predictions" not in writes
    assert notified == []
    assert out["buy_no_shadow_graded"] == 1 and out["resolved"] == 0


def test_carga_incluye_shadow_y_excluye_otros_estados(monkeypatch):
    class _Snap:
        def __init__(self, i, d):
            self.id, self._d = i, d

        def to_dict(self):
            return self._d
    docs = [_Snap("a", {"status": "shadow", "market_id": "1"}),
            _Snap("b", {"status": "sent", "market_id": "2"}),
            _Snap("c", {"status": "resent", "market_id": "3"}),
            _Snap("d", {"status": "shadow", "market_id": "4", "result": "win", "shadow_synced": True})]
    monkeypatch.setattr(pr, "iter_all", lambda *a, **k: iter(docs))
    assert [i for i, _ in pr._load_pending_alerts()] == ["a", "b"]


# --- check_and_alert de punta a punta ---
from unittest.mock import MagicMock, patch  # noqa: E402

import alert_engine  # noqa: E402


def _check(analysis, shadow_flag, history=None):
    store, posts, alerted = {}, [], []

    class _Ref:
        def __init__(self, k):
            self.k = k

        def get(self, transaction=None):
            return MagicMock(exists=self.k in store, to_dict=lambda: dict(store.get(self.k, {})))

        def update(self, d):
            store[self.k].update(d)

        def delete(self):
            store.pop(self.k, None)

    def _col(name):
        c = MagicMock()
        if name == "alerts_sent":
            c.where.return_value.limit.return_value.stream.return_value = [
                MagicMock(to_dict=lambda h=h: dict(h)) for h in (history or [])]
            c.document.side_effect = _Ref
        elif name == "poly_predictions":
            c.document.return_value.update.side_effect = lambda d: alerted.append(d)
        else:
            c.document.return_value.get.return_value = MagicMock(exists=False)
        return c

    tx = MagicMock()
    tx.set.side_effect = lambda ref, d: store.__setitem__(ref.k, dict(d))

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, headers=None):
            posts.append(json)
            return MagicMock(status_code=200)

    async def _state(mid):
        return {"open": True, "reason": None}

    from google.cloud import firestore as gfs  # misma ruta de import que check_and_alert
    with patch("shared.firestore_client.col", side_effect=_col), \
         patch("shared.firestore_client.get_client", return_value=MagicMock(transaction=lambda: tx)), \
         patch.object(gfs, "transactional", lambda f: f, create=True), \
         patch("httpx.AsyncClient", _Client), \
         patch("scanner.fetch_market_state", _state), \
         patch.object(alert_engine, "_load_learned_thresholds",
                      lambda: dict(alert_engine._BASE_THRESHOLDS)), \
         patch.object(alert_engine, "TELEGRAM_BOT_URL", "http://bot"), \
         patch.object(alert_engine, "POLY_BUY_NO_SHADOW", shadow_flag):
        ok = asyncio.run(alert_engine.check_and_alert(analysis))
    return ok, store, posts, alerted


_BUY_NO = {"market_id": "m9", "recommendation": "BUY_NO", "edge": -0.20, "confidence": 0.70,
           "market_price_yes": 0.60, "real_prob": 0.40, "category": "sports"}
_BUY_YES = {**_BUY_NO, "recommendation": "BUY_YES", "edge": 0.20, "real_prob": 0.80}


def test_buy_no_en_sombra_no_se_envia_y_queda_graduable():
    ok, store, posts, alerted = _check(dict(_BUY_NO), True)
    assert ok is False and posts == [] and alerted == []
    doc = store["m9_BUY_NO"]
    assert doc["status"] == "shadow" and doc["recommendation"] == "BUY_NO"
    assert doc["market_price_yes"] == 0.60 and doc["edge"] == -0.20 and doc["confidence"] == 0.70


def test_con_el_indicador_apagado_buy_no_se_envia_como_antes():
    ok, store, posts, alerted = _check(dict(_BUY_NO), False)
    assert ok is True and len(posts) == 1 and store["m9_BUY_NO"]["status"] == "sent"
    assert alerted == [{"alerted": True}]


def test_buy_yes_no_cambia_con_el_indicador():
    ok, store, posts, _ = _check(dict(_BUY_YES), True)
    assert ok is True and len(posts) == 1 and store["m9_BUY_YES"]["status"] == "sent"


def test_buy_no_en_sombra_bloquea_el_buy_yes_contrario():
    h = [{"sent_at": T0, "direction": "BUY_NO", "recommendation": "BUY_NO",
          "market_price_yes": 0.60, "edge": -0.20, "status": "shadow", "alert_key": "m9_BUY_NO",
          "market_id": "m9", "type": "polymarket"}]
    ok, store, posts, alerted = _check(dict(_BUY_YES), True, history=h)
    assert ok is False and posts == [] and alerted == [] and "m9_BUY_YES" not in store


def test_sombra_antigua_bloquea_igual_que_un_envio_antiguo():
    # La protección contra la contraria de un BUY_NO enviado no caduca (todo el historial
    # del mercado); la sombra bloquea lo mismo, también 60 días después.
    now = T0 + timedelta(days=60)
    base = {"sent_at": T0, "direction": "BUY_NO", "market_price_yes": 0.7, "edge": -0.2,
            "alert_key": "m_BUY_NO"}
    assert _slot_decision([{**base, "status": "sent_archived"}], "BUY_YES", 0.7, 0.2, now)[0] == "skip"
    assert _shadow_opposite([{**base, "status": "shadow"}], "BUY_YES") is not None
