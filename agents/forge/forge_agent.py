from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
forge_agent.py — Orquestador de la capa FORGE
Coordina: CALÍOPE, HERMES, ECHO, HEPHAESTUS, IRIS, DAEDALUS
"""
from core.context import Context
from utils.logger import get_logger

logger = get_logger("FORGE_AGENT")

class ForgeAgent:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self._load_agents()

    def _load_agents(self):
        try:
            from agents.forge.caliope import CALIOPE as Caliope
            self._caliope = Caliope(self.config, self.db)
        except Exception as e:
            logger.warning(f"CALIOPE no disponible: {e}")
            self._caliope = None

        try:
            from agents.forge.hermes import HERMES as Hermes
            self._hermes = Hermes(self.config, self.db)
        except Exception as e:
            logger.warning(f"HERMES no disponible: {e}")
            self._hermes = None

        try:
            from agents.forge.echo import ECHO as Echo
            self._echo = Echo(self.config, self.db)
        except Exception as e:
            logger.warning(f"ECHO no disponible: {e}")
            self._echo = None

        try:
            from agents.forge.daedalus import DAEDALUS as Daedalus
            self._daedalus = Daedalus(self.config, self.db)
        except Exception as e:
            logger.warning(f"DAEDALUS no disponible: {e}")
            self._daedalus = None

        try:
            from agents.forge.helios import HELIOS as Helios
            self._helios = Helios(self.config, self.db)
        except Exception as e:
            logger.warning(f"HELIOS no disponible: {e}")
            self._helios = None

        try:
            from agents.forge.prometheus import PROMETHEUS as Prometheus
            self._prometheus = Prometheus(self.config, self.db)
        except Exception as e:
            logger.warning(f"PROMETHEUS no disponible: {e}")
            self._prometheus = None

        try:
            from agents.forge.hephaestus import HEPHAESTUS as Hephaestus
            self._hephaestus = Hephaestus(self.config, self.db)
        except Exception as e:
            logger.warning(f"HEPHAESTUS no disponible: {e}")
            self._hephaestus = None

        try:
            from agents.forge.iris import IRIS as Iris
            self._iris = Iris(self.config, self.db)
        except Exception as e:
            logger.warning(f"IRIS no disponible: {e}")
            self._iris = None

    def run(self, ctx: Context) -> Context:
        logger.info("FORGE_AGENT iniciado")

        if self._caliope:
            try:
                ctx = self._caliope.run(ctx)
                logger.info("CALIOPE completado")
            except Exception as e:
                ctx.add_error("CALIOPE", str(e))
                logger.error(f"CALIOPE error: {e}")
                return ctx  # Sin guión no tiene sentido continuar

        if self._hermes:
            try:
                ctx = self._hermes.run(ctx)
                logger.info("HERMES completado")
            except Exception as e:
                ctx.add_warning("HERMES", str(e))

        if self._echo:
            try:
                ctx = self._echo.run(ctx)
                logger.info("ECHO completado")
            except Exception as e:
                ctx.add_warning("ECHO", str(e))
                logger.error(f"ECHO error: {e}")

        if self._daedalus:
            try:
                ctx = self._daedalus.run(ctx)
                logger.info("DAEDALUS completado")
            except Exception as e:
                ctx.add_warning("DAEDALUS", str(e))

        # HELIOS y PROMETHEUS desactivados temporalmente — modo sin avatar
        # ctx.avatar_path queda vacío → HEPHAESTUS usa layout FULLSCREEN

        if self._hephaestus:
            try:
                ctx = self._hephaestus.run(ctx)
                logger.info("HEPHAESTUS completado")
            except Exception as e:
                ctx.add_warning("HEPHAESTUS", str(e))
                logger.error(f"HEPHAESTUS error: {e}")

        if self._iris:
            try:
                ctx = self._iris.run(ctx)
                logger.info("IRIS completado")
            except Exception as e:
                ctx.add_warning("IRIS", str(e))

        return ctx

