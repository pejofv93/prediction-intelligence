"""SONDA de Highlightly BALONCESTO para la ACB (antes de cargar hlb_games). Exactamente 3 peticiones:
  1. GET /leagues?leagueName=ACB           → ids, nombre de la liga, país y temporadas
  2. GET /matches?leagueId=&season=&limit=100&offset=0  (temporada SEASON o la anterior a la
     más reciente)                          → recuentos, nombres de campos, estados y formato
                                              del marcador (dígitos cambiados por 9)
  3. GET /statistics/{id} de un terminado  → nombres de campos de estadística por equipo
                                              (índice, sin nombre) y si vienen rellenos
De cada respuesta imprime el código HTTP y las cabeceras x-ratelimit-*. No escribe nada.
El log es público: NUNCA nombres de equipos, marcadores ni valores de estadísticas.
Datos de Highlightly: ni al repo ni a logs públicos.

env: HIGHLIGHTLY_API_KEY (la misma clave que fútbol) · SEASON (opcional) · LEAGUE_ID (opcional,
salta la elección de liga pero la petición 1 se hace igual para ver las temporadas).
"""
import os
import re
import sys
import time
from collections import Counter

import httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from collectors import highlightly_basket as hb  # noqa: E402


def ratelimit_headers(resp: httpx.Response) -> dict:
    return {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}


def data_of(raw) -> list:
    if isinstance(raw, dict):
        return raw.get("data") or []
    return raw if isinstance(raw, list) else []


def shape(s) -> str:
    """Formato sin cifras: '85 - 80' → '99 - 99'."""
    return re.sub(r"\d", "9", str(s)) if s is not None else "None"


def season_values(league: dict) -> list:
    out = []
    for x in league.get("seasons") or []:
        out.append(x.get("season") if isinstance(x, dict) else x)
    return out


def pick_league(leagues: list) -> dict:
    """La ACB española si se distingue por el país; si no, la primera."""
    for lg in leagues:
        c = lg.get("country") or {}
        if str(c.get("code") or "").upper() in ("ES", "ESP") or "spain" in str(c.get("name") or "").lower():
            return lg
    return leagues[0] if leagues else {}


def pick_season(seasons: list):
    """La anterior a la más reciente (temporada terminada, con partidos acabados seguro)."""
    vals = sorted((s for s in seasons if s is not None), key=lambda v: str(v))
    if not vals:
        return hb.current_season() - 1
    return vals[-2] if len(vals) >= 2 else vals[-1]


def get(client, key: str, path: str):
    t0 = time.monotonic()
    resp = client.get(hb.HLB_BASE + path, headers={"x-rapidapi-key": key})
    print(f"  HTTP {resp.status_code} · {(time.monotonic() - t0) * 1000:.0f} ms · {ratelimit_headers(resp)}")
    try:
        return resp.json() if resp.status_code == 200 else None
    except ValueError:
        print("  respuesta no JSON")
        return None


def main() -> int:
    key = os.environ.get("HIGHLIGHTLY_API_KEY")
    if not key:
        print("Falta HIGHLIGHTLY_API_KEY")
        return 1
    with httpx.Client(timeout=30) as client:
        print("1) /leagues?leagueName=ACB")
        leagues = data_of(get(client, key, "/leagues?leagueName=ACB"))
        print(f"  ligas: {len(leagues)}")
        for lg in leagues[:10]:
            c = lg.get("country") or {}
            print(f"   id {lg.get('id')} · {lg.get('name')} · país {c.get('code') or c.get('name')} · "
                  f"temporadas {season_values(lg)}")
        if leagues:
            print(f"  campos de una liga: {sorted(leagues[0])}")
        env_id = os.environ.get("LEAGUE_ID")
        league = pick_league(leagues)
        league_id = int(env_id) if env_id else league.get("id")
        if not league_id:
            print("Sin liga: no se hacen las peticiones 2 y 3.")
            return 1
        season = os.environ.get("SEASON") or pick_season(season_values(league))
        print(f"\n2) /matches?leagueId={league_id}&season={season}")
        raw = get(client, key, f"/matches?leagueId={league_id}&season={season}&limit=100&offset=0")
        matches = data_of(raw)
        pag = raw.get("pagination") if isinstance(raw, dict) else None
        print(f"  partidos en la página: {len(matches)} · paginación: {pag}")
        if matches:
            m0 = matches[0]
            print(f"  campos de un partido: {sorted(m0)}")
            print(f"  campos de state: {sorted((m0.get('state') or {}))}")
            print(f"  campos de state.score: {sorted(((m0.get('state') or {}).get('score') or {}))}")
            print(f"  campos de homeTeam: {sorted((m0.get('homeTeam') or {}))}")
            print(f"  round (formato): {shape(m0.get('round'))}")
        states = Counter((m.get("state") or {}).get("description") for m in matches)
        print(f"  estados: {dict(states)}")
        print(f"  formatos de marcador: {dict(Counter(shape(((m.get('state') or {}).get('score') or {}).get('current')) for m in matches))}")
        fin = [m for m in matches if hb.is_finished(m.get("state") or {})]
        print(f"  terminados según el colector: {len(fin)}")
        if not fin:
            print("Sin terminados: no se hace la petición 3.")
            return 1
        mid = fin[0].get("id")
        print(f"\n3) /statistics/{{id de un terminado}}")
        stats = data_of(get(client, key, f"/statistics/{mid}"))
        print(f"  equipos: {len(stats)}")
        if stats:
            print(f"  campos de un equipo: {sorted(stats[0])}")
        for i, t in enumerate(stats):
            fields = [(x.get("displayName"), x.get("value") not in (None, "", "-", "--"))
                      for x in t.get("statistics") or []]
            print(f"  equipo {i}: {len(fields)} campos, {sum(1 for _, f in fields if f)} rellenos")
            print("    " + ", ".join(f"{n}{'' if f else ' (vacío)'}" for n, f in fields))
            mapped = hb.map_stats({n: x.get("value") for n, x in
                                   ((x.get("displayName"), x) for x in t.get("statistics") or []) if n})
            print(f"    el colector reconoce: {sorted(k for k, v in mapped.items() if v is not None)} · "
                  f"no encuentra: {sorted(k for k, v in mapped.items() if v is None)}")
    print("\nSin escribir nada. Peticiones: 3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
