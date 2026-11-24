"""
services/sports-agent/analyzers/tennis_top.py

Tenis top en Tendencias (tp-v1) — filtro de categorías y partidos planos desde los docs de
tennis_tournaments (collectors/tennis_data.py). Puro: sin Firestore, sin red, sin escribir nada.

Categorías top (decisión del usuario, 24-nov-2026): Grand Slam, Masters 1000 / WTA 1000, 500,
250 y Finals (ATP y WTA). Lista CERRADA de valores de `series` (Series en ATP, Tier en WTA,
normalizados sin espacios ni signos); lo que no esté en la lista queda fuera y lo cuenta la
consulta de solo lectura (F0) para revisarlo. Además se excluyen por nombre de torneo los eventos
por equipos (United Cup, ATP Cup, Davis/Billie Jean King/Fed Cup, Laver Cup, Hopman Cup), los
Juegos Olímpicos y las Next Gen Finals, aunque traigan una serie de la lista.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Iterable, Optional

# Serie normalizada → categoría. Los nombres WTA anteriores a 2021 se mapean a su equivalente.
TOP_SERIES: dict[str, str] = {
    "grandslam": "grand_slam",
    "masters1000": "1000", "wta1000": "1000",
    "premiermandatory": "1000", "premier5": "1000",          # WTA hasta 2020
    "atp500": "500", "wta500": "500", "premier": "500",       # WTA hasta 2020
    "atp250": "250", "wta250": "250", "international": "250",  # WTA hasta 2020
    "masterscup": "finals", "atpfinals": "finals",
    "tourchampionships": "finals", "wtafinals": "finals",
}
CATEGORIES = ("grand_slam", "1000", "500", "250", "finals")

EXCLUDED_NAME_RE = re.compile(
    r"united cup|atp cup|davis cup|billie jean king|\bbjk\b|fed cup|laver cup|hopman"
    r"|olympic|olimpi|next ?gen", re.I)

VOID_STATUSES = ("walkover", "awarded", "unknown")


def norm_series(series) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(series or "").lower())


def tournament_category(doc: dict) -> tuple[Optional[str], str]:
    """(categoría, motivo). Categoría None = fuera; motivo 'top', 'excluido_por_nombre' o
    'serie_fuera_de_lista'."""
    name = f"{doc.get('tournament') or ''} {doc.get('location') or ''}"
    if EXCLUDED_NAME_RE.search(name):
        return None, "excluido_por_nombre"
    cat = TOP_SERIES.get(norm_series(doc.get("series")))
    return (cat, "top") if cat else (None, "serie_fuera_de_lista")


def is_top(doc: dict) -> bool:
    return tournament_category(doc)[0] is not None


def flat_matches(docs: Iterable[dict], top_only: bool = True) -> list[dict]:
    """Un registro por partido con lo del torneo (tour, superficie, best_of, categoría, inicio).
    Jugadores con clave '{tour}|{nombre}' (un mismo «Apellido I.» puede estar en ATP y WTA).
    Juegos de cada jugador sumados de los sets anotados."""
    out = []
    for d in docs:
        cat, _ = tournament_category(d)
        if top_only and cat is None:
            continue
        tour = str(d.get("tour") or "").lower()
        tid = f"{tour}_{d.get('year')}_{d.get('num')}_{d.get('tournament')}"
        bo = int(d.get("best_of") or 3)
        for m in d.get("matches") or []:
            sets = [tuple(s) for s in (m.get("sets") or [])]
            out.append({
                "tid": tid, "tour": tour, "category": cat, "series": d.get("series"),
                "surface": str(d.get("surface") or "").strip() or "?", "best_of": bo,
                "start": d.get("start") or m.get("date"), "date": m.get("date"), "round": m.get("round"),
                "w": f"{tour}|{m.get('winner')}", "l": f"{tour}|{m.get('loser')}",
                "wrank": m.get("wrank"), "lrank": m.get("lrank"),
                "sets": sets, "w_games": sum(a for a, _ in sets), "l_games": sum(b for _, b in sets),
                "total_games": m.get("total_games", sum(a + b for a, b in sets)),
                "n_sets": m.get("n_sets", len(sets)), "tiebreaks": m.get("tiebreaks", 0),
                "status": m.get("status") or "unknown", "valid": bool(m.get("valid")),
            })
    out.sort(key=lambda x: (x["date"] or "", x["tid"]))
    return out


# ── F0: agregados de solo lectura ──────────────────────────────────────────────

def series_summary(docs: Iterable[dict]) -> dict[tuple[str, str], dict]:
    """{(tour, serie tal cual): {torneos, partidos, categoria, motivo, fuera: [nombres de torneo]}}.
    Los nombres de torneo y de serie son categorías públicas (no hay jugadores)."""
    out: dict[tuple[str, str], dict] = {}
    for d in docs:
        key = (str(d.get("tour") or "?").lower(), str(d.get("series") or ""))
        cat, why = tournament_category(d)
        v = out.setdefault(key, {"torneos": 0, "partidos": 0, "categoria": TOP_SERIES.get(norm_series(key[1])),
                                 "motivos": Counter(), "fuera": set()})
        v["torneos"] += 1
        v["partidos"] += len(d.get("matches") or [])
        v["motivos"][why] += 1
        if cat is None:
            v["fuera"].add(str(d.get("tournament") or ""))
    return out


def shape_summary(matches: Iterable[dict]) -> dict[tuple[str, str, int], dict]:
    """{(tour, superficie, best_of): {partidos, valid, status: Counter}}."""
    out: dict = {}
    for m in matches:
        v = out.setdefault((m["tour"], m["surface"], m["best_of"]),
                           {"partidos": 0, "valid": 0, "status": Counter()})
        v["partidos"] += 1
        v["valid"] += int(m["valid"])
        v["status"][m["status"]] += 1
    return out


BUCKETS = ((1, 1), (2, 3), (4, 7), (8, 15), (16, 31), (32, 10 ** 6))


def bucket_label(lo: int, hi: int) -> str:
    return f"{lo}+" if hi >= 10 ** 6 else (str(lo) if lo == hi else f"{lo}-{hi}")


def player_surface_histogram(matches: list[dict], ref_date: str, months: int = 24,
                             min_surface: int = 8) -> dict[tuple[str, str], dict]:
    """
    Partidos por jugador y superficie en los `months` meses antes de ref_date (sin walkovers),
    como histograma por (tour, superficie): {buckets: {etiqueta: jugadores}, jugadores,
    partidos, partidos_ambos_min: partidos del último año de la ventana cuyos dos jugadores
    tenían ya ≥ min_surface partidos en esa superficie en los `months` meses anteriores a la fecha
    del partido}. Solo recuentos: ningún nombre sale de aquí.
    """
    ref = date.fromisoformat(ref_date)
    lo = (ref - timedelta(days=round(months * 30.44))).isoformat()
    last_year = (ref - timedelta(days=365)).isoformat()
    played = [m for m in matches if m["status"] not in VOID_STATUSES and m["date"] and m["date"] <= ref_date]
    counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    n_matches: Counter = Counter()
    for m in played:
        if m["date"] < lo:
            continue
        key = (m["tour"], m["surface"])
        counts[key][m["w"]] += 1
        counts[key][m["l"]] += 1
        n_matches[key] += 1
    # Partidos del último año con los dos jugadores ya con muestra: ventana móvil por superficie.
    both: Counter = Counter()
    total_last: Counter = Counter()
    hist: dict[tuple[str, str, str], list[str]] = defaultdict(list)   # (tour, sup, jugador) → fechas
    span = timedelta(days=round(months * 30.44))
    for m in played:
        key = (m["tour"], m["surface"])
        if m["date"] >= last_year:
            since = (date.fromisoformat(m["date"]) - span).isoformat()
            ok = all(sum(1 for d in hist[(*key, p)] if since <= d < m["date"]) >= min_surface
                     for p in (m["w"], m["l"]))
            total_last[key] += 1
            both[key] += int(ok)
        hist[(*key, m["w"])].append(m["date"])
        hist[(*key, m["l"])].append(m["date"])
    out = {}
    for key in sorted(set(counts) | set(total_last)):
        c = counts.get(key, Counter())
        b = {bucket_label(a, z): sum(1 for n in c.values() if a <= n <= z) for a, z in BUCKETS}
        out[key] = {"jugadores": len(c), "partidos": n_matches[key], "buckets": b,
                    "jugadores_min": sum(1 for n in c.values() if n >= min_surface),
                    "partidos_ultimo_año": total_last[key], "partidos_ambos_min": both[key]}
    return out
