"""
Handlers de comandos Telegram.
/start /sports /poly /stats /calc /help
"""
import logging

logger = logging.getLogger(__name__)


async def handle_start(update: dict) -> None:
    """Mensaje de bienvenida + lista de comandos disponibles."""
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def handle_sports(update: dict) -> None:
    """
    Lee predictions Firestore: edge > 0.08, result==None, top 3 por edge DESC.
    """
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def handle_poly(update: dict) -> None:
    """
    Lee poly_predictions Firestore: alerted==True o edge>0.12, top 5 por edge DESC.
    """
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def handle_stats(update: dict) -> None:
    """Lee accuracy_log (semana actual) + model_weights doc 'current'."""
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def handle_calc(update: dict, args: list[str]) -> None:
    """
    Uso: /calc <stake> <back_odds> <lay_odds> <comision%>
    Calcula qualifying bet.
    Responde con lay_stake, liability, profit_back, profit_lay, rating.
    """
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def handle_help(update: dict) -> None:
    """Lista todos los comandos con descripcion breve."""
    # TODO: implementar en Sesion 6
    raise NotImplementedError


async def dispatch_update(update: dict) -> None:
    """Despacha el update al handler correcto segun el comando."""
    # TODO: implementar en Sesion 6
    raise NotImplementedError
