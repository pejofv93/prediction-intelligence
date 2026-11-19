"""Tabla de nombres de pantalla (shared/team_display_names.py) y su uso en la exportación web."""
from datetime import datetime, timezone

import pytest

from shared import team_display_names as tn

# Nombres de football-data.co.uk de la temporada 2026/27 (rates.json del 9-nov-2026)
CURRENT_FDCO = {
    "SP1": ["Alaves", "Ath Bilbao", "Ath Madrid", "Barcelona", "Betis", "Celta", "Elche", "Espanol", "Getafe",
            "La Coruna", "Levante", "Malaga", "Osasuna", "Real Madrid", "Santander", "Sevilla", "Sociedad",
            "Valencia", "Vallecano", "Villarreal"],
    "SP2": ["Albacete", "Almeria", "Andorra", "Burgos", "Cadiz", "Castellon", "Celta B", "Ceuta", "Cordoba",
            "Eibar", "Eldense", "Girona", "Granada", "Las Palmas", "Leganes", "Mallorca", "Oviedo", "Sabadell",
            "Sociedad B", "Sp Gijon", "Tenerife", "Valladolid"],
    "I1": ["Atalanta", "Bologna", "Cagliari", "Como", "Fiorentina", "Frosinone", "Genoa", "Inter", "Juventus",
           "Lazio", "Lecce", "Milan", "Monza", "Napoli", "Parma", "Roma", "Sassuolo", "Torino", "Udinese", "Venezia"],
    "E0": ["Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton", "Chelsea", "Coventry",
           "Crystal Palace", "Everton", "Fulham", "Hull", "Ipswich", "Leeds", "Liverpool", "Man City",
           "Man United", "Newcastle", "Nott'm Forest", "Sunderland", "Tottenham"],
    "F1": ["Angers", "Auxerre", "Brest", "Le Havre", "Le Mans", "Lens", "Lille", "Lorient", "Lyon", "Marseille",
           "Monaco", "Nice", "Paris FC", "Paris SG", "Rennes", "Strasbourg", "Toulouse", "Troyes"],
    "P1": ["Academico Viseu", "Alverca", "Arouca", "Benfica", "Casa Pia", "Estoril", "Estrela", "Famalicao",
           "Gil Vicente", "Guimaraes", "Maritimo", "Moreirense", "Nacional", "Porto", "Rio Ave", "Santa Clara",
           "Sp Braga", "Sp Lisbon"],
    "D1": ["Augsburg", "Bayern Munich", "Dortmund", "Ein Frankfurt", "Elversberg", "FC Koln", "Freiburg",
           "Hamburg", "Hoffenheim", "Leverkusen", "M'gladbach", "Mainz", "Paderborn", "RB Leipzig", "Schalke 04",
           "Stuttgart", "Union Berlin", "Werder Bremen"],
    "N1": ["AZ Alkmaar", "Ajax", "Cambuur", "Den Haag", "Excelsior", "Feyenoord", "For Sittard", "Go Ahead Eagles",
           "Groningen", "Heerenveen", "Nijmegen", "PSV Eindhoven", "Sparta Rotterdam", "Telstar", "Twente",
           "Utrecht", "Willem II", "Zwolle"],
}

# Mismos casos que trends_web/tests/initials.test.mjs (initials() de la web)
INITIALS_CASES = [
    ("Athletic", "ATH"), ("Espanyol", "ESP"), ("Colonia", "COL"), ("Real Madrid", "RM"),
    ("Sporting de Portugal", "SP"), ("Nottingham Forest", "NF"), ("St. Pauli", "SP"),
    ("Atlético", "ATL"), ("Mönchengladbach", "MÖN"), ("PSG", "PSG"), ("ADO Den Haag", "ADH"),
    ("Académico de Viseu", "AV"), ("Willem II", "WI"), ("Saint-Étienne", "SÉ"), ("", "?"),
]


def test_tabla_sin_colisiones():
    assert tn.collisions() == []


@pytest.mark.parametrize("div", sorted(CURRENT_FDCO))
def test_todos_los_equipos_actuales_estan_y_sin_nombres_repetidos(div):
    names = CURRENT_FDCO[div]
    assert [n for n in names if not tn.known(n)] == []
    shown = [tn.display_name(n) for n in names]
    assert len(set(shown)) == len(shown)                     # dos clubes de la liga nunca se llaman igual


