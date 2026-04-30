"""
services/sports-agent/collectors/fdco_collector.py

Descarga datos históricos de football-data.co.uk (gratis, sin API key).
Columnas usadas: HC/AC (corners), HY/AY/HR/AR (tarjetas), HomeTeam/AwayTeam.

Calcula promedios por equipo (últimos N partidos en casa / fuera) y los guarda
en Firestore colección team_corner_stats/{league}_{team_slug}.

Ligas verificadas con datos 2024/25 (2026-04-20):
  SP1→PD, E0→PL, D1→BL1, I1→SA, F1→FL1, N1→DED, P1→PPL,
  SP2→SD, D2→BL2, I2→SB, B1→(Belgian), SC0→(Scottish)
"""
import csv
import io
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Base URL football-data.co.uk — temporada codificada como "2425" para 2024/25
_FDCO_BASE = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"

# Mapeo código interno → código FDCO, nombre legible
FDCO_LEAGUES: dict[str, tuple[str, str]] = {
    "PD":  ("SP1", "La Liga"),
    "PL":  ("E0",  "Premier League"),
    "BL1": ("D1",  "Bundesliga"),
    "SA":  ("I1",  "Serie A"),
    "FL1": ("F1",  "Ligue 1"),
    "SD":  ("SP2", "Segunda"),
    "BL2": ("D2",  "Bundesliga 2"),
    "SB":  ("I2",  "Serie B"),
}

# Número de partidos recientes a considerar para los promedios
_ROLLING_N = 15
_HTTP_TIMEOUT = 20.0


def _season_code(year: int = 2025) -> str:
    """2024/25 → '2425', 2023/24 → '2324'"""
    return f"{str(year - 1)[-2:]}{str(year)[-2:]}"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _safe(val: str, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


async def fetch_league_csv(league_code: str, season_year: int = 2025) -> list[dict]:
    """
    Descarga el CSV de football-data.co.uk y devuelve lista de filas con datos.
    Solo filas que tienen HC (corners disponibles).
    """
    fdco_code, league_name = FDCO_LEAGUES.get(league_code, (None, None))
    if not fdco_code:
        logger.warning("fdco_collector: liga %s no mapeada", league_code)
        return []

    season = _season_code(season_year)
    url = _FDCO_BASE.format(season=season, code=fdco_code)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning("fdco_collector: HTTP %d para %s (%s)", resp.status_code, league_name, url)
            return []

        content = resp.text.lstrip("\ufeff")  # BOM
        reader = csv.DictReader(io.StringIO(content))
        rows = []
        for row in reader:
            if not row.get("HC", "").strip():
                continue  # partido sin stats (en curso o futuro)
            rows.append(row)

        logger.info("fdco_collector: %s → %d partidos con stats", league_name, len(rows))
        return rows

    except Exception:
        logger.error("fdco_collector: error descargando %s", league_name, exc_info=True)
        return []


def compute_team_averages(rows: list[dict]) -> dict[str, dict]:
    """
    Calcula promedios de corners y tarjetas por equipo usando los últimos N partidos.
    Los datos FDCO están en orden cronológico ascendente.

    Devuelve:
      { team_name: {
          home_corners: float, away_corners: float,
          home_yellows: float, away_yellows: float,
          home_reds: float, away_reds: float,
          home_matches: int, away_matches: int,
        }
      }
    """
    # Acumular histórico por equipo
    home_history: dict[str, list[dict]] = {}
    away_history: dict[str, list[dict]] = {}

    for row in rows:
        home = row.get("HomeTeam", "").strip()
        away = row.get("AwayTeam", "").strip()
        if not home or not away:
            continue

        stats = {
            "hc": _safe(row.get("HC")),
            "ac": _safe(row.get("AC")),
            "hy": _safe(row.get("HY")),
            "ay": _safe(row.get("AY")),
            "hr": _safe(row.get("HR")),
            "ar": _safe(row.get("AR")),
        }
        home_history.setdefault(home, []).append(stats)
        away_history.setdefault(away, []).append(stats)

    all_teams = set(home_history) | set(away_history)
    result: dict[str, dict] = {}

    for team in all_teams:
        h_rows = home_history.get(team, [])[-_ROLLING_N:]
        a_rows = away_history.get(team, [])[-_ROLLING_N:]

        def avg(lst, key, default=0.0):
            vals = [r[key] for r in lst if r[key] > 0 or True]
            return round(sum(vals) / len(vals), 2) if vals else default

        result[team] = {
            "home_corners":      avg(h_rows, "hc"),
            "away_corners":      avg(a_rows, "ac"),
            "home_yellows":      avg(h_rows, "hy"),
            "away_yellows":      avg(a_rows, "ay"),
            "home_reds":         avg(h_rows, "hr"),
            "away_reds":         avg(a_rows, "ar"),
            "home_matches":      len(h_rows),
            "away_matches":      len(a_rows),
            # Corners concedidos (para ajuste del oponente)
            "home_corners_conceded": avg(h_rows, "ac"),
            "away_corners_conceded": avg(a_rows, "hc"),
        }

    return result


async def collect_and_save(league_code: str, season_year: int = 2025) -> int:
    """
    Descarga CSV, computa promedios y los guarda en Firestore.
    Devuelve número de equipos guardados.
    """
    from shared.firestore_client import col

    rows = await fetch_league_csv(league_code, season_year)
    if not rows:
        return 0

    team_avgs = compute_team_averages(rows)
    now = datetime.now(timezone.utc).isoformat()
    collection = col("team_corner_stats")
    saved = 0

    for team, stats in team_avgs.items():
        doc_id = f"{league_code}_{_slugify(team)}"
        try:
            collection.document(doc_id).set({
                "league": league_code,
                "team": team,
                "season": season_year,
                "updated_at": now,
                **stats,
            })
            saved += 1
        except Exception:
            logger.error("fdco_collector: error guardando %s", doc_id, exc_info=True)

    logger.info("fdco_collector: %s → %d equipos guardados en Firestore", league_code, saved)
    return saved


async def run_all_leagues(season_year: int = 2025) -> dict[str, int]:
    """Recolecta todas las ligas mapeadas. Devuelve {league: teams_saved}."""
    results = {}
    for league_code in FDCO_LEAGUES:
        saved = await collect_and_save(league_code, season_year)
        results[league_code] = saved
        await _async_sleep(0.3)  # cortesía a football-data.co.uk
    return results


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
