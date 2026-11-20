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
    txt = ts.match_block(_preview())
    lines = txt.splitlines()
    assert lines[:3] == [ts.SEP, "⚽ LaLiga · 21:00", "<b>Athletic – Espanyol</b>"]
    assert ("<blockquote expandable><b>Todos los mercados</b> (esperado)\n"
            "Córners: Athletic 6,4 · Espanyol 3,7 · total 10,1</blockquote>") in txt
    assert "Goles" not in txt and "cuota" not in txt.lower() and "local +" not in txt
    assert "⭐" not in txt                                     # sin ventaja medida no hay píldoras


def test_un_solo_mensaje_para_el_dia_con_cabecera_y_enlace():
    msgs = ts.build_messages([_preview(div="E0", home="Chelsea", away="Leeds", ko="2026-11-20T15:00:00+00:00"),
                              _preview(),
                              {**_preview(div="WLF", home="Barcelona", away="Levante"), "group": "women"}],
                             date(2026, 11, 20))
    assert len(msgs) == 1
    m = msgs[0]
    assert m.startswith("<b>📊 TENDENCIAS · Viernes 20 nov</b>\n3 partidos\n" + ts.STAR_LEGEND)
    assert m.index("Chelsea") < m.index("Athletic") < m.index("Barcelona")      # por hora
    assert "👩 Liga F · 21:00" in m and "⚽ Premier · 16:00" in m
    assert m.endswith('<a href="https://tendencias-pi.pages.dev/#partidos/2026-11-20">Fichas completas en la web</a>')


def test_dia_largo_se_parte_entre_partidos():
    many = [_preview(home=f"Equipo {i}") for i in range(60)]
    msgs = ts.build_messages(many, date(2026, 11, 21))
    assert len(msgs) > 1 and all(len(m) <= ts.MAX_LEN for m in msgs)
    assert "(sigue)" in msgs[1] and "Fichas completas" in msgs[-1]
    for m in msgs:                                           # ningún partido ni plegable cortado
        assert m.count("<blockquote expandable>") == m.count("</blockquote>")


def test_sin_fichas_no_hay_mensajes():
    assert ts.build_messages([], date(2026, 11, 20)) == []


def test_resumen_a_las_ocho_utc_en_el_repartidor():
    t = ds.TASKS["trends-summary"]
    assert t.workflow == "trends-daily-summary.yml" and t.cron == "0 8 * * *"


def test_copa_con_icono_y_nombre_de_copa():
    cup = {**_preview(div="P1", home="Guimaraes", away="Sp Braga", ko="2026-11-21T20:45:00+00:00"),
           "group": "cups", "cup": "Taça de Portugal"}
    m = ts.build_messages([cup], date(2026, 11, 21))[0]
    assert "🏆 Taça de Portugal · 21:45" in m and "<b>Vitória Guimarães – Braga</b>" in m


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
