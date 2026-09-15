"""
services/sports-agent/analyzers/trend_finder.py

Feed de tendencias estadísticas para el tema Telegram "Tendencias" — SOLO
estadística/hit-rate, sin cuotas ni EV. Aislado a propósito del sistema de
valor: colección propia trend_signals, lee team_stats/team_corner_stats en
modo solo-lectura y nunca escribe en predictions/shadow_trades/accuracy_log
ni toca model_weights.

Dos tipos de evidencia, marcados de forma distinta en el mensaje y graduados
por separado (pattern_type en trend_signals):
  series  — serie partido a partido del propio equipo (team_stats.raw_matches):
            goles marcados, BTTS, margen de victoria (hándicap). Hit-rate real
            sobre una ventana de partidos → evidencia fuerte.
  rolling — promedio rolling-15 de football-data.co.uk (team_corner_stats):
            córners y tarjetas. No hay serie partido a partido, solo el
            promedio acumulado vs la media de la liga → evidencia más débil.
"""
import logging
import math
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from collectors.fdco_collector import _slugify
from shared.config import (
    CLOUD_RUN_TOKEN,
    TELEGRAM_BOT_URL,
    TREND_MAX_SIGNALS_PER_RUN,
    TREND_MAX_SIGNALS_PER_TEAM,
    TREND_ROLLING_MIN_RATIO,
    TREND_ROLLING_MIN_SAMPLE,
    TREND_SERIES_MIN_HIT_RATE,
    TREND_SERIES_MIN_SAMPLE,
    TREND_SERIES_WINDOW,
)

logger = logging.getLogger(__name__)

# Umbrales de goles a probar de mayor a menor — se emite el más alto que aún
# cumpla el hit-rate: es la afirmación más específica que se sostiene con los datos.
_GOAL_THRESHOLDS = (3, 2)
_HANDICAP_MARGIN = 2  # "ganó por 2+"

_MARKET_EMOJI = {
    "team_goals_over": "⚽",
    "btts": "🟩",
    "handicap": "📐",
    "corners": "🚩",
    "cards": "🟨",
}
_DISCLAIMER = "📈 Tendencia estadística, no consejo de inversión."


# ── Lectura de datos (solo-lectura) ─────────────────────────────────────────────

def _read_team_stats(team_id: int) -> dict:
    from shared.firestore_client import col
    try:
        snap = col("team_stats").document(str(team_id)).get()
        return (snap.to_dict() or {}) if snap.exists else {}
    except Exception:
        logger.error("trend_finder: error leyendo team_stats(%s)", team_id, exc_info=True)
        return {}


def _read_corner_stats(league: str, team_name: str) -> dict:
    from shared.firestore_client import col
    doc_id = f"{league}_{_slugify(team_name)}"
    try:
        snap = col("team_corner_stats").document(doc_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}
    except Exception:
        logger.error("trend_finder: error leyendo team_corner_stats(%s)", doc_id, exc_info=True)
        return {}


_LEAGUE_AVG_CACHE: dict[str, tuple[datetime, dict]] = {}
_LEAGUE_AVG_TTL_SECONDS = 3600


def _league_averages(league: str) -> dict:
    """
    Media de córners/tarjetas de la liga a partir de team_corner_stats (~20
    equipos/liga). Cacheado 1h en memoria — evita releer la colección por cada
    candidato de la jornada.
    """
    now = datetime.now(timezone.utc)
    cached = _LEAGUE_AVG_CACHE.get(league)
    if cached and (now - cached[0]).total_seconds() < _LEAGUE_AVG_TTL_SECONDS:
        return cached[1]

    from shared.firestore_client import col
    corners_sum, cards_sum, n = 0.0, 0.0, 0
    try:
        query = col("team_corner_stats").where(filter=FieldFilter("league", "==", league))
        for d in query.stream():
            doc = d.to_dict() or {}
            corners_sum += (doc.get("home_corners", 0) + doc.get("away_corners", 0)) / 2
            cards_sum += (
                doc.get("home_yellows", 0) + doc.get("away_yellows", 0)
                + doc.get("home_reds", 0) + doc.get("away_reds", 0)
            ) / 2
            n += 1
    except Exception:
        logger.error("trend_finder: error calculando medias de liga %s", league, exc_info=True)

    result = {"corners": round(corners_sum / n, 2), "cards": round(cards_sum / n, 2), "n_teams": n} if n else {}
    _LEAGUE_AVG_CACHE[league] = (now, result)
    return result


