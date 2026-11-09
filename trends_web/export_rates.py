"""Exporta los porcentajes por equipo y mercado para la web privada de Tendencias.

Los CALCULA desde los CSV de football-data.co.uk con las MISMAS funciones del colector
(services/sports-agent/collectors/fdco_team_matches.py: filas equipo-partido, media de liga,
ponderación por temporada y ajuste a la media), así que no necesita Firestore ni credenciales
de GCP. compare_rates.py comprueba aparte que salen iguales que fdco_team_market_rates.

Uso: python trends_web/export_rates.py --out trends_web/site/data/rates.json
Imprime solo recuentos. El JSON no se sube a git (trends_web/site/data/ está en .gitignore).
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "services", "sports-agent")))

import httpx  # noqa: E402

from collectors import fdco_team_matches as fd  # noqa: E402

# Diferencia con la media de la liga por debajo de la cual se dice "en línea con la liga"
LEAGUE_GAP = 0.05

LEAGUE_NAMES = {
    "SP1": "LaLiga", "SP2": "Segunda", "I1": "Serie A", "E0": "Premier League",
    "F1": "Ligue 1", "P1": "Primeira Liga", "D1": "Bundesliga", "N1": "Eredivisie",
}
# (clave, etiqueta, ámbito) en el orden de las pestañas
MARKET_LABELS = [
    ("corners_for", "Córners", "equipo"),
    ("cards_for", "Tarjetas", "equipo"),
    ("fouls_for", "Faltas cometidas", "equipo"),
    ("shots_for", "Remates", "equipo"),
    ("sot_for", "Remates a puerta", "equipo"),
    ("saves_derived", "Paradas (derivadas)", "equipo"),
    ("corners_total", "Córners", "partido"),
    ("cards_total", "Tarjetas", "partido"),
    ("fouls_total", "Faltas", "partido"),
    ("shots_total", "Remates", "partido"),
    ("sot_total", "Remates a puerta", "partido"),
]
SPLITS = ("all", "home", "away")


def reliability(cell: dict) -> str:
    """insuficiente | sin_liga | en_linea | encima | debajo."""
    if not cell.get("meets_min_sample"):
        return "insuficiente"
    p, pl = cell.get("p_adj"), cell.get("p_league")
    if p is None or pl is None:
        return "sin_liga"
    gap = p - pl
    if abs(gap) < LEAGUE_GAP:
        return "en_linea"
    return "encima" if gap > 0 else "debajo"


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 3)


def compact(docs: dict[str, dict], cur_by_div: dict[str, list[dict]], generated_at: str) -> dict:
    """Docs de build_rate_docs → JSON compacto de la página.
    Celda = [p_adj, p_liga, partidos_actual, partidos_anterior, n_efectivo, fiabilidad]."""
    lines_by_market = {m: [fd.line_key(x) for x in lines] for m, _, lines in fd.MARKETS}
    out = {
        "generated_at": generated_at,
        "league_gap": LEAGUE_GAP,
        "markets": [{"key": k, "label": lab, "scope": sc,
                     "lines": [lk.replace("over_", "").replace("_", ",") for lk in lines_by_market[k]],
                     "source": "derivada" if k == "saves_derived" else "football-data.co.uk"}
                    for k, lab, sc in MARKET_LABELS],
        "leagues": {},
    }
    for div in fd.MAIN_DIVS:
        rows = cur_by_div.get(div) or []
        teams = []
        for doc in sorted((d for d in docs.values() if d["div"] == div), key=lambda d: d["team"]):
            r = {}
            for k, _, _ in MARKET_LABELS:
                m = doc["markets"][k]["lines"]
                r[k] = [[[_r(c["p_adj"]), _r(c["p_league"]), c["n_cur"], c["n_prev"], round(c["n_w"], 2),
                          reliability(c)] for c in (m[lk][s] for s in SPLITS)]
                        for lk in lines_by_market[k]]
            teams.append({
                "team": doc["team"], "slug": doc["team_slug"],
                "mc": doc["matches_current"], "mp": doc["matches_previous"],
                "prev_div": doc["previous_div"], "prev_w": doc["previous_weight"],
                "cards": doc["cards_convention"], "r": r,
            })
        out["leagues"][div] = {
            "name": LEAGUE_NAMES.get(div, div),
            "data_through": max((x["match_date"] for x in rows), default=None),
            "matches": len({x["match_key"] for x in rows}),
            "teams": teams,
        }
    return out


def _read_local(csv_dir: str, div: str, season: str) -> Optional[list[dict]]:
    """Pruebas en local: {csv_dir}/{div}_{season}.csv ya descargado (None si falta)."""
    path = os.path.join(csv_dir, f"{div}_{season}.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


async def load_rows(today: Optional[date] = None, csv_dir: Optional[str] = None):
    """Descarga los CSV (o los lee de csv_dir) y arma las filas igual que
    run_fdco_team_matches, sin escribir nada."""
    end_year = fd.current_season_end_year(today or datetime.now(timezone.utc).date())
    cur_season, prev_season = fd.season_code(end_year), fd.season_code(end_year - 1)
    cur_by_div, prev_by_div, failed = {}, {}, []
    async with httpx.AsyncClient(timeout=fd._HTTP_TIMEOUT, follow_redirects=True) as client:
        async def raw_rows(div, season):
            if csv_dir:
                return _read_local(csv_dir, div, season)
            raw = await fd.fetch_csv_rows(client, div, season)
            await asyncio.sleep(0.3)  # cortesía con football-data.co.uk
            return raw

        for div in fd.MAIN_DIVS:
            for season, target in ((cur_season, cur_by_div), (prev_season, prev_by_div)):
                raw = await raw_rows(div, season)
                if raw is None:
                    failed.append(f"{div}_{season}")
                else:
                    target[div] = fd._to_team_rows(raw, div, season)
        main_teams = {r["team"] for rows in cur_by_div.values() for r in rows}
        for div in fd.LOWER_DIVS:
            raw = await raw_rows(div, prev_season)
            if raw is None:
                failed.append(f"{div}_{prev_season}")
            else:
                prev_by_div[div] = [r for r in fd._to_team_rows(raw, div, prev_season) if r["team"] in main_teams]
    return cur_by_div, prev_by_div, cur_season, prev_season, failed


def build(today: Optional[date] = None, csv_dir: Optional[str] = None):
    cur_by_div, prev_by_div, cur_season, prev_season, failed = asyncio.run(load_rows(today, csv_dir))
    docs = fd.build_rate_docs(cur_by_div, prev_by_div, cur_season, prev_season)
    return docs, cur_by_div, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--csv-dir", help="solo pruebas en local: leer {div}_{temporada}.csv de esta carpeta")
    args = ap.parse_args()
    docs, cur_by_div, failed = build(csv_dir=args.csv_dir)
    if failed:
        print(f"ERROR: descargas fallidas {failed}; no se genera nada (no se publican datos parciales)")
        return 1
    data = compact(docs, cur_by_div, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    for div, lg in data["leagues"].items():
        print(f"  {div}: {len(lg['teams'])} equipos, {lg['matches']} partidos, datos hasta {lg['data_through']}")
    print(f"Generado: {os.path.getsize(args.out) / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
