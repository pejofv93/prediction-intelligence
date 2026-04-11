import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / '.env')
"""
utils/llm_client.py
Abstracción sobre Groq y Ollama.
Usa Groq por defecto; si falla, intenta Ollama automáticamente.

Uso:
    client = LLMClient(config)
    respuesta = client.generate("Explica Bitcoin en 3 frases.")
"""

import os
import httpx
from utils.logger import get_logger

console_logger = get_logger("LLM_CLIENT")
logger = get_logger("LLM_CLIENT")


class LLMClient:
    """
    Abstracción sobre Groq y Ollama.
    Usa Groq por defecto; si falla, intenta Ollama automáticamente.

    Uso:
        client = LLMClient(config)
        respuesta = client.generate("Explica Bitcoin en 3 frases.")
    """

    def __init__(self, config: dict):
        self.config = config
        self.primary = config.get("llm", {}).get("primary", "groq")
        self.model = config.get("llm", {}).get("model", "llama-3.3-70b-versatile")
        self.fallback = config.get("llm", {}).get("fallback", "ollama")
        self.fallback_model = config.get("llm", {}).get("fallback_model", "llama3.2")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self._groq_client = None

    def _get_groq_client(self):
        if self._groq_client is None:
            try:
                from groq import Groq
                api_key = os.getenv("GROQ_API_KEY")
                if not api_key:
                    raise ValueError("GROQ_API_KEY no encontrada en el entorno.")
                self._groq_client = Groq(api_key=api_key)
            except ImportError:
                raise ImportError("La librería 'groq' no está instalada. Ejecuta: pip install groq")
        return self._groq_client

    def _generate_groq(self, prompt: str, system: str, max_tokens: int, temperature: float = 0.7) -> str:
        client = self._get_groq_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        logger.info(f"[dim]LLM[/] [bold #F7931A]Groq[/] [{self.model}]")
        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()

    def _generate_ollama(self, prompt: str, system: str, max_tokens: int, temperature: float = 0.7) -> str:
        url = f"{self.ollama_base_url}/api/chat"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        logger.info(f"[dim]LLM[/] [bold cyan]Ollama[/] [{self.fallback_model}]")
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                url,
                json={
                    "model": self.fallback_model,
                    "messages": messages,
                    "stream": False,
                    "options": {"num_predict": max_tokens, "temperature": temperature},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"].strip()

    def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        """
        Genera texto usando Groq. Si falla, usa Ollama como fallback.

        Args:
            prompt:      Mensaje del usuario.
            system:      System prompt opcional.
            max_tokens:  Límite de tokens en la respuesta.
            temperature: Temperatura de muestreo (0.0-2.0). Default 0.7.

        Returns:
            str con la respuesta del LLM.
        """
        # Intento primario (Groq)
        groq_error_msg = "Groq no intentado"
        try:
            return self._generate_groq(prompt, system, max_tokens, temperature)
        except Exception as exc:
            groq_error_msg = str(exc)
            logger.warning(f"Groq falló ({groq_error_msg}), intentando Ollama...")

        # Fallback (Ollama)
        try:
            return self._generate_ollama(prompt, system, max_tokens, temperature)
        except Exception as ollama_exc:
            ollama_error_msg = str(ollama_exc)
            logger.error(f"Ollama también falló: {ollama_error_msg}")
            raise RuntimeError(
                f"Ambos backends LLM fallaron.\n"
                f"  Groq:   {groq_error_msg}\n"
                f"  Ollama: {ollama_error_msg}"
            )

    def health_check(self) -> dict:
        """Verifica disponibilidad de Groq y Ollama."""
        results = {"groq": False, "ollama": False}
        try:
            self._generate_groq("Di 'ok'", "", 10)
            results["groq"] = True
        except Exception as e:
            logger.warning(f"Groq health check falló: {e}")

        try:
            self._generate_ollama("Di 'ok'", "", 10)
            results["ollama"] = True
        except Exception as e:
            logger.warning(f"Ollama health check falló: {e}")

        return results

