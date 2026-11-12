"""Plan de borrado de despliegues antiguos: se conserva solo el de producción más reciente."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from prune_plan import plan  # noqa: E402


def _dep(i, env="Production"):
    return {"Id": f"{i}-0000", "Environment": env, "Branch": "main",
            "Deployment": f"https://{i}.tendencias-pi.pages.dev", "Status": "x"}


def test_conserva_el_de_produccion_mas_reciente_y_borra_el_resto():
    keep, rest = plan([_dep("aaaa"), _dep("bbbb"), _dep("cccc", "Preview"), _dep("dddd")])
    assert keep == "aaaa-0000"
    assert [i for i, _ in rest] == ["bbbb-0000", "cccc-0000", "dddd-0000"]
    assert all(u.endswith(".tendencias-pi.pages.dev") for _, u in rest)


def test_si_el_primero_es_preview_conserva_la_primera_produccion():
    keep, rest = plan([_dep("pppp", "Preview"), _dep("aaaa"), _dep("bbbb")])
    assert keep == "aaaa-0000" and [i for i, _ in rest] == ["pppp-0000", "bbbb-0000"]


def test_sin_produccion_o_salida_rara_no_borra_nada():
    assert plan([_dep("pppp", "Preview")]) == ("", [])
    assert plan({"error": "x"}) == ("", [])
    assert plan([{"Id": "x"}]) == ("", [])


def test_solo_uno_no_borra_nada():
    assert plan([_dep("aaaa")]) == ("aaaa-0000", [])
