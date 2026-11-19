"""
shared/es_bookmakers.py — lista ÚNICA de casas con licencia en España (DGOJ, apuestas
deportivas) y del único exchange permitido (betfair.es).

Decisión del usuario (19-nov-2026) para el matched betting con dinero real: solo casas
con licencia española (.es) y el exchange de betfair.es. Nada de casas sin licencia en
España ni del exchange .com (The Odds API `betfair_ex_eu`).

Fuentes: catálogo de odds-api.io (GET /v3/bookmakers, 527 casas, descargado 19-nov) y
páginas de operadores de la DGOJ (dominios .es). Las casas "sin API" no tienen cuotas en
el plan gratuito de odds-api.io (o no están en su catálogo): se apuntan para el registro
de bonos, no para leer cuotas.

Reglas de identificación (is_es_licensed):
  - Solo cuentan identificadores EXPLÍCITAMENTE españoles: el slug de esta lista, el
    nombre, el dominio .es y los ids por API ("Bwin ES" en odds-api.io…).
  - Una marca genérica NO cuenta: "bwin", "bet365", "williamhill", "sport888",
    "pinnacle"… son las cuotas del sitio .com/.co.uk/.eu, no las del .es. Para esas
    marcas es_counterpart() dice qué versión .es existe (la cuota allí puede ser otra).
  - Las casas que solo operan en España (Sportium, Kirolbet, Jokerbet…) cuentan también
    por su nombre comercial.
Módulo puro: sin red ni Firestore.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Comisión del exchange de betfair.es mientras el usuario no confirme la suya real.
BETFAIR_ES_DEFAULT_COMMISSION = 0.05

EXCHANGE_SLUG = "betfair_es_exchange"


@dataclass(frozen=True)
class EsBookmaker:
    slug: str                      # id interno estable (docs de Firestore, calculadora)
    name: str                      # nombre de pantalla
    domain: str                    # dominio con licencia DGOJ
    kind: str = "sportsbook"       # "sportsbook" | "exchange"
    api_ids: dict = field(default_factory=dict)   # {"odds_api_io": "Bwin ES"}
    api_paid_only: bool = False    # está en la API pero no en el plan gratuito
    aliases: tuple = ()            # otros nombres que identifican SIN ambigüedad la .es
    verify: bool = False           # dato pendiente de verificar por el usuario
    note: str = ""

    @property
    def has_api(self) -> bool:
        """True si hay cuotas por API en el plan GRATUITO (lo único que se usa)."""
        return bool(self.api_ids) and not self.api_paid_only

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "name": self.name, "domain": self.domain,
            "kind": self.kind, "api_ids": dict(self.api_ids),
            "has_api": self.has_api, "api_paid_only": self.api_paid_only,
            "verify": self.verify, "note": self.note,
        }


def _oaio(name: str) -> dict:
    return {"odds_api_io": name}


# ── Casas con cuotas en odds-api.io (nombre ".. ES" del catálogo) ──────────────────
_WITH_API = (
    EsBookmaker("bet365_es", "Bet365 ES", "bet365.es", api_ids=_oaio("Bet365 ES"),
                api_paid_only=True, note="En odds-api.io solo en planes de pago"),
    EsBookmaker("bwin_es", "Bwin ES", "bwin.es", api_ids=_oaio("Bwin ES")),
    EsBookmaker("codere_es", "Codere ES", "codere.es", api_ids=_oaio("Codere ES"),
                aliases=("codereapuestas.es",)),
    EsBookmaker("betfair_es", "Betfair ES (casa)", "betfair.es", api_ids=_oaio("Betfair ES"),
                note="Casa de apuestas (sportsbook) de betfair.es, no el exchange"),
    EsBookmaker("sport888_es", "888Sport ES", "888sport.es", api_ids=_oaio("888Sport ES")),
    EsBookmaker("betway_es", "Betway ES", "betway.es", api_ids=_oaio("Betway ES")),
    EsBookmaker("winamax_es", "Winamax ES", "winamax.es", api_ids=_oaio("Winamax ES")),
    EsBookmaker("leovegas_es", "LeoVegas ES", "leovegas.es", api_ids=_oaio("LeoVegas ES")),
    EsBookmaker("pokerstars_es", "PokerStars ES", "pokerstars.es",
                api_ids=_oaio("PokerStars ES"), aliases=("pokerstarssports.es",)),
    EsBookmaker("paf_es", "PAF ES", "paf.es", api_ids=_oaio("PAF ES")),
    EsBookmaker("casumo_es", "Casumo ES", "casumo.es", api_ids=_oaio("Casumo ES")),
    EsBookmaker("retabet_es", "Retabet ES", "retabet.es", api_ids=_oaio("Retabet ES")),
    EsBookmaker("luckia_es", "Luckia ES", "luckia.es", api_ids=_oaio("Luckia ES")),
    EsBookmaker("daznbet_es", "DAZN Bet ES", "daznbet.es", api_ids=_oaio("DAZN Bet ES")),
)

# ── Casas con licencia DGOJ sin cuotas por API (solo registro de bonos) ────────────
# Las marcas que solo operan en España cuentan por su nombre (aliases).
_WITHOUT_API = (
    EsBookmaker("sportium", "Sportium", "sportium.es", aliases=("sportium",)),
    EsBookmaker("kirolbet", "Kirolbet", "kirolbet.es", aliases=("kirolbet",)),
    EsBookmaker("juegging", "Juegging", "juegging.es", aliases=("juegging",)),
    EsBookmaker("olybet_es", "Olybet ES", "olybet.es"),
    EsBookmaker("versus", "Versus", "versus.es", aliases=("versus.es",)),
    EsBookmaker("admiralbet_es", "Admiralbet ES", "admiralbet.es"),
    EsBookmaker("bet777", "Bet777", "bet777.es"),
    EsBookmaker("goldenpark", "Golden Park", "goldenpark.es", aliases=("goldenpark",)),
    EsBookmaker("marcaapuestas", "Marca Apuestas", "marcaapuestas.es",
                aliases=("marcaapuestas",)),
    EsBookmaker("jokerbet", "Jokerbet", "jokerbet.es", aliases=("jokerbet",)),
    EsBookmaker("aupabet", "Aupabet", "aupabet.es", aliases=("aupabet",)),
    EsBookmaker("betinia_es", "Betinia ES", "betinia.es"),
    EsBookmaker("efbet_es", "Efbet ES", "efbet.es"),
    EsBookmaker("zebet_es", "ZEbet ES", "zebet.es"),
    EsBookmaker("dafabet_es", "Dafabet ES", "dafabet.es"),
    EsBookmaker("yaass", "Yaass", "yaasscasino.es", aliases=("yaass", "yaasscasino")),
    EsBookmaker("williamhill_es", "William Hill ES", "williamhill.es"),
    EsBookmaker("betsson_es", "Betsson ES", "betsson.es"),
    EsBookmaker("interwetten_es", "Interwetten ES", "interwetten.es"),
    EsBookmaker("tonybet_es", "TonyBet ES", "tonybet.es"),
    EsBookmaker("betano_es", "Betano ES", "betano.es", verify=True,
                note="No aparece en las páginas DGOJ descargadas: verificar licencia"),
    EsBookmaker("marathonbet_es", "Marathonbet ES", "marathonbet.es",
                aliases=("maratonbet.es",),
                note="Dominio presente en las páginas DGOJ descargadas"),
)

# ── Exchange: SOLO betfair.es ──────────────────────────────────────────────────────
# Sin API gratuita: el plan gratis de odds-api.io no da exchanges, y el `betfair_ex_eu`
# de The Odds API es el exchange .com (no usable desde España).
BETFAIR_ES_EXCHANGE = EsBookmaker(
    EXCHANGE_SLUG, "Betfair Exchange ES", "betfair.es", kind="exchange",
    aliases=("betfair.es exchange", "betfair exchange es", "betfair_ex_es"),
    note="Único exchange permitido. Comisión por defecto 5 % hasta que el usuario la confirme",
)

ES_BOOKMAKERS: tuple[EsBookmaker, ...] = _WITH_API + _WITHOUT_API + (BETFAIR_ES_EXCHANGE,)
BY_SLUG: dict[str, EsBookmaker] = {b.slug: b for b in ES_BOOKMAKERS}

# Exchanges conocidos que NO son el de betfair.es (para avisar en las alertas).
NON_ES_EXCHANGES = frozenset({
    "betfair_ex_eu", "betfair_ex_uk", "betfair_ex_au", "betfair exchange",
    "matchbook", "smarkets", "orbit", "orbit exchange", "sharp", "skyexchange",
})


def _norm(name: str) -> str:
    """'Bwin ES' / 'bwin_es' / 'BWIN-ES' → 'bwines'. Quita acentos y todo lo no alfanumérico."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _keys(b: EsBookmaker) -> set[str]:
    raw = {b.slug, b.name, b.domain, *b.api_ids.values(), *b.aliases}
    return {_norm(x) for x in raw if x}


