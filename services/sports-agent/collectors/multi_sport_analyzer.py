"""
Analizador IA para deportes sin modelo Poisson (NBA, NFL, MLB, NHL, MMA).
Usa Groq (LLM) + Tavily (web search) para estimar probabilidades de victoria.
data_source="groq_ai" distingue estas predicciones de las estadisticas puras.
"""
import asyncio
import json
import logging
import re

import shared.groq_client as groq_client
from shared.groq_client import GROQ_CALL_DELAY

logger = logging.getLogger(__name__)

# Nombres legibles por deporte para el system prompt
_SPORT_NAMES = {
    "nba": "baloncesto NBA",
    "nfl": "futbol americano NFL",
    "mlb": "beisbol MLB",
    "nhl": "hockey sobre hielo NHL",
    "mma": "artes marciales mixtas MMA/UFC",
    "ufc": "artes marciales mixtas MMA/UFC",
}


def _extract_json(text: str) -> dict | None:
    """
    Estrategia de extraccion JSON en 4 pasos segun las reglas globales del spec:
    1. json.loads directo
    2. re.search para extraer objeto JSON embebido
    3. Devuelve None si falla (el caller reintentara con instruccion explicita)
    """
    # Paso 1: parse directo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Paso 2: extraer objeto JSON embebido (GPT a veces envuelve en ```json ... ```)
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


async def analyze_non_football_game(
    game: dict,
    home_stats: list[dict],
    away_stats: list[dict],
) -> dict:
    """
    Estima probabilidad de victoria local para deportes sin modelo Poisson.

    1. Busca noticias recientes con Tavily (lesiones, forma, contexto).
    2. Llama Groq con stats + noticias → estima probabilidades.
    3. Aplica estrategia de extraccion JSON (4 pasos).
    4. Si todo falla → devuelve estimacion neutral con data_quality="partial".

    Devuelve {home_win_prob, confidence, key_factors, data_source: "groq_ai"}.
    """
    sport = game.get("sport", "nba")
    sport_name = _SPORT_NAMES.get(sport, sport.upper())
    home_team = game.get("home_team_name", "Local")
    away_team = game.get("away_team_name", "Visitante")

    # Construir resumen de stats para el prompt
    home_summary = _summarize_stats(home_team, home_stats)
    away_summary = _summarize_stats(away_team, away_stats)

    system_prompt = (
        f"Eres un experto en {sport_name}. "
        f"Dados los siguientes datos estadisticos y noticias recientes, "
        f"estima la probabilidad de victoria del equipo local ({home_team}). "
        "Responde SOLO JSON valido, sin texto adicional, con exactamente estas claves: "
        '{"home_win_prob": float, "confidence": float, "key_factors": ["str", ...]}'
        " donde home_win_prob y confidence estan en [0.0, 1.0]."
    )

    user_prompt = (
        f"Partido: {home_team} vs {away_team}\n\n"
        f"Stats {home_team} (ultimos partidos):\n{home_summary}\n\n"
        f"Stats {away_team} (ultimos partidos):\n{away_summary}\n\n"
        f"Analiza el partido y estima la probabilidad de victoria local."
    )

    await asyncio.sleep(GROQ_CALL_DELAY)

    # Intento 1: con web_search=True (Tavily busca noticias)
    try:
        response_text = groq_client.analyze(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            web_search=True,
        )
        parsed = _extract_json(response_text)
        if parsed:
            return _build_result(parsed, sport)
    except Exception as e:
        logger.error(
            "analyze_non_football_game(%s, %s vs %s) intento 1 fallido: %s",
            sport, home_team, away_team, e, exc_info=True,
        )

    # Intento 2 (paso 3 del spec): reintentar con instruccion explicita SOLO JSON
    await asyncio.sleep(GROQ_CALL_DELAY)
    try:
        retry_system = (
            system_prompt
            + " IMPORTANTE: tu respuesta debe ser UNICAMENTE el JSON, "
            "sin ningun texto antes ni despues, sin markdown, sin explicaciones."
        )
        response_text = groq_client.analyze(
            system_prompt=retry_system,
            user_prompt=user_prompt,
            web_search=False,  # sin web search en el reintento para ser mas rapido
        )
        parsed = _extract_json(response_text)
        if parsed:
            return _build_result(parsed, sport)
    except Exception as e:
        logger.error(
            "analyze_non_football_game(%s, %s vs %s) intento 2 fallido: %s",
            sport, home_team, away_team, e, exc_info=True,
        )

    # Paso 4: descartar analisis — devolver neutral con data_quality="partial"
    logger.warning(
        "analyze_non_football_game(%s, %s vs %s): ambos intentos fallaron — devolviendo neutral",
        sport, home_team, away_team,
    )
    return {
        "home_win_prob": 0.5,
        "confidence": 0.3,
        "key_factors": ["datos insuficientes"],
        "data_source": "groq_ai",
        "data_quality": "partial",
    }


def _build_result(parsed: dict, sport: str) -> dict:
    """
    Valida y normaliza el resultado del LLM.
    Clampea valores a [0.0, 1.0] y asegura que key_factors es una lista.
    """
    try:
        home_win_prob = float(parsed.get("home_win_prob", 0.5))
        confidence = float(parsed.get("confidence", 0.5))
        key_factors = parsed.get("key_factors", [])

        # Clampear a rango valido
        home_win_prob = max(0.0, min(1.0, home_win_prob))
        confidence = max(0.0, min(1.0, confidence))

        if not isinstance(key_factors, list):
            key_factors = [str(key_factors)]

        return {
            "home_win_prob": home_win_prob,
            "confidence": confidence,
            "key_factors": key_factors[:5],  # max 5 factores
            "data_source": "groq_ai",
            "data_quality": "full",
        }
    except (TypeError, ValueError) as e:
        logger.warning("_build_result: error normalizando respuesta LLM: %s — %s", parsed, e)
        return {
            "home_win_prob": 0.5,
            "confidence": 0.3,
            "key_factors": ["error al parsear respuesta"],
            "data_source": "groq_ai",
            "data_quality": "partial",
        }


def _summarize_stats(team_name: str, matches: list[dict]) -> str:
    """
    Genera un resumen textual de los ultimos partidos de un equipo
    para incluir en el prompt de Groq.
    """
    if not matches:
        return f"{team_name}: sin datos de partidos recientes disponibles."

    wins = losses = draws = 0
    goals_for = goals_against = 0

    for m in matches:
        home_id = m.get("home_team_id")
        home_name = m.get("home_team_name", "")
        away_name = m.get("away_team_name", "")
        gh = m.get("goals_home") or 0
        ga = m.get("goals_away") or 0

        # Determinar si el equipo fue local o visitante por nombre
        is_home = home_name.lower() == team_name.lower()
        gf = gh if is_home else ga
        gc = ga if is_home else gh

        goals_for += gf
        goals_against += gc

        if gf > gc:
            wins += 1
        elif gf < gc:
            losses += 1
        else:
            draws += 1

    total = wins + losses + draws
    avg_for = goals_for / total if total > 0 else 0
    avg_against = goals_against / total if total > 0 else 0

    return (
        f"{team_name}: {wins}W-{draws}D-{losses}L "
        f"en ultimos {total} partidos. "
        f"Media: {avg_for:.1f} goles/pts favor, {avg_against:.1f} en contra."
    )