def _team_series(raw_matches: list[dict], team_id: int, window: int) -> list[dict]:
    """
    Serie del propio equipo (gf/ga por partido), más reciente primero.
    raw_matches ya viene ordenado DESC (save_team_stats._merge_raw_matches).
    """
    out = []
    for m in raw_matches[:window]:
        was_home = m.get("home_team_id") == team_id
        gf = m.get("goals_home") if was_home else m.get("goals_away")
        ga = m.get("goals_away") if was_home else m.get("goals_home")
        if gf is None or ga is None:
            continue
        out.append({"gf": gf, "ga": ga})
    return out


def _score(rate_or_ratio: float, n: int) -> float:
    """'Fuerza' del patrón: hit-rate/ratio alto, sin premiar en exceso una
    muestra diminuta frente a un porcentaje algo menor con más partidos detrás."""
    return round(rate_or_ratio * math.log(max(n, 2)), 4)


# ── Candidatos: series (hit-rate, evidencia fuerte) ─────────────────────────────

def _series_candidates(team_name: str, team_id: int, side: str, opponent: str,
                        raw_matches: list[dict]) -> list[dict]:
    matches = _team_series(raw_matches, team_id, TREND_SERIES_WINDOW)
    n = len(matches)
    if n < TREND_SERIES_MIN_SAMPLE:
        return []

    candidates: list[dict] = []

    for threshold in _GOAL_THRESHOLDS:
        hits = sum(1 for m in matches if m["gf"] >= threshold)
        rate = hits / n
        if rate >= TREND_SERIES_MIN_HIT_RATE:
            candidates.append({
                "market": "team_goals_over", "threshold": threshold,
                "sample": n, "rate": round(rate, 4),
                "label": f"{threshold}+ goles",
                "detail": f"Marcó {threshold}+ goles en {hits} de sus últimos {n} partidos ({rate*100:.0f}%)",
            })
            break  # el umbral más alto que cumple es la señal a emitir, no los dos

    hits = sum(1 for m in matches if m["gf"] > 0 and m["ga"] > 0)
    rate = hits / n
    if rate >= TREND_SERIES_MIN_HIT_RATE:
        candidates.append({
            "market": "btts", "threshold": None,
            "sample": n, "rate": round(rate, 4),
            "label": "Ambos marcan",
            "detail": f"BTTS se cumplió en {hits} de sus últimos {n} partidos ({rate*100:.0f}%)",
        })

    hits = sum(1 for m in matches if (m["gf"] - m["ga"]) >= _HANDICAP_MARGIN)
    rate = hits / n
    if rate >= TREND_SERIES_MIN_HIT_RATE:
        candidates.append({
            "market": "handicap", "threshold": _HANDICAP_MARGIN,
            "sample": n, "rate": round(rate, 4),
            "label": f"Hándicap -{_HANDICAP_MARGIN}",
            "detail": f"Ganó por margen de {_HANDICAP_MARGIN}+ goles en {hits} de sus últimos {n} partidos ({rate*100:.0f}%)",
        })

    for c in candidates:
        c.update({
            "pattern_type": "series", "team": team_name, "team_id": team_id,
            "opponent": opponent, "side": side, "score": _score(c["rate"], c["sample"]),
        })
    return candidates


# ── Candidatos: rolling (promedio, evidencia más débil) ─────────────────────────

