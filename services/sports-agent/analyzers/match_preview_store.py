"""
services/sports-agent/analyzers/match_preview_store.py

Ficha de partido de Tendencias (mp-v1) — Paso 3: guardar, congelar y graduar.
Aislado del feed actual, de trend_finder y de Polymarket: solo escribe en sus colecciones nuevas
(match_previews, match_preview_grades, match_preview_metrics, match_previews_state) y en el
contador de cuota de Highlightly. dry_run=True por defecto en las tres funciones: calculan y
cuentan, sin escribir nada (ni siquiera el estado). Única excepción: si se pide Highlightly,
cada petición se apunta en el contador de la reserva manual, también en dry-run.

  run_match_preview_backtest → match_preview_metrics/{versión}_backtest
      Backtest walk-forward de la temporada anterior completa y de lo jugado de la actual (como
      el del 8-nov: 2025/26 + 2026/27, con la 2024/25 de historial). De aquí salen las
      etiquetas "ventaja medida" por mercado y liga; se recalcula a mano (manual).
  run_match_previews → match_previews/{match_id}
      Lista del paso 1 + cálculo del paso 2. Se recalcula solo si cambia el hash del contenido
      y nunca después de freeze_at (= inicio − 2 h): lo último escrito antes de freeze_at es la
      versión congelada, la que se gradúa. Sin etiquetas del backtest → todo "solo referencia".
  run_match_preview_grade → match_preview_grades/{match_id} + match_preview_metrics/{versión}_live
      Partidos empezados hace más de GRADE_AFTER_H horas, contra el CSV de la temporada de
      football-data.co.uk (mismos nombres: los de la ficha). Se anula (status "anulada") si la
      ficha se escribió después del inicio o usó datos del día del partido o posteriores.
      A los NO_RESULT_DAYS días sin resultado queda "sin resultado".
"""
import asyncio
import hashlib
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from analyzers import match_preview as mp
from collectors.fdco_team_matches import (
    LOWER_DIVS, MAIN_DIVS, _to_team_rows, current_season_end_year, fetch_csv_rows, season_code,
)

logger = logging.getLogger(__name__)

PREVIEWS, GRADES, METRICS, STATE = ("match_previews", "match_preview_grades",
                                    "match_preview_metrics", "match_previews_state")
FREEZE_BEFORE = timedelta(hours=2)
GRADE_AFTER_H = 6
NO_RESULT_DAYS = 7
GRADE_LOOKBACK_DAYS = 14
HL_RECHECK_H = 20                   # (liga, día) mirado en Highlightly hace menos → no se repite
_HTTP_TIMEOUT = 30.0
REVIEW_DIVS = ("N1", "P1")          # DED y PPL: alias a revisar aparte en el dry-run


def backtest_doc_id(version: str = mp.MODEL_VERSION) -> str:
    return f"{version}_backtest"


def live_doc_id(version: str = mp.MODEL_VERSION) -> str:
    return f"{version}_live"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── Historial: CSV de football-data.co.uk (los mismos del colector de equipos) ──

async def load_matches(season: str, client: Optional[httpx.AsyncClient] = None) -> tuple[list[dict], list[str]]:
    """Partidos de `season` y de la anterior en las 8 ligas, más las segundas de la anterior
    (solo de equipos que en `season` están en una principal). Devuelve (partidos, fallos)."""
    prev = mp.prev_season(season)
    rows, failed = [], []
    own = client is None
    client = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    try:
        for div in MAIN_DIVS:
            for s in (season, prev):
                raw = await fetch_csv_rows(client, div, s)
                if raw is None:
                    failed.append(f"{div}_{s}")
                else:
                    rows.extend(_to_team_rows(raw, div, s))
                await asyncio.sleep(0.3)        # cortesía con football-data.co.uk
        main_teams = {r["team"] for r in rows if r["season"] == season}
        for div in LOWER_DIVS:
            raw = await fetch_csv_rows(client, div, prev)
            if raw is None:
                failed.append(f"{div}_{prev}")
            else:
                rows.extend(r for r in _to_team_rows(raw, div, prev) if r["team"] in main_teams)
            await asyncio.sleep(0.3)
    finally:
        if own:
            await client.aclose()
    return mp.matches_from_team_rows(rows), failed


