"""Sombra de baloncesto: se guarda y se gradúa, pero no se envía a Telegram. Sin red."""
import asyncio

from analyzers import basketball_analyzer as ba
from analyzers import value_bet_engine as vbe


def _pred(league, ev=0.15):
    return {"league": league, "sport": "basketball", "ev": ev, "edge": ev,
            "selection": "Over 223.5", "match_id": "x"}


def test_nba_totales_y_handicap_a_sombra():
    for sfx in ("tot", "spread", "h1_spread", "h1_tot", "q1_tot"):
        assert ba._shadow_reason(_pred("NBA"), sfx) == "nba_alt_market"
    assert ba._shadow_reason(_pred("NBA"), "ml") is None
    assert ba._shadow_reason(_pred("EUROLEAGUE"), "tot") is None
    assert ba._shadow_reason(_pred("ACB"), "spread") is None


def test_moneyline_ev_alto_a_sombra():
    for lg in ("NBA", "EUROLEAGUE", "ACB"):
        assert ba._shadow_reason(_pred(lg, ev=0.257), "ml") == "ml_ev_high"
        assert ba._shadow_reason(_pred(lg, ev=0.25), "ml") is None
        assert ba._shadow_reason(_pred(lg, ev=0.234), "ml") is None
    # el EV alto no cambia la razón de los mercados alternativos NBA
    assert ba._shadow_reason(_pred("NBA", ev=0.4), "tot") == "nba_alt_market"
    assert ba._shadow_reason(_pred("EUROLEAGUE", ev=0.4), "tot") is None


def _run_save(monkeypatch, pred, existing=None):
    sent, saved = [], []
    async def fake_send(payload):
        sent.append(payload)
        return True
    monkeypatch.setattr(vbe, "_send_telegram_alert", fake_send)
    monkeypatch.setattr(vbe, "_build_alert_payload", lambda p, e: p)

    class Batch:
        def set(self, ref, data): saved.append(data)
    monkeypatch.setattr(ba, "col", lambda name: type("C", (), {"document": lambda s, i: i})())
    kept = asyncio.run(ba._save_and_alert(pred, "x_tot", {}, batch=Batch(), existing=existing))
    return kept, sent, saved


def test_sombra_no_se_envia_pero_se_guarda(monkeypatch):
    p = {**_pred("NBA"), "shadow": True, "shadow_reason": "nba_alt_market"}
    kept, sent, saved = _run_save(monkeypatch, p)
    assert kept and not sent and saved and not saved[0].get("alerted")


def test_sin_sombra_se_envia(monkeypatch):
    kept, sent, saved = _run_save(monkeypatch, _pred("EUROLEAGUE"))
    assert kept and sent and saved[0]["alerted"] is True


def test_sombra_no_pisa_un_doc_ya_alertado(monkeypatch):
    p = {**_pred("NBA"), "shadow": True, "shadow_reason": "nba_alt_market"}
    kept, sent, saved = _run_save(monkeypatch, p, existing={"alerted": True, "selection": "Over 220.5"})
    assert not kept and not sent and not saved
