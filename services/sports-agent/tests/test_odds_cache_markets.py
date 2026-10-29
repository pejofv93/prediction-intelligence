"""odds_cache: los mercados alternativos (BTTS/AH/CS) llevan su propia fecha y no reviven
cuando otra fuente refresca el 1X2 (27-29 oct). Sin red ni Firestore."""
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

from analyzers.value_bet_engine import _fresh_cached_all_markets, _odds_cache_extra

NOW = datetime(2026, 10, 29, 6, 53, tzinfo=timezone.utc)
TTL = timedelta(hours=24)
AM = {"btts": {"yes_odds": 1.4}, "correct_score": {}}


def test_sin_fecha_propia_no_se_usan():
    # Docs anteriores al cambio: solo fetched_at (renovado por The Odds API)
    assert _fresh_cached_all_markets({"all_markets": AM, "fetched_at": NOW}, NOW, TTL) is None


def test_con_fecha_propia_dentro_y_fuera_del_ttl():
    fresh = {"all_markets": AM, "all_markets_fetched_at": NOW - timedelta(hours=3)}
    old = {"all_markets": AM, "all_markets_fetched_at": NOW - timedelta(hours=60)}
    assert _fresh_cached_all_markets(fresh, NOW, TTL) == AM
    assert _fresh_cached_all_markets(old, NOW, TTL) is None
    assert _fresh_cached_all_markets({"all_markets": AM, "all_markets_fetched_at": "2026-10-29T01:00:00Z"},
                                     NOW, TTL) == AM


def test_fuente_sin_mercados_borra_los_anteriores():
    extra = _odds_cache_extra({"source": "theoddsapi", "home_odds": 2.9}, NOW, existing=True)
    assert extra["source"] == "theoddsapi"
    assert extra["all_markets"] is firestore.DELETE_FIELD
    assert extra["all_markets_fetched_at"] is firestore.DELETE_FIELD


def test_doc_nuevo_sin_mercados_no_lleva_delete():
    extra = _odds_cache_extra({"source": "theoddsapi"}, NOW, existing=False)
    assert "all_markets" not in extra and "all_markets_fetched_at" not in extra


def test_fuente_con_mercados_guarda_su_fecha():
    extra = _odds_cache_extra({"source": "oddsapiio", "all_markets": AM}, NOW, existing=True)
    assert extra["all_markets"] == AM and extra["all_markets_fetched_at"] == NOW
