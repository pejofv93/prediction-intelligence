"""
matched/odds_lay.py — fetch de back+lay desde The Odds API.

Una llamada por sport_key:
  GET /v4/sports/{key}/odds?regions=eu&markets=h2h,h2h_lay&oddsFormat=decimal
devuelve, por evento:
  - h2h (back) de TODAS las casas eu
  - h2h_lay (lay) de betfair_ex_eu   ← única fuente real de lay (verificado 2026-07-04)
Coste = markets(2) × regiones(1) = 2 créditos/llamada. Presupuesto 500/mes compartido
con el value engine → doble gate: cuota real de la API + presupuesto matched reservado.

Devuelve por evento una lista de BackLayQuote (una por selección con back Y lay válidos).
"""
import logging

import httpx

from shared.config import (
    ODDS_API_KEY,
    MATCHED_LAY_ODDS_MAX,
)
from shared.api_quota_manager import quota

from .models import BackLayQuote

logger = logging.getLogger(__name__)

_BASE = "https://api.the-odds-api.com/v4/sports"
_HTTP_TIMEOUT = 20.0
_REGIONS = "eu"
_MARKETS = "h2h,h2h_lay"
_CREDITS_PER_CALL = 2   # markets(2) × regions(1)
_LAY_BOOK_PREFIX = "betfair_ex"   # betfair_ex_eu / betfair_ex_uk


def budget_ok() -> tuple[bool, str]:
    """Doble gate antes de gastar créditos: cuota real API + presupuesto matched reservado."""
    if not ODDS_API_KEY:
        return False, "ODDS_API_KEY no configurada"
    if not quota.can_call_monthly("the_odds_api"):
        return False, "The Odds API — cuota mensual agotada"
    if not quota.can_call_monthly("the_odds_api_matched"):
        return False, "presupuesto matched mensual agotado (MATCHED_MONTHLY_CREDIT_BUDGET)"
    return True, ""


def _best_backs_and_lays(event: dict) -> dict[str, dict]:
    """
    Agrega por selección: mejor back entre casas NO-exchange + lay de betfair_ex.
    Cada entrada guarda (book, odds, last_update). last_update se toma del mercado
    (o de la casa si el mercado no lo trae) para poder medir staleness del lay.
    Devuelve {selection_name: {"back": tuple|None, "lay": tuple|None}}.
    """
    agg: dict[str, dict] = {}
    for bk in event.get("bookmakers", []):
        bkey = bk.get("key", "")
        is_exchange = bkey.startswith(_LAY_BOOK_PREFIX)
        bk_lu = bk.get("last_update", "")
        for mkt in bk.get("markets", []):
            mkey = mkt.get("key")
            if mkey not in ("h2h", "h2h_lay"):
                continue
            lu = mkt.get("last_update", "") or bk_lu   # mercado > casa como fallback
            for oc in mkt.get("outcomes", []):
                name = oc.get("name")
                try:
                    price = float(oc.get("price"))
                except (TypeError, ValueError):
                    continue
                if not name or price <= 1.0:
                    continue
                slot = agg.setdefault(name, {"back": None, "lay": None})

                if mkey == "h2h" and not is_exchange:
                    # mejor back entre casas normales (el exchange no cuenta como back)
                    if slot["back"] is None or price > slot["back"][1]:
                        slot["back"] = (bkey, price, lu)
                elif mkey == "h2h_lay" and is_exchange:
                    # lay real del exchange; filtrar sentinela (1000.0 = sin lay) y absurdos
                    if 1.01 < price <= MATCHED_LAY_ODDS_MAX:
                        if slot["lay"] is None or price < slot["lay"][1]:
                            slot["lay"] = (bkey, price, lu)
    return agg


def _quotes_from_event(event: dict) -> list[BackLayQuote]:
    quotes: list[BackLayQuote] = []
    for name, slot in _best_backs_and_lays(event).items():
        back, lay = slot["back"], slot["lay"]
        if not back or not lay:
            continue
        quotes.append(BackLayQuote(
            selection=name,
            back_odds=back[1], back_bookmaker=back[0], back_last_update=back[2],
            lay_odds=lay[1], lay_bookmaker=lay[0], lay_last_update=lay[2],
        ))
    return quotes


async def fetch_event_quotes(sport_key: str) -> list[dict]:
    """
    Llama a The Odds API para un sport_key. Devuelve lista de eventos:
      {event_id, sport_key, commence_time, home_team, away_team, quotes: [BackLayQuote]}
    Solo incluye eventos con al menos una selección back+lay válida.
    Registra el consumo de créditos en ambos contadores (real + matched).
    Devuelve [] si no hay presupuesto, la liga no tiene eventos, o error transitorio.
    """
    ok, reason = budget_ok()
    if not ok:
        logger.info("matched.odds_lay: skip %s — %s", sport_key, reason)
        return []

    url = f"{_BASE}/{sport_key}/odds"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params={
                "apiKey": ODDS_API_KEY,
                "regions": _REGIONS,
                "markets": _MARKETS,
                "oddsFormat": "decimal",
            })
    except Exception:
        logger.error("matched.odds_lay: error HTTP %s", sport_key, exc_info=True)
        return []

    if resp.status_code == 404:
        logger.info("matched.odds_lay: %s sin eventos (404)", sport_key)
        return []
    if resp.status_code == 422:
        logger.warning("matched.odds_lay: %s 422 (h2h_lay no disponible en el plan)", sport_key)
        return []
    if resp.status_code in (401, 429):
        logger.warning("matched.odds_lay: %s cuota/clave (%d) — marcando agotada",
                       sport_key, resp.status_code)
        quota.track_monthly("the_odds_api", remaining=0)
        return []
    if resp.status_code != 200:
        logger.warning("matched.odds_lay: %s HTTP %d", sport_key, resp.status_code)
        return []

    remaining = resp.headers.get("x-requests-remaining")
    quota.track_monthly("the_odds_api", remaining=remaining, cost=_CREDITS_PER_CALL)
    quota.track_monthly("the_odds_api_matched", cost=_CREDITS_PER_CALL)

    events = resp.json() or []
    out: list[dict] = []
    for ev in events:
        quotes = _quotes_from_event(ev)
        if not quotes:
            continue
        out.append({
            "event_id": str(ev.get("id") or ""),
            "sport_key": sport_key,
            "commence_time": ev.get("commence_time", ""),
            "home_team": ev.get("home_team", ""),
            "away_team": ev.get("away_team", ""),
            "quotes": quotes,
        })
    logger.info("matched.odds_lay: %s → %d eventos con back+lay (%d con odds, %s créditos restantes)",
                sport_key, len(out), len(events), remaining or "?")
    return out
