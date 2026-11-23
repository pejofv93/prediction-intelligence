"""job_runner: al log público (Actions) solo salen resúmenes con recuentos; de avisos y errores,
el módulo, el nivel y la clase de la excepción, nunca el mensaje."""
import logging

import job_runner


def _rec(msg, level=logging.INFO, name="main", exc=None):
    r = logging.LogRecord(name, level, __file__, 1, msg, None, exc)
    return r


def test_resumenes_pasan_y_lo_demas_no():
    f = job_runner.PublicFilter()
    for msg in ("collect: iniciando pipeline", "collect: completado en 1520.3s",
                "collect.football: 105 partidos guardados",
                "collect.standings: sin datos para SA (id=2019)",
                "collect.football: equipos por motivo {'caducado': 12, 'nuevo': 3}",
                "get_finished_matches: 377 partidos terminados",
                "RESULTS_UPDATE: 377 partidos escritos en match_results"[:46]):
        r = _rec(msg)
        assert f.filter(r) and r.public == msg, msg
    for msg in ("generate_signal(m1): Arsenal @ 2.10", "collect.other_sports: obteniendo partidos de NBA",
                "collect.uefa: Real Madrid vs Liverpool 2-1"):
        assert not f.filter(_rec(msg)), msg


def test_errores_sin_mensaje_y_avisos_solo_contados():
    f = job_runner.PublicFilter()
    try:
        raise ValueError("Arsenal")
    except ValueError:
        import sys
        r = _rec("fallo con Arsenal", logging.ERROR, "collectors.x", sys.exc_info())
    assert f.filter(r) and r.public == "ERROR en collectors.x (ValueError)"
    assert "Arsenal" not in job_runner.PublicFormatter().format(r)
    w = _rec("cuota rara de Arsenal", logging.WARNING, "analyzers.y")
    assert not f.filter(w)
    assert f.counts[("WARNING", "analyzers.y")] == 1 and f.counts[("ERROR", "collectors.x")] == 1


def test_marca_de_fallo():
    flag = job_runner.FailFlag()
    flag.emit(_rec("collect: error no controlado — boom", logging.ERROR))
    assert flag.failed


def test_trabajo_desconocido():
    assert job_runner.main(["job_runner.py", "nada"]) == 2
