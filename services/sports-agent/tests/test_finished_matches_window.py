"""get_finished_matches: football-data trata dateTo como exclusivo → cada tramo se pide hasta el
día siguiente a su último día, para que ayer (y el último día de cada tramo) no se pierdan."""
import asyncio
from datetime import date, datetime, timedelta, timezone

from collectors import football_api as fa


def test_tramos_piden_hasta_el_dia_siguiente_y_cubren_hasta_ayer(monkeypatch):
    paths = []

    async def fake_request(path):
        paths.append(path)
        return {"matches": []}

    monkeypatch.setattr(fa, "_request", fake_request)
    asyncio.run(fa.get_finished_matches(days_back=30))
    today = datetime.now(timezone.utc).date()
    spans = []
    for p in paths:
        q = dict(kv.split("=", 1) for kv in p.split("?", 1)[1].split("&"))
        f, t = date.fromisoformat(q["dateFrom"]), date.fromisoformat(q["dateTo"])
        assert (t - f).days <= 9                                    # dentro del límite del plan gratis
        spans.append((f, t))
    assert spans[0][1] == today                                     # ayer entero (dateTo exclusivo)
    covered = {f + timedelta(days=i) for f, t in spans for i in range((t - f).days)}
    want = {today - timedelta(days=i) for i in range(1, 31)}
    assert want <= covered                                          # ningún día perdido entre tramos
