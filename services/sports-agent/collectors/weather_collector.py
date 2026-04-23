"""
Weather impact for sports signals using Open-Meteo API (free, no key).
https://api.open-meteo.com/v1/forecast
"""
import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

# Coordenadas de estadios principales (lat, lon)
STADIUM_COORDS: dict[str, tuple[float, float]] = {
    # España
    "Santiago Bernabéu": (40.4531, -3.6883),
    "Camp Nou": (41.3809, 2.1228),
    "Metropolitano": (40.4361, -3.5996),
    "Ramón Sánchez-Pizjuán": (37.3838, -5.9706),
    "Mestalla": (39.4746, -0.3585),
    # Inglaterra
    "Old Trafford": (53.4631, -2.2913),
    "Anfield": (53.4308, -2.9608),
    "Etihad Stadium": (53.4831, -2.2004),
    "Emirates Stadium": (51.5549, -0.1084),
    "Stamford Bridge": (51.4817, -0.1910),
    "Tottenham Hotspur Stadium": (51.6042, -0.0665),
    # Alemania
    "Allianz Arena": (48.2188, 11.6248),
    "Signal Iduna Park": (51.4926, 7.4519),
    # Italia
    "San Siro": (45.4781, 9.1240),
    "Stadio Olimpico": (41.9340, 12.4547),
    "Stadio Diego Armando Maradona": (40.8278, 14.1931),
    # Francia
    "Parc des Princes": (48.8414, 2.2530),
    # Portugal
    "Estádio da Luz": (38.7524, -9.1843),
    "Estádio do Dragão": (41.1619, -8.5831),
}

# Mapeo team_name → stadium (approx)
TEAM_TO_STADIUM: dict[str, str] = {
    "real madrid": "Santiago Bernabéu",
    "barcelona": "Camp Nou",
    "atletico madrid": "Metropolitano",
    "atlético de madrid": "Metropolitano",
    "sevilla": "Ramón Sánchez-Pizjuán",
    "valencia": "Mestalla",
    "manchester united": "Old Trafford",
    "liverpool": "Anfield",
    "manchester city": "Etihad Stadium",
    "arsenal": "Emirates Stadium",
    "chelsea": "Stamford Bridge",
    "tottenham": "Tottenham Hotspur Stadium",
    "spurs": "Tottenham Hotspur Stadium",
    "bayern": "Allianz Arena",
    "dortmund": "Signal Iduna Park",
    "borussia dortmund": "Signal Iduna Park",
    "inter": "San Siro",
    "milan": "San Siro",
    "ac milan": "San Siro",
    "internazionale": "San Siro",
    "lazio": "Stadio Olimpico",
    "roma": "Stadio Olimpico",
    "napoli": "Stadio Diego Armando Maradona",
    "psg": "Parc des Princes",
    "paris saint-germain": "Parc des Princes",
    "benfica": "Estádio da Luz",
    "porto": "Estádio do Dragão",
}

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


