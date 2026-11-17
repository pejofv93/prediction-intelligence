"""Descarga de CSV de football-data.co.uk: reintenta 5xx y errores de red; no reintenta 404."""
import asyncio

import httpx

from collectors import fdco_team_matches as fd


class _Client:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    async def get(self, url):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return httpx.Response(a, text="Div,HomeTeam\nF1,Lyon\n" if a == 200 else "")


def _fetch(monkeypatch, answers):
    monkeypatch.setattr(fd, "_RETRY_DELAYS", (0.0, 0.0))
    c = _Client(answers)
    return asyncio.run(fd.fetch_csv_rows(c, "F1", "2627")), c.calls


def test_504_y_error_de_red_se_reintentan(monkeypatch):
    rows, calls = _fetch(monkeypatch, [504, httpx.ConnectTimeout("t"), 200])
    assert rows == [{"Div": "F1", "HomeTeam": "Lyon"}] and calls == 3


def test_tras_tres_fallos_devuelve_none(monkeypatch):
    assert _fetch(monkeypatch, [504, 502, 503]) == (None, 3)


def test_404_no_se_reintenta(monkeypatch):
    assert _fetch(monkeypatch, [404]) == (None, 1)