def _rolling_candidates(team_name: str, side: str, opponent: str, league: str,
                         corner_stats: dict) -> list[dict]:
    if not corner_stats:
        return []
    league_avg = _league_averages(league)
    if not league_avg:
        return []

    sample = corner_stats.get(f"{side}_matches", 0)
    if sample < TREND_ROLLING_MIN_SAMPLE:
        return []

    candidates: list[dict] = []

    team_corners = corner_stats.get(f"{side}_corners", 0.0)
    league_corners = league_avg.get("corners", 0)
    if league_corners > 0 and team_corners / league_corners >= TREND_ROLLING_MIN_RATIO:
        ratio = team_corners / league_corners
        n_line = max(1, math.floor(team_corners))
        candidates.append({
            "market": "corners", "threshold": n_line,
            "sample": sample, "rate": round(ratio, 3),
            "label": f"{n_line}+ córners",
            "detail": (f"Promedia {team_corners:.1f} córners por partido "
                       f"en sus últimos {sample} (liga: {league_corners:.1f})"),
        })

    team_cards = corner_stats.get(f"{side}_yellows", 0.0) + corner_stats.get(f"{side}_reds", 0.0)
    league_cards = league_avg.get("cards", 0)
    if league_cards > 0 and team_cards / league_cards >= TREND_ROLLING_MIN_RATIO:
        ratio = team_cards / league_cards
        n_line = max(1, math.floor(team_cards))
        candidates.append({
            "market": "cards", "threshold": n_line,
            "sample": sample, "rate": round(ratio, 3),
            "label": f"{n_line}+ tarjetas",
            "detail": (f"Promedia {team_cards:.1f} tarjetas por partido "
                       f"en sus últimos {sample} (liga: {league_cards:.1f})"),
        })

    for c in candidates:
        c.update({
            "pattern_type": "rolling", "team": team_name, "team_id": None,
            "opponent": opponent, "side": side, "score": _score(c["rate"], c["sample"]),
        })
    return candidates


async def _fixture_candidates(enriched: dict) -> list[dict]:
    match_id = enriched.get("match_id", "")
    league = enriched.get("league", "")
    home_team = enriched.get("home_team", "")
    away_team = enriched.get("away_team", "")
    home_id = enriched.get("home_team_id")
    away_id = enriched.get("away_team_id")
    match_date = enriched.get("match_date") or enriched.get("date")

    home_stats = _read_team_stats(home_id) if home_id else {}
    away_stats = _read_team_stats(away_id) if away_id else {}
    home_corner = _read_corner_stats(league, home_team)
    away_corner = _read_corner_stats(league, away_team)

    out: list[dict] = []
    if home_id and home_stats.get("raw_matches"):
        out += _series_candidates(home_team, home_id, "home", away_team, home_stats["raw_matches"])
    if away_id and away_stats.get("raw_matches"):
        out += _series_candidates(away_team, away_id, "away", home_team, away_stats["raw_matches"])
    out += _rolling_candidates(home_team, "home", away_team, league, home_corner)
    out += _rolling_candidates(away_team, "away", home_team, league, away_corner)

    for c in out:
        c.update({
            "match_id": match_id, "league": league, "match_date": str(match_date),
            "home_team": home_team, "away_team": away_team,
        })
    return out


# ── Ranking, tope de volumen y envío ─────────────────────────────────────────────

def rank_and_cap(candidates: list[dict]) -> list[dict]:
    """
    Ordena por 'fuerza de patrón' (score) y capa a TREND_MAX_SIGNALS_PER_RUN,
    con máximo TREND_MAX_SIGNALS_PER_TEAM por equipo para no repetir el mismo
    equipo en varios mercados.
    """
    ranked = sorted(candidates, key=lambda c: -c["score"])
    selected: list[dict] = []
    per_team: dict[str, int] = {}
    for c in ranked:
        key = f"{c['team']}_{c.get('team_id') or ''}"
        if per_team.get(key, 0) >= TREND_MAX_SIGNALS_PER_TEAM:
            continue
        selected.append(c)
        per_team[key] = per_team.get(key, 0) + 1
        if len(selected) >= TREND_MAX_SIGNALS_PER_RUN:
            break
    return selected


def _format_message(c: dict) -> str:
    emoji = _MARKET_EMOJI.get(c["market"], "📊")
    tag = "🎯 PATRÓN" if c["pattern_type"] == "series" else "📉 PROMEDIO (evidencia más débil)"
    return f"{emoji} {tag} — {c['team']} — {c['label']}\n{c['detail']}\n\n{_DISCLAIMER}"


