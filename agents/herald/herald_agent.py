from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
herald_agent.py — Orquestador de la capa HERALD
Coordina: OLYMPUS, RAPID, MERCURY
"""
from core.context import Context
from utils.logger import get_logger

logger = get_logger("HERALD_AGENT")

class HeraldAgent:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self._load_agents()

    def _load_agents(self):
        try:
            from agents.herald.olympus import OLYMPUS as Olympus
            self._olympus = Olympus(self.config, self.db)
        except Exception as e:
            logger.warning(f"OLYMPUS no disponible: {e}")
            self._olympus = None

        try:
            from agents.herald.rapid import RAPID as Rapid
            self._rapid = Rapid(self.config, self.db)
        except Exception as e:
            logger.warning(f"RAPID no disponible: {e}")
            self._rapid = None

        try:
            from agents.herald.mercury import MERCURY as Mercury
            self._mercury = Mercury(self.config, self.db)
        except Exception as e:
            logger.warning(f"MERCURY no disponible: {e}")
            self._mercury = None

        try:
            from agents.herald.aurora import AURORA as Aurora
            self._aurora = Aurora(self.config, self.db)
        except Exception as e:
            logger.warning(f"AURORA no disponible: {e}")
            self._aurora = None

        try:
            from agents.herald.proteus import PROTEUS as Proteus
            self._proteus = Proteus(self.config, self.db)
        except Exception as e:
            logger.warning(f"PROTEUS no disponible: {e}")
            self._proteus = None

    def run(self, ctx: Context) -> Context:
        logger.info("HERALD_AGENT iniciado")

        if self._olympus:
            try:
                ctx = self._olympus.run(ctx)
                logger.info("OLYMPUS completado")
            except Exception as e:
                ctx.add_warning("OLYMPUS", str(e))
                logger.error(f"OLYMPUS error: {e}")

        # PROTEUS después de OLYMPUS para que youtube_url esté disponible en ctx
        if self._proteus:
            try:
                ctx = self._proteus.run(ctx)
                logger.info("PROTEUS completado")
            except Exception as e:
                ctx.add_warning("PROTEUS", str(e))
                logger.error(f"PROTEUS error: {e}")

        if self._rapid:
            try:
                ctx = self._rapid.run(ctx)
                logger.info("RAPID completado")
            except Exception as e:
                ctx.add_warning("RAPID", str(e))
                logger.error(f"RAPID error: {e}")

        if self._mercury:
            try:
                ctx = self._mercury.run(ctx)
                logger.info("MERCURY completado")
            except Exception as e:
                ctx.add_warning("MERCURY", str(e))

        if self._aurora:
            try:
                ctx = self._aurora.run(ctx)
                logger.info("AURORA completado")
            except Exception as e:
                ctx.add_warning("AURORA", str(e))

        return ctx

    def run_urgent(self, ctx: Context) -> Context:
        """Modo urgente: TikTok primero, luego YouTube."""
        logger.info("HERALD_AGENT modo urgente")

        if self._rapid:
            try:
                ctx = self._rapid.run(ctx)
                logger.info("RAPID (urgente) completado")
            except Exception as e:
                ctx.add_warning("RAPID", str(e))

        if self._olympus:
            try:
                ctx = self._olympus.run(ctx)
                logger.info("OLYMPUS (urgente) completado")
            except Exception as e:
                ctx.add_warning("OLYMPUS", str(e))

        if self._proteus:
            try:
                ctx = self._proteus.run(ctx)
                logger.info("PROTEUS (urgente) completado")
            except Exception as e:
                ctx.add_warning("PROTEUS", str(e))

        if self._mercury:
            try:
                ctx = self._mercury.run(ctx)
            except Exception as e:
                ctx.add_warning("MERCURY", str(e))

        if self._aurora:
            try:
                ctx = self._aurora.run(ctx)
                logger.info("AURORA (urgente) completado")
            except Exception as e:
                ctx.add_warning("AURORA", str(e))

        return ctx

