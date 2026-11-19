"""
matched/es_odds_reader.py — lector de cuotas 1X2 SOLO de casas con licencia en España
desde odds-api.io (plan gratuito), y mejor cobertura entre casas ES para un bono.

PREPARADO, NO ACTIVO: no está en ningún cron ni endpoint, y aunque se llame no hace
ninguna petición si MATCHED_ES_READER_ENABLED no es "true" y ODDSAPIIO_ES_BOOKMAKERS no
nombra al menos una casa ES con API gratuita. Activarlo es decisión del usuario.

Límites del plan gratis de odds-api.io (compartidos con el motor de valor de Sports):
  - 500 peticiones/día, ráfaga 100/hora (el presupuesto horario lo lleva
    collectors.odds_apiio_client; este lector pide como prioridad BAJA para no quitarle
    hueco al fútbol del motor de valor).
  - SOLO 2 casas seleccionadas en la cuenta (hoy: Unibet, que no tiene licencia en España).
    Pedir una casa no seleccionada en la cuenta da 403 → antes de activar hay que elegir
    en la web de odds-api.io qué casas ES se seleccionan (y eso afecta al motor de valor).
  - Sin exchanges ni Bet365 → el lay de betfair.es no llega por aquí; la cobertura es
    dutching entre casas ES.

Una petición /odds/multi por cada 10 eventos: GET /odds/multi?eventIds=..&bookmakers=..&markets=ML
"""
from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from shared.es_bookmakers import free_api_bookmakers, lookup
from shared.matched_calc import BonusBet, CalcError, CoverLeg, calculate

logger = logging.getLogger(__name__)

MAX_BOOKMAKERS = 2           # plan gratis: máximo 2 casas por cuenta y por petición
MAX_EVENTS_PER_CALL = 10     # /odds/multi admite hasta 10 eventIds
_MARKETS = "ML"

Getter = Callable[..., Awaitable[dict | list | None]]


def reader_enabled() -> bool:
    return os.environ.get("MATCHED_ES_READER_ENABLED", "false").strip().lower() == "true"


def selected_bookmakers(raw: str | None = None) -> list[str]:
    """Nombres exactos de odds-api.io ('Bwin ES') de las casas ES elegidas en
    ODDSAPIIO_ES_BOOKMAKERS (slugs o nombres, separados por comas). Descarta lo que no sea
    casa ES con API gratuita y se queda con las 2 primeras."""
    raw = os.environ.get("ODDSAPIIO_ES_BOOKMAKERS", "") if raw is None else raw
    free = {b.slug for b in free_api_bookmakers()}
    out: list[str] = []
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        b = lookup(token)
        if b is None or b.slug not in free:
            logger.warning("es_odds_reader: '%s' descartada (no es casa ES con API gratuita)", token)
            continue
        name = b.api_ids["odds_api_io"]
        if name not in out:
            out.append(name)
    if len(out) > MAX_BOOKMAKERS:
        logger.warning("es_odds_reader: %d casas pedidas, el plan gratis admite %d — uso %s",
                       len(out), MAX_BOOKMAKERS, out[:MAX_BOOKMAKERS])
    return out[:MAX_BOOKMAKERS]


def build_odds_params(event_ids: list[str], bookmakers: list[str]) -> list[dict]:
    """Parámetros de cada petición /odds/multi (lotes de 10 eventos)."""
    ids = [str(e) for e in event_ids if e]
    return [
        {"eventIds": ",".join(ids[i:i + MAX_EVENTS_PER_CALL]),
         "bookmakers": ",".join(bookmakers), "markets": _MARKETS}
        for i in range(0, len(ids), MAX_EVENTS_PER_CALL)
    ]


def _to_float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 else None


def _ml_prices(markets) -> dict[str, float]:
    """{'1': x, 'X': y, '2': z} del mercado ML de una casa (X solo si hay empate)."""
    if not isinstance(markets, list):
        markets = [markets] if markets else []
    for mkt in markets:
        if not isinstance(mkt, dict):
            continue
        if str(mkt.get("name") or mkt.get("key") or "").strip().upper() not in ("ML", "1X2", "H2H"):
            continue
        odds = mkt.get("odds") or []
        o = odds[0] if isinstance(odds, list) and odds else odds
        if not isinstance(o, dict):
            continue
        prices = {"1": _to_float(o.get("home")), "X": _to_float(o.get("draw")),
                  "2": _to_float(o.get("away"))}
        return {k: v for k, v in prices.items() if v is not None}
    return {}


