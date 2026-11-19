"""shared/matched_calc.py: calculadora entre casas ES con las fórmulas acordadas."""
import pytest

from shared.matched_calc import (
    BonusBet, CalcError, CoverLeg, calculate, closed_form_dutch, closed_form_lay, solve_stakes,
)

O3 = ["1", "X", "2"]
O2 = ["1", "2"]


def _bonus(kind="qualifying", stake=10.0, odds=2.0, book="bwin_es", outcome="1", **kw):
    return BonusBet(kind=kind, stake=stake, odds=odds, bookmaker=book, outcome=outcome, **kw)


# ── 2 resultados ──────────────────────────────────────────────────────────────────

def test_calificante_2_resultados():
    # H = S·B/O = 10·2/2.1 = 9.5238; beneficio = S·B·(1 − 1/B − 1/O) = 0.4762
    r = calculate(O2, _bonus(), [CoverLeg("2", "back", 2.1, "codere_es")])
    assert r["legs"][0]["stake"] == 9.52
    assert r["profit_exact"] == pytest.approx(20 * (1 - 1 / 2 - 1 / 2.1), abs=1e-4)
    assert r["profit_by_outcome"] == {"1": 0.48, "2": 0.47}   # con el stake redondeado
    assert r["profit_min"] == 0.47
    assert r["retention_pct"] == 4.7
    assert r["outlay"] == 19.52


# ── 3 resultados (dutching) ───────────────────────────────────────────────────────

def test_calificante_3_resultados_dutching():
    legs = [CoverLeg("X", "back", 3.4, "codere_es"), CoverLeg("2", "back", 4.0, "betway_es")]
    r = calculate(O3, _bonus(odds=2.5), legs)
    hs, profit = closed_form_dutch("qualifying", 10, 2.5, [3.4, 4.0])
    assert [l["stake"] for l in r["legs"]] == [7.35, 6.25]
    assert hs == pytest.approx([25 / 3.4, 6.25])
    assert r["profit_exact"] == pytest.approx(profit, abs=1e-4)
    assert profit == pytest.approx(25 * (1 - 1 / 2.5 - 1 / 3.4 - 1 / 4.0))
    assert set(r["profit_by_outcome"]) == {"1", "X", "2"}
    assert max(r["profit_by_outcome"].values()) - min(r["profit_by_outcome"].values()) <= 0.03


def test_freebet_snr_3_resultados():
    # Hi = F(B−1)/Oi; beneficio F(B−1)(1 − Σ1/Oi) = 40·(1 − 0.25 − 1/3) = 16.67
    legs = [CoverLeg("X", "back", 4.0, "codere_es"), CoverLeg("2", "back", 3.0, "winamax_es")]
    r = calculate(O3, _bonus("freebet_snr", odds=5.0), legs)
    assert [l["stake"] for l in r["legs"]] == [10.0, 13.33]
    assert r["profit_exact"] == pytest.approx(40 * (1 - 1 / 4 - 1 / 3), abs=1e-4)
    # con stakes redondeados: X → 10·4 − 23.33 = 16.67; 2 → 13.33·3 − 23.33 = 16.66
    assert r["profit_by_outcome"] == {"1": 16.67, "X": 16.67, "2": 16.66}
    assert r["profit_min"] == 16.66
    assert r["retention_pct"] == 166.6
    assert r["outlay"] == 23.33           # la freebet no cuesta dinero


def test_freebet_sr():
    legs = [CoverLeg("2", "back", 2.0, "codere_es")]
    r = calculate(O2, _bonus("freebet_sr", odds=2.0), legs)
    # Hi = F·B/Oi = 10; beneficio F·B(1 − 1/O) = 10
    assert r["legs"][0]["stake"] == 10.0
    assert r["profit_exact"] == pytest.approx(10.0)


def test_reembolso_si_pierde():
    # Hi = (S·B − k·R)/Oi con S=10, B=3, R=10, k=0.7 → (30 − 7)/Oi
    legs = [CoverLeg("X", "back", 3.5, "codere_es"), CoverLeg("2", "back", 3.0, "betway_es")]
    b = _bonus("refund", odds=3.0, refund_amount=10, refund_value=0.7)
    r = calculate(O3, b, legs)
    hs, profit = closed_form_dutch("refund", 10, 3.0, [3.5, 3.0], 10, 0.7)
    assert hs == pytest.approx([23 / 3.5, 23 / 3.0])
    assert [l["stake"] for l in r["legs"]] == [6.57, 7.67]
    assert r["profit_exact"] == pytest.approx(profit, abs=1e-4)
    # mismo beneficio si pierde el bono (−S + k·R + H·O − ΣH)
    assert -10 + 7 + 23 - sum(hs) == pytest.approx(profit)


