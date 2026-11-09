"""Búsqueda del id de un despliegue de Pages por su URL (para borrar uno fallido)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from deployment_id import find_id  # noqa: E402

LISTING = [
    {"Id": "aaaa1111-0000-0000-0000-000000000000", "Environment": "Production", "Branch": "main",
     "Deployment": "https://aaaa1111.tendencias-pi.pages.dev", "Status": "1 minute ago"},
    {"Id": "bbbb2222-0000-0000-0000-000000000000", "Environment": "Production", "Branch": "main",
     "Deployment": "https://bbbb2222.tendencias-pi.pages.dev", "Status": "5 minutes ago"},
]


def test_encuentra_el_id_por_la_url():
    assert find_id(LISTING, "https://bbbb2222.tendencias-pi.pages.dev") == "bbbb2222-0000-0000-0000-000000000000"
    assert find_id(LISTING, "https://bbbb2222.tendencias-pi.pages.dev/") == "bbbb2222-0000-0000-0000-000000000000"


def test_no_inventa_si_no_esta_o_la_salida_es_rara():
    assert find_id(LISTING, "https://cccc3333.tendencias-pi.pages.dev") == ""
    assert find_id({"error": "x"}, "https://aaaa1111.tendencias-pi.pages.dev") == ""
    assert find_id([{"url": "https://aaaa1111.tendencias-pi.pages.dev"}], "https://aaaa1111.tendencias-pi.pages.dev") == ""
