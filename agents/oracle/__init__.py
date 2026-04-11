from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/oracle/__init__.py
Capa ORÁCULO — NEXUS v1.0 · CryptoVerdad

Agentes disponibles:
  ARGOS   — Vigilante de precios (CoinGecko)
  PYTHIA  — Oráculo de noticias (RSS)
  RECON   — Espía de competidores (YouTube)
  VECTOR  — Análisis de tendencias (Google Trends)
  THEMIS  — Juez estratégico (LLM)
"""

from agents.oracle.argos  import ARGOS
from agents.oracle.pythia import PYTHIA
from agents.oracle.recon  import RECON
from agents.oracle.vector import VECTOR
from agents.oracle.themis import THEMIS

__all__ = ["ARGOS", "PYTHIA", "RECON", "VECTOR", "THEMIS"]

