"""Resumen diario de Tendencias: hora de Madrid, línea principal por equipo y reparto en mensajes."""
from datetime import date, datetime, timezone

import trends_summary as ts
from shared import dispatch_schedule as ds

UTC = timezone.utc


def test_hora_de_madrid_y_dia():
    assert ts.madrid_offset(datetime(2026, 11, 20, 12, tzinfo=UTC)).seconds == 3600        # invierno
    assert ts.madrid_offset(datetime(2026, 7, 1, 12, tzinfo=UTC)).seconds == 7200           # verano
    d, lo, hi = ts.day_window(datetime(2026, 11, 20, 8, tzinfo=UTC))
    assert d == date(2026, 11, 20)
    assert lo == datetime(2026, 11, 19, 23, tzinfo=UTC) and hi == datetime(2026, 11, 20, 23, tzinfo=UTC)


def _block(lines, main=None):
    b = {"lines": {f"over_{ln}": {"line": ln, "p_model": p} for ln, p in lines}}
    if main is not None:
        b["main_line"] = main
    return b


def _preview(home="Ath Bilbao", away="Espanol", div="SP1", ko="2026-11-20T20:00:00+00:00"):
    stat = {"available": True, "exp_home": 6.42, "exp_away": 3.71, "exp_total": 10.13, "lines": {
        "home": _block([(5.5, 0.60), (6.5, 0.47)], main=6.5),
        "away": _block([(3.5, 0.45), (4.5, 0.28)]),
        "total": _block([(9.5, 0.54), (10.5, 0.42)], main=9.5)}}
    return {"home_team": home, "away_team": away, "div": div, "kickoff_utc": ko,
            "stats": {"corners": stat, "goals": {"available": False}}}


def _edge(p, stat, side):
    p["stats"][stat]["lines"][side]["market_label"] = "ventaja medida"


def test_bloque_de_partido_html():
    p = _preview()
    _edge(p, "corners", "home")
    txt = ts.match_block(p)
    lines = txt.splitlines()
    assert lines[:3] == [ts.SEP, "⚽ LaLiga · 21:00", "<b>Athletic – Espanyol</b>"]
    assert lines[3] == "⭐ Athletic más de <b>5,5</b> córners · <b>60 %</b>"
    assert "blockquote" not in txt and "Todos los mercados" not in txt and "cuota" not in txt.lower()


def test_un_solo_mensaje_para_el_dia_con_cabecera_y_enlace():
    msgs = ts.build_messages([_preview(div="E0", home="Chelsea", away="Leeds", ko="2026-11-20T15:00:00+00:00"),
                              _preview(),
                              {**_preview(div="WLF", home="Barcelona", away="Levante"), "group": "women"}],
                             date(2026, 11, 20))
    assert len(msgs) == 1
    m = msgs[0]
    assert m.startswith("<b>📊 TENDENCIAS · Viernes 20 nov</b>\n3 partidos · 0 con ⭐\n" + ts.STAR_LEGEND)
    # sin ninguna ⭐: todos juntos en la línea final, por hora
    assert "<b>Sin tendencias destacadas:</b> Chelsea–Leeds, Athletic–Espanyol, 👩 Barcelona–Levante" in m
    assert m.endswith('<a href="https://tendencias-pi.pages.dev/#partidos/2026-11-20">Fichas completas en la web</a>')


def test_dia_largo_se_parte_entre_partidos():
    many = [_preview(home=f"Equipo {i}") for i in range(60)]
    for p in many:
        _edge(p, "corners", "home")
    msgs = ts.build_messages(many, date(2026, 11, 21))
    assert len(msgs) > 1 and all(ts.tg_len(m) <= ts.TG_LIMIT for m in msgs)
    assert "(sigue)" in msgs[1] and "Fichas completas" in msgs[-1]
    for m in msgs:                                           # ningún partido cortado
        assert m.count("<b>Equipo") == m.count("córners · ")


def test_sin_fichas_no_hay_mensajes():
    assert ts.build_messages([], date(2026, 11, 20)) == []


def test_resumen_a_las_ocho_utc_en_el_repartidor():
    t = ds.TASKS["trends-summary"]
    assert t.workflow == "trends-daily-summary.yml" and t.cron == "0 8 * * *"


