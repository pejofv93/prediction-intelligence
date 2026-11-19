"""
shared/matched_calc.py — calculadora de matched betting ENTRE CASAS con licencia en
España (dutching a 2 y 3 resultados), con lay en betfair.es y mezclas de ambos.

Módulo puro (sin red ni Firestore). Vive en shared/ porque lo usan el dashboard
(endpoint /api/matched-calc) y el lector de cuotas ES de sports-agent
(matched/es_odds_reader.py) para puntuar coberturas con la misma matemática.

Notación: S stake del bono a cuota B; F freebet; Oi cuotas de cobertura; L lay; c comisión.
  calificante:   Hi = S·B/Oi            beneficio S·B·(1 − 1/B − Σ1/Oi)
  freebet SNR:   Hi = F(B−1)/Oi         beneficio F(B−1)(1 − Σ1/Oi)
  freebet SR:    Hi = F·B/Oi            beneficio F·B(1 − Σ1/Oi)
  reembolso:     Hi = (S·B − k·R)/Oi    (R importe del reembolso si pierde, k su valor 0..1)
  betfair.es:    Ls = S·B/(L−c) calificante; F(B−1)/(L−c) SNR; F·B/(L−c) SR
Las fórmulas cerradas son casos particulares del sistema general que resuelve
calculate(): stakes tales que el beneficio sea IGUAL en todos los resultados. Así se
admite cualquier mezcla (X en una casa y el 2 en el exchange, lay del bono…).

Tipos de pata de cobertura:
  back           back en otra casa ES (sin comisión)
  exchange_back  back en el exchange de betfair.es: cuota efectiva 1 + (O−1)(1−c)
  lay            lay en betfair.es de un resultado (en la práctica, el del bono)
Reglas: la cobertura NUNCA en la casa del bono; solo casas ES y el exchange betfair.es.

Redondeo: stakes a céntimos (mitad hacia arriba); el beneficio por resultado se calcula
con los stakes YA redondeados (lo que de verdad vas a cobrar). Retención % = beneficio
mínimo / importe del bono (S en calificante, F en freebet, R en reembolso).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from shared.es_bookmakers import (
    BETFAIR_ES_DEFAULT_COMMISSION,
    display_name,
    is_es_exchange,
    is_es_sportsbook,
    lookup,
)

BONUS_KINDS = ("qualifying", "freebet_snr", "freebet_sr", "refund")
LEG_KINDS = ("back", "exchange_back", "lay")

_KIND_LABEL = {
    "qualifying": "apuesta calificante",
    "freebet_snr": "freebet (sin devolución del stake)",
    "freebet_sr": "freebet (con devolución del stake)",
    "refund": "apuesta con reembolso si pierde",
}


class CalcError(ValueError):
    """Entrada inválida o combinación de patas sin cobertura posible."""


@dataclass
class BonusBet:
    kind: str                 # BONUS_KINDS
    stake: float              # S (o F en freebet)
    odds: float               # B
    bookmaker: str            # casa ES del bono (slug o nombre)
    outcome: str              # resultado al que va el bono ("1", "X", "2", "Over"...)
    refund_amount: float = 0.0   # R (solo refund)
    refund_value: float = 0.0    # k en [0, 1] (solo refund)


@dataclass
class CoverLeg:
    outcome: str
    kind: str                 # LEG_KINDS
    odds: float
    bookmaker: str = ""       # casa ES (back) o betfair.es (exchange_back / lay)
    commission: float | None = None   # solo exchange; None → BETFAIR_ES_DEFAULT_COMMISSION


@dataclass
class _Solved:
    stakes: list[float] = field(default_factory=list)


def _cents(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── Modelo de pagos ────────────────────────────────────────────────────────────────

def _bonus_payoff(b: BonusBet, outcome: str) -> float:
    """Beneficio neto de la pata del bono si sale `outcome`."""
    wins = outcome == b.outcome
    if b.kind == "qualifying":
        return b.stake * (b.odds - 1) if wins else -b.stake
    if b.kind == "freebet_snr":
        return b.stake * (b.odds - 1) if wins else 0.0
    if b.kind == "freebet_sr":
        return b.stake * b.odds if wins else 0.0
    if b.kind == "refund":
        return b.stake * (b.odds - 1) if wins else -b.stake + b.refund_value * b.refund_amount
    raise CalcError(f"tipo de bono desconocido: {b.kind}")


def _commission(leg: CoverLeg) -> float:
    return BETFAIR_ES_DEFAULT_COMMISSION if leg.commission is None else float(leg.commission)


def effective_back_odds(leg: CoverLeg) -> float:
    """Cuota efectiva de una pata back (en el exchange descuenta la comisión de la ganancia)."""
    if leg.kind == "exchange_back":
        return 1 + (leg.odds - 1) * (1 - _commission(leg))
    return leg.odds


def _leg_coef(leg: CoverLeg, outcome: str) -> float:
    """Beneficio neto POR EURO de stake de la pata si sale `outcome`."""
    if leg.kind in ("back", "exchange_back"):
        return effective_back_odds(leg) - 1 if outcome == leg.outcome else -1.0
    if leg.kind == "lay":
        return -(leg.odds - 1) if outcome == leg.outcome else 1 - _commission(leg)
    raise CalcError(f"tipo de pata desconocido: {leg.kind}")


def _profit(b: BonusBet, legs: list[CoverLeg], stakes: list[float], outcome: str) -> float:
    return _bonus_payoff(b, outcome) + sum(_leg_coef(l, outcome) * s for l, s in zip(legs, stakes))


# ── Resolución (eliminación gaussiana, sistemas de 1-3 incógnitas) ─────────────────

def _solve(rows: list[list[float]], rhs: list[float], n: int) -> list[float]:
    """Resuelve rows·x = rhs (m×n, m ≥ n). Error si no hay solución única."""
    a = [r[:] + [v] for r, v in zip(rows, rhs)]
    m = len(a)
    piv_row = 0
    piv_cols = []
    for col in range(n):
        best = max(range(piv_row, m), key=lambda i: abs(a[i][col]), default=None)
        if best is None or abs(a[best][col]) < 1e-12:
            raise CalcError("patas redundantes: dos patas cubren lo mismo y el reparto no es único")
        a[piv_row], a[best] = a[best], a[piv_row]
        p = a[piv_row][col]
        a[piv_row] = [v / p for v in a[piv_row]]
        for i in range(m):
            if i != piv_row and abs(a[i][col]) > 0:
                f = a[i][col]
                a[i] = [vi - f * vp for vi, vp in zip(a[i], a[piv_row])]
        piv_cols.append(col)
        piv_row += 1
    for i in range(piv_row, m):
        if abs(a[i][n]) > 1e-9:
            raise CalcError("las patas no cubren todos los resultados: el beneficio no puede igualarse")
    return [a[i][n] for i in range(n)]


def solve_stakes(outcomes: list[str], bonus: BonusBet, legs: list[CoverLeg]) -> list[float]:
    """Stakes (sin redondear) que igualan el beneficio en todos los resultados."""
    if not legs:
        raise CalcError("falta al menos una pata de cobertura")
    base = outcomes[0]
    rows, rhs = [], []
    for o in outcomes[1:]:
        rows.append([_leg_coef(l, o) - _leg_coef(l, base) for l in legs])
        rhs.append(_bonus_payoff(bonus, base) - _bonus_payoff(bonus, o))
    if len(rows) < len(legs):
        raise CalcError(f"demasiadas patas: con {len(outcomes)} resultados bastan {len(outcomes) - 1}")
    stakes = _solve(rows, rhs, len(legs))
    for leg, s in zip(legs, stakes):
        if s <= 0:
            raise CalcError(f"la pata {leg.kind} de '{leg.outcome}' saldría con stake ≤ 0: "
                            "esa combinación no cubre (¿lay de un resultado que no es el del bono?)")
    return stakes


# ── Validación ─────────────────────────────────────────────────────────────────────

def validate(outcomes: list[str], bonus: BonusBet, legs: list[CoverLeg]) -> None:
    if len(outcomes) not in (2, 3):
        raise CalcError("el mercado debe tener 2 o 3 resultados")
    if len(set(outcomes)) != len(outcomes):
        raise CalcError("resultados repetidos")
    if bonus.kind not in BONUS_KINDS:
        raise CalcError(f"tipo de bono no válido: {bonus.kind} (usa {', '.join(BONUS_KINDS)})")
    if bonus.outcome not in outcomes:
        raise CalcError(f"el resultado del bono '{bonus.outcome}' no está en el mercado")
    if bonus.stake <= 0 or bonus.odds <= 1:
        raise CalcError("el bono necesita stake > 0 y cuota > 1")
    if bonus.kind == "refund":
        if bonus.refund_amount <= 0 or not 0 <= bonus.refund_value <= 1:
            raise CalcError("reembolso: importe > 0 y valor k entre 0 y 1")
    if not is_es_sportsbook(bonus.bookmaker):
        raise CalcError(f"la casa del bono '{bonus.bookmaker}' no está en la lista de casas "
                        "con licencia en España")
    bonus_slug = lookup(bonus.bookmaker).slug
    for leg in legs:
        if leg.kind not in LEG_KINDS:
            raise CalcError(f"tipo de pata no válido: {leg.kind} (usa {', '.join(LEG_KINDS)})")
        if leg.outcome not in outcomes:
            raise CalcError(f"la pata apunta a '{leg.outcome}', que no está en el mercado")
        if leg.odds <= 1:
            raise CalcError("todas las cuotas de cobertura deben ser > 1")
        if leg.kind == "back":
            if not is_es_sportsbook(leg.bookmaker):
                raise CalcError(f"casa de cobertura '{leg.bookmaker}' sin licencia en España "
                                "(o no está en la lista)")
            if lookup(leg.bookmaker).slug == bonus_slug:
                raise CalcError("la cobertura no puede ir en la misma casa del bono")
            if leg.outcome == bonus.outcome:
                raise CalcError("un back de cobertura no puede ir al mismo resultado del bono")
        else:
            if leg.bookmaker and not is_es_exchange(leg.bookmaker):
                raise CalcError(f"exchange '{leg.bookmaker}' no permitido: solo betfair.es")
            c = _commission(leg)
            if not 0 <= c < 1:
                raise CalcError("la comisión debe estar entre 0 y 1 (5 % → 0.05)")
            if leg.kind == "lay" and leg.odds <= c:
                raise CalcError("la cuota lay debe ser mayor que la comisión")


# ── Cálculo completo ──────────────────────────────────────────────────────────────

def _bonus_base(b: BonusBet) -> float:
    return b.refund_amount if b.kind == "refund" else b.stake


def calculate(outcomes: list[str], bonus: BonusBet, legs: list[CoverLeg]) -> dict:
    """Reparto de stakes con beneficio igual en todos los resultados.

    Devuelve: legs (stake redondeado, responsabilidad del lay, cuota efectiva),
    profit_by_outcome (con stakes redondeados), profit_exact (sin redondeo),
    profit_min/max, retention_pct, outlay (dinero que hay que poner), steps."""
    validate(outcomes, bonus, legs)
    raw = solve_stakes(outcomes, bonus, legs)
    exact = _profit(bonus, legs, raw, outcomes[0])
    stakes = [_cents(s) for s in raw]
    by_outcome = {o: _cents(_profit(bonus, legs, stakes, o)) for o in outcomes}
    pmin, pmax = min(by_outcome.values()), max(by_outcome.values())
    base = _bonus_base(bonus)

    legs_out, steps = [], []
    bonus_name = display_name(bonus.bookmaker)
    if bonus.kind in ("freebet_snr", "freebet_sr"):
        steps.append(f"Freebet de {bonus.stake:.2f} € a '{bonus.outcome}' @ {bonus.odds:.2f} "
                     f"en {bonus_name}")
    else:
        steps.append(f"Apuesta {bonus.stake:.2f} € a '{bonus.outcome}' @ {bonus.odds:.2f} "
                     f"en {bonus_name} ({_KIND_LABEL[bonus.kind]})")
    outlay = 0.0 if bonus.kind.startswith("freebet") else bonus.stake
    for leg, s in zip(legs, stakes):
        book = display_name(leg.bookmaker) if leg.kind == "back" else "Betfair Exchange ES"
        item = {
            "outcome": leg.outcome, "kind": leg.kind, "bookmaker": leg.bookmaker or "betfair_es_exchange",
            "bookmaker_name": book, "odds": leg.odds, "stake": s,
        }
        if leg.kind == "lay":
            liability = _cents(s * (leg.odds - 1))
            item.update(liability=liability, commission=_commission(leg))
            outlay += liability
            steps.append(f"LAY {s:.2f} € a '{leg.outcome}' @ {leg.odds:.2f} en {book} "
                         f"(responsabilidad {liability:.2f} €, comisión {_commission(leg):.0%})")
        else:
            if leg.kind == "exchange_back":
                item.update(commission=_commission(leg),
                            effective_odds=round(effective_back_odds(leg), 4))
            outlay += s
            steps.append(f"Back {s:.2f} € a '{leg.outcome}' @ {leg.odds:.2f} en {book}")
        legs_out.append(item)

    return {
        "outcomes": outcomes,
        "bonus": {"kind": bonus.kind, "label": _KIND_LABEL[bonus.kind], "stake": bonus.stake,
                  "odds": bonus.odds, "bookmaker": bonus.bookmaker, "bookmaker_name": bonus_name,
                  "outcome": bonus.outcome},
        "legs": legs_out,
        "profit_by_outcome": by_outcome,
        "profit_exact": round(exact, 4),
        "profit_min": pmin,
        "profit_max": pmax,
        "retention_pct": round(pmin / base * 100, 2) if base else 0.0,
        "outlay": _cents(outlay),
        "steps": steps,
    }


# ── Fórmulas cerradas (referencia y tests) ─────────────────────────────────────────

def closed_form_dutch(kind: str, stake: float, odds: float, cover_odds: list[float],
                      refund_amount: float = 0.0, refund_value: float = 0.0) -> tuple[list[float], float]:
    """(stakes de cobertura, beneficio) sin redondear, con las fórmulas acordadas."""
    inv = sum(1 / o for o in cover_odds)
    if kind == "qualifying":
        return [stake * odds / o for o in cover_odds], stake * odds * (1 - 1 / odds - inv)
    if kind == "freebet_snr":
        base = stake * (odds - 1)
        return [base / o for o in cover_odds], base * (1 - inv)
    if kind == "freebet_sr":
        base = stake * odds
        return [base / o for o in cover_odds], base * (1 - inv)
    if kind == "refund":
        # Hi = (S·B − k·R)/Oi; beneficio (si gana el bono) = S(B−1) − ΣHi
        hs = [(stake * odds - refund_value * refund_amount) / o for o in cover_odds]
        return hs, stake * (odds - 1) - sum(hs)
    raise CalcError(kind)


def closed_form_lay(kind: str, stake: float, odds: float, lay_odds: float,
                    commission: float = BETFAIR_ES_DEFAULT_COMMISSION) -> float:
    """Stake lay en betfair.es con las fórmulas acordadas."""
    if kind == "qualifying":
        return stake * odds / (lay_odds - commission)
    if kind == "freebet_snr":
        return stake * (odds - 1) / (lay_odds - commission)
    if kind == "freebet_sr":
        return stake * odds / (lay_odds - commission)
    raise CalcError(kind)
