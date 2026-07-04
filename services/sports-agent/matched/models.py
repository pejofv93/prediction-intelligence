"""
matched/models.py — estructuras del detector back/lay.
"""
from dataclasses import dataclass, asdict


@dataclass
class BackLayQuote:
    """Mejor back (casa) y lay (Betfair) para una selección de un evento."""
    selection: str            # nombre de la selección ("Real Madrid", "Draw", "Nadal, R.")
    back_odds: float          # mejor cuota back entre casas NO-exchange
    back_bookmaker: str       # casa que ofrece ese back
    lay_odds: float           # cuota lay en betfair_ex_eu
    lay_bookmaker: str        # "betfair_ex_eu"
    lay_last_update: str = "" # ISO8601 del mercado h2h_lay (staleness) — "" si la API no lo da
    back_last_update: str = ""# ISO8601 del mercado h2h de la casa back


@dataclass
class MatchedSignal:
    """
    Señal emitida por el motor: cobertura de bono o surebet.
    Todos los importes económicos se dan por back_stake=100 para comparar entre señales.
    """
    signal_id: str            # determinista: sha1(sport_key:event_id:selection) → idempotente
    signal_type: str          # "surebet" | "coverage"
    sport_key: str
    event_id: str
    commence_time: str        # ISO8601 UTC — kickoff
    home_team: str
    away_team: str
    selection: str            # selección back/lay
    back_bookmaker: str
    back_odds: float
    lay_bookmaker: str        # "betfair_ex_eu"
    lay_odds: float
    commission: float         # comisión exchange usada en el cálculo
    confidence: str           # "high" | "medium" | "unknown" — según frescura del lay
    lay_age_seconds: int      # antigüedad del lay al detectarlo (-1 si sin last_update)
    back_age_seconds: int     # antigüedad del back (-1 si sin last_update)
    qualifying_rating: float  # % del stake garantizado (negativo = pérdida qualifying)
    freebet_snr_rating: float # % de la free bet convertido a beneficio (EV si fuera bono SNR)
    lay_stake_per_100: float  # lay stake para back_stake=100
    liability_per_100: float  # responsabilidad en el exchange para back_stake=100
    profit_per_100: float     # beneficio garantizado con back_stake=100 (neg = pérdida qualifying)
    detected_at: str          # ISO8601 UTC
    expires_at: str           # ISO8601 UTC — se invalida al empezar el partido

    def to_doc(self) -> dict:
        return asdict(self)
