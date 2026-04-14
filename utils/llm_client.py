import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / '.env')
"""
utils/llm_client.py
Rotación automática de proveedores LLM gratuitos.

Orden de prioridad:
  1. OpenRouter — meta-llama/llama-3.3-70b-instruct:free  (sin límite diario)
  2. Groq       — llama-3.3-70b-versatile                 (100k tokens/día)
  3. Gemini     — gemini-2.0-flash                        (1M tokens/día)
  4. Cerebras   — llama3.1-8b                             (1M tokens/día, OpenAI-compatible)
  5. Ollama     — llama3.2 local                          (ilimitado, solo si está corriendo)

Lógica:
  - Si un proveedor da 429/rate_limit → pasar al siguiente inmediatamente
  - El uso diario se persiste en SQLite (tabla llm_usage)
  - Al inicio de cada día UTC se resetea el contador y se vuelve a OpenRouter
"""

import os
import time
import httpx
from datetime import date
from utils.logger import get_logger

logger = get_logger("LLM_CLIENT")

# Límites diarios orientativos (tokens). Se usa para logging, no para bloquear.
_DAILY_LIMITS = {
    "openrouter": 999_999_999,
    "groq":       100_000,
    "gemini":     1_000_000,
    "cerebras":   1_000_000,
    "ollama":     999_999_999,
}

# Orden de rotación
_PROVIDER_ORDER = ["openrouter", "groq", "gemini", "cerebras", "ollama"]

# Modelos OpenRouter gratuitos — se rotan si el primero da error
_OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-7b-instruct:free",
]


