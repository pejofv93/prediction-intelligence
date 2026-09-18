"""
services/sports-agent/analyzers/trend_finder.py

Feed de tendencias estadísticas para el tema Telegram "Tendencias" — SOLO
estadística/hit-rate/modelo, sin cuotas ni EV. Aislado a propósito del sistema
de valor: colección propia trend_signals, lee team_stats/team_corner_stats/
enriched_matches en modo solo-lectura y nunca escribe en predictions/
shadow_trades/accuracy_log ni toca model_weights.

Un mensaje por PARTIDO (no por señal suelta) con todos los mercados que
apliquen debajo. Tres tipos de evidencia, marcados de forma distinta en el
mensaje y graduados por separado (pattern_type en trend_signals):
  series  — serie partido a partido del propio equipo (team_stats.raw_matches):
            goles marcados, BTTS, margen de victoria (hándicap). Hit-rate real
            sobre una ventana de partidos → evidencia fuerte.
  rolling — promedio rolling-15 de football-data.co.uk (team_corner_stats):
            córners, tarjetas, expulsiones, tiros, tiros a puerta, faltas,
            goles en la 1ª parte. No hay serie partido a partido, solo el
            promedio acumulado vs la media de la liga → evidencia más débil.
  model   — salida directa de Poisson/ELO ya calculada en enriched_matches
            (doble oportunidad, DNB, total exacto, margen de victoria): NO es
            hit-rate histórico, es la probabilidad de un solo modelo para ESE
            partido concreto — evidencia de naturaleza distinta a las otras
            dos, marcada aparte.
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
    TREND_MAX_FIXTURES_PER_RUN,
    TREND_MAX_FIXTURES_PER_TEAM,
    TREND_ROLLING_MIN_SAMPLE,
    TREND_SERIES_MIN_SAMPLE,
    TREND_SERIES_WINDOW,
)

logger = logging.getLogger(__name__)

# Umbrales de goles a probar de mayor a menor — se emite el más alto que aún
# cumpla el hit-rate: es la afirmación más específica que se sostiene con los datos.
_GOAL_THRESHOLDS = (3, 2)
_HANDICAP_MARGIN = 2  # "ganó por 2+"

# Nombre legible por competición — solo para el mensaje, no afecta a la lógica.
# Duplicado del de alert_manager.py (telegram-bot): son servicios Cloud Run
# independientes, cada uno con su propia copia de shared/, sin código compartido
# entre services/sports-agent y services/telegram-bot más allá de esa carpeta.
_LEAGUE_LABEL = {
    "PL": "Premier League", "PD": "La Liga", "BL1": "Bundesliga",
    "SA": "Serie A", "FL1": "Ligue 1",
    "CL": "Champions League", "EL": "Europa League", "ECL": "Conference League",
}

_PATTERN_TAG = {"series": "🎯", "rolling": "📉", "model": "📐"}
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
    Media de córners/amarillas/rojas/tiros/tiros a puerta/faltas/goles al
    descanso de la liga a partir de team_corner_stats (~20 equipos/liga).
    Cacheado 1h en memoria — evita releer la colección por cada candidato de
    la jornada. Amarillas y rojas separadas (antes "cards" las mezclaba) para
    poder emitir "expulsiones" como mercado propio.
    """
    now = datetime.now(timezone.utc)
    cached = _LEAGUE_AVG_CACHE.get(league)
    if cached and (now - cached[0]).total_seconds() < _LEAGUE_AVG_TTL_SECONDS:
        return cached[1]

    from shared.firestore_client import col
    corners_sum, yellows_sum, reds_sum = 0.0, 0.0, 0.0
    shots_sum, shots_on_target_sum, fouls_sum, ht_goals_sum = 0.0, 0.0, 0.0, 0.0
    n = 0
    try:
        query = col("team_corner_stats").where(filter=FieldFilter("league", "==", league))
        for d in query.stream():
            doc = d.to_dict() or {}
            corners_sum += (doc.get("home_corners", 0) + doc.get("away_corners", 0)) / 2
            yellows_sum += (doc.get("home_yellows", 0) + doc.get("away_yellows", 0)) / 2
            reds_sum += (doc.get("home_reds", 0) + doc.get("away_reds", 0)) / 2
            shots_sum += (doc.get("home_shots", 0) + doc.get("away_shots", 0)) / 2
            shots_on_target_sum += (doc.get("home_shots_on_target", 0) + doc.get("away_shots_on_target", 0)) / 2
            fouls_sum += (doc.get("home_fouls", 0) + doc.get("away_fouls", 0)) / 2
            ht_goals_sum += (doc.get("home_ht_goals", 0) + doc.get("away_ht_goals", 0)) / 2
            n += 1
    except Exception:
        logger.error("trend_finder: error calculando medias de liga %s", league, exc_info=True)

    result = (
        {"corners": round(corners_sum / n, 2), "yellows": round(yellows_sum / n, 2),
         "reds": round(reds_sum / n, 3), "shots": round(shots_sum / n, 2),
         "shots_on_target": round(shots_on_target_sum / n, 2), "fouls": round(fouls_sum / n, 2),
         "ht_goals": round(ht_goals_sum / n, 3), "n_teams": n}
        if n else {}
    )
    _LEAGUE_AVG_CACHE[league] = (now, result)
    return result


