"""
services/sports-agent/collectors/fdco_team_matches.py

Ampliación de Tendencias — Fase 1: tabla por equipo y partido + porcentajes por
mercado/línea con football-data.co.uk (CSV gratis, sin API key).

Independiente del feed actual: NO lo usa trend_finder ni fdco_collector (team_corner_stats
sigue igual). Escribe por el cliente de BD actual (shared.firestore_client.col), así que
las colecciones nuevas entran en la copia/migración como el resto:

  fdco_team_matches/{div}_{season}_{fecha}_{local}_{visitante}_{equipo}
      un doc por equipo y partido: a favor / en contra de goles, remates, a puerta,
      faltas, córners, amarillas, rojas; paradas DERIVADAS (saves_source="derivada").
  fdco_team_market_rates/{div}_{equipo}
      por mercado → línea → lado (all/home/away): muestra actual/anterior, aciertos y n
      ponderados, % bruto, media de liga, % ajustado a la media y marca de muestra mínima.
  fdco_team_matches_state/{div}_{season}   hash por partido → solo se escribe lo nuevo/cambiado
  fdco_team_rates_state/{div}              hash por equipo  → solo se reescribe lo que cambia

Ponderación: temporada actual 1.0; anterior en la misma división 0.5; anterior en OTRA
división (ascendido o descendido) 0.25 y marcada. Ajuste a la media:
    p_adj = (aciertos_pond + K·p_liga) / (n_pond + K),   K = 8
Muestra mínima: lado "all" → n_pond >= 10 y >= 5 partidos actuales; home/away → n_pond >= 8.

Notas de la fuente (football-data.co.uk/notes.txt, verificado 2026-11-07):
  - Inglaterra (E0/E1): la doble amarilla NO cuenta la primera amarilla (solo la roja);
    en el resto de ligas cuenta amarilla + roja. Se guarda cards_convention por liga.
  - Francia 2ª (F2): HF/AF son "Free Kicks Conceded" (faltas + fueras de juego + otras),
    no faltas → fouls = None en F2 y esos partidos no cuentan en el mercado de faltas.
  - Paradas = remates a puerta del rival − goles encajados. Un negativo solo puede venir
    de un autogol: se guarda 0 con own_goal_probable=True.
"""
import asyncio
import csv
import hashlib
import io
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_FDCO_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
_HTTP_TIMEOUT = 30.0

# División principal (código FDCO) → código interno de liga
MAIN_DIVS: dict[str, str] = {
    "SP1": "PD", "SP2": "SD", "I1": "SA", "E0": "PL",
    "F1": "FL1", "P1": "PPL", "D1": "BL1", "N1": "DED",
}
# Segundas divisiones: solo temporada anterior y solo filas de equipos que hoy están
# en una división principal (historial de ascendidos).
LOWER_DIVS: dict[str, str] = {"E1": "ELC", "I2": "SB", "F2": "FL2", "D2": "BL2"}

_FREE_KICKS_NOT_FOULS = {"F2"}
_SECOND_YELLOW_EXCLUDED = {"E0", "E1"}

WEIGHT_CURRENT = 1.0
WEIGHT_PREV_SAME_DIV = 0.5
WEIGHT_PREV_OTHER_DIV = 0.25
SHRINK_K = 8
MIN_N_EFF_ALL = 10
MIN_CUR_ALL = 5
MIN_N_EFF_SPLIT = 8

# Mercados: (nombre, clave del valor en la fila equipo-partido, líneas). Todas "más de";
# el "menos de" es 1 − p sobre la misma muestra.
MARKETS: list[tuple[str, str, tuple[float, ...]]] = [
    ("corners_for",    "corners_for",    (3.5, 4.5, 5.5, 6.5)),
    ("cards_for",      "cards_for",      (0.5, 1.5, 2.5, 3.5)),
    ("fouls_for",      "fouls_for",      (9.5, 10.5, 11.5, 12.5, 13.5)),
    ("shots_for",      "shots_for",      (8.5, 10.5, 12.5, 14.5)),
    ("sot_for",        "sot_for",        (2.5, 3.5, 4.5, 5.5)),
    ("saves_derived",  "saves_derived",  (1.5, 2.5, 3.5, 4.5)),
    ("corners_total",  "corners_total",  (8.5, 9.5, 10.5, 11.5)),
    ("cards_total",    "cards_total",    (3.5, 4.5, 5.5)),
    ("fouls_total",    "fouls_total",    (20.5, 22.5, 24.5)),
    ("shots_total",    "shots_total",    (20.5, 22.5, 24.5, 26.5)),
    ("sot_total",      "sot_total",      (6.5, 7.5, 8.5, 9.5)),
]
_MARKET_SOURCE = {"saves_derived": "derivada"}

