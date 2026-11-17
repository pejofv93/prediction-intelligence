"""Informe semanal: etiqueta de totales en baloncesto (puntos, no goles)."""
import main


def test_totales_de_baloncesto_son_puntos():
    b = {"home_team": "Real Madrid", "away_team": "Barça", "sport": "basketball",
         "market_type": "totals", "selection": "Over 162.5"}
    assert main._describe_bet(b) == "Real Madrid vs Barça (Más/Menos puntos: Over 162.5)"
    assert "puntos" in main._describe_bet({**b, "sport": "", "league": "ACB"})


def test_totales_de_futbol_siguen_siendo_goles():
    f = {"home_team": "A", "away_team": "B", "sport": "football", "market_type": "totals",
         "selection": "Over 2.5"}
    assert main._describe_bet(f) == "A vs B (Más/Menos goles: Over 2.5)"


def test_operacion_de_baloncesto_por_senal_o_por_liga():
    assert main._trade_is_basketball({"signal_data": {"sport": "basketball"}})
    assert main._trade_is_basketball({"category": "ACB"})
    assert not main._trade_is_basketball({"signal_data": {"sport": "football"}, "category": "PL"})
