"""SOLO LECTURA. Fase 0 de baloncesto (Euroliga) y tenis en Tendencias.

Comprueba, sin escribir nada:
  Euroliga (feed incrowdsports + live.euroleague.net, sin clave):
    1. feed E2024/E2025/E2026: partidos, terminados y con prórroga;
    2. boxscore de un partido con prórroga (Minutes 225, ot_periods 1 cuadra con el feed),
       uno de E2024 y uno sin jugar de E2026 (debe venir vacío o sin totales);
    3. que el gamecode del feed abre el boxscore correcto (los puntos cuadran).
  Tenis (tennis-data.co.uk):
    4. alldata.php → enlaces .xlsx y carpeta aleatoria actual;
    5. xlsx ATP y WTA del año en curso: filas, última fecha, estados, sets incoherentes;
    6. nombres de upcoming_matches de tenis (Firestore, solo lectura) que emparejan con
       «Apellido I.» de tennis-data (objetivo ≥90 %).
Peticiones: feed ≤7 páginas + 3 boxscores + alldata.php + 2 xlsx ≈ 13. 0 de Highlightly.
El log es público: solo agregados (recuentos y porcentajes, sin nombres ni cuotas).
"""
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from collectors import euroleague_boxscores as eb  # noqa: E402
from collectors import tennis_data as td  # noqa: E402


def euroleague() -> bool:
    ok = True
    seasons = eb.default_seasons()
    print(f"\n== Euroliga: temporadas {seasons}")
    feeds = {}
    for season in seasons:
        games = eb._fetch_eur_season_games(season)
        feeds[season] = games
        done = eb.finished(games)
        ot = [g for g in done if eb.ot_from_feed(g)]
        phases = Counter((g.get("phaseType") or {}).get("code") for g in games)
        print(f"  {season}: {len(games)} partidos en el feed, {len(done)} terminados, "
              f"{len(ot)} con prórroga; fases {dict(phases)}")

    checks = []
    prev, cur = seasons[1], seasons[2]
    ot_games = [g for g in eb.finished(feeds[prev]) if eb.ot_from_feed(g)]
    if ot_games:
        checks.append(("prórroga " + prev, prev, ot_games[0], True))
    else:
        print(f"  AVISO: sin partidos con prórroga en {prev}")
        ok = False
    old_done = eb.finished(feeds[seasons[0]])
    if old_done:
        checks.append(("terminado " + seasons[0], seasons[0], old_done[len(old_done) // 2], True))
    pending = [g for g in feeds[cur] if str(g.get("status") or "").lower() != "result"]
    if pending:
        checks.append(("sin jugar " + cur, cur, pending[-1], False))

    for label, season, g, expect_doc in checks:
        box = eb._http_get(eb.BOXSCORE_URL.format(code=eb._n(g.get("code")), season=season))
        doc, why = eb.build_game_doc(g, box) if isinstance(box, dict) else (None, "boxscore_error")
        if expect_doc:
            good = doc is not None
            extra = (f"minutes={doc['minutes']} ot={doc['ot_periods']} poss≈{doc['home']['poss_est']}"
                     if doc else why)
        else:
            good = doc is None
            extra = f"sin doc ({why})"
        ok &= good
        print(f"  boxscore {label}: {'OK' if good else 'FALLO'} — {extra}")
    return ok


async def tennis() -> bool:
    import httpx
    ok = True
    year = datetime.now(timezone.utc).year
    print(f"\n== Tenis: tennis-data.co.uk {year}")
    index: set[str] = set()
    async with httpx.AsyncClient(timeout=td._HTTP_TIMEOUT, follow_redirects=True) as client:
        links = await td.fetch_links(client)
        years = sorted({y for _, y in links})
        print(f"  alldata.php: {len(links)} enlaces .xlsx ({years[0] if years else '-'}–"
              f"{years[-1] if years else '-'}), carpeta '{td.link_folder(next(iter(links.values()), ''))}'")
        for tour in td.TOURS:
            url = links.get((tour, year))
            if not url:
                print(f"  {tour}: FALLO sin enlace {year}")
                ok = False
                continue
            rows = await td.fetch_xlsx(client, url)
            docs, st = td.build_tournament_docs(rows, tour, year)
            last = max((m["date"] for d in docs.values() for m in d["matches"]), default=None)
            print(f"  {tour}: {st['rows']} filas, {len(docs)} torneos, última fecha {last}, "
                  f"estados {st['status']}, sets incoherentes {st['sets_incoherentes']}, "
                  f"sin fecha/jugador {st['sin_fecha_o_jugador']}, superficies {st['surface']}")
            for d in docs.values():
                for m in d["matches"]:
                    for k in (td.td_player_key(m["winner"]), td.td_player_key(m["loser"])):
                        if k:
                            index.add(k)
    print(f"  claves de jugador (año en curso): {len(index)}")
    # Calendario: upcoming_matches de tenis (solo lectura)
    from shared.firestore_client import col
    from google.cloud.firestore_v1.base_query import FieldFilter
    docs = list(col("upcoming_matches").where(filter=FieldFilter("sport", "==", "tennis")).stream())
    names, dobles = [], 0
    by_tour = Counter()
    for snap in docs:
        d = snap.to_dict() or {}
        for side in ("home_team", "away_team"):
            n = str(d.get(side) or "")
            if "/" in n or " & " in n:
                dobles += 1
                continue
            if n:
                names.append((str(d.get("league") or "")[:3].upper(), n))
    matched = Counter()
    for tour, n in names:
        by_tour[tour] += 1
        if td.match_player(n, index):
            matched[tour] += 1
    total = sum(by_tour.values())
    pct = 100 * sum(matched.values()) / total if total else 0
    print(f"  upcoming_matches tenis: {len(docs)} partidos, {total} nombres individuales, "
          f"{dobles} de dobles fuera")
    for tour in sorted(by_tour):
        print(f"    {tour or '?'}: {matched[tour]}/{by_tour[tour]} emparejan "
              f"({100 * matched[tour] / by_tour[tour]:.0f} %)")
    print(f"  emparejamiento total: {pct:.0f} % (objetivo ≥90 %)"
          + ("" if total else " — sin partidos de tenis en el calendario ahora mismo"))
    if total and pct < 90:
        ok = False
    return ok


def main() -> int:
    ok_e = euroleague()
    ok_t = asyncio.run(tennis())
    print(f"\nRESULTADO: Euroliga {'OK' if ok_e else 'REVISAR'} · Tenis {'OK' if ok_t else 'REVISAR'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
