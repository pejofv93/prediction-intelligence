"""
Motor de value bets — recibe enriched_match → genera senal si hay edge suficiente.
Thresholds: edge > SPORTS_MIN_EDGE (0.08) + confianza > SPORTS_MIN_CONFIDENCE (0.65).
Si edge > SPORTS_ALERT_EDGE (0.10) → POST al telegram-bot /send-alert.

Flujo por llamada a generate_signal():
  load_weights → ensemble_probability (home y away) → fetch_bookmaker_odds
  → calculate_edge → si supera threshold → kelly_criterion → guarda predictions
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

from shared.config import (
    CLOUD_RUN_TOKEN,
    DEFAULT_WEIGHTS,
    FOOTBALL_RAPID_API_KEY,
    SPORTS_ALERT_EDGE,
    SPORTS_MIN_CONFIDENCE,
    SPORTS_MIN_EDGE,
    TELEGRAM_BOT_URL,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Host API-Football via RapidAPI para consulta de cuotas
_ODDS_API_HOST = "api-football-v1.p.rapidapi.com"
_ODDS_API_BASE = "https://v3.football.api-sports.io"

# Timeout para llamadas HTTP a API externa
_HTTP_TIMEOUT = 15.0


def load_weights() -> dict:
    """
    Lee doc 'current' de Firestore model_weights.
    Si no existe, usa DEFAULT_WEIGHTS de config.py.
    """
    try:
        doc = col("model_weights").document("current").get()
        if doc.exists:
            data = doc.to_dict()
            weights = data.get("weights", {})
            # Verificar que tiene las cuatro claves necesarias
            if all(k in weights for k in DEFAULT_WEIGHTS):
                return dict(weights)
            logger.warning("load_weights: pesos incompletos en Firestore — usando DEFAULT_WEIGHTS")
    except Exception:
        logger.error("load_weights: error leyendo Firestore — usando DEFAULT_WEIGHTS", exc_info=True)
    return dict(DEFAULT_WEIGHTS)


def ensemble_probability(enriched_match: dict, weights: dict, team: str = "home") -> dict:
    """
    Combina senales estadisticas con pesos del modelo.
    team: "home" analiza victoria local | "away" analiza victoria visitante.

    Senales para home:
      poisson = poisson_home_win  (o 0.5 si None — no-football)
      elo     = elo_home_win_prob (o 0.5 si None)
      form    = home_form_score / 100
      h2h     = (h2h_advantage + 1) / 2  (de [-1,1] a [0,1])

    Senales para away (invertidas):
      poisson = poisson_away_win
      elo     = 1 - elo_home_win_prob
      form    = away_form_score / 100
      h2h     = 1 - (h2h_advantage + 1) / 2

    final_prob = sum(signal * weights[key] for key, signal in signals.items())
    confidence = max(0.0, 1 - np.std(list(signals.values())))

    Devuelve {"prob": float, "confidence": float, "signals": dict}
    """
    poisson_home = enriched_match.get("poisson_home_win")
    poisson_away = enriched_match.get("poisson_away_win")
    elo_home = enriched_match.get("elo_home_win_prob")
    home_form = enriched_match.get("home_form_score", 50.0)
    away_form = enriched_match.get("away_form_score", 50.0)
    h2h_adv = enriched_match.get("h2h_advantage", 0.0)

    # Fallback neutral para deportes sin modelo Poisson/ELO
    poisson_home_s = float(poisson_home) if poisson_home is not None else 0.5
    poisson_away_s = float(poisson_away) if poisson_away is not None else 0.5
    elo_home_s = float(elo_home) if elo_home is not None else 0.5

    if team == "home":
        signals = {
            "poisson": poisson_home_s,
            "elo":     elo_home_s,
            "form":    float(home_form) / 100.0,
            "h2h":     (float(h2h_adv) + 1.0) / 2.0,
        }
    else:  # away
        signals = {
            "poisson": poisson_away_s,
            "elo":     1.0 - elo_home_s,
            "form":    float(away_form) / 100.0,
            "h2h":     1.0 - (float(h2h_adv) + 1.0) / 2.0,
        }

    # Clampear todas las senales al rango [0.0, 1.0]
    signals = {k: max(0.0, min(1.0, v)) for k, v in signals.items()}

    # Probabilidad final ponderada
    final_prob = sum(signals[k] * weights.get(k, 0.25) for k in signals)
    final_prob = max(0.0, min(1.0, final_prob))

    # Confianza: mayor dispersion de senales → menor confianza
    confidence = max(0.0, 1.0 - float(np.std(list(signals.values()))))

    return {
        "prob": round(final_prob, 4),
        "confidence": round(confidence, 4),
        "signals": {k: round(v, 4) for k, v in signals.items()},
    }


async def fetch_bookmaker_odds(match_id: str) -> dict | None:
    """
    Cache-first: verifica odds_cache en Firestore antes de llamar a la API.
    TTL del cache: 4 horas.
    1. Si existe en cache y es reciente → devuelve del cache
    2. Si no existe o expirado → llama API-Football GET /odds?fixture={match_id}
    3. Guarda resultado en odds_cache (opening_* solo si es la primera vez)
    Devuelve {bookmaker, home_odds, draw_odds, away_odds, opening_home_odds} o None.
    """
    now = datetime.now(timezone.utc)
    cache_ttl = timedelta(hours=4)

    # --- 1. Verificar cache ---
    try:
        doc = col("odds_cache").document(match_id).get()
        if doc.exists:
            data = doc.to_dict()
            fetched_at = data.get("fetched_at")
            # Normalizar timezone si es naive
            if fetched_at and hasattr(fetched_at, "tzinfo") and fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            if fetched_at and (now - fetched_at) < cache_ttl:
                # Cache valido — devolver sin llamar API
                return {
                    "bookmaker": data.get("bookmaker", "bet365"),
                    "home_odds": float(data.get("home_odds", 2.0)),
                    "draw_odds": float(data.get("draw_odds", 3.2)),
                    "away_odds": float(data.get("away_odds", 3.5)),
                    "opening_home_odds": float(data.get("opening_home_odds", data.get("home_odds", 2.0))),
                }
    except Exception:
        logger.error("fetch_bookmaker_odds(%s): error leyendo odds_cache", match_id, exc_info=True)

    # --- 2. Llamar API-Football si la key esta disponible ---
    if not FOOTBALL_RAPID_API_KEY:
        logger.debug("fetch_bookmaker_odds: FOOTBALL_RAPID_API_KEY no configurada — omitiendo")
        return None

    try:
        headers = {
            "X-RapidAPI-Key": FOOTBALL_RAPID_API_KEY,
            "X-RapidAPI-Host": _ODDS_API_HOST,
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{_ODDS_API_BASE}/odds",
                headers=headers,
                params={"fixture": match_id, "bookmaker": 1},
            )

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            logger.warning(
                "fetch_bookmaker_odds(%s): rate limit 429 — esperando %ds",
                match_id, retry_after,
            )
            await asyncio.sleep(retry_after)
            return None

        if resp.status_code != 200:
            logger.warning(
                "fetch_bookmaker_odds(%s): API respondio %d", match_id, resp.status_code
            )
            return None

        data = resp.json()
        fixtures = data.get("response", [])
        if not fixtures:
            return None

        # Buscar cuotas 1X2 (Match Winner) en el primer bookmaker disponible
        odds_result = _parse_odds_response(fixtures[0])
        if not odds_result:
            return None

        # --- 3. Guardar en odds_cache ---
        await _save_odds_cache(match_id, odds_result, now)
        return odds_result

    except Exception:
        logger.error("fetch_bookmaker_odds(%s): error llamando API", match_id, exc_info=True)
        return None


def _parse_odds_response(fixture_data: dict) -> dict | None:
    """Extrae cuotas 1X2 de la respuesta de API-Football /odds."""
    try:
        bookmakers = fixture_data.get("bookmakers", [])
        if not bookmakers:
            return None

        bookmaker = bookmakers[0]
        bookmaker_name = bookmaker.get("name", "bet365")
        bets = bookmaker.get("bets", [])

        for bet in bets:
            if bet.get("name") in ("Match Winner", "1X2"):
                home_odds = draw_odds = away_odds = None
                for value in bet.get("values", []):
                    label = value.get("value", "")
                    odd = float(value.get("odd", 0))
                    if label == "Home":
                        home_odds = odd
                    elif label == "Draw":
                        draw_odds = odd
                    elif label == "Away":
                        away_odds = odd

                if home_odds and draw_odds and away_odds:
                    return {
                        "bookmaker": bookmaker_name,
                        "home_odds": home_odds,
                        "draw_odds": draw_odds,
                        "away_odds": away_odds,
                        "opening_home_odds": home_odds,
                    }
        return None
    except Exception:
        logger.error("_parse_odds_response: error parseando respuesta", exc_info=True)
        return None


async def _save_odds_cache(match_id: str, odds: dict, now: datetime) -> None:
    """Guarda cuotas en odds_cache. opening_* solo se establece la primera vez."""
    try:
        doc_ref = col("odds_cache").document(match_id)
        existing = doc_ref.get()

        if existing.exists:
            # Actualizar cuotas actuales — NO tocar opening_*
            doc_ref.update({
                "home_odds": odds["home_odds"],
                "draw_odds": odds["draw_odds"],
                "away_odds": odds["away_odds"],
                "bookmaker": odds["bookmaker"],
                "fetched_at": now,
            })
        else:
            # Primera vez — guardar opening_* y actuales
            doc_ref.set({
                "fixture_id": match_id,
                "home_odds": odds["home_odds"],
                "draw_odds": odds["draw_odds"],
                "away_odds": odds["away_odds"],
                "opening_home_odds": odds["home_odds"],
                "opening_draw_odds": odds["draw_odds"],
                "opening_away_odds": odds["away_odds"],
                "bookmaker": odds["bookmaker"],
                "first_fetched_at": now,
                "fetched_at": now,
            })
    except Exception:
        logger.error("_save_odds_cache(%s): error guardando en Firestore", match_id, exc_info=True)


def calculate_edge(prob_calculated: float, decimal_odds: float) -> float:
    """edge = prob_calculated - (1 / decimal_odds)"""
    if decimal_odds <= 1.0:
        return 0.0
    return round(prob_calculated - (1.0 / decimal_odds), 4)


def kelly_criterion(edge: float, decimal_odds: float) -> float:
    """
    Kelly fraction = edge / (decimal_odds - 1).
    Si edge <= 0 devuelve 0.0 (nunca apostar con edge negativo).
    Clampea resultado entre 0.0 y 0.25 (max 25% del bankroll — fraccion Kelly completa).
    """
    if edge <= 0.0 or decimal_odds <= 1.0:
        return 0.0
    fraction = edge / (decimal_odds - 1.0)
    return round(max(0.0, min(0.25, fraction)), 4)


async def _send_telegram_alert(prediction: dict) -> None:
    """
    Envia alerta al telegram-bot via POST /send-alert.
    Si falla → loggear y continuar (no bloquear pipeline).
    """
    if not TELEGRAM_BOT_URL or not CLOUD_RUN_TOKEN:
        logger.debug("_send_telegram_alert: TELEGRAM_BOT_URL o CLOUD_RUN_TOKEN no configurados")
        return

    try:
        payload = {"type": "sports", "data": prediction}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TELEGRAM_BOT_URL}/send-alert",
                json=payload,
                headers={"x-cloud-token": CLOUD_RUN_TOKEN},
            )
        if resp.status_code not in (200, 202):
            logger.warning(
                "_send_telegram_alert: bot respondio %d", resp.status_code
            )
        else:
            logger.info("_send_telegram_alert: alerta enviada correctamente")
    except Exception:
        logger.error("_send_telegram_alert: error al enviar alerta — continuando", exc_info=True)


async def generate_signal(enriched_match: dict) -> dict | None:
    """
    Pipeline completo de generacion de senal para un partido enriquecido.
    Analiza home y away por separado y elige el mejor edge.

    1. load_weights()
    2. ensemble_probability() para home y away
    3. fetch_bookmaker_odds() con fallback a odds_current del enriched_match
    4. calculate_edge() para home y away
    5. Seleccionar el lado con mayor edge (si supera threshold)
    6. Si edge > SPORTS_MIN_EDGE AND confidence > SPORTS_MIN_CONFIDENCE:
       - kelly_criterion()
       - Guarda en Firestore predictions
       - Si edge > SPORTS_ALERT_EDGE: envia alerta Telegram
    7. Devuelve prediction dict o None si no hay edge suficiente
    """
    match_id = str(enriched_match.get("match_id", ""))
    sport = enriched_match.get("sport", "football")
    home_team = enriched_match.get("home_team", enriched_match.get("home_team_id", ""))
    away_team = enriched_match.get("away_team", enriched_match.get("away_team_id", ""))
    league = enriched_match.get("league", "")
    match_date = enriched_match.get("match_date") or enriched_match.get("date")
    data_quality = enriched_match.get("data_quality", "partial")

    # Si faltan nombres o liga (docs enriquecidos antes del fix), leerlos de upcoming_matches
    if not home_team or not away_team or not league:
        try:
            um_doc = col("upcoming_matches").document(match_id).get()
            if um_doc.exists:
                um = um_doc.to_dict()
                home_team = home_team or um.get("home_team", str(enriched_match.get("home_team_id", "")))
                away_team = away_team or um.get("away_team", str(enriched_match.get("away_team_id", "")))
                league = league or um.get("league", "")
                match_date = match_date or um.get("match_date") or um.get("date")
        except Exception:
            logger.warning("generate_signal(%s): no se pudo resolver nombre de equipo desde upcoming_matches", match_id)

    # --- Guardia de calidad: omitir partidos sin datos reales ---
    poisson_none = enriched_match.get("poisson_home_win") is None
    form_default = (
        enriched_match.get("home_form_score", 50.0) == 50.0
        and enriched_match.get("away_form_score", 50.0) == 50.0
    )
    h2h_neutral = enriched_match.get("h2h_advantage", 0.0) == 0.0
    if poisson_none and form_default and h2h_neutral:
        logger.warning(
            "generate_signal(%s): omitido por datos insuficientes "
            "(poisson=None, form=50/50 default, h2h=0)",
            match_id,
        )
        return None


    # Determinar data_source segun si hay modelo estadistico
    has_statistical_model = (
        enriched_match.get("poisson_home_win") is not None
        and enriched_match.get("elo_home_win_prob") is not None
    )
    data_source = "statistical_model" if has_statistical_model else "groq_ai"

    # --- 1. Pesos del modelo ---
    weights = load_weights()
    weights_version = _get_weights_version()

    # --- 2. Ensemble probability para home y away ---
    result_home = ensemble_probability(enriched_match, weights, team="home")
    result_away = ensemble_probability(enriched_match, weights, team="away")

    # --- 3. Cuotas ---
    odds_data = await fetch_bookmaker_odds(match_id)
    if odds_data is None:
        # Fallback a cuotas del enriched_match
        odds_current = enriched_match.get("odds_current", {})
        if odds_current.get("home") and odds_current.get("away"):
            odds_data = {
                "bookmaker": "unknown",
                "home_odds": float(odds_current.get("home", 2.0)),
                "draw_odds": float(odds_current.get("draw", 3.2)),
                "away_odds": float(odds_current.get("away", 3.5)),
                "opening_home_odds": float(enriched_match.get("odds_opening", {}).get("home", odds_current.get("home", 2.0))),
            }
        else:
            logger.debug(
                "generate_signal(%s): sin cuotas disponibles — omitiendo", match_id
            )
            return None

    home_odds = odds_data["home_odds"]
    away_odds = odds_data["away_odds"]

    # --- 4. Edge para home y away ---
    edge_home = calculate_edge(result_home["prob"], home_odds)
    edge_away = calculate_edge(result_away["prob"], away_odds)

    # --- 5. Seleccionar el lado con mayor edge ---
    if edge_home >= edge_away:
        best_edge = edge_home
        best_prob = result_home["prob"]
        best_confidence = result_home["confidence"]
        best_signals = result_home["signals"]
        best_odds = home_odds
        team_to_back = str(home_team)
    else:
        best_edge = edge_away
        best_prob = result_away["prob"]
        best_confidence = result_away["confidence"]
        best_signals = result_away["signals"]
        best_odds = away_odds
        team_to_back = str(away_team)

    # --- 6. Verificar thresholds ---
    if best_edge <= SPORTS_MIN_EDGE or best_confidence <= SPORTS_MIN_CONFIDENCE:
        logger.debug(
            "generate_signal(%s): edge=%.3f conf=%.3f — debajo del umbral",
            match_id, best_edge, best_confidence,
        )
        return None

    # Calidad de datos: si es partial, reducir confianza un 10%
    if data_quality == "partial":
        best_confidence = round(max(0.0, best_confidence * 0.9), 4)
        if best_confidence <= SPORTS_MIN_CONFIDENCE:
            return None

    kelly = kelly_criterion(best_edge, best_odds)

    # Construir factors segun data_source
    if data_source == "statistical_model":
        factors = dict(best_signals)
    else:
        home_form = enriched_match.get("home_form_score", 50.0)
        away_form = enriched_match.get("away_form_score", 50.0)
        h2h_adv = enriched_match.get("h2h_advantage", 0.0)
        factors = {
            "stats_score": round(
                (max(home_form, away_form) / 100.0 * 0.6)
                + (abs(h2h_adv) * 0.4), 4
            ),
            "groq_estimate": best_prob,
        }

    prediction = {
        "match_id": match_id,
        "home_team": str(home_team),
        "away_team": str(away_team),
        "sport": sport,
        "league": league,
        "data_source": data_source,
        "match_date": match_date,
        "team_to_back": team_to_back,
        "bookmaker": odds_data["bookmaker"],
        "odds": best_odds,
        "calculated_prob": best_prob,
        "edge": best_edge,
        "confidence": best_confidence,
        "kelly_fraction": kelly,
        "factors": factors,
        "signals": best_signals,  # siempre presente: poisson/elo/form/h2h reales
        "weights_version": weights_version,
        "created_at": datetime.now(timezone.utc),
        "result": None,
        "correct": None,
        "error_type": None,
    }

    # --- Guardar en Firestore predictions ---
    try:
        col("predictions").document(match_id).set(prediction)
        logger.info(
            "generate_signal(%s): %s @ %.2f | edge=%.1%% conf=%.0%% kelly=%.1%%",
            match_id, team_to_back, best_odds,
            best_edge * 100, best_confidence * 100, kelly * 100,
        )
    except Exception:
        logger.error("generate_signal(%s): error guardando prediction", match_id, exc_info=True)

    # --- Alerta Telegram si edge alto ---
    if best_edge > SPORTS_ALERT_EDGE:
        await _send_telegram_alert(_build_alert_payload(prediction, enriched_match))

    return prediction


def _get_weights_version() -> int:
    """Lee la version actual de model_weights. Devuelve 0 si no existe."""
    try:
        doc = col("model_weights").document("current").get()
        if doc.exists:
            return int(doc.to_dict().get("version", 0))
    except Exception:
        pass
    return 0


def _build_alert_payload(prediction: dict, enriched_match: dict) -> dict:
    """Construye el payload de alerta con los campos del formato Telegram."""
    # signals siempre contiene poisson/elo/form/h2h del ensemble_probability,
    # independientemente de si data_source es statistical_model o groq_ai.
    signals = prediction.get("signals", prediction.get("factors", {}))
    return {
        **prediction,
        "home_team": prediction.get("home_team", ""),
        "away_team": prediction.get("away_team", ""),
        "match_date": str(prediction.get("match_date", "")),
        "poisson": signals.get("poisson"),
        "elo": signals.get("elo"),
        "form": signals.get("form"),
        "h2h": signals.get("h2h"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
