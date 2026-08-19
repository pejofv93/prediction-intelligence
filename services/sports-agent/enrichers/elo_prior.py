"""
Prior de ELO: valor inicial de un club antes de aplicarle ningun partido.

El problema que resuelve: arrancar a todos en 1500 y aplicarles 10-14 partidos convierte
al ELO en un indicador de forma reciente, no de fuerza. Medido tras la reconstruccion del
2026-08-19: Bournemouth 4o de las top-5, Chelsea 83o de 88, Liverpool 471o de 537. No es un
bug — es que 10 partidos desde un prior plano no contienen informacion de fuerza.

Hay precedente en el propio repo: `init_wc26_national_elos` siembra a las selecciones con
sus puntos FIFA en vez de 1500, y solo las pisa mientras no tengan historial real. Esto es
lo mismo para clubes, con el ranking que existe para clubes.

El prior tiene dos componentes:

  base de liga   — de que competicion viene el club. El orden es el del coeficiente de
                   pais de la UEFA, agrupado en escalones anchos a proposito: no es una
                   cifra que podamos verificar aqui, y un escalon ancho equivocado cuesta
                   mucho menos que un numero falsamente preciso. Revisable en una tabla.

  ajuste por     — donde acabo el club en su liga la temporada pasada, de las
  clasificacion    clasificaciones reales de allsportsapi2. Es lo que separa a Liverpool de
                   Burnley dentro del mismo escalon, que es justo lo que hoy no distingue.

El prior es solo el punto de partida: con K=32, 60-90 partidos de historial lo desplazan
sin dificultad. Cuantos mas partidos se siembren, menos manda el prior — son
complementarios, no alternativas.
"""
import logging
import re

logger = logging.getLogger(__name__)

DEFAULT_PRIOR = 1450.0        # club europeo sin liga reconocida
_SPREAD = 80.0                # puntos entre el 1o y el ultimo de una liga

# Escalones por pais, ordenados por coeficiente UEFA (aproximado, revisable).
# Deliberadamente anchos: agrupar mal dentro de un escalon cuesta poco, y el ajuste por
# clasificacion de abajo corrige gran parte de lo que el escalon no ve.
_COUNTRY_BASE: dict[str, float] = {
    "england": 1600.0, "spain": 1600.0, "italy": 1590.0, "germany": 1590.0,
    "france": 1550.0, "portugal": 1540.0, "netherlands": 1530.0, "belgium": 1510.0,
    "turkiye": 1500.0, "austria": 1500.0, "czechia": 1495.0, "greece": 1495.0,
    "switzerland": 1490.0, "scotland": 1490.0, "denmark": 1485.0, "norway": 1480.0,
    "ukraine": 1480.0, "poland": 1475.0, "israel": 1470.0, "croatia": 1470.0,
    "sweden": 1470.0, "serbia": 1465.0, "cyprus": 1465.0, "hungary": 1455.0,
    "romania": 1455.0, "bulgaria": 1450.0, "slovakia": 1450.0, "slovenia": 1450.0,
    "azerbaijan": 1450.0, "kazakhstan": 1445.0, "iceland": 1440.0, "finland": 1440.0,
    "ireland": 1440.0, "faroe islands": 1420.0, "gibraltar": 1410.0, "malta": 1415.0,
    "brazil": 1520.0, "argentina": 1510.0,   # sudamericanos que si tenemos en la base
}

# Codigo interno de liga → pais
LEAGUE_COUNTRY: dict[str, str] = {
    "PL": "england", "PD": "spain", "SA": "italy", "BL1": "germany", "FL1": "france",
    "BSA": "brazil", "ARG": "argentina", "TU1": "turkiye",
}

