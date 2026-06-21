"""
Guard de timing para señales deportivas.

Evita emitir/enviar señales de partidos que ya empezaron o que están demasiado
próximos al inicio (faltan menos de SIGNAL_MIN_MINUTES_BEFORE_KICKOFF minutos).

Filosofía fail-open: si match_date no es parseable o es solo fecha (sin hora),
NO se descarta la señal — no podemos determinar la hora de inicio con certeza y
preferimos no tirar señales legítimas por un formato inesperado. El guard solo
actúa cuando conocemos la hora real de inicio.

Compartido por sports-agent (generación) y telegram-bot (envío).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from shared.config import SIGNAL_MIN_MINUTES_BEFORE_KICKOFF as _DEFAULT_MARGIN
except Exception:  # pragma: no cover — fallback si config no disponible
    _DEFAULT_MARGIN = 20


def parse_kickoff(match_date) -> tuple[datetime | None, bool]:
    """
    Parsea match_date (datetime | ISO str | 'YYYY-MM-DD HH:MM' | 'YYYY-MM-DD').

    Devuelve (dt_utc_aware, has_time):
      - dt_utc_aware: datetime con tz UTC, o None si no parseable.
      - has_time: True si el valor incluye hora de inicio; False si es solo fecha.
    """
    if match_date is None:
        return None, False

    # Objeto datetime (p.ej. Timestamp de Firestore) → siempre con hora real
    if hasattr(match_date, "strftime") and not isinstance(match_date, str):
        dt = match_date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), True

    if isinstance(match_date, str):
        raw = match_date.strip()
        if not raw:
            return None, False
        # ¿incluye componente horario? 'T' (ISO) o un espacio seguido de HH:MM
        has_time = ("T" in raw) or (len(raw) > 10 and ":" in raw[10:])
        try:
            if has_time:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None, False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), has_time

    return None, False


def signal_is_too_late(match_date, margin_minutes: int | None = None) -> bool:
    """
    True si el partido ya empezó o faltan menos de `margin_minutes` para el inicio.

    Fail-open: devuelve False (no descartar) si match_date no es parseable o es
    solo fecha sin hora — no podemos afirmar que ya empezó.
    """
    margin = _DEFAULT_MARGIN if margin_minutes is None else margin_minutes
    dt, has_time = parse_kickoff(match_date)
    if dt is None or not has_time:
        return False
    now = datetime.now(timezone.utc)
    return dt <= now + timedelta(minutes=margin)


def kickoff_label(match_date) -> str:
    """Etiqueta legible de la hora de inicio para logs ('2026-06-21 19:00 UTC' o repr)."""
    dt, has_time = parse_kickoff(match_date)
    if dt is None:
        return repr(match_date)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if has_time else dt.strftime("%Y-%m-%d (sin hora)")
