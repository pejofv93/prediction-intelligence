"""Aviso de cuota: va a /send-alert (type "matched", texto plano) con el token
inter-servicios (antes iba a /send, que el bot no tiene: 404), y sale una sola vez
por API y periodo gracias a un doc en Firestore creado solo si no existe."""
import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for _mod in ("google", "google.cloud", "google.cloud.firestore", "google.cloud.firestore_v1",
             "google.cloud.firestore_v1.base_query", "google.api_core",
             "google.api_core.exceptions", "grpc"):
    sys.modules.setdefault(_mod, MagicMock())

import shared.api_quota_manager as aqm  # noqa: E402
import shared.config as cfg  # noqa: E402

KEY = "the_odds_api_2026-11_monthly"


class AlreadyExists(Exception):
    """Mismo nombre que google.api_core.exceptions.AlreadyExists."""


class _FakeFirestore:
    """Colecciones en memoria con la semántica de create(): falla si el doc existe."""

    def __init__(self, broken=False):
        self.docs, self.broken = {}, broken

    def col(self, name):
        fs = self

        class _Doc:
            def __init__(self, doc_id):
                self.path = (name, doc_id)

            def create(self, data):
                if fs.broken:
                    raise RuntimeError("Firestore caído")
                if self.path in fs.docs:
                    raise AlreadyExists("409 Document already exists")
                fs.docs[self.path] = dict(data)

            def delete(self):
                fs.docs.pop(self.path, None)

        return MagicMock(document=_Doc)


def _send(fs, n=1, status=200, key=KEY):
    """n llamadas, cada una con un QuotaManager nuevo (= arranque en frío)."""
    calls = []

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, headers=None):
            calls.append((url, json, headers))
            return MagicMock(status_code=status, text="")

    with patch.object(cfg, "TELEGRAM_BOT_URL", "http://bot"), \
         patch.object(cfg, "CLOUD_RUN_TOKEN", "tok"), \
         patch.object(aqm.httpx, "AsyncClient", _Client):
        for _ in range(n):
            qm = aqm.QuotaManager()
            qm._col_fn = fs.col
            asyncio.run(qm._send_alert("the_odds_api", 450, 500, 90, "mes", key))
    return calls


def test_va_a_send_alert_matched_con_token():
    (url, body, headers), = _send(_FakeFirestore())
    assert url == "http://bot/send-alert"
    assert body["type"] == "matched"
    assert "the_odds_api al 90%" in body["data"]["text"] and "*" not in body["data"]["text"]
    assert headers == {"x-cloud-token": "tok"}


def test_dos_llamadas_mismo_api_y_periodo_envian_una_vez():
    fs = _FakeFirestore()
    assert len(_send(fs, n=2)) == 1
    assert ("api_quota_alerts", KEY) in fs.docs


def test_otro_periodo_si_avisa():
    fs = _FakeFirestore()
    _send(fs)
    assert len(_send(fs, key="the_odds_api_2026-12_monthly")) == 1


def test_firestore_caido_no_avisa():
    assert _send(_FakeFirestore(broken=True)) == []


def test_si_el_bot_no_envia_se_libera_y_el_siguiente_avisa():
    fs = _FakeFirestore()
    assert len(_send(fs, status=502)) == 1
    assert fs.docs == {}
    assert len(_send(fs)) == 1
