"""
Diagnóstico de cobertura de odds-api.io para baloncesto y tenis.

Uso:
  # Con key en env:
  ODDSAPIIO_KEY=xxx python scripts/diagnose_oddsapiio.py

  # Desde el endpoint del servicio (key ya en Cloud Run):
  curl -s -H "X-Cloud-Token: $CLOUD_RUN_TOKEN" \
    https://<sports-agent-url>/api/oddsapiio-coverage | python -m json.tool
"""
import asyncio
import json
import os
import sys

import httpx

_BASE = "https://api.odds-api.io/v3"
_KEY = os.environ.get("ODDSAPIIO_KEY", "")


async def main() -> None:
    if not _KEY:
        print("ERROR: ODDSAPIIO_KEY no configurada. Exportar antes de ejecutar.")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=20.0) as cli:

        # ── 1. Catálogo de deportes (no requiere auth) ──────────────────────────
        print("\n=== 1. SPORTS CATALOG ===")
        r = await cli.get(f"{_BASE}/sports")
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            raw = r.json()
            sports = raw if isinstance(raw, list) else raw.get("data", raw.get("sports", []))
            slugs = [s.get("slug") or s.get("key") or s.get("id") for s in sports]
            print(f"  Total deportes: {len(slugs)}")
            print(f"  Slugs: {slugs}")
        else:
            print(f"  Error: {r.text[:300]}")

        # ── 2. Baloncesto ───────────────────────────────────────────────────────
        print("\n=== 2. BASKETBALL ===")
        bball_events = []
        for slug in ("basketball", "nba", "basketball_nba", "usa-nba"):
            r = await cli.get(f"{_BASE}/events", params={"apiKey": _KEY, "sport": slug})
            print(f"  slug={slug!r} → HTTP {r.status_code}")
            if r.status_code == 429:
                print(f"    Rate limit. Body: {r.text[:200]}")
                break
            if r.status_code != 200:
                print(f"    Body: {r.text[:150]}")
                continue
            raw = r.json()
            events = raw if isinstance(raw, list) else raw.get("data", raw.get("events", []))
            print(f"    Eventos: {len(events)}")
            if events:
                leagues = list({
                    str((ev.get("league") or {}).get("name") or
                        ev.get("competitionName") or ev.get("tournament") or
                        ev.get("sport_title") or "?")
                    for ev in events[:30]
                })
                print(f"    Ligas/torneos detectados: {sorted(leagues)}")
                print(f"    Campos del primer evento: {list(events[0].keys())}")
                bball_events = events
                break

        # ── 3. Odds baloncesto de muestra ───────────────────────────────────────
        if bball_events:
            print("\n=== 3. BASKETBALL ODDS SAMPLE ===")
            eid = str(bball_events[0].get("id") or bball_events[0].get("eventId") or "")
            ev0 = bball_events[0]
            home = ev0.get("homeTeam") or ev0.get("home_team") or ev0.get("home") or "?"
            away = ev0.get("awayTeam") or ev0.get("away_team") or ev0.get("away") or "?"
            print(f"  Partido: {home} vs {away} (id={eid})")
            if eid:
                r = await cli.get(f"{_BASE}/odds/multi", params={
                    "apiKey": _KEY,
                    "eventIds": eid,
                    "bookmakers": "Bet365,Unibet",
                    "markets": "h2h,totals,spreads,btts",
                })
                print(f"  /odds/multi → HTTP {r.status_code}")
                if r.status_code == 200:
                    raw = r.json()
                    items = raw if isinstance(raw, list) else raw.get("data", raw.get("odds", []))
                    print(f"  Items de odds: {len(items)}")
                    print(f"  Respuesta completa:\n{json.dumps(items[:1], indent=2, default=str)}")
                else:
                    print(f"  Error: {r.text[:400]}")

        # ── 4. Tenis ────────────────────────────────────────────────────────────
        print("\n=== 4. TENNIS ===")
        for slug in ("tennis", "tennis_atp", "tennis-atp", "atp"):
            r = await cli.get(f"{_BASE}/events", params={"apiKey": _KEY, "sport": slug})
            print(f"  slug={slug!r} → HTTP {r.status_code}")
            if r.status_code == 429:
                print(f"    Rate limit. Body: {r.text[:200]}")
                break
            if r.status_code != 200:
                print(f"    Body: {r.text[:150]}")
                continue
            raw = r.json()
            events = raw if isinstance(raw, list) else raw.get("data", raw.get("events", []))
            print(f"    Eventos: {len(events)}")
            if events:
                leagues = list({
                    str((ev.get("league") or {}).get("name") or
                        ev.get("competitionName") or ev.get("tournament") or
                        ev.get("sport_title") or "?")
                    for ev in events[:30]
                })
                print(f"    Torneos detectados: {sorted(leagues)}")
                print(f"    Campos del primer evento: {list(events[0].keys())}")
                break

    print("\n=== FIN ===")


if __name__ == "__main__":
    asyncio.run(main())
