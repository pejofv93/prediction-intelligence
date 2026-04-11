from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
mind_agent.py — Orquestador de la capa MIND
Coordina: MNEME, KAIROS, ALETHEIA
"""
from core.context import Context
from utils.logger import get_logger

logger = get_logger("MIND_AGENT")

class MindAgent:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self._load_agents()

    def _load_agents(self):
        try:
            from agents.mind.mneme import MNEME as Mneme
            self._mneme = Mneme(self.config, self.db)
        except Exception as e:
            logger.warning(f"MNEME no disponible: {e}")
            self._mneme = None

        try:
            from agents.mind.kairos import KAIROS as Kairos
            self._kairos = Kairos(self.config, self.db)
        except Exception as e:
            logger.warning(f"KAIROS no disponible: {e}")
            self._kairos = None

        try:
            from agents.mind.aletheia import ALETHEIA as Aletheia
            self._aletheia = Aletheia(self.config, self.db)
        except Exception as e:
            logger.warning(f"ALETHEIA no disponible: {e}")
            self._aletheia = None

    def run(self, ctx: Context) -> Context:
        logger.info("MIND_AGENT iniciado")

        if self._mneme:
            try:
                ctx = self._mneme.run(ctx)
                logger.info("MNEME completado")
            except Exception as e:
                ctx.add_warning("MNEME", str(e))

        if self._kairos:
            try:
                ctx = self._kairos.run(ctx)
                logger.info("KAIROS completado")
            except Exception as e:
                ctx.add_warning("KAIROS", str(e))

        if self._aletheia:
            try:
                ctx = self._aletheia.run(ctx)
                logger.info("ALETHEIA completado")
            except Exception as e:
                ctx.add_warning("ALETHEIA", str(e))

        return ctx