def _estimate_tokens(text: str) -> int:
    """Estimación rápida: ~1 token por cada 4 caracteres."""
    return max(1, len(text) // 4)


class LLMClient:
    """
    Cliente LLM con rotación automática Groq → Gemini → Cerebras → Ollama.

    Uso:
        client = LLMClient(config)                    # sin persistencia
        client = LLMClient(config, db=db_manager)     # con tracking SQLite
        respuesta = client.generate("Explica Bitcoin en 3 frases.")
    """

    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db  # DBManager opcional — si None no persiste uso
        llm_cfg = config.get("llm", {})
        self.model          = llm_cfg.get("model",          "llama-3.3-70b-versatile")
        self.fallback_model = llm_cfg.get("fallback_model", "llama3.2")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

        # Caché de clientes (lazy init)
        self._groq_client = None

    # ══════════════════════════════════════════════════════════════════════════
    # Método público principal
    # ══════════════════════════════════════════════════════════════════════════

    def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        """
        Genera texto rotando proveedores automáticamente.
        Si todos fallan lanza RuntimeError con el resumen de errores.
        """
        errors = {}
        for provider in _PROVIDER_ORDER:
            try:
                text = self._call_provider(
                    provider, prompt, system, max_tokens, temperature
                )
                self._track_usage(provider, prompt, text)
                return text
            except _RateLimitError as exc:
                logger.warning(
                    f"[yellow]LLM[/] {provider} rate_limit — "
                    f"rotando al siguiente proveedor. ({exc})"
                )
                errors[provider] = f"rate_limit: {exc}"
                continue
            except _ProviderUnavailable as exc:
                logger.warning(
                    f"[yellow]LLM[/] {provider} no disponible — "
                    f"rotando. ({exc})"
                )
                errors[provider] = f"unavailable: {exc}"
                continue
            except Exception as exc:
                logger.warning(f"[yellow]LLM[/] {provider} error inesperado: {exc}")
                errors[provider] = str(exc)
                continue

        raise RuntimeError(
            "Todos los proveedores LLM fallaron.\n"
            + "\n".join(f"  {p}: {e}" for p, e in errors.items())
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Dispatcher por proveedor
    # ══════════════════════════════════════════════════════════════════════════

    def _call_provider(
        self,
        provider: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        if provider == "openrouter":
            return self._generate_openrouter(prompt, system, max_tokens, temperature)
        if provider == "groq":
            return self._generate_groq(prompt, system, max_tokens, temperature)
        if provider == "gemini":
            return self._generate_gemini(prompt, system, max_tokens, temperature)
        if provider == "cerebras":
            return self._generate_cerebras(prompt, system, max_tokens, temperature)
        if provider == "ollama":
            return self._generate_ollama(prompt, system, max_tokens, temperature)
        raise _ProviderUnavailable(f"Proveedor desconocido: {provider}")

    # ══════════════════════════════════════════════════════════════════════════
    # Proveedor 1 — OpenRouter (modelos gratuitos, sin límite diario)
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_openrouter(
        self, prompt: str, system: str, max_tokens: int, temperature: float
    ) -> str:
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            raise _ProviderUnavailable("OPENROUTER_API_KEY no configurada")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_exc: Exception = Exception("sin intentos")
        for model in _OPENROUTER_MODELS:
            logger.info(f"[dim]LLM[/] [bold #6C5CE7]OpenRouter[/] [{model}]")
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://cryptoverdad.com",
                            "X-Title": "CryptoVerdad NEXUS",
                        },
                        json={
                            "model": model,
                            "messages": messages,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                        },
                    )
                if resp.status_code == 429:
                    raise _RateLimitError(f"OpenRouter 429 [{model}]: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise _ProviderUnavailable(
                        f"OpenRouter HTTP {resp.status_code} [{model}]: {resp.text[:200]}"
                    )
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if content:
                    return content.strip()
                raise _ProviderUnavailable(f"OpenRouter [{model}]: respuesta vacía")
            except (_RateLimitError, _ProviderUnavailable):
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(f"[yellow]LLM[/] OpenRouter [{model}] falló: {exc} — probando siguiente modelo")
                continue

        raise _ProviderUnavailable(f"Todos los modelos OpenRouter fallaron: {last_exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # Proveedor 3 — Groq
    # ══════════════════════════════════════════════════════════════════════════

    def _get_groq_client(self):
        if self._groq_client is None:
            api_key = os.getenv("GROQ_API_KEY", "")
            if not api_key:
                raise _ProviderUnavailable("GROQ_API_KEY no configurada")
            try:
                from groq import Groq
                self._groq_client = Groq(api_key=api_key)
            except ImportError:
                raise _ProviderUnavailable("librería groq no instalada")
        return self._groq_client

    def _generate_groq(
        self, prompt: str, system: str, max_tokens: int, temperature: float
    ) -> str:
        client = self._get_groq_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        logger.info(f"[dim]LLM[/] [bold #F7931A]Groq[/] [{self.model}]")
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            msg = str(exc).lower()
            if "rate_limit_exceeded" in msg or "429" in msg:
                raise _RateLimitError(str(exc))
            raise

    # ══════════════════════════════════════════════════════════════════════════
    # Proveedor 4 — Google Gemini
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_gemini(
        self, prompt: str, system: str, max_tokens: int, temperature: float
    ) -> str:
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise _ProviderUnavailable("GEMINI_API_KEY no configurada")
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError:
            raise _ProviderUnavailable("librería google-generativeai no instalada")

        genai.configure(api_key=api_key)
        logger.info("[dim]LLM[/] [bold #4285F4]Gemini[/] [gemini-2.0-flash]")

        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        def _call_gemini() -> str:
            model = genai.GenerativeModel(
                "gemini-2.0-flash",
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                ),
            )
            response = model.generate_content(full_prompt)
            return response.text.strip()

        try:
            return _call_gemini()
        except Exception as exc:
            msg = str(exc).lower()
            if "quota" in msg or "429" in msg or "resource_exhausted" in msg:
                logger.warning(
                    "[yellow]LLM[/] Gemini quota — esperando 60s y reintentando..."
                )
                time.sleep(60)
                try:
                    return _call_gemini()
                except Exception as exc2:
                    msg2 = str(exc2).lower()
                    if "quota" in msg2 or "429" in msg2 or "resource_exhausted" in msg2:
                        raise _RateLimitError(str(exc2))
                    raise _ProviderUnavailable(str(exc2))
            raise _ProviderUnavailable(str(exc))

    # ══════════════════════════════════════════════════════════════════════════
    # Proveedor 5 — Cerebras (API OpenAI-compatible)
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_cerebras(
        self, prompt: str, system: str, max_tokens: int, temperature: float
    ) -> str:
        api_key = os.getenv("CEREBRAS_API_KEY", "")
        if not api_key:
            raise _ProviderUnavailable("CEREBRAS_API_KEY no configurada")

        logger.info("[dim]LLM[/] [bold #00C5CD]Cerebras[/] [llama3.1-8b]")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    "https://api.cerebras.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "llama3.1-8b",
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
            if resp.status_code == 429:
                raise _RateLimitError(f"Cerebras 429: {resp.text[:200]}")
            if resp.status_code >= 400:
                raise _ProviderUnavailable(
                    f"Cerebras HTTP {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except (_RateLimitError, _ProviderUnavailable):
            raise
        except Exception as exc:
            raise _ProviderUnavailable(str(exc))

    # ══════════════════════════════════════════════════════════════════════════
    # Proveedor 6 — Ollama (local, sin límite)
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_ollama(
        self, prompt: str, system: str, max_tokens: int, temperature: float
    ) -> str:
        logger.info(f"[dim]LLM[/] [bold cyan]Ollama[/] [{self.fallback_model}]")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    f"{self.ollama_base_url}/api/chat",
                    json={
                        "model": self.fallback_model,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "num_predict": max_tokens,
                            "temperature": temperature,
                        },
                    },
                )
                resp.raise_for_status()
                return resp.json()["message"]["content"].strip()
        except Exception as exc:
            raise _ProviderUnavailable(str(exc))

    # ══════════════════════════════════════════════════════════════════════════
    # Tracking de uso en SQLite
    # ══════════════════════════════════════════════════════════════════════════

    def _track_usage(self, provider: str, prompt: str, response: str) -> None:
        """Registra tokens estimados en la tabla llm_usage de SQLite."""
        if self.db is None:
            return
        tokens = _estimate_tokens(prompt) + _estimate_tokens(response)
        try:
            self.db.save_llm_usage(provider, tokens)
            today_total = self.db.get_llm_usage_today(provider)
            limit = _DAILY_LIMITS.get(provider, 0)
            pct = today_total / limit * 100 if limit else 0
            logger.info(
                f"[dim]LLM uso[/] {provider}: "
                f"+{tokens} tokens hoy ({today_total:,}/{limit:,} = {pct:.1f}%)"
            )
        except Exception as exc:
            logger.warning(f"LLM tracking falló (no crítico): {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # Health check
    # ══════════════════════════════════════════════════════════════════════════

    def health_check(self) -> dict:
        """Verifica qué proveedores responden con una llamada mínima."""
        results = {}
        for provider in _PROVIDER_ORDER:
            try:
                self._call_provider(provider, "Di 'ok'", "", 10, 0.1)
                results[provider] = True
            except Exception as exc:
                results[provider] = False
                logger.warning(f"Health check {provider}: {exc}")
        return results


# ══════════════════════════════════════════════════════════════════════════════
# Excepciones internas
# ══════════════════════════════════════════════════════════════════════════════

class _RateLimitError(Exception):
    """El proveedor rechazó la petición por límite de tasa o cuota diaria."""


class _ProviderUnavailable(Exception):
    """El proveedor no está configurado o no responde."""