_INDEX: dict[str, EsBookmaker] = {}
for _b in ES_BOOKMAKERS:
    for _k in _keys(_b):
        _INDEX.setdefault(_k, _b)


def lookup(name: str) -> EsBookmaker | None:
    """La casa ES que identifica ese nombre (slug, nombre, dominio, id de API), o None."""
    if not name:
        return None
    return _INDEX.get(_norm(name))


def is_es_licensed(name: str) -> bool:
    """True si el nombre (de cualquier API o de esta lista) identifica una casa o el
    exchange con licencia en España. Marcas genéricas ('bwin', 'bet365', 'pinnacle',
    'betfair_ex_eu') → False."""
    return lookup(name) is not None


def is_es_sportsbook(name: str) -> bool:
    b = lookup(name)
    return b is not None and b.kind == "sportsbook"


def is_es_exchange(name: str) -> bool:
    """True solo para el exchange de betfair.es."""
    b = lookup(name)
    return b is not None and b.kind == "exchange"


def is_non_es_exchange(name: str) -> bool:
    """True para exchanges que no son el de betfair.es (betfair_ex_eu, matchbook…)."""
    n = str(name or "").strip().lower()
    return n in NON_ES_EXCHANGES or n.startswith("betfair_ex_")


def odds_api_io_name(slug: str) -> str | None:
    """Nombre exacto en odds-api.io ('Bwin ES') de una casa ES, o None."""
    b = BY_SLUG.get(slug)
    return b.api_ids.get("odds_api_io") if b else None


