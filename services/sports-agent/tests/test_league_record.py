"""accuracy_by_league acumulado: aciertos/total por liga, None = sin datos, 0% real visible."""
from learner.learning_engine import _league_accuracy, _merge_league_record
from shared.report_generator import generate_weekly_report


def test_acumula_sobre_lo_anterior():
    rec = _merge_league_record({"SA": {"correct": 1, "total": 2}}, {"SA": [True, False], "PL": [], "PD": [False]})
    assert rec == {"SA": {"correct": 2, "total": 4}, "PL": {"correct": 0, "total": 0},
                   "PD": {"correct": 0, "total": 1}}
    assert _league_accuracy(rec) == {"SA": 0.5, "PL": None, "PD": 0.0}


def test_arranca_de_cero_y_ignora_basura():
    assert _merge_league_record({"_id": "x", "SA": None}, {"SA": [True]}) == {"SA": {"correct": 1, "total": 1}}


def _report(stats):
    return generate_weekly_report({"week": "2026-W44", **stats}, {}, {})


def test_informe_muestra_el_cero_real_y_oculta_sin_datos():
    txt = _report({"league_record": {"PL": {"correct": 0, "total": 3}, "PD": {"correct": 2, "total": 4},
                                     "SA": {"correct": 0, "total": 0}},
                   "accuracy_by_league": {"PL": 0.0, "PD": 0.5, "SA": None},
                   "league_record_since": "2026-10-26"})
    line = next(ln for ln in txt.splitlines() if "PL 0% (0/3)" in ln)
    assert "PD 50% (2/4)" in line and "SA" not in line
    assert line.endswith("(acumulado desde 26/10/2026)")


def test_informe_semana_antigua_sin_record_sigue_ocultando_ceros():
    txt = _report({"accuracy_by_league": {"PL": 0.0, "SA": 0.5}})
    assert "SA 50%" in txt and "PL 0%" not in txt and "acumulado" not in txt
