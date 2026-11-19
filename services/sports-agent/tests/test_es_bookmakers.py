"""shared/es_bookmakers.py: lista única de casas con licencia en España."""
from shared import es_bookmakers as es


def test_nombres_odds_api_io_es_son_licenciados():
    for name in ("Bwin ES", "Codere ES", "Betfair ES", "888Sport ES", "Betway ES",
                 "Winamax ES", "LeoVegas ES", "PokerStars ES", "PAF ES", "Casumo ES",
                 "Retabet ES", "Luckia ES", "DAZN Bet ES", "Bet365 ES"):
        assert es.is_es_licensed(name), name


def test_claves_normalizadas_del_cliente_tambien():
    # odds_apiio_client guarda bkm_key = nombre.lower().replace(" ", "_")
    assert es.is_es_licensed("bwin_es")
    assert es.is_es_licensed("888sport_es")
    assert es.is_es_licensed("bwin.es")
    assert es.lookup("dazn_bet_es").slug == "daznbet_es"


def test_marcas_genericas_y_casas_sin_licencia_no():
    for name in ("Unibet", "unibet_eu", "pinnacle", "gtbets", "onexbet", "bet365", "Bwin",
                 "williamhill", "sport888", "Codere MX", "Sportium IT", "Winamax FR",
                 "Betfair Exchange", "betfair_ex_eu", "Luckia", "", None):
        assert not es.is_es_licensed(name), name


def test_betano_fuera_sin_web_de_juego_en_la_dgoj():
    # Kaizen Gaming XYZ figura en la DGOJ pero "No posee un sitio web que ofrezca juego".
    for name in ("Betano", "Betano ES", "betano_es", "betano.es"):
        assert not es.is_es_licensed(name), name
        assert not es.is_es_sportsbook(name), name


def test_casas_solo_espanolas_por_nombre_comercial():
    assert es.is_es_licensed("Sportium")
    assert es.is_es_licensed("Kirolbet")
    assert es.is_es_licensed("marcaapuestas.es")


def test_exchange_unico_betfair_es():
    exchanges = [b for b in es.ES_BOOKMAKERS if b.kind == "exchange"]
    assert [b.slug for b in exchanges] == ["betfair_es_exchange"]
    assert es.is_es_exchange("betfair_es_exchange")
    assert not es.is_es_exchange("betfair_ex_eu")
    assert not es.is_es_exchange("Betfair ES")          # la casa, no el exchange
    assert es.is_non_es_exchange("betfair_ex_eu")
    assert es.is_non_es_exchange("betfair_ex_uk")
    assert not es.is_non_es_exchange("betfair_es_exchange")


def test_slugs_unicos_y_campos():
    slugs = [b.slug for b in es.ES_BOOKMAKERS]
    assert len(slugs) == len(set(slugs))
    for b in es.ES_BOOKMAKERS:
        assert b.domain.endswith(".es")
        d = b.to_dict()
        assert set(d) >= {"slug", "name", "domain", "kind", "api_ids", "has_api"}


def test_api_gratuita():
    free = {b.slug for b in es.free_api_bookmakers()}
    assert "bwin_es" in free and "codere_es" in free
    assert "bet365_es" not in free          # solo pago
    assert "sportium" not in free           # sin API
    assert es.odds_api_io_name("bwin_es") == "Bwin ES"
    assert es.odds_api_io_name("sportium") is None


def test_version_es_de_marca_generica():
    assert es.es_counterpart("bet365").slug == "bet365_es"
    assert es.es_counterpart("williamhill").slug == "williamhill_es"
    assert es.es_counterpart("Bwin ES") is None
    assert es.es_counterpart("pinnacle") is None


def test_motivo_solo_referencia():
    assert es.reference_only_reason("bwin_es", "betfair_es_exchange") is None
    r = es.reference_only_reason("pinnacle", "betfair_ex_eu")
    assert "casa sin licencia en España (pinnacle)" in r
    assert "exchange sin licencia en España" in r
    assert es.reference_only_reason("Codere ES") is None
    assert es.reference_only_reason("unibet") == "solo referencia: casa sin licencia en España (unibet)"