async def _persist_and_send(selected: list[dict]) -> int:
    """
    Escribe cada señal seleccionada en trend_signals (colección propia, nunca
    predictions/shadow_trades) y envía la alerta al tema Telegram "Tendencias".
    """
    from shared.firestore_client import col

    now = datetime.now(timezone.utc)
    sent = 0
    for c in selected:
        team_key = c.get("team_id") or _slugify(c["team"])
        doc_id = f"{c['match_id']}_{c['market']}_{team_key}"
        doc = {
            "match_id": c["match_id"], "league": c["league"], "match_date": c["match_date"],
            "home_team": c["home_team"], "away_team": c["away_team"],
            "team": c["team"], "team_id": c.get("team_id"), "side": c["side"], "opponent": c["opponent"],
            "pattern_type": c["pattern_type"], "market": c["market"], "threshold": c.get("threshold"),
            "label": c["label"], "detail": c["detail"],
            "sample_size": c["sample"], "rate_or_ratio": c["rate"], "score": c["score"],
            "sent_at": now.isoformat(), "graded": False, "result": None,
        }
        try:
            col("trend_signals").document(doc_id).set(doc)
        except Exception:
            logger.error("trend_finder: error guardando trend_signals %s", doc_id, exc_info=True)
            continue

        if not (TELEGRAM_BOT_URL and CLOUD_RUN_TOKEN):
            continue
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{TELEGRAM_BOT_URL}/send-alert",
                    headers={"x-cloud-token": CLOUD_RUN_TOKEN},
                    json={"type": "trend", "data": {"text": _format_message(c)}},
                )
            if resp.status_code in (200, 202) and resp.json().get("sent"):
                sent += 1
        except Exception:
            logger.error("trend_finder: error enviando alerta %s", doc_id, exc_info=True)

    return sent


async def run_trend_finder() -> dict:
    """
    Punto de entrada del job periódico (/run-trends). Lee upcoming_matches de
    fútbol SCHEDULED/TIMED de la próxima semana + sus enriched_matches, genera
    candidatos, rankea, capa el volumen y envía al tema Telegram "Tendencias".
    Solo LECTURA de upcoming_matches/enriched_matches/team_stats/team_corner_stats
    — no escribe nada fuera de trend_signals.
    """
    from shared.config import COLLECTION_PREFIX
    from shared.firestore_client import col, get_client
    from shared.match_timing import signal_is_too_late

    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    cutoff_str = (now + timedelta(days=7)).date().isoformat()

    upcoming = list(
        col("upcoming_matches")
        .where(filter=FieldFilter("status", "in", ["SCHEDULED", "TIMED"]))
        .stream()
    )
    match_ids: set[str] = set()
    for d in upcoming:
        data = d.to_dict() or {}
        if data.get("sport", "football") not in ("football", "soccer", ""):
            continue
        md = data.get("match_date") or data.get("date")
        if isinstance(md, str):
            md_str = md[:10]
        elif hasattr(md, "date"):
            md_str = md.date().isoformat()
        else:
            md_str = ""
        if md_str and today_str <= md_str <= cutoff_str:
            mid = str(data.get("match_id", ""))
            if mid:
                match_ids.add(mid)

    fs = get_client()
    refs = [fs.collection(f"{COLLECTION_PREFIX}enriched_matches").document(mid) for mid in match_ids]
    enriched_docs = [d.to_dict() for d in fs.get_all(refs)] if refs else []
    enriched_docs = [e for e in enriched_docs if e]

    all_candidates: list[dict] = []
    for enriched in enriched_docs:
        if signal_is_too_late(enriched.get("match_date") or enriched.get("date")):
            continue
        try:
            all_candidates += await _fixture_candidates(enriched)
        except Exception:
            logger.error("trend_finder: error en fixture %s", enriched.get("match_id"), exc_info=True)

    selected = rank_and_cap(all_candidates)
    sent = await _persist_and_send(selected)
    logger.info(
        "trend_finder: %d enriched, %d candidatos -> %d seleccionados -> %d alertas enviadas",
        len(enriched_docs), len(all_candidates), len(selected), sent,
    )
    return {"enriched": len(enriched_docs), "candidates": len(all_candidates),
            "selected": len(selected), "sent": sent}
