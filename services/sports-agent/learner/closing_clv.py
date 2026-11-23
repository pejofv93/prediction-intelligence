"""
CLV real de lo ENVIADO en fútbol 1X2 (alerts_sent, type=sports_sent) y del 1X2 en sombra
(type=sports_shadow, desde el 23-nov-2026: registrado sin enviar) contra la cuota de
cierre de football-data.co.uk, cuando el CSV de la liga publica el partido (suele tardar
de 2 a 8 días).

Referencia de cierre: la temporada 2026/27 de football-data.co.uk NO trae Pinnacle (ni
PSH ni PSCH; comprobado el 16-nov-2026 en E0 y SP1). Trae el cierre de Bet365 (B365C*),
el máximo (MaxC*) y la media del mercado (AvgC*). Se usa la MEDIA sin margen:
  p_cierre = (1/AvgC_sel) / Σ(1/AvgC_H, 1/AvgC_D, 1/AvgC_A)
  clv_close = cuota_enviada × p_cierre − 1      (valor esperado frente al cierre justo)
y se guardan también las tres cuotas en bruto. Si algún día vuelven PSC*, se usan esas y
close_source lo dice.

Escribe en el doc de alerts_sent (solo campos nuevos; no toca result/correct):
  close_odds_avg, close_odds_max, close_odds_b365, close_odds_pinnacle (None si no hay),
  close_p_fair, clv_close, close_source, close_checked_at
  clv_close_max = cuota_enviada / MaxC_sel − 1 (frente a la mejor cuota al cierre, en
  bruto: la comparación más dura, porque la enviada suele ser la mejor entre casas)
y close_status = "sin_cierre" si a los SIN_CIERRE_DAYS días el CSV sigue sin el partido.
Nunca lanza. Devuelve solo recuentos (sin equipos ni partidos).
"""
import csv
import io
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from collectors.fdco_collector import FDCO_LEAGUES
from shared.config import SPORTS_GRADED_TYPES

logger = logging.getLogger(__name__)

FDCO_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
# liga interna → división de football-data.co.uk (las de fdco_collector + Championship)
LEAGUE_DIV: dict[str, str] = {**{k: v[0] for k, v in FDCO_LEAGUES.items()}, "ELC": "E1"}
MAX_AGE_DAYS = 60          # envíos más antiguos no se buscan
SIN_CIERRE_DAYS = 21       # pasado esto sin fila en el CSV → close_status "sin_cierre"
MIN_AGE_HOURS = 6          # el partido tiene que haberse jugado


