"""hlb_acb_probe: 3 peticiones exactas y un log sin equipos, marcadores ni valores. Sin red."""
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT))
import hlb_acb_probe as probe  # noqa: E402


def test_tres_peticiones_y_log_limpio(monkeypatch, capsys):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        assert req.headers["x-rapidapi-key"] == "clave-falsa"
        h = {"x-ratelimit-requests-remaining": "97", "x-ratelimit-requests-limit": "100"}
        if req.url.path == "/leagues":
            return httpx.Response(200, headers=h, json={"data": [
                {"id": 1111, "name": "ACB", "country": {"code": "XX", "name": "Otro"}, "seasons": [{"season": 2024}]},
                {"id": 2222, "name": "ACB", "country": {"code": "ES", "name": "Spain"},
                 "seasons": [{"season": 2024}, {"season": 2025}, {"season": 2026}]}]})
        if req.url.path == "/matches":
            assert req.url.params["leagueId"] == "2222" and req.url.params["season"] == "2025"
            return httpx.Response(200, headers=h, json={"data": [
                {"id": 77, "round": "Round 3", "homeTeam": {"id": 5, "name": "Equipo Secreto Uno"},
                 "awayTeam": {"id": 6, "name": "Equipo Secreto Dos"},
                 "state": {"description": "Finished", "score": {"current": "5871 - 5819"}}}],
                "pagination": {"totalCount": 306}})
        return httpx.Response(200, headers=h, json=[
            {"team": {"id": 5, "name": "Equipo Secreto Uno"},
             "statistics": [{"displayName": "Assists", "value": 4321}, {"displayName": "Steals", "value": None}]},
            {"team": {"id": 6, "name": "Equipo Secreto Dos"},
             "statistics": [{"displayName": "Assists", "value": 4312}]}])

    real = httpx.Client
    monkeypatch.setattr(probe.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("HIGHLIGHTLY_API_KEY", "clave-falsa")
    monkeypatch.delenv("SEASON", raising=False)
    monkeypatch.delenv("LEAGUE_ID", raising=False)
    assert probe.main() == 0
    assert calls == ["/leagues", "/matches", "/statistics/77"]
    out = capsys.readouterr().out
    assert "2222" in out and "x-ratelimit-requests-remaining" in out and "Steals (vacío)" in out
    assert "9999 - 9999" in out and "terminados según el colector: 1" in out
    for secret in ("Secreto", "5871", "5819", "4321", "4312", "clave-falsa"):
        assert secret not in out
