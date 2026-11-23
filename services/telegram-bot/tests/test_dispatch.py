"""Repartidor de pasadas programadas: horario (shared/dispatch_schedule) y dispatcher."""
from datetime import datetime, timedelta, timezone

import pytest

import dispatcher as dp
from shared import dispatch_schedule as ds

UTC = timezone.utc


def T(d, h, m=0):
    return datetime(2026, 11, d, h, m, tzinfo=UTC)          # 16-nov-2026 es lunes


# ── horario ──────────────────────────────────────────────────────────────────────

def test_cron_basico():
    assert ds.matches("0 1,7,13,19 * * *", T(16, 13))
    assert not ds.matches("0 1,7,13,19 * * *", T(16, 13, 1))
    assert ds.matches("*/15 * * * *", T(16, 9, 45)) and not ds.matches("*/15 * * * *", T(16, 9, 50))
    assert ds.matches("20 */2 * * *", T(16, 4, 20)) and not ds.matches("20 */2 * * *", T(16, 5, 20))
    assert ds.matches("0 8 * * 1", T(16, 8)) and not ds.matches("0 8 * * 1", T(17, 8))   # lunes
    assert ds.matches("0 8 1 * *", datetime(2026, 12, 1, 8, tzinfo=UTC))


def test_franja_vigente_y_retraso_maximo():
    t = ds.Task("x.yml", "0 7 * * *", max_late_min=60)
    assert ds.latest_slot(t, T(16, 7, 0)) == T(16, 7)
    assert ds.latest_slot(t, T(16, 7, 59)) == T(16, 7)
    assert ds.latest_slot(t, T(16, 8, 1)) is None            # pasada de una hora: se salta
    assert ds.latest_slot(t, T(16, 6, 59)) is None


def test_que_toca_a_las_siete_de_un_lunes():
    names = {n for n, _ in ds.due(T(16, 7, 2))}
    assert {"sports-analyze", "daily-report", "oracle-a1-retry",
            "sports-fdco-collect"} <= names
    assert not any(n.startswith("poly") for n in names)       # Polymarket en pausa (23-nov)
    assert "polymarket-resolve" in {n for n, _ in ds.due(T(16, 3, 2))}   # salvo la resolución
    assert "sports-collect" not in names                      # 05:30, ya fuera del margen de 60 min
    assert "weekly-report" not in names                       # es a las 08:00
    assert "weekly-report" in {n for n, _ in ds.due(T(16, 8, 3))}


def test_frecuencias_acordadas():
    def per_day(name):
        task = ds.TASKS[name]
        t, n = T(17, 0), 0
        while t < T(18, 0):
            n += ds.matches(task.cron, t)
            t += timedelta(minutes=1)
        return n
    assert per_day("sports-analyze") == 4
    assert per_day("sports-collect") == 2 and per_day("sports-enrich") == 2     # ritmo 17-nov
    assert ds.matches(ds.TASKS["sports-collect"].cron, T(17, 5, 30))
    assert ds.matches(ds.TASKS["sports-enrich"].cron, T(17, 18, 20))
    assert "hl-load-auto" not in ds.TASKS                                      # apagada, conserva su cron
    t = ds.TASKS["eur-tennis-daily"]                                          # fase 2 Euroliga + tenis
    assert t.workflow == "sports-eur-tennis-daily.yml" and ds.matches(t.cron, T(21, 6, 30))
    assert per_day("match-previews") == 2 and per_day("hl-load-daily") == 2
    assert per_day("oracle-a1-retry") == 96
    # Polymarket en pausa (23-nov-2026): fuera de TASKS, conservado en PAUSED_TASKS
    assert {n for n, t in ds.TASKS.items() if t.workflow.startswith("poly")} == {"polymarket-resolve"}
    assert per_day("polymarket-resolve") == 1
    assert len(ds.PAUSED_TASKS) == 8 and "polymarket-analyze" in ds.PAUSED_TASKS


def test_retraso_maximo_cubre_el_intervalo_del_reloj():
    # con el reloj cada 5 min, cada franja tiene al menos una llamada dentro del margen
    assert all(t.max_late_min >= 10 for t in ds.TASKS.values())
    assert ds.TASKS["oracle-a1-retry"].max_late_min < 15       # no se pisa con la siguiente