async def get_match_weather(lat: float, lon: float, match_datetime) -> dict:
    """
    Obtiene condiciones meteorológicas para la hora del partido.

    Returns:
    {
      temperature_c: float,
      precipitation_mm: float,
      wind_kmh: float,
      humidity_pct: float,
      description: str,
      has_impact: bool
    }
    Si falla: {"has_impact": False, "error": str(e)}
    """
    try:
        # Normalizar match_datetime a datetime con timezone
        if isinstance(match_datetime, str):
            try:
                if match_datetime.endswith("Z"):
                    match_datetime = match_datetime[:-1] + "+00:00"
                match_datetime = datetime.fromisoformat(match_datetime)
            except Exception:
                match_datetime = datetime.now(timezone.utc)

        if not isinstance(match_datetime, datetime):
            match_datetime = datetime.now(timezone.utc)

        if match_datetime.tzinfo is None:
            match_datetime = match_datetime.replace(tzinfo=timezone.utc)

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "precipitation,windspeed_10m,temperature_2m,relativehumidity_2m",
            "timezone": "auto",
            "forecast_days": 7,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_OPEN_METEO_URL, params=params)
            response.raise_for_status()
            data = response.json()

        hourly = data.get("hourly", {})
        times: list[str] = hourly.get("time", [])
        temps: list[float] = hourly.get("temperature_2m", [])
        precips: list[float] = hourly.get("precipitation", [])
        winds: list[float] = hourly.get("windspeed_10m", [])
        humidities: list[float] = hourly.get("relativehumidity_2m", [])

        # Encontrar índice más cercano al match_datetime
        best_idx = 0
        best_diff = None

        for i, time_str in enumerate(times):
            try:
                if time_str.endswith("Z"):
                    time_str = time_str[:-1] + "+00:00"
                t = datetime.fromisoformat(time_str)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                diff = abs((t - match_datetime).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_idx = i
            except Exception:
                continue

        temperature_c = float(temps[best_idx]) if best_idx < len(temps) else 15.0
        precipitation_mm = float(precips[best_idx]) if best_idx < len(precips) else 0.0
        wind_kmh = float(winds[best_idx]) if best_idx < len(winds) else 0.0
        humidity_pct = float(humidities[best_idx]) if best_idx < len(humidities) else 50.0

        # Generar descripción e impacto
        desc_parts = []
        has_impact = False

        if precipitation_mm > 10:
            desc_parts.append("Lluvia intensa")
            has_impact = True
        elif precipitation_mm > 5:
            desc_parts.append("Lluvia moderada")
            has_impact = True
        elif precipitation_mm > 1:
            desc_parts.append("Lluvia ligera")

        if wind_kmh > 50:
            desc_parts.append("Viento muy fuerte")
            has_impact = True
        elif wind_kmh > 40:
            desc_parts.append("Viento fuerte")
            has_impact = True
        elif wind_kmh > 25:
            desc_parts.append("Viento moderado")

        if temperature_c < 2:
            desc_parts.append("Frío extremo")
            has_impact = True
        elif temperature_c > 35:
            desc_parts.append("Calor extremo")
            has_impact = True

        description = ", ".join(desc_parts) if desc_parts else "Normal"

        logger.debug(
            "weather_collector: lat=%.2f lon=%.2f temp=%.1f°C precip=%.1fmm wind=%.1fkm/h",
            lat, lon, temperature_c, precipitation_mm, wind_kmh,
        )

        return {
            "temperature_c": round(temperature_c, 1),
            "precipitation_mm": round(precipitation_mm, 2),
            "wind_kmh": round(wind_kmh, 1),
            "humidity_pct": round(humidity_pct, 1),
            "description": description,
            "has_impact": has_impact,
        }

    except Exception as e:
        logger.warning("weather_collector: error en get_match_weather — %s", e)
        return {"has_impact": False, "error": str(e)}


def get_stadium_coords(home_team: str) -> tuple[float, float] | None:
    """Busca coordenadas del estadio del equipo local. Case-insensitive."""
    if not home_team:
        return None

    key = home_team.lower().strip()
    stadium = TEAM_TO_STADIUM.get(key)
    if stadium:
        return STADIUM_COORDS.get(stadium)

    # Búsqueda parcial
    for team_key, stadium_name in TEAM_TO_STADIUM.items():
        if team_key in key or key in team_key:
            return STADIUM_COORDS.get(stadium_name)

    return None


def adjust_signal_for_weather(signal: dict, weather: dict) -> dict:
    """
    Ajusta la señal según condiciones meteorológicas.
    - precipitation_mm > 5: goals_modifier *= 0.85
    - wind_kmh > 40: goals_modifier *= 0.90 (compounding)
    - temperature_c < 2 OR > 35: confidence *= 0.92
    - Añade weather_impact y weather_note si has_impact.
    Clampar confidence a [0.0, 1.0]. Nunca falla.
    """
    try:
        if not weather or not weather.get("has_impact", False):
            return signal

        precipitation_mm = float(weather.get("precipitation_mm", 0) or 0)
        wind_kmh = float(weather.get("wind_kmh", 0) or 0)
        temperature_c = float(weather.get("temperature_c", 15) or 15)
        confidence = float(signal.get("confidence", 1.0))

        goals_modifier = 1.0

        if precipitation_mm > 5:
            goals_modifier *= 0.85

        if wind_kmh > 40:
            goals_modifier *= 0.90

        # Aplicar goals_modifier a lambdas si existen, o guardar el modificador
        if goals_modifier < 1.0:
            if "lambda_home" in signal:
                signal["lambda_home"] = round(float(signal["lambda_home"]) * goals_modifier, 4)
            if "lambda_away" in signal:
                signal["lambda_away"] = round(float(signal["lambda_away"]) * goals_modifier, 4)
            if "lambda_home" not in signal and "lambda_away" not in signal:
                signal["weather_goals_modifier"] = round(goals_modifier, 4)

        if temperature_c < 2 or temperature_c > 35:
            confidence *= 0.92

        signal["confidence"] = round(min(max(confidence, 0.0), 1.0), 4)

        signal["weather_impact"] = {
            "description": weather.get("description", "Normal"),
            "temperature": weather.get("temperature_c"),
            "precipitation": precipitation_mm,
            "wind": wind_kmh,
        }

        if weather.get("has_impact"):
            signal["weather_note"] = (
                f"Clima: {weather.get('description', '')} "
                f"({temperature_c:.0f}°C, {precipitation_mm:.1f}mm lluvia, {wind_kmh:.0f}km/h viento)"
            )

        logger.debug(
            "weather_collector: adjust_signal goals_modifier=%.2f confidence→%.4f",
            goals_modifier, signal["confidence"],
        )

    except Exception as e:
        logger.warning("weather_collector: error en adjust_signal_for_weather — %s", e)

    return signal


async def enrich_signal_with_weather(match: dict, signal: dict) -> dict:
    """
    Orquestador: busca coords del estadio, obtiene clima, ajusta signal.
    Si home_team no tiene coords conocidas: devolver signal sin cambios.
    """
    try:
        home_team = (
            match.get("home_team_name")
            or match.get("home_team")
            or ""
        )
        match_dt = match.get("match_date") or match.get("match_datetime") or match.get("date")

        coords = get_stadium_coords(home_team)
        if coords is None:
            logger.debug(
                "weather_collector: sin coords para '%s' — omitiendo clima", home_team
            )
            return signal

        lat, lon = coords
        weather = await get_match_weather(lat, lon, match_dt)
        signal = adjust_signal_for_weather(signal, weather)

    except Exception as e:
        logger.warning("weather_collector: error en enrich_signal_with_weather — %s", e)

    return signal