_THRESHOLD_CACHE: dict[str, tuple[datetime, float]] = {}
_THRESHOLD_CACHE_TTL_SECONDS = 3600


def _effective_threshold(market: str) -> float:
    """
    Umbral de emisión para este mercado: el calibrado por
    analyzers/trend_calibration.py si ya hay muestra suficiente
    (trend_market_calibration/{market}), si no el fijo genérico de
    shared/config.py — nunca None, TREND_MARKET_FIXED_THRESHOLD cubre los 14
    mercados del feed. Cacheado 1h en memoria, mismo patrón que
    _league_averages: evita releer el doc por cada candidato de la jornada.
    """
    from shared.config import TREND_MARKET_FIXED_THRESHOLD
    fixed = TREND_MARKET_FIXED_THRESHOLD[market]

    now = datetime.now(timezone.utc)
    cached = _THRESHOLD_CACHE.get(market)
    if cached and (now - cached[0]).total_seconds() < _THRESHOLD_CACHE_TTL_SECONDS:
        return cached[1]

    from shared.firestore_client import col
    value = fixed
    try:
        snap = col("trend_market_calibration").document(market).get()
        if snap.exists:
            value = (snap.to_dict() or {}).get("threshold_effective", fixed)
    except Exception:
        logger.error("trend_finder: error leyendo trend_market_calibration(%s)", market, exc_info=True)

    _THRESHOLD_CACHE[market] = (now, value)
    return value


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
    muestra diminuta frente a un porcentaje algo menor con más partidos detrás.
    Los candidatos "model" no tienen un tamaño de muestra real (es la salida de
    un modelo, no un conteo histórico) — usan TREND_SERIES_WINDOW como "n"
    convencional para que su score caiga en la misma escala que series/rolling
    y se puedan sumar todos juntos al rankear partidos."""
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
        if rate >= _effective_threshold("team_goals_over"):
            candidates.append({
                "market": "team_goals_over", "threshold": threshold,
                "sample": n, "rate": round(rate, 4),
                "label": f"{threshold}+ goles",
                "detail": f"Marcó {threshold}+ goles en {hits} de sus últimos {n} partidos ({rate*100:.0f}%)",
                "line": f"{team_name} — {threshold}+ goles: {hits}/{n} ({rate*100:.0f}%)",
            })
            break  # el umbral más alto que cumple es la señal a emitir, no los dos

    hits = sum(1 for m in matches if m["gf"] > 0 and m["ga"] > 0)
    rate = hits / n
    if rate >= _effective_threshold("btts"):
        candidates.append({
            "market": "btts", "threshold": None,
            "sample": n, "rate": round(rate, 4),
            "label": "Ambos marcan",
            "detail": f"BTTS se cumplió en {hits} de sus últimos {n} partidos ({rate*100:.0f}%)",
            "line": f"Ambos marcan: {hits}/{n} ({rate*100:.0f}%)",
        })

    hits = sum(1 for m in matches if (m["gf"] - m["ga"]) >= _HANDICAP_MARGIN)
    rate = hits / n
    if rate >= _effective_threshold("handicap"):
        candidates.append({
            "market": "handicap", "threshold": _HANDICAP_MARGIN,
            "sample": n, "rate": round(rate, 4),
            "label": f"Hándicap -{_HANDICAP_MARGIN}",
            "detail": f"Ganó por margen de {_HANDICAP_MARGIN}+ goles en {hits} de sus últimos {n} partidos ({rate*100:.0f}%)",
            "line": f"{team_name} — Hándicap -{_HANDICAP_MARGIN}: {hits}/{n} ({rate*100:.0f}%)",
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
    if league_corners > 0 and team_corners / league_corners >= _effective_threshold("corners"):
        ratio = team_corners / league_corners
        n_line = max(1, math.floor(team_corners))
        candidates.append({
            "market": "corners", "threshold": n_line,
            "sample": sample, "rate": round(ratio, 3),
            "label": f"{n_line}+ córners",
            "detail": (f"Promedia {team_corners:.1f} córners por partido "
                       f"en sus últimos {sample} (liga: {league_corners:.1f})"),
            "line": f"{team_name} — {n_line}+ córners: promedia {team_corners:.1f}/partido (liga: {league_corners:.1f})",
        })

    team_yellows = corner_stats.get(f"{side}_yellows", 0.0)
    league_yellows = league_avg.get("yellows", 0)
    if league_yellows > 0 and team_yellows / league_yellows >= _effective_threshold("cards"):
        ratio = team_yellows / league_yellows
        n_line = max(1, math.floor(team_yellows))
        candidates.append({
            "market": "cards", "threshold": n_line,
            "sample": sample, "rate": round(ratio, 3),
            "label": f"{n_line}+ tarjetas",
            "detail": (f"Promedia {team_yellows:.1f} tarjetas por partido "
                       f"en sus últimos {sample} (liga: {league_yellows:.1f})"),
            "line": f"{team_name} — {n_line}+ tarjetas: promedia {team_yellows:.1f}/partido (liga: {league_yellows:.1f})",
        })

    # Expulsiones: mercado propio, separado de "cards" (antes las rojas se
    # sumaban ahí sin distinguirse). Sin línea "N+" — la media de rojas por
    # partido es casi siempre <1, así que la afirmación útil es "expulsión
    # probable" con el promedio como respaldo, no un umbral entero.
    team_reds = corner_stats.get(f"{side}_reds", 0.0)
    league_reds = league_avg.get("reds", 0)
    if league_reds > 0 and team_reds / league_reds >= _effective_threshold("red_cards"):
        ratio = team_reds / league_reds
        candidates.append({
            "market": "red_cards", "threshold": None,
            "sample": sample, "rate": round(ratio, 3),
            "label": "Expulsión probable",
            "detail": (f"Promedia {team_reds:.2f} rojas por partido "
                       f"en sus últimos {sample} (liga: {league_reds:.2f})"),
            "line": f"{team_name} — Expulsión probable: promedia {team_reds:.2f}/partido (liga: {league_reds:.2f})",
        })

    team_shots = corner_stats.get(f"{side}_shots", 0.0)
    league_shots = league_avg.get("shots", 0)
    if league_shots > 0 and team_shots / league_shots >= _effective_threshold("shots"):
        ratio = team_shots / league_shots
        n_line = max(1, math.floor(team_shots))
        candidates.append({
            "market": "shots", "threshold": n_line,
            "sample": sample, "rate": round(ratio, 3),
            "label": f"{n_line}+ tiros",
            "detail": (f"Promedia {team_shots:.1f} tiros por partido "
                       f"en sus últimos {sample} (liga: {league_shots:.1f})"),
            "line": f"{team_name} — {n_line}+ tiros: promedia {team_shots:.1f}/partido (liga: {league_shots:.1f})",
        })

    team_sot = corner_stats.get(f"{side}_shots_on_target", 0.0)
    league_sot = league_avg.get("shots_on_target", 0)
    if league_sot > 0 and team_sot / league_sot >= _effective_threshold("shots_on_target"):
        ratio = team_sot / league_sot
        n_line = max(1, math.floor(team_sot))
        candidates.append({
            "market": "shots_on_target", "threshold": n_line,
            "sample": sample, "rate": round(ratio, 3),
            "label": f"{n_line}+ tiros a puerta",
            "detail": (f"Promedia {team_sot:.1f} tiros a puerta por partido "
                       f"en sus últimos {sample} (liga: {league_sot:.1f})"),
            "line": f"{team_name} — {n_line}+ tiros a puerta: promedia {team_sot:.1f}/partido (liga: {league_sot:.1f})",
        })

    team_fouls = corner_stats.get(f"{side}_fouls", 0.0)
    league_fouls = league_avg.get("fouls", 0)
    if league_fouls > 0 and team_fouls / league_fouls >= _effective_threshold("fouls"):
        ratio = team_fouls / league_fouls
        n_line = max(1, math.floor(team_fouls))
        candidates.append({
            "market": "fouls", "threshold": n_line,
            "sample": sample, "rate": round(ratio, 3),
            "label": f"{n_line}+ faltas",
            "detail": (f"Promedia {team_fouls:.1f} faltas por partido "
                       f"en sus últimos {sample} (liga: {league_fouls:.1f})"),
            "line": f"{team_name} — {n_line}+ faltas: promedia {team_fouls:.1f}/partido (liga: {league_fouls:.1f})",
        })

    # Goles en la 1ª parte: mismo caso que expulsiones — la media por mitad
    # suele ser <1, sin línea "N+" con sentido.
    team_ht_goals = corner_stats.get(f"{side}_ht_goals", 0.0)
    league_ht_goals = league_avg.get("ht_goals", 0)
    if league_ht_goals > 0 and team_ht_goals / league_ht_goals >= _effective_threshold("ht_goals"):
        ratio = team_ht_goals / league_ht_goals
        candidates.append({
            "market": "ht_goals", "threshold": None,
            "sample": sample, "rate": round(ratio, 3),
            "label": "Gol en la 1ª parte probable",
            "detail": (f"Promedia {team_ht_goals:.2f} goles en la 1ª parte "
                       f"en sus últimos {sample} (liga: {league_ht_goals:.2f})"),
            "line": f"{team_name} — Gol en la 1ª parte probable: promedia {team_ht_goals:.2f}/partido (liga: {league_ht_goals:.2f})",
        })

    for c in candidates:
        c.update({
            "pattern_type": "rolling", "team": team_name, "team_id": None,
            "opponent": opponent, "side": side, "score": _score(c["rate"], c["sample"]),
        })
    return candidates


# ── Candidatos: model (salida de Poisson/ELO, evidencia de otra naturaleza) ─────

def _modal_outcome(dist: dict[int, float]) -> tuple[int, float, float] | None:
    """Valor con mayor probabilidad + ratio frente al segundo. None si hay <2 valores."""
    ranked = sorted(dist.items(), key=lambda kv: -kv[1])
    if len(ranked) < 2:
        return None
    (best_v, best_p), (_, second_p) = ranked[0], ranked[1]
    ratio = (best_p / second_p) if second_p > 0 else float("inf")
    return best_v, best_p, ratio


def _model_candidates(enriched: dict) -> list[dict]:
    home_team = enriched.get("home_team", "")
    away_team = enriched.get("away_team", "")
    p_home = enriched.get("poisson_home_win")
    p_draw = enriched.get("poisson_draw")
    p_away = enriched.get("poisson_away_win")

    candidates: list[dict] = []

    if p_home is not None and p_draw is not None and p_away is not None:
        combos = (
            ("1X", p_home + p_draw, f"{home_team} o empate"),
            ("X2", p_draw + p_away, f"Empate o {away_team}"),
            ("12", p_home + p_away, f"{home_team} o {away_team} (no empate)"),
        )
        best_sel, best_prob, best_label = max(combos, key=lambda c: c[1])
        if best_prob >= _effective_threshold("double_chance"):
            candidates.append({
                "market": "double_chance", "threshold": None, "selection": best_sel,
                "sample": None, "rate": round(best_prob, 4),
                "label": f"Doble oportunidad: {best_label}",
                "detail": f"Probabilidad del modelo: {best_prob*100:.0f}% ({best_sel})",
                "line": f"Doble oportunidad: {best_label} — {best_prob*100:.0f}%",
            })

        no_draw = p_home + p_away
        if no_draw > 0:
            home_dnb = p_home / no_draw
            side, prob, team_label = (
                ("home", home_dnb, home_team) if home_dnb >= (1 - home_dnb)
                else ("away", 1 - home_dnb, away_team)
            )
            if prob >= _effective_threshold("dnb"):
                candidates.append({
                    "market": "dnb", "threshold": None, "side": side,
                    "sample": None, "rate": round(prob, 4),
                    "label": f"Sin empate: {team_label}",
                    "detail": f"Probabilidad del modelo descartando el empate: {prob*100:.0f}%",
                    "line": f"Sin empate: {team_label} — {prob*100:.0f}%",
                })

    home_xg = enriched.get("home_xg")
    away_xg = enriched.get("away_xg")
    if home_xg is not None and away_xg is not None:
        try:
            from analyzers.value_bet_engine import _calculate_correct_score_probs
            cs_probs = _calculate_correct_score_probs(home_xg, away_xg, max_goals=6)
        except Exception:
            logger.error("trend_finder: error calculando matriz Poisson", exc_info=True)
            cs_probs = {}

        if cs_probs:
            totals: dict[int, float] = {}
            margins: dict[int, float] = {}
            for score, p in cs_probs.items():
                h_s, a_s = score.split("-")
                h, a = int(h_s), int(a_s)
                totals[h + a] = totals.get(h + a, 0.0) + p
                margins[h - a] = margins.get(h - a, 0.0) + p

            tm = _modal_outcome(totals)
            if tm and tm[2] >= _effective_threshold("exact_total"):
                total_v, prob, ratio = tm
                candidates.append({
                    "market": "exact_total", "threshold": total_v,
                    "sample": None, "rate": round(ratio, 3),
                    "label": f"Total exacto: {total_v} goles",
                    "detail": f"Resultado más probable del modelo ({prob*100:.0f}%, {ratio:.1f}x el siguiente)",
                    "line": f"Total exacto: {total_v} goles — {prob*100:.0f}% (modelo, {ratio:.1f}x el siguiente)",
                })

            mm = _modal_outcome(margins)
            if mm and mm[2] >= _effective_threshold("win_margin"):
                margin_v, prob, ratio = mm
                if margin_v > 0:
                    margin_label = f"{home_team} +{margin_v}"
                elif margin_v < 0:
                    margin_label = f"{away_team} +{-margin_v}"
                else:
                    margin_label = "Empate (margen 0)"
                candidates.append({
                    "market": "win_margin", "threshold": margin_v,
                    "sample": None, "rate": round(ratio, 3),
                    "label": f"Margen de victoria: {margin_label}",
                    "detail": f"Resultado más probable del modelo ({prob*100:.0f}%, {ratio:.1f}x el siguiente)",
                    "line": f"Margen de victoria: {margin_label} — {prob*100:.0f}% (modelo, {ratio:.1f}x el siguiente)",
                })

    for c in candidates:
        c.update({
            "pattern_type": "model", "team": f"{home_team}/{away_team}", "team_id": None,
            "opponent": "", "side": c.get("side", "match"),
            "score": _score(c["rate"], TREND_SERIES_WINDOW),
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
    out += _model_candidates(enriched)

    for c in out:
        c.update({
            "match_id": match_id, "league": league, "match_date": str(match_date),
            "home_team": home_team, "away_team": away_team,
        })
    return out


# ── Agrupación por partido, ranking y envío ─────────────────────────────────────

def _candidate_key(c: dict) -> tuple:
    """
    Clave de deduplicación dentro de un mismo partido. Los candidatos "model"
    y "btts" son de partido entero, no de un lado concreto — clave solo por
    mercado. BTTS se calcula desde la historia de UN equipo a la vez (mismo
    código que team_goals_over/handicap), pero es una afirmación sobre el
    partido ("ambos marcan"), no sobre ese equipo — sin este caso especial,
    el mismo partido podía salir con "Ambos marcan" repetido dos veces (una
    por la historia de cada equipo), indistinguibles en el mensaje porque la
    línea de BTTS no lleva nombre de equipo. El resto de series/rolling sí son
    por equipo — clave por mercado+equipo, para que dos docs de
    enriched_matches del mismo partido (fuentes distintas, a veces con
    local/visitante invertido) no dupliquen la misma afirmación.
    """
    if c["pattern_type"] == "model" or c["market"] == "btts":
        return (c["market"],)
    return (c["market"], c.get("team_id") or c.get("team"))


def _group_by_fixture(candidates: list[dict]) -> dict[str, dict]:
    from collectors.team_identity import physical_fixture_key

    groups: dict[str, dict] = {}
    for c in candidates:
        key = physical_fixture_key(c.get("match_date"), c.get("home_team", ""), c.get("away_team", ""))
        if not key:
            key = f"_nokey_{c.get('match_id', '')}"
        group = groups.setdefault(key, {
            "match_id": c["match_id"], "league": c["league"], "match_date": c["match_date"],
            "home_team": c["home_team"], "away_team": c["away_team"],
            "by_candidate_key": {},
        })
        ck = _candidate_key(c)
        existing = group["by_candidate_key"].get(ck)
        if existing is None or c["score"] > existing["score"]:
            group["by_candidate_key"][ck] = c
    return groups


def rank_and_cap(candidates: list[dict]) -> list[dict]:
    """
    Agrupa por partido físico (_group_by_fixture, que también deduplica
    mercado+equipo dentro del grupo — cubre el caso de dos docs del mismo
    partido con equipos invertidos, mismo motivo que el bug de duplicados
    UEFA). Rankea PARTIDOS por la suma de scores de sus mercados y capa a
    TREND_MAX_FIXTURES_PER_RUN, con máximo TREND_MAX_FIXTURES_PER_TEAM
    apariciones por equipo.

    Sin round-robin por mercado: con el mensaje agrupado por partido la
    variedad de mercados ya sale sola dentro de cada mensaje, no hace falta
    forzarla al elegir qué partidos entran.
    """
    groups = _group_by_fixture(candidates)
    fixtures: list[dict] = []
    for g in groups.values():
        cands = sorted(g["by_candidate_key"].values(), key=lambda c: -c["score"])
        if not cands:
            continue
        fixtures.append({
            "match_id": g["match_id"], "league": g["league"], "match_date": g["match_date"],
            "home_team": g["home_team"], "away_team": g["away_team"],
            "candidates": cands, "total_score": round(sum(c["score"] for c in cands), 4),
        })

    fixtures.sort(key=lambda f: -f["total_score"])

    selected: list[dict] = []
    per_team: dict[str, int] = {}
    for fx in fixtures:
        teams = (fx["home_team"], fx["away_team"])
        if any(per_team.get(t, 0) >= TREND_MAX_FIXTURES_PER_TEAM for t in teams):
            continue
        selected.append(fx)
        for t in teams:
            per_team[t] = per_team.get(t, 0) + 1
        if len(selected) >= TREND_MAX_FIXTURES_PER_RUN:
            break

    return selected


def _format_match_date_madrid(match_date) -> str:
    """
    Duplicado del de alert_manager.py (telegram-bot, ver nota de cabecera del
    módulo) — mismo formato DD/MM a las HH:MM en hora de Madrid que ya usa el
    canal de señales normal, incluido "Hoy"/"Mañana" cuando aplica. Sin el
    "📅" ni el "Madrid" final: aquí va inline en la cabecera del mensaje, no
    en su propia línea. Devuelve "" si match_date es None o no parseable.
    """
    from zoneinfo import ZoneInfo

    madrid = ZoneInfo("Europe/Madrid")

    if match_date is None:
        return ""
    if isinstance(match_date, str):
        raw = match_date.strip()
        if not raw or raw.lower() == "none":
            return ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    elif hasattr(match_date, "strftime"):
        dt = match_date
    else:
        return ""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_madrid = dt.astimezone(madrid)
    today_madrid = datetime.now(madrid).date()
    match_day = dt_madrid.date()

    time_str = dt_madrid.strftime("%H:%M")
    date_str = dt_madrid.strftime("%d/%m")

    if match_day == today_madrid:
        return f"Hoy {date_str} a las {time_str}"
    elif match_day == today_madrid + timedelta(days=1):
        return f"Mañana {date_str} a las {time_str}"
    return f"{date_str} a las {time_str}"


def _format_fixture_message(fixture: dict) -> str:
    league_label = _LEAGUE_LABEL.get(fixture["league"], fixture["league"])
    header = f"⚽ {fixture['home_team']} vs {fixture['away_team']} ({league_label})"
    date_str = _format_match_date_madrid(fixture.get("match_date"))
    if date_str:
        header += f" — {date_str}"
    lines = [header]
    for c in fixture["candidates"]:
        tag = _PATTERN_TAG.get(c["pattern_type"], "📊")
        lines.append(f"{tag} {c['line']}")
    lines.append("")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


async def _persist_and_send(selected_fixtures: list[dict]) -> int:
    """
    Escribe cada señal de cada partido seleccionado en trend_signals (colección
    propia, nunca predictions/shadow_trades — un doc por mercado, para poder
    graduarlos individualmente) y envía UN mensaje por partido al tema
    Telegram "Tendencias" con todos sus mercados debajo.
    """
    from shared.firestore_client import col

    now = datetime.now(timezone.utc)
    sent = 0
    for fx in selected_fixtures:
        for c in fx["candidates"]:
            team_key = c.get("team_id") or _slugify(c.get("team") or "") or "match"
            doc_id = f"{fx['match_id']}_{c['market']}_{team_key}"
            doc = {
                "match_id": fx["match_id"], "league": fx["league"], "match_date": fx["match_date"],
                "home_team": fx["home_team"], "away_team": fx["away_team"],
                "team": c.get("team"), "team_id": c.get("team_id"), "side": c.get("side"),
                "opponent": c.get("opponent"), "selection": c.get("selection"),
                "pattern_type": c["pattern_type"], "market": c["market"], "threshold": c.get("threshold"),
                "label": c["label"], "detail": c["detail"],
                "sample_size": c.get("sample"), "rate_or_ratio": c["rate"], "score": c["score"],
                "fixture_total_score": fx["total_score"],
                "sent_at": now.isoformat(), "graded": False, "result": None,
            }
            try:
                col("trend_signals").document(doc_id).set(doc)
            except Exception:
                logger.error("trend_finder: error guardando trend_signals %s", doc_id, exc_info=True)

        if not (TELEGRAM_BOT_URL and CLOUD_RUN_TOKEN):
            continue
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{TELEGRAM_BOT_URL}/send-alert",
                    headers={"x-cloud-token": CLOUD_RUN_TOKEN},
                    json={"type": "trend", "data": {"text": _format_fixture_message(fx)}},
                )
            if resp.status_code in (200, 202) and resp.json().get("sent"):
                sent += 1
        except Exception:
            logger.error("trend_finder: error enviando alerta %s", fx["match_id"], exc_info=True)

    return sent


async def run_trend_finder() -> dict:
    """
    Punto de entrada del job periódico (/run-trends). Lee upcoming_matches de
    fútbol SCHEDULED/TIMED de la próxima semana + sus enriched_matches, genera
    candidatos, agrupa por partido, rankea, capa el volumen y envía un mensaje
    por partido al tema Telegram "Tendencias".
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

    selected_fixtures = rank_and_cap(all_candidates)
    sent = await _persist_and_send(selected_fixtures)
    n_signals = sum(len(fx["candidates"]) for fx in selected_fixtures)
    logger.info(
        "trend_finder: %d enriched, %d candidatos -> %d partidos (%d señales) -> %d mensajes enviados",
        len(enriched_docs), len(all_candidates), len(selected_fixtures), n_signals, sent,
    )
    return {"enriched": len(enriched_docs), "candidates": len(all_candidates),
            "fixtures_selected": len(selected_fixtures), "signals": n_signals, "sent": sent}
