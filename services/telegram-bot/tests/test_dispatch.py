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
    assert {"sports-analyze", "daily-report", "polymarket-scan", "oracle-a1-retry",
            "sports-fdco-collect"} <= names
    assert "sports-collect" not in names                      # 06:00, ya fuera del margen de 60 min
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
    assert per_day("sports-analyze") == 4 and per_day("sports-collect") == 4
    assert per_day("match-previews") == 2 and per_day("hl-load-daily") == 2
    assert per_day("oracle-a1-retry") == 96
    assert per_day("polymarket-scan") == 4 and per_day("polymarket-analyze") == 3
    assert per_day("poly-ws-keepalive") == 4


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


@pytest.fixture
def env(monkeypatch):
    colls = {"live": _Coll(), "dry": _Coll()}
    fired = []
    monkeypatch.setattr(dp, "_claims", lambda mode: colls["live" if mode == "live" else "dry"])
    monkeypatch.setattr(dp, "_prune", lambda coll, now: 0)
    monkeypatch.setattr(dp, "_heartbeat", lambda now, source: {source: 1})
    monkeypatch.setattr(ds, "TASKS", {"a": ds.Task("a.yml", "0 7 * * *"),
                                      "b": ds.Task("b.yml", "0 7 * * *", inputs={"programado": "true"})})
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "t")
    state = {"ok": True}

    def fake_fire(workflow, inputs, token):
        fired.append((workflow, dict(inputs)))
        return state["ok"], "HTTP 204" if state["ok"] else "HTTP 500"

    monkeypatch.setattr(dp, "_fire", fake_fire)
    return colls, fired, state


def test_lanza_una_vez_por_franja_aunque_llamen_dos_relojes(env):
    colls, fired, _ = env
    r1 = dp.run_dispatch(T(16, 7, 2), mode="live")
    r2 = dp.run_dispatch(T(16, 7, 7), mode="live")            # el reloj de Cloudflare 5 min después
    r3 = dp.run_dispatch(T(16, 7, 7), mode="live")            # respaldo de GitHub a la vez
    assert sorted(r1["lanzadas"]) == ["a@07:00", "b@07:00"]
    assert r2["lanzadas"] == [] and r2["ya_lanzadas"] == 2 and r3["ya_lanzadas"] == 2
    assert fired == [("a.yml", {}), ("b.yml", {"programado": "true"})]
    assert all(d["status"] == "fired" for d in colls["live"].store.values())


def test_si_github_falla_se_reintenta_en_la_siguiente_llamada(env):
    colls, fired, state = env
    state["ok"] = False
    r1 = dp.run_dispatch(T(16, 7, 2), mode="live")
    assert r1["lanzadas"] == [] and len(r1["fallidas"]) == 2 and colls["live"].store == {}
    state["ok"] = True
    r2 = dp.run_dispatch(T(16, 7, 7), mode="live")
    assert sorted(r2["lanzadas"]) == ["a@07:00", "b@07:00"]


def test_dry_run_no_llama_a_github_ni_toca_las_reclamaciones_reales(env):
    colls, fired, _ = env
    r = dp.run_dispatch(T(16, 7, 2), mode="dry_run")
    assert sorted(r["lanzadas"]) == ["a@07:00", "b@07:00"] and fired == []
    assert colls["live"].store == {} and len(colls["dry"].store) == 2


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


def test_modo_por_defecto_es_dry_run():
    assert ds.DISPATCH_MODE == "dry_run"
