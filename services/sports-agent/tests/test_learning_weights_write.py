"""Aprendizaje nocturno: model_weights se escribe con precondición y, si no se guarda, las
predicciones NO se marcan como graduadas (la próxima ejecución las vuelve a graduar y sumar)."""
import learner.learning_engine as le


class _Ref:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def update(self, payload, option=None):
        self.calls.append(("update", payload, option))
        if self.fail:
            raise RuntimeError("FailedPrecondition: el documento cambió")

    def create(self, payload):
        self.calls.append(("create", payload))
        if self.fail:
            raise RuntimeError("AlreadyExists")


def _patch(monkeypatch, ref):
    class _Col:
        def document(self, name):
            assert name == "current"
            return ref

    monkeypatch.setattr(le, "col", lambda name: _Col())
    monkeypatch.setattr(le, "get_client", lambda: type("C", (), {
        "write_option": staticmethod(lambda last_update_time: ("precondicion", last_update_time))})())


def test_escribe_con_precondicion_sobre_la_lectura(monkeypatch):
    ref = _Ref()
    _patch(monkeypatch, ref)
    assert le._save_model_weights({"version": 2}, True, "t0") is True
    assert ref.calls == [("update", {"version": 2}, ("precondicion", "t0"))]


def test_si_otro_lo_cambio_entre_medias_no_escribe_y_avisa(monkeypatch):
    ref = _Ref(fail=True)
    _patch(monkeypatch, ref)
    assert le._save_model_weights({"version": 2}, True, "t0") is False


def test_documento_nuevo_se_crea(monkeypatch):
    ref = _Ref()
    _patch(monkeypatch, ref)
    assert le._save_model_weights({"version": 1}, False, None) is True
    assert ref.calls == [("create", {"version": 1})]


def test_si_la_lectura_fallo_no_se_escriben_defaults(monkeypatch):
    ref = _Ref()
    _patch(monkeypatch, ref)
    assert le._save_model_weights({"weights": "defaults"}, None, None) is False
    assert ref.calls == []


def test_solo_contadores_no_crea_un_documento_que_no_existe(monkeypatch):
    ref = _Ref()
    _patch(monkeypatch, ref)
    assert le._save_model_weights({"total_predictions": 1}, False, None, only_update=True) is False
    assert ref.calls == []


def test_sin_model_weights_no_se_marcan_ni_se_suma_al_log():
    hecho = []
    assert le._persist_grading(False, lambda: hecho.append("log"), lambda: hecho.append("marcar")) is False
    assert hecho == []


def test_con_model_weights_guardado_se_hace_log_y_se_marcan():
    hecho = []
    assert le._persist_grading(True, lambda: hecho.append("log"), lambda: hecho.append("marcar")) is True
    assert hecho == ["log", "marcar"]


def test_marcar_graduadas_escribe_el_mismo_payload_que_antes(monkeypatch):
    escritos = {}

    class _Doc:
        def __init__(self, name):
            self.name = name

        def update(self, payload):
            escritos[self.name] = payload

        def get(self):
            return type("S", (), {"exists": self.name == "m1_synthetic"})()

    monkeypatch.setattr(le, "col", lambda c: type("C", (), {"document": lambda s, n: _Doc(n)})())
    le._mark_predictions_graded([
        {"match_id": "m1", "result": "HOME", "correct": True, "error_type": None, "model_epoch": 2},
        {"match_id": "x", "_firestore_doc_id": "m2", "result": "AWAY", "correct": False,
         "error_type": "odds_inefficiency", "model_epoch": 1},
    ])
    assert escritos["m1"] == {"result": "HOME", "correct": True, "error_type": None, "model_epoch": 2}
    assert escritos["m1_synthetic"] == escritos["m1"]
    assert escritos["m2"]["excluded_from_learning"] is True and "x" not in escritos