def _season_now(now: datetime) -> str:
    return season_code(current_season_end_year(now.date()))


# ── Contenido, hash y congelado ────────────────────────────────────────────────

_FIXTURE_FIELDS = ("match_id", "div", "league", "date", "kickoff_utc", "home_team", "away_team", "source",
                   "src_home", "src_away", "hl_match_id", "fd_match_id", "kickoff_source", "kickoff_csv_utc")


def preview_content(fixture: dict, pred: dict, labels_source: Optional[str]) -> dict:
    """Lo que se guarda (sin marcas de tiempo): partido + ficha + de dónde salen las etiquetas."""
    return {**{k: fixture.get(k) for k in _FIXTURE_FIELDS if fixture.get(k) is not None},
            "odds": fixture.get("odds"), **pred, "labels_source": labels_source}


def content_hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:16]


def freeze_at(kickoff_utc: str) -> datetime:
    return _parse(kickoff_utc) - FREEZE_BEFORE


def decide(content: dict, existing: Optional[dict], now: datetime) -> str:
    """'nueva' | 'cambia' | 'igual' | 'congelada' (no se toca) | 'tarde' (nueva tras freeze_at)."""
    fz = freeze_at(content["kickoff_utc"])
    if existing is not None:
        # si la hora cambió, manda la más temprana de las dos: nunca se toca tras congelar
        if now >= min(fz, _parse(existing.get("freeze_at") or fz.isoformat())):
            return "congelada"
        return "igual" if existing.get("hash") == content_hash(content) else "cambia"
    if now >= _parse(content["kickoff_utc"]):
        return "empezado"
    return "tarde" if now >= fz else "nueva"


def labels_from_doc(doc: Optional[dict]) -> dict[tuple[str, str], str]:
    if not doc:
        return {}
    return mp.labels_from_metrics(doc.get("metrics") or {})


def _mapping_review(fixtures: dict, divs=REVIEW_DIVS) -> dict:
    """team_mapping y unmatched_teams de DED/PPL, con el método de cada traducción y los
    equipos de football-data.co.uk de esa liga que no ha traducido ninguna fuente."""
    from collectors.match_fixtures import RAW_ALIASES, TEAM_ALIASES, _raw_key
    from collectors.team_identity import normalize

    out = {}
    for div in divs:
        maps = [m for m in fixtures.get("team_mapping", []) if m["div"] == div]
        for m in maps:
            n = normalize(m["name"])
            m["method"] = ("alias" if (RAW_ALIASES.get(_raw_key(m["name"])) or TEAM_ALIASES.get(n))
                           else "igual" if n == normalize(m["fdco"]) else "contención (candidato único)")
        seen = {m["fdco"] for m in maps}
        teams = {t for x in fixtures.get("matches", []) if x["div"] == div for t in (x["home_team"], x["away_team"])}
        out[div] = {
            "team_mapping": maps,
            "unmatched_teams": [u for u in fixtures.get("unmatched_teams", []) if u.startswith(f"{div}:")],
            "fdco_in_window_not_translated": sorted(teams - seen),
        }
    return out


# ── Lectura / escritura (Firestore por col()) ──────────────────────────────────

def _get_doc(collection: str, doc_id: str) -> Optional[dict]:
    from shared.firestore_client import col
    snap = col(collection).document(doc_id).get()
    return snap.to_dict() if snap.exists else None


