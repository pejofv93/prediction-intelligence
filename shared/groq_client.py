"""
Cliente Groq (IA) y Tavily (web search) compartido.
NO importar openai ni tavily a nivel de modulo — evita ModuleNotFoundError
en servicios que no necesitan IA (ej: telegram-bot).
"""
import logging

from shared.config import (
    GROQ_API_KEY, TAVILY_API_KEY,
    GROQ_MODEL, GROQ_FALLBACK_MODEL, GROQ_BASE_URL,
)

logger = logging.getLogger(__name__)

_groq = None
_tavily = None

# Delay entre llamadas en batch para no exceder 6,000 tokens/min del free tier Groq
GROQ_CALL_DELAY = 4  # segundos


def _get_groq():
    global _groq
    if _groq is None:
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY no configurada para este servicio")
        from openai import OpenAI  # import aqui, no a nivel de modulo
        _groq = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
    return _groq


def _get_tavily():
    global _tavily
    if _tavily is None:
        if not TAVILY_API_KEY:
            raise RuntimeError("TAVILY_API_KEY no configurada para este servicio")
        from tavily import TavilyClient  # import aqui, no a nivel de modulo
        _tavily = TavilyClient(api_key=TAVILY_API_KEY)
    return _tavily


def search_web(query: str, max_results: int = 5) -> str:
    """Busca en la web con Tavily. Devuelve resultados formateados como string."""
    results = _get_tavily().search(query=query, max_results=max_results)
    return "\n\n".join(
        f"[{r['title']}]\n{r['content']}" for r in results.get("results", [])
    )


def analyze(system_prompt: str, user_prompt: str, web_search: bool = True) -> str:
    """
    Llama a Groq con contexto de busqueda web opcional.
    Si web_search=True, primero busca con Tavily y anade resultados al contexto.
    Si GROQ_MODEL falla con 404/model_not_found → reintenta con GROQ_FALLBACK_MODEL.
    Devuelve texto de respuesta.
    """
    if web_search:
        search_results = search_web(user_prompt[:200])
        enriched_prompt = f"""Resultados de busqueda web actuales:
{search_results}

---
{user_prompt}"""
    else:
        enriched_prompt = user_prompt

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": enriched_prompt},
    ]

    # Intentar con modelo principal, fallback si el modelo fue deprecado
    for model in [GROQ_MODEL, GROQ_FALLBACK_MODEL]:
        try:
            response = _get_groq().chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=2048,
                temperature=0.3,
            )
            return response.choices[0].message.content
        except Exception as e:
            if "model_not_found" in str(e).lower() or "404" in str(e):
                logger.warning("Modelo %s no encontrado, intentando fallback", model)
                continue
            raise  # otros errores propagar
    raise RuntimeError(f"Ambos modelos Groq fallaron: {GROQ_MODEL}, {GROQ_FALLBACK_MODEL}")
