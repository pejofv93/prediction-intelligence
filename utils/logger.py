# -*- coding: utf-8 -*-
"""
logger.py
Logger centralizado de NEXUS basado en rich.
"""

import sys
import logging
from rich.console import Console
from rich.logging import RichHandler

# En Windows con cp1252, forzar stderr a UTF-8 para que ñ/tildes
# se muestren correctamente en el log de consola.
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_console = Console(stderr=True)

# Evitamos registrar handlers duplicados
_registered: set[str] = set()


def get_logger(name: str) -> logging.Logger:
    """
    Devuelve un logger con RichHandler.
    Llamadas múltiples con el mismo nombre devuelven el mismo logger
    sin duplicar handlers.
    """
    logger = logging.getLogger(name)

    if name not in _registered:
        logger.setLevel(logging.DEBUG)
        handler = RichHandler(
            console=_console,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            show_time=True,
            show_level=True,
            show_path=False,
            markup=True,
        )
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        _registered.add(name)

    return logger
