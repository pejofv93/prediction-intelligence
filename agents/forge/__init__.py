from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/forge/__init__.py
Capa FORGE de NEXUS — Produccion de contenido.

Agentes:
    CALIOPE    — Guionista Maestra (7 modos)
    HERMES     — Motor SEO completo
    ECHO       — Sintetizador de voz (edge-tts)
    HEPHAESTUS — Compositor de video (NEXUS Lite, sin SadTalker)
    IRIS       — Disenadora de thumbnails A/B
    DAEDALUS   — Generador de graficos de precio
"""

from agents.forge.caliope import CALIOPE
from agents.forge.hermes import HERMES
from agents.forge.echo import ECHO
from agents.forge.hephaestus import HEPHAESTUS
from agents.forge.iris import IRIS
from agents.forge.daedalus import DAEDALUS

__all__ = [
    "CALIOPE",
    "HERMES",
    "ECHO",
    "HEPHAESTUS",
    "IRIS",
    "DAEDALUS",
]