def parse_es_odds(items: list[dict]) -> list[dict]:
    """Normaliza la respuesta de /odds/multi quedándose SOLO con casas ES.
    Devuelve [{event_id, home_team, away_team, commence_time, league, outcomes,
               prices: {slug: {'1':..,'X':..,'2':..}}, ignored: [casas no ES]}]."""
    events = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        books = it.get("bookmakers") or {}
        if not isinstance(books, dict):
            continue
        prices: dict[str, dict] = {}
        ignored: list[str] = []
        for name, markets in books.items():
            b = lookup(name)
            if b is None or b.kind != "sportsbook":
                ignored.append(name)
                continue
            p = _ml_prices(markets)
            if p:
                prices[b.slug] = p
        if not prices:
            continue
        has_draw = any("X" in p for p in prices.values())
        lg = it.get("league")
        events.append({
            "event_id": str(it.get("id") or it.get("eventId") or ""),
            "home_team": it.get("home") or it.get("homeTeam") or it.get("home_team") or "",
            "away_team": it.get("away") or it.get("awayTeam") or it.get("away_team") or "",
            "commence_time": it.get("date") or it.get("commence_time") or "",
            "league": (lg.get("slug") or lg.get("name") or "") if isinstance(lg, dict) else str(lg or ""),
            "outcomes": ["1", "X", "2"] if has_draw else ["1", "2"],
            "prices": prices,
            "ignored": ignored,
        })
    return events


def best_cover(event: dict, bonus_bookmaker: str, bonus_outcome: str, kind: str,
               stake: float, refund_amount: float = 0.0, refund_value: float = 0.0) -> dict | None:
    """Mejor cobertura (dutching) entre casas ES para un bono en `bonus_bookmaker`:
    para cada otro resultado, la mejor cuota de una casa distinta a la del bono.
    None si la casa del bono no tiene cuota o falta cobertura para algún resultado."""
    b = lookup(bonus_bookmaker)
    if b is None or b.slug not in event["prices"]:
        return None
    bonus_odds = event["prices"][b.slug].get(bonus_outcome)
    if not bonus_odds:
        return None
    legs = []
    for o in event["outcomes"]:
        if o == bonus_outcome:
            continue
        cands = [(p[o], slug) for slug, p in event["prices"].items() if slug != b.slug and o in p]
        if not cands:
            return None
        odds, slug = max(cands)
        legs.append(CoverLeg(outcome=o, kind="back", odds=odds, bookmaker=slug))
    bonus = BonusBet(kind=kind, stake=stake, odds=bonus_odds, bookmaker=b.slug,
                     outcome=bonus_outcome, refund_amount=refund_amount, refund_value=refund_value)
    try:
        res = calculate(event["outcomes"], bonus, legs)
    except CalcError as e:
        logger.info("es_odds_reader: sin cobertura en %s — %s", event.get("event_id"), e)
        return None
    return res | {"event_id": event.get("event_id"),
                  "event": f"{event.get('home_team')} vs {event.get('away_team')}",
                  "commence_time": event.get("commence_time")}


async def _default_getter(path: str, params: dict) -> dict | list | None:
    """Petición real vía el cliente compartido (presupuesto horario + cuota mensual)."""
    from collectors.odds_apiio_client import _get
    return await _get(path, params, low_priority=True)


async def fetch_es_odds(event_ids: list[str], *, getter: Getter | None = None,
                        enabled: bool | None = None, bookmakers: list[str] | None = None) -> list[dict]:
    """Lee las cuotas ML de las casas ES elegidas para esos eventos y las normaliza.
    Desactivado (o sin casas ES elegidas) → [] sin ninguna petición."""
    if not (reader_enabled() if enabled is None else enabled):
        logger.info("es_odds_reader: desactivado (MATCHED_ES_READER_ENABLED) — 0 peticiones")
        return []
    # Siempre por selected_bookmakers: valida que sean casas ES con API gratis y las
    # traduce al nombre exacto de odds-api.io ('bwin_es' → 'Bwin ES').
    books = selected_bookmakers(None if bookmakers is None else ",".join(bookmakers))
    if not books:
        logger.info("es_odds_reader: ninguna casa ES elegida (ODDSAPIIO_ES_BOOKMAKERS) — 0 peticiones")
        return []
    get = getter or _default_getter
    items: list[dict] = []
    for params in build_odds_params(event_ids, books):
        data = await get("/odds/multi", params)
        if data is None:
            logger.warning("es_odds_reader: lote sin respuesta (presupuesto o error) — corto aquí")
            break
        batch = data if isinstance(data, list) else data.get("data", data.get("odds", []))
        if isinstance(batch, list):
            items.extend(batch)
    return parse_es_odds(items)
