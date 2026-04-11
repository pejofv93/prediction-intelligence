from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
oracle_agent.py — Orquestador de la capa ORÁCULO
Coordina: ARGOS, PYTHIA, RECON, VECTOR, THEMIS
"""
from core.context import Context
from utils.logger import get_logger

logger = get_logger("ORACLE_AGENT")

class OracleAgent:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self._load_agents()

    def _load_agents(self):
        try:
            from agents.oracle.argos import ARGOS as Argos
            self._argos = Argos(self.config, self.db)
        except Exception as e:
            logger.warning(f"ARGOS no disponible: {e}")
            self._argos = None

        try:
            from agents.oracle.pythia import PYTHIA as Pythia
            self._pythia = Pythia(self.config, self.db)
        except Exception as e:
            logger.warning(f"PYTHIA no disponible: {e}")
            self._pythia = None

        try:
            from agents.oracle.themis import THEMIS as Themis
            self._themis = Themis(self.config, self.db)
        except Exception as e:
            logger.warning(f"THEMIS no disponible: {e}")
            self._themis = None

        try:
            from agents.oracle.vector import VECTOR as Vector
            self._vector = Vector(self.config, self.db)
        except Exception as e:
            logger.warning(f"VECTOR no disponible: {e}")
            self._vector = None

        try:
            from agents.oracle.recon import RECON as Recon
            self._recon = Recon(self.config, self.db)
        except Exception as e:
            logger.warning(f"RECON no disponible: {e}")
            self._recon = None

    def run(self, ctx: Context) -> Context:
        logger.info("ORACLE_AGENT iniciado")

        if self._argos:
            try:
                ctx = self._argos.run(ctx)
                logger.info("ARGOS completado")
            except Exception as e:
                ctx.add_warning("ARGOS", str(e))
                logger.error(f"ARGOS error: {e}")

        if self._pythia:
            try:
                ctx = self._pythia.run(ctx)
                logger.info("PYTHIA completado")
            except Exception as e:
                ctx.add_warning("PYTHIA", str(e))
                logger.error(f"PYTHIA error: {e}")

        if self._vector:
            try:
                ctx = self._vector.run(ctx)
                logger.info("VECTOR completado")
            except Exception as e:
                ctx.add_warning("VECTOR", str(e))

        if self._recon:
            try:
                ctx = self._recon.run(ctx)
                logger.info("RECON completado")
            except Exception as e:
                ctx.add_warning("RECON", str(e))

        if self._themis:
            try:
                ctx = self._themis.run(ctx)
                logger.info("THEMIS completado")
            except Exception as e:
                ctx.add_warning("THEMIS", str(e))
                logger.error(f"THEMIS error: {e}")

        return ctx