def test_los_que_distinguen_el_cron_reciben_programado():
    for n in ("rebuild-elo", "match-previews", "tier-review"):
        assert ds.TASKS[n].inputs == {"programado": "true"}


# ── repartidor ───────────────────────────────────────────────────────────────────

class AlreadyExists(Exception):
    """Mismo nombre que google.api_core.exceptions.AlreadyExists."""


class _Doc:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def create(self, data):
        if self.key in self.store:
            raise AlreadyExists("ya")
        self.store[self.key] = dict(data)

    def update(self, data):
        self.store[self.key].update(data)

    def delete(self):
        self.store.pop(self.key, None)


class _Coll:
    def __init__(self):
        self.store = {}

    def document(self, key):
        return _Doc(self.store, key)


def _slots(coll):
    """Reclamaciones de franja (sin el marcador de inicio del modo live)."""
    return {k: v for k, v in coll.store.items() if k != "_inicio_live"}


@pytest.fixture
def env(monkeypatch):
    colls = {"live": _Coll(), "dry": _Coll()}
    colls["live"].store["_inicio_live"] = {"inicio": T(1, 0)}   # ya en live; la siembra se prueba aparte
    fired = []
    monkeypatch.setattr(dp, "_claims", lambda mode: colls["live" if mode == "live" else "dry"])
    monkeypatch.setattr(dp, "_prune", lambda coll, now: 0)
    monkeypatch.setattr(dp, "_heartbeat", lambda now, source: {source: 1})
    alerts = _Coll()
    monkeypatch.setattr(dp, "_alerts", lambda: alerts)
    monkeypatch.setattr(ds, "TASKS", {"a": ds.Task("a.yml", "0 7 * * *"),
                                      "b": ds.Task("b.yml", "0 7 * * *", inputs={"programado": "true"})})
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "t")
    state = {"ok": True}

    def fake_fire(workflow, inputs, token):
        fired.append((workflow, dict(inputs)))
        if state.get("disabled"):
            return False, dp.DISABLED
        return state["ok"], "HTTP 204" if state["ok"] else "HTTP 500"

    monkeypatch.setattr(dp, "_fire", fake_fire)
    state["alerts"] = alerts
    return colls, fired, state


def test_lanza_una_vez_por_franja_aunque_llamen_dos_relojes(env):
    colls, fired, _ = env
    r1 = dp.run_dispatch(T(16, 7, 2), mode="live")
    r2 = dp.run_dispatch(T(16, 7, 7), mode="live")            # el reloj de Cloudflare 5 min después
    r3 = dp.run_dispatch(T(16, 7, 7), mode="live")            # respaldo de GitHub a la vez
    assert sorted(r1["lanzadas"]) == ["a@07:00", "b@07:00"]
    assert r2["lanzadas"] == [] and r2["ya_lanzadas"] == 2 and r3["ya_lanzadas"] == 2
    assert fired == [("a.yml", {}), ("b.yml", {"programado": "true"})]
    assert all(d["status"] == "fired" for k, d in colls["live"].store.items() if k != "_inicio_live")


def test_si_github_falla_se_reintenta_en_la_siguiente_llamada(env):
    colls, fired, state = env
    state["ok"] = False
    r1 = dp.run_dispatch(T(16, 7, 2), mode="live")
    assert r1["lanzadas"] == [] and len(r1["fallidas"]) == 2 and _slots(colls["live"]) == {}
    state["ok"] = True
    r2 = dp.run_dispatch(T(16, 7, 7), mode="live")
    assert sorted(r2["lanzadas"]) == ["a@07:00", "b@07:00"]


def test_dry_run_no_llama_a_github_ni_toca_las_reclamaciones_reales(env):
    colls, fired, _ = env
    r = dp.run_dispatch(T(16, 7, 2), mode="dry_run")
    assert sorted(r["lanzadas"]) == ["a@07:00", "b@07:00"] and fired == []
    assert _slots(colls["live"]) == {} and len(colls["dry"].store) == 2


