"""check_and_alert solo marca la alerta "sent" si telegram-bot responde 200 con
sent=true; con 409 (dedup del bot) o 502 (Telegram rechazó) libera el slot y no
marca alerted en poly_predictions."""
import pytest

from tests.test_buy_no_shadow import _BUY_YES, _check


def test_200_y_sent_true_marca_sent_y_alerted():
    ok, store, posts, alerted = _check(dict(_BUY_YES), True)
    assert ok is True and len(posts) == 1
    assert store["m9_BUY_YES"]["status"] == "sent"
    assert alerted == [{"alerted": True}]


@pytest.mark.parametrize("status,reason", [
    (409, "duplicate"),
    (409, "too_late"),
    (502, "rejected"),
])
def test_no_enviada_libera_slot_y_no_marca_alerted(status, reason):
    body = {"ok": False, "sent": False, "reason": reason}
    ok, store, posts, alerted = _check(dict(_BUY_YES), True, bot_status=status, bot_body=body)
    assert ok is False and len(posts) == 1
    assert "m9_BUY_YES" not in store          # slot liberado: se reintentará
    assert alerted == []


def test_200_con_sent_false_tampoco_cuenta():
    # Bot antiguo (antes de este cambio) respondía 200 con sent=false en el dedup.
    body = {"ok": True, "sent": False}
    ok, store, _, alerted = _check(dict(_BUY_YES), True, bot_body=body)
    assert ok is False and "m9_BUY_YES" not in store and alerted == []
