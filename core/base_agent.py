"""
base_agent.py
Clase base para todos los agentes de NEXUS.
Cada agente DEBE implementar run(ctx: Context) -> Context.
"""

from abc import ABC, abstractmethod
from core.context import Context
from utils.logger import get_logger


class BaseAgent(ABC):
    """
    Contrato que deben cumplir todos los agentes de NEXUS.

    Reglas:
    - run() recibe y devuelve un Context (comunicación exclusiva entre agentes)
    - run() NUNCA hace crash silencioso: todo error va a ctx.errors
    - El output de terminal usa rich (via logger)
    """

    def __init__(self, config: dict):
        self.config = config
        self.logger = get_logger(self.__class__.__name__.upper())

    @abstractmethod
    def run(self, ctx: Context) -> Context:
        """
        Ejecuta la lógica del agente.
        Siempre retorna el Context, nunca lanza excepciones al llamador.
        """
        ...

    def _safe_run(self, ctx: Context) -> Context:
        """
        Wrapper de seguridad: envuelve run() en try/except.
        Usar si se llama desde otro agente o desde NexusCore como capa extra.
        """
        try:
            return self.run(ctx)
        except Exception as exc:
            ctx.add_error(self.__class__.__name__, str(exc))
            self.logger.exception(f"Error no capturado en {self.__class__.__name__}.run()")
            return ctx
