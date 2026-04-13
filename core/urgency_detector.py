"""
urgency_detector.py
Analiza un topic o titular y devuelve un score de urgencia 0-100.
Si el score >= 70 el pipeline se trata como URGENTE (TikTok primero).
"""

import re
from typing import Tuple
from utils.logger import get_logger

logger = get_logger("URGENCY_DETECTOR")

# ── Tabla de palabras clave con peso ──────────────────────────────────────────
KEYWORD_WEIGHTS: dict[str, int] = {
    # Seguridad / hack
    "hack": 25,
    "exploit": 25,
    "rugpull": 25,
    "rug pull": 25,
    "hackeo": 25,
    "hackeado": 25,
    "robado": 20,
    "robo": 20,
    # Colapso / crash
    "crash": 25,
    "colapso": 25,
    "caída": 5,          # reducido: palabra común en análisis normales
    "caida": 5,          # reducido: palabra común en análisis normales
    "desplome": 10,      # reducido: requiere contexto adicional
    "liquidaciones": 20,
    "liquidación masiva": 25,
    # Regulación
    "sec": 20,
    "regulación": 15,
    "regulacion": 15,
    "ban": 20,
    "prohibición": 20,
    "prohibicion": 20,
    "sanción": 15,
    "sancion": 15,
    # Máximos / mínimos históricos
    "all-time-high": 20,
    "ath": 20,
    "all time high": 20,
    "máximo histórico": 20,
    "maximo historico": 20,
    "mínimo histórico": 20,
    "minimo historico": 20,
    # Variaciones extremas (se detectan con regex)
    # "+20%", "-20%", "+30%", etc. → ver regex abajo
    # Urgencia explícita
    "urgente": 30,
    "breaking": 30,
    "alert": 20,
    "alerta": 20,
    "flash": 15,
    # Movimientos de ballenas
    "ballena": 10,
    "whale": 10,
    "masiva compra": 15,
    "massive buy": 15,
    "dump masivo": 20,
    "massive sell": 20,
    # ETF / institucional
    "etf aprobado": 20,
    "etf rechazado": 20,
    "blackrock": 10,
    "fidelity": 10,
}

# Regex para variaciones de precio en el texto del topic
_PRICE_MOVE_PATTERN = re.compile(
    r"([+\-±])\s*(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# Keywords que indican emergencia real (hack, ban, colapso sistémico)
# Solo con estas o con movimiento de precio >=5% se activa URGENTE
_EMERGENCY_KEYWORDS: frozenset[str] = frozenset({
    "hack", "exploit", "rugpull", "rug pull", "hackeo", "hackeado",
    "robado", "robo", "ban", "prohibición", "prohibicion",
    "urgente", "breaking", "liquidación masiva", "liquidacion masiva",
})


def _score_price_moves(text: str) -> int:
    """Suma puntos por cada variación de precio >= 10% mencionada en el texto."""
    total = 0
    for match in _PRICE_MOVE_PATTERN.finditer(text):
        try:
            pct = float(match.group(2))
            if pct >= 20:
                total += 25
            elif pct >= 15:
                total += 15
            elif pct >= 10:
                total += 10
        except ValueError:
            pass
    return total


def _has_significant_price_move(text: str, min_pct: float = 5.0) -> bool:
    """Devuelve True si el texto menciona explícitamente un movimiento >= min_pct%."""
    for match in _PRICE_MOVE_PATTERN.finditer(text):
        try:
            if float(match.group(2)) >= min_pct:
                return True
        except ValueError:
            pass
    return False


def detect_urgency(text: str) -> Tuple[float, bool, list[str]]:
    """
    Analiza el texto y devuelve:
      - score (float 0–100)
      - is_urgent (bool)
      - matched_keywords (list[str])

    Regla: URGENTE solo se activa si:
      a) El score >= 70 Y hay una keyword de emergencia real (hack/ban/etc.), O
      b) El score >= 70 Y el topic menciona un movimiento de precio >= 5%.
    Palabras genéricas como "caída" o "desplome" solas NO activan URGENTE.
    """
    lower = text.lower()
    score = 0
    matched: list[str] = []

    for keyword, weight in KEYWORD_WEIGHTS.items():
        if keyword in lower:
            score += weight
            matched.append(keyword)
            logger.debug(f"Keyword '{keyword}' encontrada (+{weight})")

    price_score = _score_price_moves(lower)
    if price_score:
        score += price_score
        matched.append(f"variación_precio(+{price_score}pts)")

    score = min(score, 100)

    # Condición de urgencia: score alto + (emergencia real O movimiento >=5%)
    has_emergency = any(kw in lower for kw in _EMERGENCY_KEYWORDS)
    has_price_move = _has_significant_price_move(text, min_pct=5.0)
    is_urgent = score >= 70 and (has_emergency or has_price_move)

    logger.info(
        f"Urgency score: {score}/100  |  urgente={is_urgent}  |  "
        f"emergency={has_emergency}  |  price_move_5pct={has_price_move}  |  "
        f"keywords={matched}"
    )
    return float(score), is_urgent, matched


class UrgencyDetector:
    """Wrapper orientado a objetos para usar desde el pipeline."""

    def run(self, ctx):
        """
        Enriquece el Context con urgency_score e is_urgent.
        Usa ctx.topic como texto a analizar.
        Nunca hace crash: todos los errores van a ctx.errors.
        """
        try:
            score, is_urgent, keywords = detect_urgency(ctx.topic)
            ctx.urgency_score = score
            ctx.is_urgent = is_urgent
            if keywords:
                ctx.add_warning(
                    "URGENCY_DETECTOR",
                    f"Keywords de urgencia detectadas: {keywords}",
                )
        except Exception as exc:
            ctx.add_error("URGENCY_DETECTOR", str(exc))
            logger.exception("Error en UrgencyDetector.run()")
        return ctx
