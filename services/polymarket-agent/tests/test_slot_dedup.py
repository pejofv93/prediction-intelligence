"""Slot por mercado+dirección: casos reales de alerts_sent (23-sep → 22-oct)."""
from datetime import datetime, timedelta, timezone

from alert_engine import _same_direction_count, _slot_decision

T0 = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def _doc(h, direction, price, edge, status="sent"):
    return {"sent_at": T0 + timedelta(hours=h), "direction": direction,
            "market_price_yes": price, "edge": edge, "status": status, "alert_key": f"k{h}"}


def test_primer_envio():
    assert _slot_decision([], "BUY_NO", 0.82, -0.12, T0)[0] == "first"


def test_nvidia_5180413_segundo_envio_mismo_dia_bloqueado():
    # 14-oct BUY_NO p0.824 e-0.12; 15-oct (≈20h después) p0.738 e-0.28
    h = [_doc(0, "BUY_NO", 0.824, -0.12)]
    act, why, _ = _slot_decision(h, "BUY_NO", 0.738, -0.28, T0 + timedelta(hours=20))
    assert act == "skip" and "<" in why


def test_buy_no_reenvio_solo_si_el_yes_sube_005_y_edge_no_baja():
    h = [_doc(0, "BUY_NO", 0.60, -0.15)]
    later = T0 + timedelta(hours=30)
    assert _slot_decision(h, "BUY_NO", 0.66, -0.16, later)[0] == "resend"
    assert _slot_decision(h, "BUY_NO", 0.64, -0.16, later)[0] == "skip"   # +0,04 no basta
    assert _slot_decision(h, "BUY_NO", 0.56, -0.20, later)[0] == "skip"   # precio peor para el NO
    assert _slot_decision(h, "BUY_NO", 0.66, -0.12, later)[0] == "skip"   # edge menor


def test_buy_yes_reenvio_si_el_yes_baja():
    h = [_doc(0, "BUY_YES", 0.55, 0.12)]
    assert _slot_decision(h, "BUY_YES", 0.49, 0.14, T0 + timedelta(days=2))[0] == "resend"


def test_maximo_un_reenvio_y_cuenta_historial_antiguo():
    # Fed 2589812: ya 9 envíos con la clave vieja → ninguno más
    h = [_doc(24 * i, "BUY_NO", 0.505 + 0.04 * i, -0.15 - 0.01 * i) for i in range(9)]
    act, why, _ = _slot_decision(h, "BUY_NO", 0.90, -0.40, T0 + timedelta(days=20))
    assert act == "skip" and "reenvío ya usado" in why
    h2 = [_doc(0, "BUY_NO", 0.6, -0.15), _doc(30, "BUY_NO", 0.66, -0.16, status="resent")]
    assert _slot_decision(h2, "BUY_NO", 0.80, -0.30, T0 + timedelta(days=5))[0] == "skip"
    assert _same_direction_count(h2, "BUY_NO") == 2


def test_cambio_de_direccion_no_se_alerta_peltola_634976():
    # 10-oct BUY_YES p0.735; 18-oct BUY_NO al mismo precio
    h = [_doc(0, "BUY_YES", 0.735, 0.13)]
    act, why, _ = _slot_decision(h, "BUY_NO", 0.735, -0.14, T0 + timedelta(days=8))
    assert act == "skip" and "cambio de dirección" in why


def test_pending_y_fallidos_no_cuentan():
    h = [_doc(0, "BUY_NO", 0.6, -0.15, status="pending")]
    assert _slot_decision(h, "BUY_NO", 0.6, -0.15, T0)[0] == "first"


def test_doc_antiguo_sin_direction_ni_market_price_usa_edge_y_last_price():
    h = [{"sent_at": T0, "edge": -0.2, "last_price": 0.6, "status": "sent_archived"}]
    assert _slot_decision(h, "BUY_NO", 0.67, -0.21, T0 + timedelta(days=2))[0] == "resend"
    assert _slot_decision(h, "BUY_YES", 0.5, 0.2, T0 + timedelta(days=2))[0] == "skip"


def test_doc_con_clave_antigua_market_edge():
    h = [{"sent_at": T0, "alert_key": "5180413_-0.12", "last_price": 0.824, "status": "sent_archived"}]
    act, why, _ = _slot_decision(h, "BUY_NO", 0.90, -0.15, T0 + timedelta(days=2))
    assert act == "resend", why


# --- Racha de ciclos con lecturas fallidas del historial ---
import asyncio
from unittest.mock import MagicMock, patch

import alert_engine


def _run_cycles(fails, state_store, posts, firestore_down=False):
    out = []

    def _col(name):
        doc = MagicMock()
        if firestore_down:
            doc.get.side_effect = RuntimeError("firestore caído")
            doc.set.side_effect = RuntimeError("firestore caído")
        else:
            snap = MagicMock(exists=bool(state_store)); snap.to_dict.return_value = dict(state_store)
            doc.get.return_value = snap
            doc.set.side_effect = lambda d, merge=False: state_store.update(d)
        return MagicMock(document=lambda k: doc)

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None): posts.append(json["data"]["text"])

    with patch("shared.firestore_client.col", side_effect=_col), \
         patch("httpx.AsyncClient", _Client), \
         patch.object(alert_engine, "TELEGRAM_BOT_URL", "http://bot"):
        for f in fails:
            alert_engine._SLOT_READS.update(ok=3, fail=f)
            out.append(asyncio.run(alert_engine.record_slot_cycle("analyze"))["streak"])
    return out


def test_racha_avisa_una_vez_y_al_recuperarse():
    alert_engine._SLOT_STREAK_MEM.update(failed_cycles=0, alerted=False)
    store, posts = {}, []
    assert _run_cycles([1, 2, 1, 0], store, posts) == [1, 2, 3, 0]
    assert len(posts) == 2 and "🚨" in posts[0] and "2 ciclos seguidos" in posts[0] and "✅" in posts[1]


def test_un_ciclo_suelto_no_avisa():
    alert_engine._SLOT_STREAK_MEM.update(failed_cycles=0, alerted=False)
    store, posts = {}, []
    assert _run_cycles([1, 0, 1, 0], store, posts) == [1, 0, 1, 0] and posts == []


def test_con_firestore_caido_la_racha_va_en_memoria_y_avisa():
    alert_engine._SLOT_STREAK_MEM.update(failed_cycles=0, alerted=False)
    posts = []
    assert _run_cycles([2, 2], {}, posts, firestore_down=True) == [1, 2]
    assert len(posts) == 1 and "🚨" in posts[0]
