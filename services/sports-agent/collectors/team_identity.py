"""
Identidad de equipo entre fuentes: football-data.org, Sofascore/allsportsapi2, odds-api.io.

El problema: `team_stats/{id}` y `team_elo/{id}` se indexan por un único team_id, pero cada
fuente numera los clubes a su manera. Si el Barça entra por football-data como 81 y por
allsportsapi2 como 2817, acaba con DOS documentos y DOS ELO distintos — el rodado de LaLiga
y otro que empieza de cero cada vez que juega en Europa.

Este módulo resuelve el nombre del club contra los equipos que ya existen en `team_stats`
y devuelve el id canónico. Si el club no existe (Qarabağ, KI Klaksvík...), acuña un id
propio con prefijo de fuente, `sf_2817`, en vez de arriesgar una colisión con un id de
football-data (ambos son enteros bajos y solapan).

Emparejamiento DELIBERADAMENTE estricto — exacto o mismo conjunto de palabras, nunca
subcadena. Fusionar dos clubes distintos corrompe el ELO de los dos; duplicar uno solo
cuesta un doc extra y se corrige en la siguiente pasada.
"""
import logging
import unicodedata

logger = logging.getLogger(__name__)

# Palabras que no distinguen un club de otro. Incluye sufijos societarios de varios
# idiomas (fk/sk/if/bk/kv...) porque cada fuente los pone o los quita a su antojo:
# football-data trae "Sabah FK" y "Fenerbahçe SK", allsportsapi2 trae "Sabah" y
# "Fenerbahçe" a secas — sin esto son clubes distintos para resolve() y acaban con
# dos team_id, dos ELO y dos docs en upcoming_matches (el bug del doble Como/Leipzig).
_GENERIC_WORDS = {
    "fc", "cf", "ac", "as", "sc", "sv", "ss", "ssc", "us", "afc", "rc", "rcd", "cd", "ud",
    "fk", "sk", "if", "bk", "kv", "nk", "hnk", "gnk", "ik", "aik", "sad", "kf", "cs", "ks",
    "club", "de", "the", "team", "calcio", "futbol", "football", "atletico", "athletic",
}


# Alias por id de Sofascore/allsportsapi2 → team_id de football-data, para clubes cuyo nombre
# no casa con el emparejamiento estricto ("Fiorentina" vs "ACF Fiorentina") o, peor, casa con
# otro club ("FC Barcelona" → Barcelona SC de Ecuador, 2026-10-11). Se consulta antes que el
# nombre. Cada id está verificado contra la API (2026-10-11/12), no solo por nombre: los de las
# 8 ligas, en la clasificación 26/27 de su liga; los UEFA, por /api/team/{id} (país y liga).
SOFASCORE_ALIASES: dict[str, str] = {
    # LaLiga (España)
    "2817": "81",     # FC Barcelona
    "2816": "90",     # Real Betis
    "2832": "560",    # Deportivo de A Coruña
    "2825": "77",     # Athletic Club
    "2814": "80",     # Espanyol
    "2818": "87",     # Rayo Vallecano
    "2820": "79",     # Osasuna
    "2835": "5335",   # Real Racing Club
    # Liga Portugal Betclic (Portugal)
    "3001": "498",    # Sporting CP
    "3006": "1903",   # Benfica
    "2999": "5613",   # Sporting Braga
    "3035": "9136",   # CF Estrela Amadora
    "4500": "582",    # Estoril Praia
    # Serie A (Italia)
    "2697": "108",    # Inter
    "2686": "102",    # Atalanta
    "2713": "107",    # Genoa
    "2693": "99",     # Fiorentina
    # Ligue 1 (Francia)
    "1646": "519",    # Auxerre
    "1643": "521",    # Lille
    "1661": "522",    # Nice
    "1658": "529",    # Stade Rennais
    "1652": "531",    # Troyes
    "1684": "532",    # Angers
    "1648": "546",    # RC Lens
    "1659": "576",    # RC Strasbourg
    # Eredivisie (Países Bajos)
    "2970": "1909",   # SC Cambuur
    "2962": "1915",   # NEC Nijmegen
    "2967": "670",    # Excelsior
    "2952": "674",    # PSV Eindhoven
    "2959": "675",    # Feyenoord
    "2950": "682",    # AZ Alkmaar
    # Bundesliga (Alemania) y Championship (Inglaterra)
    "2527": "18",     # Borussia M'gladbach
    "3": "76",        # Wolverhampton
    # Clubes UEFA de otras ligas: verificados por /api/team/{id} (país + liga principal)
    "3250": "1899",   # AEK Athens (Grecia)
    "2058": "2016",   # LASK (Austria)
    "1164": "5720",   # Viking FK (Noruega)
    "656": "5721",    # Bodø/Glimt (Noruega)
    "3061": "610",    # Galatasaray (Turquía)
    "2404": "7509",   # ŠK Slovan Bratislava (Eslovaquia)
    "2216": "930",    # SK Slavia Praha (Chequia)
}