def free_api_bookmakers() -> list[EsBookmaker]:
    """Casas ES con cuotas en el plan GRATUITO de odds-api.io."""
    return [b for b in ES_BOOKMAKERS if b.has_api and "odds_api_io" in b.api_ids]


# Marca genérica (como la nombran The Odds API / odds-api.io sin sufijo) → versión .es.
_BRAND_COUNTERPART = {
    "bet365": "bet365_es", "bwin": "bwin_es", "codere": "codere_es",
    "betfair": "betfair_es", "betfairsportsbook": "betfair_es",
    "sport888": "sport888_es", "888sport": "sport888_es",
    "betway": "betway_es", "winamax": "winamax_es", "winamaxfr": "winamax_es",
    "winamaxde": "winamax_es", "leovegas": "leovegas_es", "pokerstars": "pokerstars_es",
    "paf": "paf_es", "casumo": "casumo_es", "williamhill": "williamhill_es",
    "betsson": "betsson_es", "marathonbet": "marathonbet_es", "interwetten": "interwetten_es",
    "tonybet": "tonybet_es", "betano": "betano_es", "olybet": "olybet_es",
    "admiralbet": "admiralbet_es", "zebet": "zebet_es", "dafabet": "dafabet_es",
    "efbet": "efbet_es", "betinia": "betinia_es", "retabet": "retabet_es",
    "luckia": "luckia_es",
}


def es_counterpart(name: str) -> EsBookmaker | None:
    """Para una marca genérica sin licencia española en esa cuota ('bet365', 'williamhill'),
    la versión .es que existe. None si el nombre ya es ES o la marca no tiene .es."""
    if is_es_licensed(name):
        return None
    slug = _BRAND_COUNTERPART.get(_norm(name))
    return BY_SLUG.get(slug) if slug else None


def display_name(name: str) -> str:
    """Nombre de pantalla de la casa ES, o el nombre tal cual si no es ES."""
    b = lookup(name)
    return b.name if b else str(name or "?")


# ── Avisos para alertas (solo presentación) ────────────────────────────────────────
REFERENCE_ONLY_BOOK = "solo referencia: casa sin licencia en España"
REFERENCE_ONLY_EXCHANGE = "solo referencia: exchange sin licencia en España (no es betfair.es)"


def reference_only_reason(back_bookmaker: str | None = None,
                          lay_bookmaker: str | None = None) -> str | None:
    """Motivo por el que una alerta NO es apostable desde España, o None si lo es.
    back_bookmaker: casa del back (si la hay). lay_bookmaker: exchange del lay (si lo hay)."""
    reasons = []
    if back_bookmaker is not None and not is_es_sportsbook(back_bookmaker):
        reasons.append(f"{REFERENCE_ONLY_BOOK} ({back_bookmaker})")
    if lay_bookmaker is not None and not is_es_exchange(lay_bookmaker):
        reasons.append(f"{REFERENCE_ONLY_EXCHANGE} ({lay_bookmaker})")
    return "; ".join(reasons) or None
