"""Categorizador v2: casos reales de los mercados abiertos del 17-oct-2026."""
import pytest

from categories import CATEGORIZER_VERSION, categorize_market, _CRICKET_RE, _TENNIS_RE


def test_midterm_no_lleva_geopolitica_a_politica():
    assert categorize_market("US-Iran ceasefire continues through the midterms?") == "geopolitics"
    # sigue siendo política cuando hay otra señal de política
    assert categorize_market("Will Democrats win the House in the 2026 midterms?") == "politics"


@pytest.mark.parametrize("q", ["Eritrea invades Ethiopia by October 31?",
                               "Will China invade Taiwan by 2027?"])
def test_invade_es_geopolitica(q):
    assert categorize_market(q) == "geopolitics"


@pytest.mark.parametrize("q", [
    "W50 Heraklion: Ipek Oz vs Alisa Oktiabreva",
    "W15 Chisinau: Astrid Wanja Brune Olsen vs Felitsata Dorofeeva-Rybas",
])
def test_itf_femenino_es_partido_de_tenis(q):
    assert categorize_market(q) == "sports"
    assert _TENNIS_RE.search(q)


@pytest.mark.parametrize("q", [
    "West Indies Tour of India T20s: India vs West Indies",
    "Sri Lanka Tour of Pakistan T20s: Pakistan vs Sri Lanka",
])
def test_t20s_es_partido_de_cricket(q):
    assert categorize_market(q) == "sports"
    assert _CRICKET_RE.search(q)


def test_groq_analyzer_usa_los_mismos_patrones():
    import groq_analyzer
    assert groq_analyzer._TENNIS_RE is _TENNIS_RE and groq_analyzer._CRICKET_RE is _CRICKET_RE
    assert groq_analyzer.CATEGORIZER_VERSION == CATEGORIZER_VERSION == 2


@pytest.mark.parametrize("q", ["Will the Seattle Storm win 2026 WNBA?", "Bitcoin above $150k by December?",
                               "Will Kanye West release an album in 2026?"])
def test_sin_falsos_positivos_de_los_patrones_nuevos(q):
    assert not _TENNIS_RE.search(q) and not _CRICKET_RE.search(q)


@pytest.mark.parametrize("q", [
    "M15 Sharm ElSheikh: Artur Kukasian vs Idriss Haddouch",
    "M25 East Lansing Men: Alexander Okonkwo vs Strong Kirchheimer",
])
def test_itf_masculino_es_partido_de_tenis(q):
    assert categorize_market(q) == "sports"
    assert _TENNIS_RE.search(q)


@pytest.mark.parametrize("q", [
    "Will Iliana Iotova win the 2026 Bulgarian presidential first round by at least 40%?",
    "Will Andrey Gyurov win the 2026 Bulgarian presidential first round by 20%-30%?",
])
def test_presidential_es_politica(q):
    assert categorize_market(q) == "politics"


def test_m15_m25_no_casan_dentro_de_palabras():
    assert not _TENNIS_RE.search("Will the AM15X token launch?")
    assert not _TENNIS_RE.search("Highway M250 closure by Friday?")
