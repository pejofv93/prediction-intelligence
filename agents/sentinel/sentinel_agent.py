"""
sentinel_agent.py — Orquestador de la capa SENTINEL
Coordina: AGORA, SCROLL, CROESUS, ARGONAUT

SENTINEL se ejecuta DESPUÉS del pipeline principal (post-publish) y en modo
mantenimiento periódico. No bloquea el pipeline si falla.
"""
from core.context import Context
from utils.logger import get_logger

logger = get_logger("SENTINEL_AGENT")


class SentinelAgent:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self._load_agents()

    def _load_agents(self):
        try:
            from agents.sentinel.agora import AGORA
            self._agora = AGORA(self.config, self.db)
        except Exception as e:
            logger.warning(f"AGORA no disponible: {e}")
            self._agora = None

        try:
            from agents.sentinel.scroll import SCROLL
            self._scroll = SCROLL(self.config, self.db)
        except Exception as e:
            logger.warning(f"SCROLL no disponible: {e}")
            self._scroll = None

        try:
            from agents.sentinel.croesus import CROESUS
            self._croesus = CROESUS(self.config, self.db)
        except Exception as e:
            logger.warning(f"CROESUS no disponible: {e}")
            self._croesus = None

        try:
            from agents.sentinel.argonaut import ARGONAUT
            self._argonaut = ARGONAUT(self.config, self.db)
        except Exception as e:
            logger.warning(f"ARGONAUT no disponible: {e}")
            self._argonaut = None

    def run(self, ctx: Context) -> Context:
        """
        Ejecución post-pipeline: monitoreo, comentarios, costes, auditoría.
        Nunca lanza excepciones que bloqueen el pipeline principal.
        """
        logger.info("SENTINEL_AGENT iniciado")

        # 1. CROESUS — costes y límites API (siempre primero)
        if self._croesus:
            try:
                ctx = self._croesus.run(ctx)
            except Exception as e:
                logger.error(f"CROESUS error: {e}")

        # 2. AGORA — responder comentarios YouTube
        if self._agora:
            try:
                ctx = self._agora.run(ctx)
            except Exception as e:
                logger.error(f"AGORA error: {e}")

        # 3. SCROLL — newsletter semanal (solo lunes)
        if self._scroll:
            try:
                ctx = self._scroll.run(ctx)
            except Exception as e:
                logger.error(f"SCROLL error: {e}")

        # 4. ARGONAUT — auditoría y limpieza (último, no urgente)
        if self._argonaut:
            try:
                ctx = self._argonaut.run(ctx)
            except Exception as e:
                logger.error(f"ARGONAUT error: {e}")

        logger.info("SENTINEL_AGENT completado")
        return ctx

    def run_maintenance(self, ctx: Context) -> Context:
        """
        Modo mantenimiento standalone (sin pipeline de contenido):
        solo auditoría, costes y newsletter. Para KAIROS periódico.
        """
        logger.info("SENTINEL_AGENT modo mantenimiento")

        for agent, name in [
            (self._croesus,  "CROESUS"),
            (self._scroll,   "SCROLL"),
            (self._argonaut, "ARGONAUT"),
        ]:
            if agent:
                try:
                    ctx = agent.run(ctx)
                    logger.info(f"{name} mantenimiento OK")
                except Exception as e:
                    logger.error(f"{name} mantenimiento error: {e}")

        return ctx