def test_copa_con_icono_y_nombre_de_copa():
    cup = {**_preview(div="P1", home="Guimaraes", away="Sp Braga", ko="2026-11-21T20:45:00+00:00"),
           "group": "cups", "cup": "Taça de Portugal"}
    _edge(cup, "corners", "home")
    m = ts.build_messages([cup], date(2026, 11, 21))[0]
    assert "🏆 Taça de Portugal · 21:45" in m and "<b>Vitória Guimarães – Braga</b>" in m


def test_europa_con_icono_y_competicion_y_sin_pildora_hasta_la_estrella():
    eu = {**_preview(div="CL", home="Zorrovia Norte", away="Quimeria Valle", ko="2026-11-25T20:00:00+00:00"),
          "group": "uefa", "comp": "CL", "comp_name": "Champions League"}
    eu["stats"]["corners"]["lines"]["home"]["market_label"] = "medido en backtest"
    m = ts.build_messages([eu], date(2026, 11, 25))[0]
    assert "0 con ⭐" in m and "🌍 Zorrovia Norte–Quimeria Valle" in m          # medido en backtest: sin píldora
    _edge(eu, "corners", "home")
    m = ts.build_messages([eu], date(2026, 11, 25))[0]
    assert "🌍 Champions League · 21:00" in m and "⭐ Zorrovia Norte más de" in m
    assert ts.group_icon_label({"group": "uefa", "div": "EL"}) == "🌍 Europa League"
    cup = {**_preview(div="P1"), "group": "cups", "cup": "Taça"}
    league = _preview(div="SP1")
    assert ts._group_rank(league) < ts._group_rank(eu) < ts._group_rank(cup) < ts._group_rank(_preview(div="XX"))


def _basket(home="Zorrovia Norte", away="Quimeria Valle", ko="2026-11-26T19:00:00+00:00"):
    def stat():
        return {"available": True, "lines": {"home": _block([(75.5, 0.86), (80.5, 0.71), (85.5, 0.52)]),
                                             "away": _block([(75.5, 0.66), (80.5, 0.48)]),
                                             "total": _block([(155.5, 0.80), (160.5, 0.70)])}}
    return {"home_team": home, "away_team": away, "div": "EUR", "group": "basket", "league_name": "Euroliga",
            "kickoff_utc": ko, "stats": {s: stat() for s in ("pts", "reb", "ast", "fg3m")}}


def test_baloncesto_icono_pildoras_como_las_ligas_y_orden():
    b = _basket()
    m = ts.build_messages([b], date(2026, 11, 26))[0]
    assert "0 con ⭐" in m and "🏀 Zorrovia Norte–Quimeria Valle" in m            # sin ventaja medida: sin píldora
    b["stats"]["pts"]["lines"]["total"]["market_label"] = "ventaja medida"
    assert ts.match_pills(b) == []                                                # el total nunca da píldora
    _edge(b, "pts", "home")
    _edge(b, "fg3m", "away")
    pills = ts.match_pills(b)
    assert [(x["key"], x["line"], x["p"]) for x in pills] == [("pts", 80.5, 0.71), ("fg3m", 75.5, 0.66)]
    m = ts.build_messages([b], date(2026, 11, 26))[0]
    assert "🏀 Euroliga · 20:00" in m and "<b>Zorrovia Norte – Quimeria Valle</b>" in m
    assert "⭐ Zorrovia Norte más de <b>80,5</b> puntos · <b>71 %</b>" in m
    assert "⭐ Quimeria Valle más de <b>75,5</b> triples · <b>66 %</b>" in m
    assert ts.group_icon_label({"group": "basket", "div": "EUR"}) == "🏀 Euroliga"
    cup = {**_preview(div="P1"), "group": "cups", "cup": "Taça"}
    assert ts._group_rank(cup) < ts._group_rank(b) < ts._group_rank(_preview(div="XX"))