def test_primera_llamada_live_siembra_sin_lanzar(env):
    colls, fired, _ = env
    del colls["live"].store["_inicio_live"]                    # primera vez en live
    r1 = dp.run_dispatch(T(16, 7, 2), mode="live")            # justo tras el cambio a live
    assert r1["lanzadas"] == [] and sorted(r1["sembradas"]) == ["a@07:00", "b@07:00"]
    assert fired == []
    r2 = dp.run_dispatch(T(16, 7, 7), mode="live")            # no repite las sembradas
    assert r2["lanzadas"] == [] and r2["ya_lanzadas"] == 2 and "sembradas" not in r2
    r3 = dp.run_dispatch(T(17, 7, 2), mode="live")            # la franja siguiente sí se lanza
    assert sorted(r3["lanzadas"]) == ["a@07:00", "b@07:00"] and len(fired) == 2
    assert colls["live"].store["_inicio_live"]["inicio"] == T(16, 7, 2)


def test_live_sin_token_no_lanza(env, monkeypatch):
    _, fired, _ = env
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN")
    r = dp.run_dispatch(T(16, 7, 2), mode="live")
    assert r["error"] == "falta GITHUB_DISPATCH_TOKEN" and fired == []


def test_origen_de_la_llamada():
    assert dp.source_of("reloj-cloudflare/1") == "cloudflare"
    assert dp.source_of("Google-Cloud-Scheduler") == "otro"
    assert dp.source_of("curl/8.5.0") == "respaldo"
    assert dp.source_of("Mozilla/5.0") == "otro" and dp.source_of("") == "otro"


def test_recuento_de_llamadas_de_la_ultima_hora():
    now = T(16, 11, 7)
    by_hour = {T(16, 10): ["05:cloudflare", "10:cloudflare", "55:cloudflare", "07:respaldo"],
               T(16, 11): ["00:cloudflare", "05:cloudflare", "07:respaldo"]}
    # 10:05 y 10:07 quedan fuera (más de 60 min); 10:10, 10:55, 11:00, 11:05 → 4 del reloj
    assert dp.count_calls(by_hour, now) == {"cloudflare": 4, "respaldo": 1}


def test_la_respuesta_lleva_origen_y_latidos(env):
    r = dp.run_dispatch(T(16, 7, 2), mode="dry_run", source="cloudflare")
    assert r["origen"] == "cloudflare" and r["llamadas_ultima_hora"] == {"cloudflare": 1}


def test_modo_activo_es_live():
    assert ds.DISPATCH_MODE == "live"


def test_workflow_desactivado_avisa_una_vez_y_no_reintenta(env):
    colls, fired, state = env
    state["disabled"] = True
    r1 = dp.run_dispatch(T(16, 7, 2), mode="live")
    assert sorted(r1["desactivadas"]) == ["a@07:00", "b@07:00"] and r1["fallidas"] == []
    assert len(r1["avisos"]) == 2 and "a.yml" in r1["avisos"][0] and "una sola vez" in r1["avisos"][0]
    r2 = dp.run_dispatch(T(16, 7, 7), mode="live")            # misma franja: ya cerrada, sin llamar
    assert r2["ya_lanzadas"] == 2 and len(fired) == 2 and r2["avisos"] == []
    r3 = dp.run_dispatch(T(17, 7, 2), mode="live")            # franja siguiente: sin aviso repetido
    assert len(r3["desactivadas"]) == 2 and r3["avisos"] == []
    state["disabled"] = False                                  # se reactiva → se rearma el aviso
    r4 = dp.run_dispatch(T(18, 7, 2), mode="live")
    assert len(r4["lanzadas"]) == 2 and state["alerts"].store == {}


def test_fire_422_se_confirma_con_el_estado_del_workflow(monkeypatch):
    class R:
        def __init__(self, code, js=None):
            self.status_code, self._js = code, js or {}
        def json(self):
            return self._js
    state = {"wf": "disabled_manually"}
    monkeypatch.setattr(dp.httpx, "post", lambda *a, **k: R(422))
    monkeypatch.setattr(dp.httpx, "get", lambda *a, **k: R(200, {"state": state["wf"]}))
    assert dp._fire("x.yml", {}, "t") == (False, dp.DISABLED)
    state["wf"] = "active"                     # 422 por otra cosa (p. ej. input inválido)
    assert dp._fire("x.yml", {}, "t") == (False, "HTTP 422")
    monkeypatch.setattr(dp.httpx, "post", lambda *a, **k: R(204))
    assert dp._fire("x.yml", {}, "t") == (True, "HTTP 204")