def normalize(name: str) -> str:
    """
    Minúsculas, sin acentos, sin puntuación, sin palabras genéricas y SIN tokens
    puramente numéricos (años de fundación: "Como 1907" → "como", "Schalke 04" →
    "schalke", "Bayer 04 Leverkusen" → "bayer leverkusen"). Cada fuente decide por su
    cuenta si incluye el año, así que dejarlo dentro parte la identidad del club.
    """
    n = unicodedata.normalize("NFD", (name or "").lower().strip())
    n = n.encode("ascii", "ignore").decode()
    n = "".join(c if c.isalnum() else " " for c in n)
    return " ".join(
        w for w in n.split()
        if w and not w.isdigit() and w not in _GENERIC_WORDS
    )


def _tokens(name: str) -> frozenset:
    return frozenset(normalize(name).split())


def build_identity_map(team_stats_docs: list[dict]) -> dict[str, str]:
    """
    {nombre_normalizado → team_id canónico} a partir de los docs de team_stats.

    team_stats_docs: lista de dicts con al menos team_name y team_id. Se pasa desde fuera
    (en vez de leer Firestore aquí) para que los scripts one-shot puedan usar su propio
    transporte sin duplicar la lógica de emparejamiento.
    """
    out: dict[str, str] = {}
    for d in team_stats_docs:
        tid = d.get("team_id")
        name = d.get("team_name") or ""
        if tid is None or not name or name.startswith("Team_"):
            continue
        key = normalize(name)
        if not key:
            continue
        if key in out and out[key] != str(tid):
            logger.warning(
                "team_identity: nombre ambiguo '%s' → ids %s y %s; conservando %s",
                name, out[key], tid, out[key],
            )
            continue
        out[key] = str(tid)
    logger.info("team_identity: mapa con %d nombres de %d docs", len(out), len(team_stats_docs))
    return out


def resolve(team_name: str, source_id: int | str, identity_map: dict[str, str],
            prefix: str = "sf") -> str:
    """
    Id canónico del club. Devuelve el team_id ya existente si el nombre coincide;
    si no, acuña f"{prefix}_{source_id}". Los ids de SOFASCORE_ALIASES ganan al nombre.
    """
    if prefix == "sf" and str(source_id) in SOFASCORE_ALIASES:
        return SOFASCORE_ALIASES[str(source_id)]
    key = normalize(team_name)
    if key:
        hit = identity_map.get(key)
        if hit:
            return hit
        toks = frozenset(key.split())
        if len(toks) > 1:
            for known, tid in identity_map.items():
                if frozenset(known.split()) == toks:
                    return tid
    return f"{prefix}_{source_id}"


def physical_fixture_key(match_date, home_team: str, away_team: str) -> str:
    """
    Huella de un partido a partir de FECHA + NOMBRES (no ids), independiente de la
    fuente. Se usa para deduplicar señales entre docs distintos del mismo partido
    (`CL_SF_16939034` y `575339` para Como–RB Leipzig): match_fingerprint() no sirve
    ahí porque cada fuente resuelve el club a un id distinto.

    Los nombres pasan por normalize() (sin año, sin sufijo societario) y se ordenan,
    así que "Como 1907 vs RB Leipzig" y "RB Leipzig vs Como" dan la misma clave.
    Devuelve "" si falta algún dato — el llamador debe tratar "" como "sin huella".
    """
    day = str(match_date or "")[:10].replace("/", "-")
    a, b = normalize(home_team), normalize(away_team)
    if not day or not a or not b:
        return ""
    lo, hi = sorted((a, b))
    return f"{day}|{lo}|{hi}"


def physical_fixture_id_key(match_date, home_id, away_id) -> str:
    """
    Huella de un partido por FECHA + IDS CANÓNICOS de los dos equipos (ordenados), para
    cuando el mismo partido llega con dos match_id y nombres distintos por fuente
    (Roma–Real Madrid: CL_SF_16938872 "AS Roma" y 575352 "AS Roma"/"Real Madrid CF").
    Devuelve "" si falta la fecha o algún id.
    """
    day = str(match_date or "")[:10].replace("/", "-")
    if not day or home_id in (None, "") or away_id in (None, ""):
        return ""
    lo, hi = sorted((str(home_id), str(away_id)))
    return f"{day}|id|{lo}|{hi}"


def match_fingerprint(date: str, home_id, away_id) -> str:
    """
    Clave única de un partido, independiente de la fuente que lo trajo.

    Es lo que permite que el mismo Barça-Madrid no se aplique dos veces al ELO por llegar
    con el id 12345 de football-data y con CL_SF_67890 de allsportsapi2. Se usa como doc ID
    en `elo_applied`, así que no puede llevar '/'.
    """
    day = str(date or "")[:10].replace("/", "-")
    return f"{day}_{home_id}_{away_id}".replace("/", "_")
