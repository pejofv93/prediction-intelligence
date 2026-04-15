"""
API endpoints: POST /find-odds y POST /fetch-offers
Busca cuotas y bonos via Groq + Tavily.
LIMITACION: resultados orientativos, posiblemente desactualizados.
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class FindOddsRequest(BaseModel):
    event: str  # ej: "Real Madrid vs Barcelona"


import json
import re
from datetime import datetime, timezone


def _extract_json(raw: str):
    """Extrae JSON de respuesta de LLM (puede estar envuelto en ```json ... ```)."""
    # Intento 1: directo
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Intento 2: buscar array
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    # Intento 3: buscar objeto
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


@router.post("/find-odds")
async def find_odds(req: FindOddsRequest) -> dict:
    """
    Busca cuotas para un evento via Groq + Tavily.
    LIMITACION: cuotas orientativas, posiblemente desactualizadas.
    """
    from fastapi import HTTPException
    from shared.groq_client import _get_groq, _get_tavily
    from shared.config import GROQ_MODEL, GROQ_FALLBACK_MODEL

    system_prompt = (
        "Eres un experto en apuestas deportivas. Busca las cuotas actuales para el evento indicado "
        "en las principales casas de apuestas (Bet365, Bwin, William Hill, Betfair, Codere, Betway, etc). "
        "Responde SOLO en JSON con este formato exacto (sin texto adicional):\n"
        '{"odds": [{"bookmaker": "Nombre", "home": 2.1, "draw": 3.4, "away": 3.2}], '
        '"best_back": {"bookmaker": "Nombre", "odds": 2.1}, '
        '"best_lay": {"bookmaker": "Betfair", "odds": 2.0}}'
    )

    try:
        tavily = _get_tavily()
        search_results = tavily.search(query=f"cuotas apuestas {req.event} hoy", max_results=5)
        context = "\n\n".join(
            f"[{r['title']}]\n{r['content']}"
            for r in search_results.get("results", [])
        )
    except Exception:
        logger.warning("find_odds: Tavily no disponible — usando Groq sin contexto web")
        context = ""

    user_prompt = (
        f"Evento: {req.event}\n\n"
        + (f"Contexto de búsqueda web:\n{context}\n\n" if context else "")
        + "Proporciona las cuotas en JSON como se indicó."
    )

    raw = ""
    groq_client = _get_groq()
    for attempt, model in enumerate([GROQ_MODEL, GROQ_FALLBACK_MODEL]):
        try:
            if attempt == 1:
                user_prompt += "\n\nIMPORTANTE: Responde SOLO JSON, sin texto adicional."
            resp = groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1024,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content
            break
        except Exception as e:
            if "model_not_found" in str(e).lower() or "404" in str(e):
                continue
            logger.error("find_odds: error Groq — %s", e)
            raise HTTPException(status_code=502, detail="Error consultando IA")

    parsed = _extract_json(raw)
    if not parsed or not isinstance(parsed, dict):
        logger.error("find_odds: no se pudo parsear JSON de Groq: %s", raw[:300])
        raise HTTPException(status_code=502, detail="IA no devolvió datos estructurados")

    return {
        "event": req.event,
        "odds": parsed.get("odds", []),
        "best_back": parsed.get("best_back"),
        "best_lay": parsed.get("best_lay"),
        "warning": "Cuotas orientativas — verifica en la casa antes de apostar",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/fetch-offers")
async def fetch_offers() -> list[dict]:
    """
    Busca ofertas y bonos vigentes en casas de apuestas españolas via Groq + Tavily.
    """
    from fastapi import HTTPException
    from shared.groq_client import _get_groq, _get_tavily
    from shared.config import GROQ_MODEL, GROQ_FALLBACK_MODEL

    system_prompt = (
        "Eres un experto en matched betting. Busca los bonos y promociones VIGENTES HOY "
        "de casas de apuestas españolas (Bet365, Bwin, William Hill, Codere, Betway, Betfair, Sportium). "
        "Responde SOLO en JSON array (sin texto adicional):\n"
        '[{"bookmaker": "Bet365", "bonus": "Bono bienvenida", "amount": 100, '
        '"type": "welcome", "requirement": "Depósito mínimo €10", '
        '"rating": 4, "status": "activo", "advice": "Usar para qualifying con Betfair Exchange"}]'
    )

    try:
        tavily = _get_tavily()
        search_results = tavily.search(
            query="bonos bienvenida casas apuestas España 2025 matched betting",
            max_results=5,
        )
        context = "\n\n".join(
            f"[{r['title']}]\n{r['content']}"
            for r in search_results.get("results", [])
        )
    except Exception:
        logger.warning("fetch_offers: Tavily no disponible")
        context = ""

    user_prompt = (
        "Busca las mejores ofertas actuales de casas de apuestas para matched betting.\n\n"
        + (f"Contexto web:\n{context}\n\n" if context else "")
        + "Devuelve el JSON array como se indicó."
    )

    raw = ""
    groq_client = _get_groq()
    for attempt, model in enumerate([GROQ_MODEL, GROQ_FALLBACK_MODEL]):
        try:
            if attempt == 1:
                user_prompt += "\n\nResponde SOLO el JSON array."
            resp = groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1500,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content
            break
        except Exception as e:
            if "model_not_found" in str(e).lower() or "404" in str(e):
                continue
            logger.error("fetch_offers: error Groq — %s", e)
            raise HTTPException(status_code=502, detail="Error consultando IA")

    parsed = _extract_json(raw)
    if not parsed:
        logger.error("fetch_offers: no se pudo parsear JSON: %s", raw[:300])
        raise HTTPException(status_code=502, detail="IA no devolvió datos estructurados")

    if isinstance(parsed, dict):
        parsed = [parsed]

    return parsed if isinstance(parsed, list) else []