# Nombre del torneo (tal y como lo devuelve allsportsapi2) → pais. Es la unica pista de
# procedencia que tenemos para los clubes que entran por la via UEFA: su doc de team_stats
# no trae liga, pero su historial si dice en que competicion juega la mayoria del tiempo.
_TOURNAMENT_COUNTRY: list[tuple[str, str]] = [
    (r"premier league", "england"), (r"championship", "england"),
    (r"laliga|la liga", "spain"), (r"serie a", "italy"), (r"bundesliga", "germany"),
    (r"ligue 1", "france"), (r"eredivisie", "netherlands"),
    (r"primeira liga|liga portugal", "portugal"), (r"jupiler|pro league", "belgium"),
    (r"super lig", "turkiye"), (r"eliteserien", "norway"), (r"allsvenskan", "sweden"),
    (r"superliga", "denmark"), (r"super league greece", "greece"),
    (r"super league|challenge league", "switzerland"),
    (r"premiership|scottish", "scotland"), (r"ekstraklasa", "poland"),
    (r"fortuna liga|czech", "czechia"), (r"nb i|hungar", "hungary"),
    (r"hnl|croat", "croatia"), (r"superleague serbia|serbian", "serbia"),
    (r"ukrain", "ukraine"), (r"bundesliga austria|admiral", "austria"),
    (r"veikkausliiga", "finland"), (r"besta deild|urvalsdeild", "iceland"),
    (r"premier division|ireland", "ireland"), (r"betri deildin|faroe", "faroe islands"),
    (r"premier league azerbaijan|azerbaijan", "azerbaijan"),
    (r"brasileir|serie a betano", "brazil"), (r"liga profesional|argentin", "argentina"),
]


def country_for(league_code: str = "", tournament_name: str = "") -> str:
    """Pais del club, por codigo de liga interno o por el nombre de su torneo habitual."""
    if league_code and league_code in LEAGUE_COUNTRY:
        return LEAGUE_COUNTRY[league_code]
    name = (tournament_name or "").lower()
    for patron, pais in _TOURNAMENT_COUNTRY:
        if re.search(patron, name):
            return pais
    return ""


def league_base(league_code: str = "", tournament_name: str = "") -> float:
    pais = country_for(league_code, tournament_name)
    return _COUNTRY_BASE.get(pais, DEFAULT_PRIOR)


def position_adjustment(position: int, total: int) -> float:
    """
    Ajuste por puesto final la temporada pasada: +_SPREAD/2 el primero, -_SPREAD/2 el
    ultimo, lineal en medio. Con _SPREAD=80 la distancia entre campeon y colista es la
    mitad de la que separa dos escalones de pais — el prior ordena, no sentencia.
    """
    if total < 2 or position < 1:
        return 0.0
    pos = min(position, total)
    return _SPREAD * (0.5 - (pos - 1) / (total - 1))


def build_priors(
    clubs: dict[str, dict],
    standings: dict[str, list[dict]] | None = None,
) -> dict[str, float]:
    """
    {team_id canonico → ELO inicial}.

    clubs:     {team_id: {"league": codigo, "tournament": nombre, "name": str}}
    standings: {codigo_liga: [{"team_name": str, "position": int}, ...]} de la temporada
               PASADA. Si falta una liga, sus clubes se quedan solo con la base de pais.
    """
    from collectors.team_identity import normalize

    posiciones: dict[str, tuple[int, int]] = {}
    for code, filas in (standings or {}).items():
        total = len(filas)
        for fila in filas:
            clave = normalize(fila.get("team_name", ""))
            if clave and fila.get("position"):
                posiciones[clave] = (int(fila["position"]), total)

    priors: dict[str, float] = {}
    con_posicion = 0
    for team_id, info in clubs.items():
        base = league_base(info.get("league", ""), info.get("tournament", ""))
        ajuste = 0.0
        pos = posiciones.get(normalize(info.get("name", "")))
        if pos:
            ajuste = position_adjustment(*pos)
            con_posicion += 1
        priors[team_id] = round(base + ajuste, 1)

    logger.info(
        "elo_prior: %d clubes (%d con puesto de la temporada pasada); rango %.0f-%.0f",
        len(priors), con_posicion,
        min(priors.values()) if priors else 0, max(priors.values()) if priors else 0,
    )
    return priors
