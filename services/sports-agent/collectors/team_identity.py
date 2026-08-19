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

# Palabras que no distinguen un club de otro
_GENERIC_WORDS = {
    "fc", "cf", "ac", "as", "sc", "sv", "ss", "ssc", "us", "afc", "rc", "rcd", "cd", "ud",
    "club", "de", "the", "team", "calcio", "futbol", "football", "atletico", "athletic",
}


def normalize(name: str) -> str:
    """Minúsculas, sin acentos, sin puntuación ni palabras genéricas."""
    n = unicodedata.normalize("NFD", (name or "").lower().strip())
    n = n.encode("ascii", "ignore").decode()
    n = "".join(c if c.isalnum() else " " for c in n)
    return " ".join(w for w in n.split() if w and w not in _GENERIC_WORDS)


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
    si no, acuña f"{prefix}_{source_id}".
    """
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


def match_fingerprint(date: str, home_id, away_id) -> str:
    """
    Clave única de un partido, independiente de la fuente que lo trajo.

    Es lo que permite que el mismo Barça-Madrid no se aplique dos veces al ELO por llegar
    con el id 12345 de football-data y con CL_SF_67890 de allsportsapi2. Se usa como doc ID
    en `elo_applied`, así que no puede llevar '/'.
    """
    day = str(date or "")[:10].replace("/", "-")
    return f"{day}_{home_id}_{away_id}".replace("/", "_")