_SPLITS = ("all", "home", "away")


# ── Utilidades ────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def season_code(end_year: int) -> str:
    """2026/27 → '2627'."""
    return f"{str(end_year - 1)[-2:]}{str(end_year)[-2:]}"


def current_season_end_year(today: date) -> int:
    """Temporada europea: de julio en adelante ya cuenta la siguiente."""
    return today.year + 1 if today.month >= 7 else today.year


def line_key(line: float) -> str:
    """3.5 → 'over_3_5' (sin puntos: claves de mapa seguras en Firestore y en la migración)."""
    return "over_" + str(line).replace(".", "_")


def _int(val) -> Optional[int]:
    try:
        s = str(val).strip()
        return int(float(s)) if s != "" else None
    except (TypeError, ValueError):
        return None


def _float(val) -> Optional[float]:
    try:
        s = str(val).strip()
        return float(s) if s != "" else None
    except (TypeError, ValueError):
        return None


def _parse_date(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ── Fila CSV → dos filas equipo-partido ────────────────────────────────────────

_REQUIRED = ("FTHG", "FTAG", "HS", "AS", "HST", "AST", "HF", "AF", "HC", "AC", "HY", "AY", "HR", "AR")


def team_rows_from_csv_row(row: dict, div: str, season: str) -> list[dict]:
    """Devuelve [fila_local, fila_visitante] o [] si el partido no tiene stats completas."""
    home = (row.get("HomeTeam") or "").strip()
    away = (row.get("AwayTeam") or "").strip()
    match_date = _parse_date(row.get("Date", ""))
    vals = {k: _int(row.get(k)) for k in _REQUIRED}
    if not home or not away or not match_date or any(v is None for v in vals.values()):
        return []

    fouls_are_fouls = div not in _FREE_KICKS_NOT_FOULS
    match_key = f"{match_date}_{slugify(home)}_{slugify(away)}"
    hxg, axg = _float(row.get("HxG")), _float(row.get("AxG"))
    ht_h, ht_a = _int(row.get("HTHG")), _int(row.get("HTAG"))

    sides = (
        ("home", home, away, "H", "A", hxg, axg, ht_h, ht_a),
        ("away", away, home, "A", "H", axg, hxg, ht_a, ht_h),
    )
    out = []
    for side, team, opp, me, op, xg_for, xg_against, ht_for, ht_against in sides:
        goals_for = vals[f"FT{me}G"]
        goals_against = vals[f"FT{op}G"]
        opp_sot = vals[f"{op}ST"]
        raw_saves = opp_sot - goals_against
        cards_for = vals[f"{me}Y"] + vals[f"{me}R"]
        cards_against = vals[f"{op}Y"] + vals[f"{op}R"]
        fouls_for = vals[f"{me}F"] if fouls_are_fouls else None
        fouls_against = vals[f"{op}F"] if fouls_are_fouls else None
        out.append({
            "div": div, "season": season, "match_date": match_date, "match_key": match_key,
            "team": team, "team_slug": slugify(team), "opponent": opp, "side": side,
            "goals_for": goals_for, "goals_against": goals_against,
            "ht_goals_for": ht_for, "ht_goals_against": ht_against,
            "shots_for": vals[f"{me}S"], "shots_against": vals[f"{op}S"],
            "sot_for": vals[f"{me}ST"], "sot_against": opp_sot,
            "fouls_for": fouls_for, "fouls_against": fouls_against,
            "corners_for": vals[f"{me}C"], "corners_against": vals[f"{op}C"],
            "yellows_for": vals[f"{me}Y"], "yellows_against": vals[f"{op}Y"],
            "reds_for": vals[f"{me}R"], "reds_against": vals[f"{op}R"],
            "cards_for": cards_for, "cards_against": cards_against,
            "corners_total": vals["HC"] + vals["AC"],
            "cards_total": cards_for + cards_against,
            "fouls_total": (vals["HF"] + vals["AF"]) if fouls_are_fouls else None,
            "shots_total": vals["HS"] + vals["AS"],
            "sot_total": vals["HST"] + vals["AST"],
            "saves_derived": max(0, raw_saves),
            "saves_source": "derivada",
            "own_goal_probable": raw_saves < 0,
            "xg_for": xg_for, "xg_against": xg_against,
            "cards_convention": ("second_yellow_excluded_from_yellows"
                                 if div in _SECOND_YELLOW_EXCLUDED else "second_yellow_counts_yellow_plus_red"),
            "fouls_note": None if fouls_are_fouls else "free_kicks_conceded_not_fouls",
        })
    return out


def doc_id_for(team_row: dict) -> str:
    return f"{team_row['div']}_{team_row['season']}_{team_row['match_key']}_{team_row['team_slug']}"


def row_hash(team_row: dict) -> str:
    payload = json.dumps(team_row, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


# ── Porcentajes ponderados con ajuste a la media ──────────────────────────────

def _hit(team_row: dict, key: str, line: float) -> Optional[bool]:
    v = team_row.get(key)
    return None if v is None else v > line


def _accumulate(rows_weighted: list[tuple[dict, float, str]], key: str, line: float, split: str) -> dict:
    """rows_weighted: (fila, peso, 'cur'|'prev')."""
    n_cur = n_prev = hits_cur = hits_prev = 0
    n_w = hits_w = 0.0
    for r, w, period in rows_weighted:
        if split != "all" and r["side"] != split:
            continue
        h = _hit(r, key, line)
        if h is None:
            continue
        n_w += w
        hits_w += w if h else 0.0
        if period == "cur":
            n_cur += 1
            hits_cur += int(h)
        else:
            n_prev += 1
            hits_prev += int(h)
    return {"n_cur": n_cur, "n_prev": n_prev, "hits_cur": hits_cur, "hits_prev": hits_prev,
            "n_w": n_w, "hits_w": hits_w}


def _meets_min(split: str, n_w: float, n_cur: int) -> bool:
    if split == "all":
        return n_w >= MIN_N_EFF_ALL and n_cur >= MIN_CUR_ALL
    return n_w >= MIN_N_EFF_SPLIT


def league_base_rates(cur_rows: list[dict], prev_rows_same_div: list[dict], markets=None) -> dict:
    """{market: {line_key: {split: p_liga}}} con la misma ponderación por temporada.
    markets: lista como MARKETS (por defecto MARKETS; la web de Tendencias añade los de goles)."""
    weighted = [(r, WEIGHT_CURRENT, "cur") for r in cur_rows] + \
               [(r, WEIGHT_PREV_SAME_DIV, "prev") for r in prev_rows_same_div]
    base: dict = {}
    for market, key, lines in (markets or MARKETS):
        base[market] = {}
        for line in lines:
            base[market][line_key(line)] = {}
            for split in _SPLITS:
                a = _accumulate(weighted, key, line, split)
                base[market][line_key(line)][split] = (a["hits_w"] / a["n_w"]) if a["n_w"] else None
    return base


def team_rates(team_weighted: list[tuple[dict, float, str]], base: dict, markets=None) -> dict:
    out: dict = {}
    for market, key, lines in (markets or MARKETS):
        out[market] = {"source": _MARKET_SOURCE.get(market, "football-data.co.uk"), "lines": {}}
        for line in lines:
            lk = line_key(line)
            out[market]["lines"][lk] = {}
            for split in _SPLITS:
                a = _accumulate(team_weighted, key, line, split)
                p_league = base.get(market, {}).get(lk, {}).get(split)
                p_raw = (a["hits_w"] / a["n_w"]) if a["n_w"] else None
                if p_league is None:
                    p_adj = p_raw
                elif a["n_w"]:
                    p_adj = (a["hits_w"] + SHRINK_K * p_league) / (a["n_w"] + SHRINK_K)
                else:
                    p_adj = None
                out[market]["lines"][lk][split] = {
                    "n_cur": a["n_cur"], "n_prev": a["n_prev"],
                    "hits_cur": a["hits_cur"], "hits_prev": a["hits_prev"],
                    "n_w": round(a["n_w"], 4), "hits_w": round(a["hits_w"], 4),
                    "p_raw": None if p_raw is None else round(p_raw, 4),
                    "p_league": None if p_league is None else round(p_league, 4),
                    "p_adj": None if p_adj is None else round(p_adj, 4),
                    "meets_min_sample": _meets_min(split, a["n_w"], a["n_cur"]),
                }
    return out


def build_rate_docs(cur_by_div: dict[str, list[dict]], prev_by_div: dict[str, list[dict]],
                    cur_season: str, prev_season: str, markets=None) -> dict[str, dict]:
    """
    cur_by_div:  filas equipo-partido de la temporada actual por división PRINCIPAL.
    prev_by_div: filas de la temporada anterior por división (principales + inferiores).
    Devuelve {doc_id: doc} para fdco_team_market_rates (sin updated_at).
    markets: por defecto MARKETS (lo que guarda el colector); la web de Tendencias pasa más.
    """
    prev_team_div: dict[str, str] = {}
    for div, rows in prev_by_div.items():
        for r in rows:
            # Un equipo aparece en una sola división por temporada; si hubiera choque,
            # gana la principal (más datos y mismo nivel).
            if r["team"] not in prev_team_div or div in MAIN_DIVS:
                prev_team_div[r["team"]] = div

    docs: dict[str, dict] = {}
    for div, cur_rows in cur_by_div.items():
        if div not in MAIN_DIVS:
            continue
        base = league_base_rates(cur_rows, prev_by_div.get(div, []), markets)
        for team in sorted({r["team"] for r in cur_rows}):
            mine_cur = [r for r in cur_rows if r["team"] == team]
            prev_div = prev_team_div.get(team)
            if prev_div is None:
                prev_weight, mine_prev = None, []
            else:
                prev_weight = WEIGHT_PREV_SAME_DIV if prev_div == div else WEIGHT_PREV_OTHER_DIV
                mine_prev = [r for r in prev_by_div.get(prev_div, []) if r["team"] == team]
            weighted = [(r, WEIGHT_CURRENT, "cur") for r in mine_cur] + \
                       [(r, prev_weight, "prev") for r in mine_prev]
            docs[f"{div}_{slugify(team)}"] = {
                "div": div, "league": MAIN_DIVS[div], "team": team, "team_slug": slugify(team),
                "season_current": cur_season, "season_previous": prev_season,
                "matches_current": len(mine_cur), "matches_previous": len(mine_prev),
                "previous_div": prev_div, "previous_weight": prev_weight,
                "previous_other_div": prev_div is not None and prev_div != div,
                "weights": {"current": WEIGHT_CURRENT, "previous_same_div": WEIGHT_PREV_SAME_DIV,
                            "previous_other_div": WEIGHT_PREV_OTHER_DIV, "shrink_k": SHRINK_K},
                "cards_convention": mine_cur[0]["cards_convention"] if mine_cur else None,
                "markets": team_rates(weighted, base, markets),
            }
    return docs


# ── Descarga ─────────────────────────────────────────────────────────────────

async def fetch_csv_rows(client: httpx.AsyncClient, div: str, season: str) -> Optional[list[dict]]:
    """Filas crudas del CSV; None si la descarga falla (distinto de 0 partidos)."""
    url = _FDCO_URL.format(season=season, div=div)
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning("fdco_team_matches: HTTP %d en %s", resp.status_code, url)
            return None
        return list(csv.DictReader(io.StringIO(resp.text.lstrip("﻿"))))
    except Exception:
        logger.error("fdco_team_matches: error descargando %s", url, exc_info=True)
        return None


def _to_team_rows(raw: list[dict], div: str, season: str) -> list[dict]:
    rows: list[dict] = []
    for r in raw:
        rows.extend(team_rows_from_csv_row(r, div, season))
    return rows


# ── Escritura incremental ───────────────────────────────────────────────────

def _write_changed(collection: str, state_collection: str, state_id: str,
                   docs: dict[str, dict], dry_run: bool) -> int:
    """Escribe solo los docs cuyo hash cambió respecto al doc de estado. Devuelve nº escritos."""
    from shared.firestore_client import col, get_client

    state_ref = col(state_collection).document(state_id)
    snap = state_ref.get()
    old = (snap.to_dict() or {}).get("hashes", {}) if snap.exists else {}
    new_hashes = {doc_id: row_hash(doc) for doc_id, doc in docs.items()}
    changed = [d for d, h in new_hashes.items() if old.get(d) != h]
    if dry_run or not changed:
        return len(changed)

    now = datetime.now(timezone.utc).isoformat()
    client = get_client()
    for i in range(0, len(changed), 400):
        batch = client.batch()
        for doc_id in changed[i:i + 400]:
            batch.set(col(collection).document(doc_id), {**docs[doc_id], "updated_at": now})
        batch.commit()
    # El estado se guarda al final: si un batch falla, la próxima pasada reintenta.
    state_ref.set({"hashes": {**old, **new_hashes}, "updated_at": now})
    return len(changed)


async def run_fdco_team_matches(seasons: str = "both", dry_run: bool = False,
                                today: Optional[date] = None) -> dict:
    """
    seasons: "current" | "previous" | "both" — qué temporadas escriben filas equipo-partido
             (para repartir la carga inicial en dos días). Los porcentajes se calculan
             SIEMPRE con las dos temporadas, a partir de los CSV descargados.
    dry_run: calcula y cuenta lo que escribiría, sin escribir nada.
    """
    if seasons not in ("current", "previous", "both"):
        return {"ok": False, "error": f"seasons inválido: {seasons}"}

    end_year = current_season_end_year(today or datetime.now(timezone.utc).date())
    cur_season, prev_season = season_code(end_year), season_code(end_year - 1)
    summary: dict = {"ok": True, "dry_run": dry_run, "seasons": seasons,
                     "season_current": cur_season, "season_previous": prev_season,
                     "failed_downloads": [], "team_match_docs": {}, "rate_docs": {}, "errors": []}

    cur_by_div: dict[str, list[dict]] = {}
    prev_by_div: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        for div in MAIN_DIVS:
            for season, target in ((cur_season, cur_by_div), (prev_season, prev_by_div)):
                raw = await fetch_csv_rows(client, div, season)
                if raw is None:
                    summary["failed_downloads"].append(f"{div}_{season}")
                else:
                    target[div] = _to_team_rows(raw, div, season)
                await asyncio.sleep(0.3)  # cortesía con football-data.co.uk
        main_teams = {r["team"] for rows in cur_by_div.values() for r in rows}
        for div in LOWER_DIVS:
            raw = await fetch_csv_rows(client, div, prev_season)
            if raw is None:
                summary["failed_downloads"].append(f"{div}_{prev_season}")
            else:
                prev_by_div[div] = [r for r in _to_team_rows(raw, div, prev_season)
                                    if r["team"] in main_teams]
            await asyncio.sleep(0.3)

    # Una liga principal sin CSV actual no puede calcular porcentajes fiables: se omite
    # entera (no se sobrescriben sus docs con datos parciales).
    missing_cur = [d for d in MAIN_DIVS if d not in cur_by_div]
    if missing_cur:
        summary["errors"].append(f"sin CSV actual: {missing_cur} — porcentajes de esas ligas no actualizados")

    # 1) Filas equipo-partido
    to_write: list[tuple[str, str, dict[str, list[dict]]]] = []
    if seasons in ("current", "both"):
        to_write.append((cur_season, "cur", cur_by_div))
    if seasons in ("previous", "both"):
        to_write.append((prev_season, "prev", prev_by_div))
    for season, _, by_div in to_write:
        for div, rows in by_div.items():
            docs = {doc_id_for(r): r for r in rows}
            try:
                n = _write_changed("fdco_team_matches", "fdco_team_matches_state",
                                   f"{div}_{season}", docs, dry_run)
                summary["team_match_docs"][f"{div}_{season}"] = n
            except Exception as e:
                logger.error("fdco_team_matches: error escribiendo %s_%s", div, season, exc_info=True)
                summary["errors"].append(f"{div}_{season}: {e}")

    # 2) Porcentajes por equipo
    rate_docs = build_rate_docs(cur_by_div, prev_by_div, cur_season, prev_season)
    by_div: dict[str, dict[str, dict]] = {}
    for doc_id, doc in rate_docs.items():
        by_div.setdefault(doc["div"], {})[doc_id] = doc
    for div, docs in by_div.items():
        try:
            summary["rate_docs"][div] = _write_changed("fdco_team_market_rates", "fdco_team_rates_state",
                                                       div, docs, dry_run)
        except Exception as e:
            logger.error("fdco_team_matches: error escribiendo porcentajes %s", div, exc_info=True)
            summary["errors"].append(f"rates {div}: {e}")

    summary["ok"] = not summary["errors"] and not summary["failed_downloads"]
    logger.info("fdco_team_matches: %s", summary)
    return summary