# ── betfair.es ────────────────────────────────────────────────────────────────────

def test_lay_calificante_betfair_es():
    # L_s = S·B/(L − c) = 20/1.99 = 10.05
    r = calculate(O3, _bonus(), [CoverLeg("1", "lay", 2.04, "betfair_es_exchange", 0.05)])
    assert closed_form_lay("qualifying", 10, 2.0, 2.04, 0.05) == pytest.approx(20 / 1.99)
    leg = r["legs"][0]
    assert leg["stake"] == 10.05 and leg["liability"] == 10.45
    assert r["profit_exact"] == pytest.approx(10 - (20 / 1.99) * 1.04, abs=1e-4)
    assert r["outlay"] == 20.45


def test_lay_freebet_snr_y_sr():
    snr = calculate(O2, _bonus("freebet_snr", odds=5.0), [CoverLeg("1", "lay", 5.2, "betfair_es_exchange")])
    assert snr["legs"][0]["stake"] == round(40 / 5.15, 2)          # comisión por defecto 0.05
    assert snr["legs"][0]["commission"] == 0.05
    sr = calculate(O2, _bonus("freebet_sr", odds=5.0), [CoverLeg("1", "lay", 5.2, "betfair_es_exchange", 0.02)])
    assert sr["legs"][0]["stake"] == round(50 / 5.18, 2)


def test_lay_sin_casa_explicita_usa_betfair_es():
    r = calculate(O2, _bonus(), [CoverLeg("1", "lay", 2.04)])
    assert r["legs"][0]["bookmaker"] == "betfair_es_exchange"


# ── Mezcla ────────────────────────────────────────────────────────────────────────

def test_mezcla_x_en_casa_y_2_en_exchange():
    legs = [CoverLeg("X", "back", 3.5, "codere_es"),
            CoverLeg("2", "exchange_back", 4.2, "betfair_es_exchange", 0.05)]
    r = calculate(O3, _bonus(odds=2.2), legs)
    eff = 1 + 3.2 * 0.95
    _, profit = closed_form_dutch("qualifying", 10, 2.2, [3.5, eff])
    assert r["legs"][1]["effective_odds"] == pytest.approx(eff)
    assert r["profit_exact"] == pytest.approx(profit, abs=1e-4)


def test_lay_de_otro_resultado_no_cubre():
    legs = [CoverLeg("X", "back", 3.5, "codere_es"), CoverLeg("2", "lay", 4.0)]
    with pytest.raises(CalcError, match="stake ≤ 0"):
        calculate(O3, _bonus(), legs)


# ── Validación ────────────────────────────────────────────────────────────────────

def test_cobertura_en_la_misma_casa_del_bono():
    with pytest.raises(CalcError, match="misma casa"):
        calculate(O2, _bonus(book="Bwin ES"), [CoverLeg("2", "back", 2.1, "bwin_es")])


def test_casas_sin_licencia_rechazadas():
    with pytest.raises(CalcError, match="licencia"):
        calculate(O2, _bonus(book="pinnacle"), [CoverLeg("2", "back", 2.1, "codere_es")])
    with pytest.raises(CalcError, match="licencia"):
        calculate(O2, _bonus(), [CoverLeg("2", "back", 2.1, "unibet")])
    with pytest.raises(CalcError, match="betfair.es"):
        calculate(O2, _bonus(), [CoverLeg("1", "lay", 2.1, "betfair_ex_eu")])


def test_falta_un_resultado_por_cubrir():
    with pytest.raises(CalcError):
        calculate(O3, _bonus(), [CoverLeg("X", "back", 3.0, "codere_es")])


def test_patas_de_mas():
    legs = [CoverLeg("2", "back", 2.1, "codere_es"), CoverLeg("2", "back", 2.0, "betway_es")]
    with pytest.raises(CalcError):
        calculate(O2, _bonus(), legs)


def test_solve_stakes_coincide_con_formula_cerrada_freebet():
    legs = [CoverLeg("X", "back", 3.6, "codere_es"), CoverLeg("2", "back", 2.9, "betway_es")]
    hs = solve_stakes(O3, _bonus("freebet_snr", odds=4.5), legs)
    exp, _ = closed_form_dutch("freebet_snr", 10, 4.5, [3.6, 2.9])
    assert hs == pytest.approx(exp)
