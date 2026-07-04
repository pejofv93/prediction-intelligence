"""
API endpoint: POST /fetch-offers
Busca bonos vigentes de casas españolas via Groq + Tavily (buscador de bonos — Fase 2).

NOTA: el antiguo POST /find-odds (cuotas inventadas por LLM) se eliminó. Las cuotas
reales las sirve api/matched.py (GET /matched-signals) desde el detector back/lay.
"""
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


import json
import re


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
