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
    stat = {"available": True, "lines": {
        "home": _block([(5.5, 0.60), (6.5, 0.47)], main=6.5),
        "away": _block([(3.5, 0.45), (4.5, 0.28)]),                   # sin main guardada: se calcula
        "total": _block([(9.5, 0.54), (10.5, 0.42)], main=9.5)}}
    return {"home_team": home, "away_team": away, "div": div, "kickoff_utc": ko,
            "stats": {"corners": stat, "goals": {"available": False}}}


def test_bloque_de_partido_por_equipo_y_total_de_referencia():
    txt = ts.match_block(_preview())
    assert txt.startswith("21:00 Ath Bilbao – Espanol")                     # hora de Madrid
    assert "  Córners: local +6,5: 47 % · visit. +3,5: 45 % (total +9,5: 54 %)" in txt
    assert "Goles" not in txt and "cuota" not in txt.lower()


def test_un_mensaje_por_liga_femenino_aparte_y_enlace_al_final():
    msgs = ts.build_messages([_preview(), _preview(div="E0", home="Chelsea", away="Leeds"),
                              _preview(div="WLF", home="Barcelona", away="Levante")], date(2026, 11, 20))
    assert len(msgs) == 3
    assert msgs[0].startswith("📊 Tendencias · viernes 20 nov · LaLiga")
    assert "Femenino · Liga F" in msgs[2]
    assert msgs[-1].endswith("https://tendencias-pi.pages.dev/#partidos/2026-11-20")
    assert all(len(m) <= 4096 for m in msgs)


def test_liga_larga_se_parte():
    many = [_preview(home=f"Equipo {i}") for i in range(40)]
    msgs = ts.build_messages(many, date(2026, 11, 21))
    assert len(msgs) > 1 and all(len(m) <= ts.MAX_LEN + 200 for m in msgs)
    assert "(sigue)" in msgs[1]


def test_sin_fichas_no_hay_mensajes():
    assert ts.build_messages([], date(2026, 11, 20)) == []


def test_resumen_a_las_ocho_utc_en_el_repartidor():
    t = ds.TASKS["trends-summary"]
    assert t.workflow == "trends-daily-summary.yml" and t.cron == "0 8 * * *"