def test_pildoras_solo_ventaja_medida_sin_0_5_salvo_marca():
    p = _preview()
    p["stats"]["corners"]["lines"]["home"] = _block([(3.5, 0.85), (4.5, 0.73), (5.5, 0.60), (6.5, 0.47)])
    p["stats"]["corners"]["lines"]["away"] = _block([(3.5, 0.70)])            # sin ventaja: fuera
    p["stats"]["cards"] = {"available": True, "exp_home": 0.9, "exp_away": 1.1, "exp_total": 2.0, "lines": {
        "home": _block([(0.5, 0.75)]), "away": _block([(0.5, 0.70)]), "total": _block([(1.5, 0.6)])}}
    p["stats"]["goals"] = {"available": True, "exp_home": 1.6, "exp_away": 0.9, "exp_total": 2.5, "lines": {
        "home": _block([(0.5, 0.95)]), "away": _block([(0.5, 0.66), (1.5, 0.29)]), "total": _block([(2.5, 0.46)])}}
    for st, side in (("corners", "home"), ("cards", "home"), ("cards", "away"), ("goals", "home"), ("goals", "away")):
        _edge(p, st, side)
    pills = ts.match_pills(p)
    assert [(x["key"], x["line"], x["p"]) for x in pills] == [("goals", 0.5, 0.66), ("corners", 5.5, 0.60)]
    txt = ts.match_block(p)
    assert "⭐ Espanyol <b>marca</b> al menos 1 gol · <b>66 %</b>" in txt
    assert "⭐ Athletic más de <b>5,5</b> córners · <b>60 %</b>" in txt
    assert "tarjetas" not in txt.split("<blockquote")[0]       # tarjetas 0,5 fuera de las píldoras


def test_pildoras_maximo_5_de_mayor_a_menor_y_nada_con_muestra_insuficiente():
    p = _preview()
    p["stats"] = {}
    for key in ("corners", "fouls", "shots", "sot"):
        p["stats"][key] = {"available": True, "exp_home": 5, "exp_away": 5, "exp_total": 10, "lines": {
            "home": _block([(4.5, 0.61)]), "away": _block([(3.5, 0.82)]), "total": _block([(9.5, 0.5)])}}
        _edge(p, key, "home"), _edge(p, key, "away")
    pills = ts.match_pills(p)
    assert len(pills) == 5 and [x["p"] for x in pills] == sorted((x["p"] for x in pills), reverse=True)
    assert ts.match_pills({**p, "low_sample": True}) == []


def test_nombres_escapados_y_de_pantalla():
    p = _preview(home="Athletic Club", away="RCD Espanyol de Barcelona")
    assert "<b>Athletic – Espanyol</b>" in ts.match_block(p)
    assert p["home_team"] == "Athletic Club"                                    # solo presentación
    w = {**_preview(div="WLF", home="A<B", away="C&D"), "group": "women"}
    assert "<b>A&lt;B – C&amp;D</b>" in ts.match_block(w)


def test_el_enlace_no_va_solo_en_otro_mensaje(monkeypatch):
    one = ts.build_messages([_preview()], date(2026, 11, 20))[0]
    monkeypatch.setattr(ts, "MAX_LEN", len(one) - 20)                # cabe el partido, no el enlace
    msgs = ts.build_messages([_preview()], date(2026, 11, 20))
    assert msgs == [one]


def test_telegram_maximo_3_estrellas_y_largo_como_telegram():
    p = _preview()
    p["stats"] = {}
    for key in ("corners", "fouls", "shots", "sot"):
        p["stats"][key] = {"available": True, "exp_home": 5, "exp_away": 5, "exp_total": 10, "lines": {
            "home": _block([(4.5, 0.61)]), "away": _block([(3.5, 0.82)]), "total": _block([(9.5, 0.5)])}}
        _edge(p, key, "home"), _edge(p, key, "away")
    m = ts.build_messages([p], date(2026, 11, 21))[0]
    assert m.count("⭐ ") == 1 + ts.TG_PILLS_MAX                  # leyenda + 3 píldoras
    assert ts.tg_len("<b>📊 a &amp; b</b>") == len("📊 a & b") + 1    # sin etiquetas; 📊 vale 2 en UTF-16


def test_partidos_sin_estrella_al_final_y_con_estrella_arriba():
    a = _preview(home="Chelsea", away="Leeds", div="E0", ko="2026-11-21T12:30:00+00:00")
    b = _preview(home="Ath Bilbao", away="Espanol", ko="2026-11-21T20:00:00+00:00")
    _edge(b, "corners", "home")
    m = ts.build_messages([a, b], date(2026, 11, 21))[0]
    assert m.index("<b>Athletic – Espanyol</b>") < m.index("Sin tendencias destacadas") < m.index("Chelsea–Leeds")
    assert "<b>Chelsea – Leeds</b>" not in m
