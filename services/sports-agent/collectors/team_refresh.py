"""¿Hay que volver a pedir a football-data.org el historial de un equipo en el collect?

football-data.org permite 10 peticiones/min y el collect pedía cada equipo de la ventana de 7 días
aunque no hubiera jugado desde la última descarga (18-nov-2026: 105 partidos → paso de fútbol de
~29 min, collect de 38 min con el corte a los 40). Un equipo solo cambia de historial cuando juega.

Regla (decidida por el usuario el 18-nov-2026):
  - Sin historial guardado → se pide.
  - Equipo con partidos en la lista de terminados (GET /matches?status=FINISHED, últimos 30 días):
    se pide solo si ha jugado desde la última descarga (fin estimado del partido posterior a
    updated_at).
  - Equipo sin partidos en esa lista (LaLiga: el plan gratuito no la devuelve por competición):
    se pide si la descarga tiene 72 h o más.
  - Si la lista de terminados no se pudo bajar: vuelve la regla anterior (20 h).
  - Salvaguarda: con 7 días o más se pide siempre (copas u otras competiciones fuera de la lista).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

MATCH_END = timedelta(hours=2, minutes=15)    # inicio + 2 h 15 min ≈ final con descanso y añadido
NO_RESULTS_TTL = timedelta(hours=72)          # equipos sin resultados por competición (LaLiga)
FALLBACK_TTL = timedelta(hours=20)            # si la lista de terminados no está disponible
MAX_AGE = timedelta(days=7)


def _dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def last_kickoff_by_team(finished: list[dict]) -> dict[int, datetime]:
    """{team_id: inicio del último partido terminado} desde la lista de get_finished_matches."""
    out: dict[int, datetime] = {}
    for m in finished:
        ko = _dt(m.get("date"))
        if ko is None:
            continue
        for tid in (m.get("home_team_id"), m.get("away_team_id")):
            if tid is not None and (tid not in out or ko > out[tid]):
                out[tid] = ko
    return out


def needs_refresh(updated_at, last_kickoff: Optional[datetime], results_available: bool,
                  now: datetime) -> tuple[bool, str]:
    """(pedir, motivo). Motivos: sin_historial, 7_dias, sin_lista_20h, ha_jugado, sin_jugar,
    sin_resultados_72h, reciente."""
    up = _dt(updated_at)
    if up is None:
        return True, "sin_historial"
    age = now - up
    if age >= MAX_AGE:
        return True, "7_dias"
    if not results_available:
        return (age >= FALLBACK_TTL), ("sin_lista_20h" if age >= FALLBACK_TTL else "reciente")
    if last_kickoff is not None:
        return (True, "ha_jugado") if last_kickoff + MATCH_END > up else (False, "sin_jugar")
    return (age >= NO_RESULTS_TTL), ("sin_resultados_72h" if age >= NO_RESULTS_TTL else "reciente")