def season_code(d: datetime) -> str:
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def parse_dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    for cand in (s, s[:16], s[:10]):          # completo, "AAAA-MM-DD HH:MM", solo fecha
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fdco_date(s: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
    return None


def _f(v) -> float | None:
    try:
        x = float(v)
        return x if x > 1.0 else None
    except (TypeError, ValueError):
        return None


def side_of(alert: dict) -> str | None:
    sel = str(alert.get("team_to_back") or alert.get("selection") or "").strip()
    if not sel:
        return None
    if sel.lower() in ("draw", "empate", "x"):
        return "D"
    if sel == str(alert.get("home_team") or ""):
        return "H"
    if sel == str(alert.get("away_team") or ""):
        return "A"
    return None


def closing_from_row(row: dict, side: str, odds_sent: float) -> dict | None:
    """Cuotas de cierre de la fila y CLV frente a la media sin margen (o Pinnacle si hay)."""
    pin = [_f(row.get(f"PSC{k}")) for k in "HDA"]
    avg = [_f(row.get(f"AvgC{k}")) for k in "HDA"]
    ref, source = (pin, "fdco-pinnacle-cierre") if all(pin) else (avg, "fdco-media-cierre")
    if not all(ref):
        return None
    i = "HDA".index(side)
    inv = [1.0 / o for o in ref]
    p_fair = inv[i] / sum(inv)
    close_max = _f(row.get(f"MaxC{side}"))
    return {
        "close_odds_avg": avg[i],
        "close_odds_max": close_max,
        "clv_close_max": round(odds_sent / close_max - 1.0, 5) if close_max else None,
        "close_odds_b365": _f(row.get(f"B365C{side}")),
        "close_odds_pinnacle": pin[i],
        "close_p_fair": round(p_fair, 5),
        "clv_close": round(odds_sent * p_fair - 1.0, 5),
        "close_source": source,
    }


def find_row(alert: dict, rows: list[dict], teams: set[str], map_team) -> dict | None:
    md = parse_dt(alert.get("match_date"))
    h, okh = map_team(str(alert.get("home_team") or ""), teams)
    a, oka = map_team(str(alert.get("away_team") or ""), teams)
    if md is None or not (okh and oka):
        return None
    for r in rows:
        if r.get("HomeTeam") == h and r.get("AwayTeam") == a:
            d = fdco_date(r.get("Date", ""))
            if d is not None and abs((d.date() - md.date()).days) <= 1:
                return r
    return None


async def _download(client: httpx.AsyncClient, season: str, div: str) -> list[dict]:
    try:
        resp = await client.get(FDCO_URL.format(season=season, div=div))
        if resp.status_code != 200:
            return []
        return list(csv.DictReader(io.StringIO(resp.content.decode("latin-1").lstrip("﻿"))))
    except Exception:
        logger.warning("closing_clv: error descargando %s %s", div, season, exc_info=True)
        return []


async def run_closing_clv(dry_run: bool = False, now: datetime | None = None) -> dict:
    from collectors.match_fixtures import map_team
    from shared.firestore_client import col, iter_all

    now = now or datetime.now(timezone.utc)
    summary = {"pendientes": 0, "con_cierre": 0, "sin_fila_aun": 0, "sin_cierre": 0,
               "liga_sin_csv": 0, "seleccion_desconocida": 0, "dry_run": dry_run}
    todo: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
    try:
        for doc in iter_all("alerts_sent", where=("type", "in", SPORTS_GRADED_TYPES)):
            d = doc.to_dict() or {}
            if str(d.get("sport") or "football").lower() not in ("football", "soccer"):
                continue
            if (d.get("market_type") or "h2h") != "h2h" or d.get("clv_close") is not None \
                    or d.get("close_status") == "sin_cierre":
                continue
            md = parse_dt(d.get("match_date"))
            if md is None or md > now - timedelta(hours=MIN_AGE_HOURS) \
                    or md < now - timedelta(days=MAX_AGE_DAYS):
                continue
            div = LEAGUE_DIV.get(str(d.get("league") or ""))
            if not div:
                summary["liga_sin_csv"] += 1
                continue
            summary["pendientes"] += 1
            todo[(div, season_code(md))].append((doc.id, d))
    except Exception:
        logger.error("closing_clv: error leyendo alerts_sent", exc_info=True)
        return summary

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for (div, season), items in todo.items():
            rows = await _download(client, season, div)
            teams = {r.get("HomeTeam") for r in rows} | {r.get("AwayTeam") for r in rows}
            teams.discard(None)
            for doc_id, d in items:
                side = side_of(d)
                if side is None:
                    summary["seleccion_desconocida"] += 1
                    continue
                row = find_row(d, rows, teams, map_team)
                out = closing_from_row(row, side, float(d.get("odds") or 0)) if row else None
                if out is None:
                    md = parse_dt(d.get("match_date"))
                    if md is not None and md < now - timedelta(days=SIN_CIERRE_DAYS):
                        summary["sin_cierre"] += 1
                        out = {"close_status": "sin_cierre"}
                    else:
                        summary["sin_fila_aun"] += 1
                        continue
                else:
                    summary["con_cierre"] += 1
                if dry_run:
                    continue
                try:
                    col("alerts_sent").document(doc_id).update({**out, "close_checked_at": now})
                except Exception:
                    logger.error("closing_clv: error escribiendo un doc", exc_info=True)
    logger.info("closing_clv: %s", summary)
    return summary