@pytest.mark.parametrize("raw,want", [
    ("Ath Bilbao", "Athletic"), ("Athletic Club", "Athletic"), ("Athletic Club Bilbao", "Athletic"),
    ("Espanol", "Espanyol"), ("RCD Espanyol de Barcelona", "Espanyol"),
    ("FC Koln", "Colonia"), ("1. FC Köln", "Colonia"),
    ("Ath Madrid", "Atlético"), ("Club Atlético de Madrid", "Atlético"), ("Real Madrid CF", "Real Madrid"),
    ("Sociedad", "Real Sociedad"), ("Sociedad B", "Real Sociedad B"), ("Celta B", "Celta B"),
    ("Vallecano", "Rayo Vallecano"), ("La Coruna", "Deportivo"), ("Sp Gijon", "Sporting"),
    ("Santander", "Racing"), ("Man United", "Manchester United"), ("Nott'm Forest", "Nottingham Forest"),
    ("Wolverhampton Wanderers FC", "Wolverhampton"), ("FC Internazionale Milano", "Inter"),
    ("Napoli", "Nápoles"), ("Bayern Munich", "Bayern"), ("FC Bayern München", "Bayern"),
    ("M'gladbach", "Mönchengladbach"), ("Borussia Mönchengladbach", "Mönchengladbach"),
    ("Bayer 04 Leverkusen", "Leverkusen"), ("TSG 1899 Hoffenheim", "Hoffenheim"), ("Schalke 04", "Schalke"),
    ("Paris SG", "PSG"), ("Paris Saint-Germain FC", "PSG"), ("Paris FC", "Paris FC"),
    ("Marseille", "Marsella"), ("Stade Rennais FC 1901", "Rennes"),
    ("Sp Lisbon", "Sporting de Portugal"), ("Sporting CP", "Sporting de Portugal"), ("Sp Braga", "Braga"),
    ("Guimaraes", "Vitória Guimarães"), ("Porto", "Oporto"),
    ("PSV Eindhoven", "PSV"), ("AZ Alkmaar", "AZ"), ("Nijmegen", "NEC"), ("For Sittard", "Fortuna Sittard"),
])
def test_variantes_de_las_fuentes(raw, want):
    assert tn.display_name(raw) == want
    assert tn.display_name(raw, "cups") == want                  # copas: mismos clubes


def test_real_madrid_no_se_confunde_con_otros_madrid():
    assert tn.display_name("Real Madrid") == "Real Madrid"
    assert tn.display_name("Atletico Madrid") == "Atlético"
    assert tn.display_name("Rayo Vallecano de Madrid") == "Rayo Vallecano"
    assert tn.display_name("Madrid CFF", "women") == "Madrid CFF"
    assert tn.display_name("Real Madrid W", "women") == "Real Madrid"


def test_femenino_quita_la_marca_y_usa_su_tabla():
    assert tn.display_name("Barcelona W", "women") == "Barcelona"
    assert tn.display_name("Athletic Club Women", "women") == "Athletic"
    assert tn.display_name("Bayern München Frauen", "women") == "Bayern"
    assert tn.display_name("UD Granadilla Tenerife", "women") == "Costa Adeje Tenerife"
    assert tn.display_name("Tenerife") == "Tenerife"                           # el masculino, aparte
    assert tn.display_name("Levante Badalona", "women") == "Badalona"
    assert tn.display_name("Levante", "women") == "Levante"
    assert tn.display_name("Club Desconocido Femenino", "women") == "Club Desconocido"


def test_respaldo_nombre_limpio():
    assert tn.display_name("FC Fantasía 1905") == "Fantasía"
    assert tn.display_name("Desconocido") == "Desconocido"
    assert tn.display_name("FC") == "FC"                                     # no se queda vacío
    assert tn.display_name("") == "" and tn.display_name(None) == ""
    assert not tn.known("FC Fantasía 1905")


@pytest.mark.parametrize("name,want", INITIALS_CASES)
def test_iniciales_del_escudo(name, want):
    assert tn.badge_initials(name) == want


def test_iniciales_salen_del_nombre_de_pantalla():
    assert tn.badge_initials(tn.display_name("Athletic Club Bilbao")) == "ATH"
    assert tn.badge_initials(tn.display_name("RCD Espanyol de Barcelona")) == "ESP"
    assert tn.badge_initials(tn.display_name("1. FC Köln")) == "COL"


def test_exportacion_web_solo_cambia_la_presentacion():
    from analyzers import match_preview_store as st
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    p = {"match_id": "SP1_2026-11-20_ath-bilbao_espanol", "div": "SP1",
         "kickoff_utc": datetime(2026, 11, 20, 20, tzinfo=timezone.utc).isoformat(),
         "home_team": "Ath Bilbao", "away_team": "Espanol", "teams": {}, "stats": {}}
    w = st.web_match(p)
    assert w["id"] == "SP1_2026-11-20_ath-bilbao_espanol"                  # id intacto
    assert (w["home"], w["away"], w["badges"]) == ("Athletic", "Espanyol", ["ATH", "ESP"])
    assert p["home_team"] == "Ath Bilbao"                                  # la ficha no se toca
    women = st.web_match({**p, "group": "women", "home_team": "Barcelona W", "away_team": "Madrid CFF"})
    assert (women["home"], women["away"], women["badges"]) == ("Barcelona", "Madrid CFF", ["BAR", "MC"])
    assert st.web_export([p], now)["matches"][0]["home"] == "Athletic"
