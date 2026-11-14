"""Historial por segmento en las alertas de Sports (shared/segment_history + alert_manager)."""
import asyncio

import alert_manager
import shared.match_timing
from shared import segment_history as sh


def _rec(sport="football", league="SA", market="h2h", tier="fuerte", odds=2.0, ok=False):
    return {"sport": sport, "league": league, "market": market, "tier": tier, "odds": odds, "ok": ok}


def test_nivel_mostrado_o_reconstruido_sobre_ev():
    assert sh.tier_of("🔥", 0.01) == "fuerte"            # manda lo que se vio
    assert sh.tier_of(None, 0.16) == "fuerte"
    assert sh.tier_of(None, 0.09) == "detectada"
    assert sh.tier_of("", 0.05) == "moderada"
    assert sh.sport_of("nba") == "basketball" and sh.sport_of(None) == "football"


def test_agregado_por_niveles_y_eleccion_del_mas_concreto():
    recs = [_rec(ok=i < 5) for i in range(22)] + [_rec(league="PL", ok=True) for _ in range(3)]
    segs = sh.aggregate(recs)
    s = segs["football|SA|h2h|fuerte"]
    assert (s["n"], s["wins"], s["expected"]) == (22, 5, 0.5)
    assert s["roi"] == round((5 * 1.0 - 17) / 22, 4) and s["roi_hi"] < 0
    assert segs["football|*|h2h|fuerte"]["n"] == 25 and segs["football|*|*|*"]["n"] == 25
    hist = {"segments": segs}
    pred = {"sport": "football", "league": "SA", "market_type": "h2h", "intensity": "🔥"}
    assert sh.pick(hist, pred)[0] == "football|SA|h2h|fuerte"
    # PL tiene 3: sube al segmento de todas las ligas
    assert sh.pick(hist, {**pred, "league": "PL"})[0] == "football|*|h2h|fuerte"
    # baloncesto sin datos: ninguno llega
    assert sh.pick(hist, {**pred, "sport": "basketball"}) == (None, None, 0)
    assert sh.verdict(s) == "muestra corta"                  # 22 < 30
    assert sh.verdict({"n": 40, "roi_lo": -0.5, "roi_hi": -0.01}) == "pierde claro"
    assert sh.verdict({"n": 40, "roi_lo": -0.2, "roi_hi": 0.1}) == "sin evidencia"


def test_linea_de_la_alerta():
    segs = sh.aggregate([_rec(ok=i < 16, odds=2.42) for i in range(50)])
    hist = {"since": "2026-10-22T00:00:00+00:00", "segments": segs}
    line = alert_manager._segment_history_line(
        {"sport": "football", "league": "SA", "market_type": "h2h", "intensity": "🔥"}, hist)
    assert line.startswith("📚 Historial desde 22/10 (Fútbol · ")
    assert "16/50 = 32 %" in line and "esperado por cuota 41 %" in line and "IC95" in line
    assert "[" not in line and "_" not in line                # seguro en Markdown
    short = alert_manager._segment_history_line({"sport": "tennis"}, hist)
    assert short == "📚 Historial: muestra corta (0 graduadas en Tenis)"
    assert alert_manager._segment_history_line({"sport": "football"}, None) == ""


def _send(monkeypatch, pred, line, record):
    sent = {}

    async def fake_send(text, **kw):
        sent["text"] = text
        return True

    async def no_sleep(*_):
        return None

    monkeypatch.setattr(alert_manager, "send_message", fake_send)
    monkeypatch.setattr(alert_manager.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(alert_manager, "_format_alert_unified",
                        lambda p: "cuerpo\n\n⚠️ Apuesta responsablemente. No es asesoramiento financiero.")
    monkeypatch.setattr(alert_manager, "_claim_match_slot", lambda p: (True, None))
    monkeypatch.setattr(alert_manager, "_segment_history_doc", lambda: {})
    monkeypatch.setattr(alert_manager, "_segment_history_line", line)
    monkeypatch.setattr(alert_manager, "_record_sports_sent", lambda p: record.append(p.get("sport")))
    monkeypatch.setattr(shared.match_timing, "signal_is_too_late", lambda d: False)
    assert asyncio.run(alert_manager.send_sports_alert(pred)) == "sent"
    return sent["text"]


def test_la_linea_va_antes_del_aviso_y_el_baloncesto_se_registra(monkeypatch):
    rec = []
    text = _send(monkeypatch, {"sport": "basketball", "edge": 0.1}, lambda p, h: "📚 Historial X", rec)
    assert text == "cuerpo\n📚 Historial X\n\n⚠️ Apuesta responsablemente. No es asesoramiento financiero."
    assert rec == ["basketball"]


def test_si_el_historial_falla_la_alerta_sale_igual(monkeypatch):
    def boom(p, h):
        raise RuntimeError("x")
    rec = []
    text = _send(monkeypatch, {"sport": "football", "edge": 0.1}, boom, rec)
    assert "Historial" not in text and rec == ["football"]


def test_nivel_mostrado_se_guarda_igual_que_en_el_mensaje():
    assert alert_manager._shown_intensity({"intensity": "✅", "ev": 0.5}) == "✅"
    assert alert_manager._shown_intensity({"ev": 0.15}) == "🔥"
    assert alert_manager._shown_intensity({"edge": 0.10}) == "✅"
    assert alert_manager._shown_intensity({"ev": 0.02}) == "📊"
