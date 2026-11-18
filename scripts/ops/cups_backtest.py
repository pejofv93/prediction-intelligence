"""SOLO LECTURA. Backtest de copas con el modelo de clubes (mp-v1): analyzers/match_preview_cups.py.

Lee hl_team_stats (grupo cups) y hl_matches (partidos de liga de Highlightly, para emparejar ids
con nombres de football-data) de Firestore, y baja los CSV de football-data.co.uk de todas las
temporadas desde la anterior a la primera copa. No escribe nada ni pide nada a Highlightly.
Log: solo agregados (recuentos, factores y métricas por mercado): nada de equipos ni partidos.
"""
import asyncio
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402

from analyzers import match_preview as mp  # noqa: E402
from analyzers import match_preview_cups as mc  # noqa: E402
from collectors import fdco_team_matches as fd  # noqa: E402
from collectors.highlightly_players import team_similarity  # noqa: E402
from shared.firestore_client import iter_all  # noqa: E402

FIRST_SEASON_END = int(os.environ.get("FIRST_SEASON_END") or 2020)   # 2019/20: la anterior a la 1.ª copa


async def _history(last_end: int) -> tuple[list[dict], list[str]]:
    rows, failed = [], []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for end in range(FIRST_SEASON_END, last_end + 1):
            season = fd.season_code(end)
            for div in (*fd.MAIN_DIVS, *fd.LOWER_DIVS):
                raw = await fd.fetch_csv_rows(client, div, season)
                if raw is None:
                    failed.append(f"{div}_{season}")
                    continue
                rows.extend(fd._to_team_rows(raw, div, season))
                await asyncio.sleep(0.3)
    return mp.matches_from_team_rows(rows), failed


def _print_metrics(name: str, rows: list[tuple]) -> None:
    if not rows:
        print(f"\n[{name}] sin filas")
        return
    met = mp.market_metrics(rows)
    print(f"\n[{name}]  mercado: partidos · Brier ref · Brier modelo · diferencia [IC95] · etiqueta")
    for v in sorted((v for v in met.values() if v["div"] == "ALL"), key=lambda v: v["market"]):
        lo = "—" if v["ci_lo"] is None else f"{v['ci_lo']:+.4f}"
        hi = "—" if v["ci_hi"] is None else f"{v['ci_hi']:+.4f}"
        print(f"  {v['market']:14} {v['n_matches']:5d} · {v['brier_league']:.4f} · {v['brier_model']:.4f} · "
              f"{v['delta']:+.4f} [{lo}, {hi}] · {v['ci_label']}")
    per_c = Counter()
    for v in met.values():
        if v["div"] != "ALL" and v["ci_label"] == mp.LABEL_EDGE:
            per_c[v["div"]] += 1
    print(f"  celdas país×mercado con IC entero bajo 0: {dict(per_c) or 'ninguna'}")


def main() -> int:
    docs = [d.to_dict() or {} for d in iter_all("hl_team_stats", where=("group", "==", "cups"))]
    league = [d.to_dict() or {} for d in iter_all("hl_matches")]
    print(f"Copas leídas: {len(docs)} · partidos de liga de Highlightly: {len(league)}")
    last_end = max((int(mc.season_of(d["date"])[2:]) + 2000 for d in docs if d.get("date")), default=2027)
    matches, failed = asyncio.run(_history(last_end))
    print(f"Historial de liga (CSV): {len(matches)} partidos · CSV que faltan: {failed or 'ninguno'}")
    hist = mp.History(matches)
    csv_by_div_day: dict = {}
    for m in matches:
        csv_by_div_day.setdefault((m["div"], m["date"]), []).append(m)
    ids = mc.hl_id_names(league, csv_by_div_day, team_similarity)
    print(f"Ids de Highlightly emparejados con nombres de football-data: {len(ids)}")
    counts = Counter()
    cups = mc.cup_matches(docs, ids, mc.team_divs(hist), counts)
    print("Por país (utilizables):", dict(Counter(c["country"] for c in cups)))
    rows = mc.backtest_cup_rows(hist, cups, counts)
    factor = counts.pop("factor_copa", {})
    print("Recuentos:", dict(counts))
    print("Factor de copa final (observado / esperado por mp-v1):", factor)
    for name in ("modelo_vs_liga", "factor_vs_liga", "modelo_vs_copa", "factor_vs_copa"):
        _print_metrics(name, rows[name])
    return 0


if __name__ == "__main__":
    sys.exit(main())