def _get_many(collection: str, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    from shared.firestore_client import col, get_client
    refs = [col(collection).document(i) for i in ids]
    return {s.id: s.to_dict() for s in get_client().get_all(refs) if s.exists}


def _set_docs(collection: str, docs: dict[str, dict]) -> None:
    from shared.firestore_client import col, get_client
    client = get_client()
    items = list(docs.items())
    for i in range(0, len(items), 400):
        batch = client.batch()
        for doc_id, doc in items[i:i + 400]:
            batch.set(col(collection).document(doc_id), doc)
        batch.commit()


# ── 1. Backtest → etiquetas ────────────────────────────────────────────────────

async def run_match_preview_backtest(dry_run: bool = True, now: Optional[datetime] = None,
                                     matches: Optional[list[dict]] = None) -> dict:
    now = now or _now()
    cur = _season_now(now)
    season = mp.prev_season(cur)
    seasons = (season, cur)                  # como el del 8-nov: la anterior completa + lo jugado
    failed: list[str] = []
    if matches is None:
        # historial de la anterior (con la suya previa y sus ascendidos) + el de la actual
        older, f1 = await load_matches(season)
        newer, f2 = await load_matches(cur)
        seen, matches = set(), []
        for m in older + newer:
            key = (m["div"], m["season"], m["date"], m["home"], m["away"])
            if key not in seen:
                seen.add(key)
                matches.append(m)
        failed = sorted(set(f1 + f2))
    summary = {"ok": True, "dry_run": dry_run, "doc_id": backtest_doc_id(), "seasons": list(seasons),
               "failed_downloads": failed, "errors": []}
    if failed:
        summary.update(ok=False, errors=[f"faltan CSV: {failed} — no se calcula con datos parciales"])
        return summary
    hist = mp.History(matches)
    rows = await asyncio.to_thread(mp.backtest_rows, hist, seasons)
    metrics = mp.market_metrics(rows)
    labels = mp.labels_from_metrics(metrics)
    edge = sorted(f"{m}|{d}" for (m, d), lab in labels.items() if lab == mp.LABEL_EDGE)
    summary.update(n_rows=len(rows), n_matches=len({r[7] for r in rows}), edge_cells=edge,
                   by_market={k: {f: v[f] for f in ("n_matches", "bss_vs_league", "ci_lo", "ci_hi", "ci_label", "label")}
                              for k, v in metrics.items() if k.endswith("|ALL")})
    doc = {"model_version": mp.MODEL_VERSION, "kind": "backtest", "seasons": list(seasons),
           "params": {"k": mp.SHRINK_K, "min_cur": mp.MIN_CUR, "rule": "IC95 por partido < 0 en la liga y en el conjunto"},
           "n_rows": len(rows), "metrics": metrics, "computed_at": now.isoformat()}
    if not dry_run:
        _set_docs(METRICS, {backtest_doc_id(): doc})
    summary["written"] = 0 if dry_run else 1
    logger.info("match_preview_backtest: %s", {k: v for k, v in summary.items() if k != "by_market"})
    return summary


# ── 2. Fichas ──────────────────────────────────────────────────────────────────

def recent_checks(state: dict, now: datetime, hours: int = HL_RECHECK_H) -> frozenset:
    """(liga, día) mirados en Highlightly hace menos de `hours` horas (state["hl_checked_at"])."""
    out = set()
    for key, ts in (state.get("hl_checked_at") or {}).items():
        div, _, day = key.partition("|")
        try:
            if now - _parse(ts) < timedelta(hours=hours):
                out.add((div, day))
        except (TypeError, ValueError, AttributeError):
            continue
    return frozenset(out)


def merge_checks(state: dict, checked: list[dict], now: datetime, keep_h: int = 48) -> dict[str, str]:
    """Lo mirado ahora más lo anterior de menos de keep_h horas."""
    out = {}
    for key, ts in (state.get("hl_checked_at") or {}).items():
        try:
            if now - _parse(ts) < timedelta(hours=keep_h):
                out[key] = ts
        except (TypeError, ValueError, AttributeError):
            continue
    for p in checked:
        out[f"{p['div']}|{p['day']}"] = now.isoformat()
    return out


async def run_match_previews(dry_run: bool = True, now: Optional[datetime] = None, days: int = 3,
                             highlightly: bool = False, hl_max: int = 3, hl_divs: Optional[frozenset] = None,
                             fixtures: Optional[dict] = None, matches: Optional[list[dict]] = None,
                             labels_doc: Optional[dict] = None, existing: Optional[dict[str, dict]] = None,
                             state: Optional[dict] = None) -> dict:
    """
    highlightly: usar Highlightly para lo que no cubren fixtures.csv ni upcoming_matches (en la
                 práctica SP2), con la reserva manual y como mucho hl_max peticiones. Necesita
                 HIGHLIGHTLY_API_KEY en el servicio; sin ella lo pendiente sale en `unchecked`.
                 Un (liga, día) mirado hace menos de HL_RECHECK_H horas no se repite.
    hl_divs:     si se pasa, Highlightly solo para esas ligas (p. ej. {"SP2"}).
    Los parámetros fixtures/matches/labels_doc/existing/state son para tests (None = leer).
    """
    from collectors.match_fixtures import build_fixture_list

    now = now or _now()
    season = _season_now(now)
    summary: dict = {"ok": True, "dry_run": dry_run, "now": now.isoformat(), "season": season,
                     "counts": {}, "previews": [], "errors": []}

    if state is None:
        state = (_get_doc(STATE, "previews") or {}) if fixtures is None else {}
    already = recent_checks(state, now)

    if fixtures is None:
        api_key = os.environ.get("HIGHLIGHTLY_API_KEY") if highlightly else None
        quota = None
        if api_key:
            # También en dry-run: cada petición se apunta en el contador de la reserva manual.
            from shared.api_quota_manager import QuotaManager
            quota = QuotaManager()
        fixtures = await build_fixture_list(now=now, days=days, api_key=api_key, quota=quota, season=season,
                                            already_checked=already, max_hl_requests=hl_max,
                                            hl_divs=hl_divs)
        if highlightly and not api_key:
            summary["errors"].append("highlightly=true pero HIGHLIGHTLY_API_KEY no está en el servicio")
    summary["fixtures"] = {k: fixtures.get(k) for k in ("window_days", "by_div", "csv_rows_by_div", "hl_requests",
                                                       "hl_checked", "unchecked", "upcoming_fresh", "errors")}
    summary["team_mapping"] = fixtures.get("team_mapping", [])
    summary["unmatched_teams"] = fixtures.get("unmatched_teams", [])
    summary["review_ded_ppl"] = _mapping_review(fixtures)

    if matches is None:
        matches, failed = await load_matches(season)
        if failed:
            summary["errors"].append(f"faltan CSV de historial: {failed}")
    hist = mp.History(matches)

    if labels_doc is None:
        labels_doc = _get_doc(METRICS, backtest_doc_id())
    labels = labels_from_doc(labels_doc)
    labels_source = backtest_doc_id() if labels_doc else None
    if not labels_doc:
        summary["errors"].append(f"sin {METRICS}/{backtest_doc_id()}: todas las etiquetas = solo referencia")

    fx = fixtures.get("matches", [])
    if existing is None:
        existing = _get_many(PREVIEWS, [m["match_id"] for m in fx])

    to_write: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    for f in fx:
        mid = f["match_id"]
        if not f.get("team_matched", True):
            counts["sin_ficha_nombre"] += 1
            summary["previews"].append({"match_id": mid, "action": "sin ficha: equipo sin emparejar",
                                        "src": [f.get("src_home"), f.get("src_away")]})
            continue
        pred = mp.predict_match(hist, f["div"], season, f["date"], f["home_team"], f["away_team"],
                                labels=labels, odds=f.get("odds"))
        content = preview_content(f, pred, labels_source)
        action = decide(content, existing.get(mid), now)
        counts[action] += 1
        hl = sorted(f"{s}:{side}" for s, b in pred["stats"].items() if b.get("available")
                    for side, blk in b["lines"].items() if blk["highlight"])
        summary["previews"].append({
            "match_id": mid, "kickoff_utc": f["kickoff_utc"], "source": f["source"], "action": action,
            "low_sample": pred["low_sample"], "n_cur": [pred["teams"]["home"]["n_cur"], pred["teams"]["away"]["n_cur"]],
            "data_cutoff": pred["data_cutoff"], "highlighted": hl,
            "exp_total": {s: b["exp_total"] for s, b in pred["stats"].items() if b.get("available")},
        })
        if action in ("nueva", "cambia", "tarde"):
            prev = existing.get(mid) or {}
            to_write[mid] = {**content, "hash": content_hash(content), "computed_at": now.isoformat(),
                             "created_at": prev.get("created_at") or now.isoformat(),
                             "freeze_at": freeze_at(f["kickoff_utc"]).isoformat(),
                             "written_after_freeze": action == "tarde", "n_writes": prev.get("n_writes", 0) + 1}
    summary["counts"] = dict(counts)
    summary["would_write" if dry_run else "written"] = len(to_write)

    if not dry_run:
        try:
            _set_docs(PREVIEWS, to_write)
            _set_docs(STATE, {"previews": {"hl_checked_at": merge_checks(state, fixtures.get("hl_checked", []), now),
                                           "last_run": now.isoformat(), "last_counts": dict(counts)}})
        except Exception as e:
            logger.error("match_previews: error escribiendo", exc_info=True)
            summary["errors"].append(f"escritura: {e}")
    summary["ok"] = not any(e.startswith(("escritura", "faltan")) for e in summary["errors"])
    logger.info("match_previews: %s; escribe %d; sin emparejar %d", dict(counts), len(to_write),
                len(summary["unmatched_teams"]))
    return summary


# ── 3. Graduación ──────────────────────────────────────────────────────────────

def find_result(preview: dict, by_teams: dict[tuple[str, str, str], list[dict]]) -> Optional[dict]:
    """Mismo local y visitante en la misma división, a ±1 día de la fecha de la ficha."""
    d0 = datetime.fromisoformat(preview["date"]).date()
    for m in by_teams.get((preview["div"], preview["home_team"], preview["away_team"]), []):
        if abs((datetime.fromisoformat(m["date"]).date() - d0).days) <= 1:
            return m
    return None


def grade_preview(preview: dict, result: dict) -> dict:
    """Una fila por línea: y (0/1), p_model, p_league y el Brier de cada una."""
    lines = []
    for s, block in (preview.get("stats") or {}).items():
        if not block.get("available"):
            continue
        for side, blk in block["lines"].items():
            if side == "total":
                y_val = None if result["h"][s] is None or result["a"][s] is None else result["h"][s] + result["a"][s]
            else:
                y_val = result["h" if side == "home" else "a"][s]
            if y_val is None:
                continue
            for lk, ln in blk["lines"].items():
                if ln.get("p_league") is None:
                    continue
                y = int(y_val > ln["line"])
                lines.append({"stat": s, "side": side, "market": blk["market"], "line": ln["line"], "y": y,
                              "value": y_val, "p_model": ln["p_model"], "p_league": ln["p_league"],
                              "brier_model": round((ln["p_model"] - y) ** 2, 5),
                              "brier_league": round((ln["p_league"] - y) ** 2, 5),
                              "label": blk["label"], "highlight": blk["highlight"]})
    return {"lines": lines, "result": {"h": result["h"], "a": result["a"], "date": result["date"]}}


def validity(preview: dict) -> Optional[str]:
    """None si la ficha vale para graduar; si no, el motivo."""
    ko = _parse(preview["kickoff_utc"])
    if _parse(preview["computed_at"]) >= ko:
        return "ficha escrita después del inicio"
    if preview.get("data_cutoff") and preview["data_cutoff"] >= preview["date"]:
        return "usó datos del día del partido o posteriores"
    return None


def rows_from_grades(grades: list[dict]) -> list[tuple]:
    """Filas con el formato de backtest_rows para market_metrics."""
    rows = []
    for g in grades:
        if g.get("status") != "graduada":
            continue
        for ln in g["lines"]:
            rows.append((g.get("season"), g["div"], ln["market"], ln["line"], ln["y"], ln["p_league"],
                         ln["p_model"], g["match_id"]))
    return rows


def _read_previews_to_grade(now: datetime) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    lo = (now - timedelta(days=GRADE_LOOKBACK_DAYS)).isoformat()
    hi = (now - timedelta(hours=GRADE_AFTER_H)).isoformat()
    q = (col(PREVIEWS).where(filter=FieldFilter("kickoff_utc", ">=", lo))
         .where(filter=FieldFilter("kickoff_utc", "<=", hi)))
    return [s.to_dict() for s in q.stream()]


def _read_all_grades() -> list[dict]:
    from shared.firestore_client import col
    return [s.to_dict() for s in col(GRADES).stream()]


async def run_match_preview_grade(dry_run: bool = True, now: Optional[datetime] = None,
                                  previews: Optional[list[dict]] = None, matches: Optional[list[dict]] = None,
                                  graded: Optional[dict[str, dict]] = None,
                                  all_grades: Optional[list[dict]] = None) -> dict:
    now = now or _now()
    summary: dict = {"ok": True, "dry_run": dry_run, "counts": {}, "grades": [], "errors": []}
    if previews is None:
        previews = _read_previews_to_grade(now)
    if graded is None:
        graded = _get_many(GRADES, [p["match_id"] for p in previews])
    pending = [p for p in previews if p["match_id"] not in graded
               or graded[p["match_id"]].get("status") == "pendiente"]
    if matches is None:
        seasons = sorted({p["season"] for p in pending})
        matches = []
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            for season in seasons:
                for div in sorted({p["div"] for p in pending if p["season"] == season}):
                    raw = await fetch_csv_rows(client, div, season)
                    if raw is None:
                        summary["errors"].append(f"sin CSV {div}_{season}")
                        continue
                    matches.extend(mp.matches_from_team_rows(_to_team_rows(raw, div, season)))
                    await asyncio.sleep(0.3)
    by_teams: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for m in matches:
        by_teams[(m["div"], m["home"], m["away"])].append(m)

    docs: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    for p in pending:
        base = {"match_id": p["match_id"], "div": p["div"], "season": p["season"], "date": p["date"],
                "kickoff_utc": p["kickoff_utc"], "home_team": p["home_team"], "away_team": p["away_team"],
                "model_version": p.get("model_version"), "preview_hash": p.get("hash"),
                "preview_computed_at": p.get("computed_at"), "graded_at": now.isoformat()}
        why = validity(p)
        res = find_result(p, by_teams)
        if why:
            doc = {**base, "status": "anulada", "reason": why}
        elif res is not None:
            doc = {**base, "status": "graduada", **grade_preview(p, res)}
        elif now - _parse(p["kickoff_utc"]) >= timedelta(days=NO_RESULT_DAYS):
            doc = {**base, "status": "sin resultado"}
        else:
            counts["esperando CSV"] += 1
            continue
        counts[doc["status"]] += 1
        docs[p["match_id"]] = doc
        summary["grades"].append({"match_id": p["match_id"], "status": doc["status"],
                                  "reason": doc.get("reason"), "n_lines": len(doc.get("lines", []))})
    summary["counts"] = dict(counts)

    if all_grades is None:
        all_grades = _read_all_grades()
    merged = {g["match_id"]: g for g in all_grades}
    merged.update(docs)
    rows = rows_from_grades(list(merged.values()))
    live = mp.market_metrics(rows) if rows else {}
    summary["live_metrics"] = {k: {f: v[f] for f in ("n_matches", "bss_vs_league", "ci_lo", "ci_hi", "label")}
                               for k, v in live.items() if k.endswith("|ALL")}
    summary["would_write" if dry_run else "written"] = len(docs)
    if not dry_run:
        try:
            _set_docs(GRADES, docs)
            if rows:
                _set_docs(METRICS, {live_doc_id(): {"model_version": mp.MODEL_VERSION, "kind": "live",
                                                    "n_rows": len(rows), "metrics": live,
                                                    "computed_at": now.isoformat()}})
        except Exception as e:
            logger.error("match_preview_grade: error escribiendo", exc_info=True)
            summary["errors"].append(f"escritura: {e}")
    summary["ok"] = not any(e.startswith("escritura") for e in summary["errors"])
    logger.info("match_preview_grade: %s", dict(counts))
    return summary


# ── 4. Exportación para la web de Tendencias ───────────────────────────────────
# Solo lo que pinta la ficha: partido, muestra, esperados y líneas (modelo frente a la media de
# la liga) con su etiqueta. Fuera: cuotas, ids y nombres de origen (Highlightly incluido),
# parámetros del modelo y hash. Los números salen del cálculo con football-data.co.uk.
WEB_DAYS = 7
WEB_PAST = FREEZE_BEFORE            # un partido sigue en la web hasta 2 h después de empezar


def web_match(p: dict) -> dict:
    stats = {}
    for s in mp.STATS:
        b = (p.get("stats") or {}).get(s) or {}
        if not b.get("available"):
            stats[s] = {"available": False}
            continue
        stats[s] = {
            "available": True,
            "exp": [b["exp_home"], b["exp_away"], b["exp_total"]],
            "league_mean": [b["league_mean_home"], b["league_mean_away"]],
            "blocks": {side: {"label": blk["label"], "highlight": bool(blk.get("highlight")),
                              "lines": [[ln["line"], ln["p_model"], ln["p_league"], ln.get("league_n")]
                                        for ln in sorted(blk["lines"].values(), key=lambda x: x["line"])]}
                       for side, blk in b["lines"].items()},
        }
    teams = p.get("teams") or {}
    return {
        "id": p["match_id"], "div": p["div"], "kickoff_utc": p["kickoff_utc"],
        "home": p["home_team"], "away": p["away_team"], "data_cutoff": p.get("data_cutoff"),
        "low_sample": bool(p.get("low_sample")),
        "sample": {side: {"n_cur": t.get("n_cur"), "n_eff": t.get("n_eff"),
                          "insufficient": bool(t.get("insufficient"))}
                   for side, t in teams.items()},
        "computed_at": p.get("computed_at"), "stats": stats,
    }


def web_export(previews: list[dict], now: datetime, days: int = WEB_DAYS) -> dict:
    lo, hi = now - WEB_PAST, now + timedelta(days=days)
    keep = sorted((p for p in previews if lo <= _parse(p["kickoff_utc"]) <= hi),
                  key=lambda p: (p["kickoff_utc"], p["div"], p["home_team"]))
    return {"model_version": mp.MODEL_VERSION, "generated_at": now.isoformat(), "days": days,
            "min_cur": mp.MIN_CUR, "matches": [web_match(p) for p in keep]}


def _read_upcoming_previews(now: datetime, days: int = WEB_DAYS) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    lo = (now - WEB_PAST).isoformat()
    hi = (now + timedelta(days=days)).isoformat()
    q = (col(PREVIEWS).where(filter=FieldFilter("kickoff_utc", ">=", lo))
         .where(filter=FieldFilter("kickoff_utc", "<=", hi)))
    return [s.to_dict() for s in q.stream()]


def build_web_export(now: Optional[datetime] = None, days: int = WEB_DAYS) -> dict:
    now = now or _now()
    return web_export(_read_upcoming_previews(now, days), now, days)
