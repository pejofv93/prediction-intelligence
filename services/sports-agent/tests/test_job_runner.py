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


def test_resumenes_de_analyze():
    f = job_runner.PublicFilter()
    for msg in ("analyze: iniciando pipeline",
                "analyze: 42 enriched (7d) de 80 upcoming SCHEDULED (get_all 42 refs)",
                "analyze: 3 senales generadas de 42 enriquecidos en 312.4s (5 partidos descartados "
                "por TIMING_GUARD: ya empezados/demasiado próximos)",
                "analyze: tenis — 2 señales generadas",
                "analyze: 7 partidos de baloncesto (48h) a analizar",
                "analyze: odds-check lanzado en telegram-bot"):
        r = _rec(msg)
        assert f.filter(r) and r.public == msg, msg
    for msg in ("analyze: TIMING_GUARD descarta 123 (Arsenal vs Chelsea) — inicio 20:00",
                "analyze: ligas en enriched=['PL'] | con señales extra=[] | sin cobertura=[]",
                "analyze: sport=basketball — skipping generate_signal loop (no es fútbol) [m1]"):
        assert not f.filter(_rec(msg)), msg
    flag = job_runner.FailFlag()
    flag.emit(_rec("analyze: error no controlado — boom", logging.ERROR))
    assert flag.failed


class _FakeBatch:
    def __init__(self, owner):
        self.owner = owner

    def log_text(self, *a, **k):
        pass

    def commit(self):
        self.owner.calls += 1
        if self.owner.calls <= self.owner.fail_first:
            raise RuntimeError("ResourceExhausted")


class _FakeLogger:
    def __init__(self, fail_first):
        self.calls, self.fail_first = 0, fail_first

    def batch(self):
        return _FakeBatch(self)


def _handler(fail_first):
    h = object.__new__(job_runner.CloudLoggingHandler)
    logging.Handler.__init__(h)
    h._logger, h._resource = _FakeLogger(fail_first), None
    h._fmt = logging.Formatter("%(message)s")
    h._reset()
    return h


def test_cloud_logging_reintenta_con_espera(monkeypatch):
    monkeypatch.setattr(job_runner.time, "sleep", lambda s: None)
    h = _handler(fail_first=2)                       # falla 2 veces y luego escribe
    h._buf = [("x", "INFO")] * 150                   # 8 lotes de 20 (el último de 10)
    h.flush()
    assert not h.broken and h.lost == 0 and h.written == 150 and h._logger.calls == 2 + 8
    assert h.codes == {"RuntimeError": 2}


def test_cloud_logging_lote_fallido_se_reintenta_linea_a_linea(monkeypatch):
    monkeypatch.setattr(job_runner.time, "sleep", lambda s: None)
    h = _handler(fail_first=4)                       # el primer lote agota los reintentos
    h._buf = [("x", "INFO")] * 150
    h.flush()
    # 4 intentos del lote + 20 líneas sueltas + 7 lotes restantes; no se pierde nada
    assert not h.broken and h.lost == 0 and h.written == 150 and h._logger.calls == 4 + 20 + 7


def test_cloud_logging_pausa_entre_lotes_y_lotes_por_tamano(monkeypatch):
    waits = []
    monkeypatch.setattr(job_runner.time, "sleep", waits.append)
    h = _handler(fail_first=0)
    big = "y" * 120_000
    h._buf = [(big, "INFO"), (big, "INFO"), ("x", "INFO")]   # 200 KB por lote: 2 lotes
    h.flush()
    assert h._logger.calls == 2 and waits == [h.PAUSE_S]


def test_cloud_logging_recorta_lineas_enormes():
    h = _handler(fail_first=0)
    h.emit(logging.LogRecord("m", logging.INFO, __file__, 1, "z" * 80_000, None, None))
    assert h.truncated == 1 and len(h._buf[0][0]) < 50_100
    assert "recortada" in h._buf[0][0]
    assert "1 recortadas" in h.report()


class _BadLineBatch(_FakeBatch):
    def __init__(self, owner):
        super().__init__(owner)
        self.items = []

    def log_text(self, text, **k):
        self.items.append(text)

    def commit(self):
        self.owner.calls += 1
        if "MALA" in self.items:
            raise RuntimeError("ResourceExhausted")


def test_cloud_logging_solo_pierde_la_linea_que_falla(monkeypatch):
    monkeypatch.setattr(job_runner.time, "sleep", lambda s: None)
    h = _handler(fail_first=0)
    h._logger.batch = lambda: _BadLineBatch(h._logger)
    h._buf = [("x", "INFO")] * 10 + [("MALA", "INFO")] + [("x", "INFO")] * 9
    h.flush()
    assert h.lost == 1 and h.written == 19 and not h.broken and h.max_lost_chars == 4
    assert "1 sin escribir" in h.report()


class _FakeCode:
    def __init__(self, name):
        self.name = name


class _ApiError(Exception):
    def __init__(self, code_name, reason, text="Arsenal sin permiso"):
        super().__init__(text)
        self.grpc_status_code, self.reason = _FakeCode(code_name), reason


def test_aviso_con_codigo_y_motivo_sin_texto():
    d = job_runner.describe_api_error(_ApiError("PERMISSION_DENIED", "IAM_PERMISSION_DENIED"))
    assert d == "_ApiError, código PERMISSION_DENIED, motivo IAM_PERMISSION_DENIED"
    d = job_runner.describe_api_error(_ApiError("PERMISSION_DENIED", "algo con Arsenal"))
    assert "Arsenal" not in d and d.endswith("sin motivo")


class _DeniedBatch(_FakeBatch):
    def commit(self):
        self.owner.calls += 1
        raise _ApiError("PERMISSION_DENIED", "IAM_PERMISSION_DENIED")


def test_permiso_denegado_no_reintenta(monkeypatch, capsys):
    waits = []
    monkeypatch.setattr(job_runner.time, "sleep", waits.append)
    h = _handler(fail_first=0)
    h._logger.batch = lambda: _DeniedBatch(h._logger)
    h._buf = [("x", "INFO")] * 250
    h.flush()
    out = capsys.readouterr().out
    assert h.broken and h.lost == 250 and h._logger.calls == 1 and not waits
    assert "código PERMISSION_DENIED, motivo IAM_PERMISSION_DENIED" in out
