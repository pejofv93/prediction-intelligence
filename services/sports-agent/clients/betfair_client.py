"""
Betfair Exchange client — Delayed App Key (1.0-DELAY).

Delayed Key limitations:
  - EX_BEST_OFFERS only (top 3 back/lay per runner, ~1s delay)
  - No totalMatched, no EX_ALL_OFFERS
  - Liquidity proxied via sum(availableToBack sizes) + relative spread
"""
import logging
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SSO_URL = "https://identitysso.betfair.es/api"
_API_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"

_MIN_BACK_SIZE = 100.0   # €  — sum of top-3 back sizes per runner
_MAX_SPREAD_PCT = 0.05   # 5% relative spread: (best_lay - best_back) / best_back


def _normalize(name: str) -> str:
    """Lowercase, strip accents and punctuation, remove noisy club suffixes."""
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"[^\w\s]", "", name.lower())
    for noise in ("fc", "cf", "afc", "fk", "sc", "united", "city", "athletic", "club"):
        name = re.sub(rf"\b{noise}\b", "", name)
    return name.strip()


class BetfairClient:

    def __init__(self) -> None:
        self._username  = os.environ.get("BETFAIR_USERNAME", "")
        self._password  = os.environ.get("BETFAIR_PASSWORD", "")
        self._app_key   = os.environ.get("BETFAIR_APP_KEY", "")
        self._session_token: str | None = None
        self._token_ts: float = 0.0
        self._TOKEN_TTL = 3 * 3600  # 3h (Betfair hard-expires tokens at 4h)

    # ── Session ───────────────────────────────────────────────────────────────

    async def _login(self) -> str:
        async with httpx.AsyncClient(verify=False) as client:  # Norton MITM workaround (local only)
            resp = await client.post(
                f"{_SSO_URL}/login",
                data={"username": self._username, "password": self._password},
                headers={
                    "Accept":        "application/json",
                    "Content-Type":  "application/x-www-form-urlencoded",
                    "X-Application": self._app_key,
                },
                timeout=15.0,
            )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "SUCCESS":
            raise RuntimeError(f"Betfair login failed: {body.get('error')!r}")
        self._session_token = body["token"]
        self._token_ts = time.monotonic()
        logger.info("Betfair login OK")
        return self._session_token

    async def keep_alive(self) -> None:
        """Call every ~15 min to prevent inactivity expiry (20 min limit)."""
        if not self._session_token:
            return
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                f"{_SSO_URL}/keepAlive",
                headers={
                    "Accept":         "application/json",
                    "X-Application":  self._app_key,
                    "X-Authentication": self._session_token,
                },
                timeout=10.0,
            )
        body = resp.json()
        if body.get("status") == "SUCCESS":
            self._token_ts = time.monotonic()
        else:
            logger.warning("Betfair keep-alive failed %s — will re-login on next call", body)
            self._session_token = None

    async def _get_token(self) -> str:
        if not self._session_token or (time.monotonic() - self._token_ts) > self._TOKEN_TTL:
            return await self._login()
        return self._session_token

    # ── Raw JSON-RPC call ────────────────────────────────────────────────────

    async def _call(self, method: str, params: dict) -> Any:
        token = await self._get_token()
        payload = [{
            "jsonrpc": "2.0",
            "method":  f"SportsAPING/v1.0/{method}",
            "params":  params,
            "id":      1,
        }]
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                _API_URL,
                json=payload,
                headers={
                    "Accept":           "application/json",
                    "Content-Type":     "application/json",
                    "X-Application":    self._app_key,
                    "X-Authentication": token,
                },
                timeout=15.0,
            )
        resp.raise_for_status()
        item = resp.json()[0]
        if "error" in item:
            raise RuntimeError(f"Betfair {method} error: {item['error']}")
        return item.get("result")

    # ── Football helpers ─────────────────────────────────────────────────────

    async def find_football_event(
        self, home: str, away: str, match_date: date
    ) -> str | None:
        """
        Returns Betfair eventId matching home v away on match_date, or None.
        Searches the full UTC day to absorb timezone offsets.
        """
        from_dt = datetime(match_date.year, match_date.month, match_date.day, tzinfo=timezone.utc)
        to_dt   = from_dt + timedelta(hours=23, minutes=59)

        events = await self._call("listEvents", {
            "filter": {
                "eventTypeIds": ["1"],
                "marketStartTime": {
                    "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to":   to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            }
        }) or []

        h_norm = _normalize(home)
        a_norm = _normalize(away)

        for item in events:
            name   = item.get("event", {}).get("name", "")
            parts  = re.split(r"\sv\s", name, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) != 2:
                continue
            e_home = _normalize(parts[0])
            e_away = _normalize(parts[1])
            if (h_norm in e_home or e_home in h_norm) and \
               (a_norm in e_away or e_away in a_norm):
                event_id = item["event"]["id"]
                logger.info("Betfair event found: %r → %s", name, event_id)
                return event_id

        logger.warning("Betfair: event not found for %s v %s on %s", home, away, match_date)
        return None

    async def get_match_odds_market(self, event_id: str) -> dict | None:
        """
        Returns {marketId, marketName, runners: [{selectionId, runnerName}]}
        or None if no MATCH_ODDS market exists for the event.
        """
        markets = await self._call("listMarketCatalogue", {
            "filter": {
                "eventIds":        [event_id],
                "marketTypeCodes": ["MATCH_ODDS"],
            },
            "marketProjection": ["RUNNER_DESCRIPTION", "EVENT"],
            "maxResults": 1,
        }) or []

        if not markets:
            logger.warning("Betfair: no MATCH_ODDS market for event %s", event_id)
            return None

        m = markets[0]
        return {
            "marketId":   m["marketId"],
            "marketName": m.get("marketName", ""),
            "runners": [
                {"selectionId": r["selectionId"], "runnerName": r["runnerName"]}
                for r in m.get("runners", [])
            ],
        }

    async def get_market_book(self, market_id: str) -> list[dict]:
        """
        Returns runner list with EX_BEST_OFFERS prices.
        Each entry: {selectionId, status, ex: {availableToBack, availableToLay}}
        """
        books = await self._call("listMarketBook", {
            "marketIds":        [market_id],
            "priceProjection":  {"priceData": ["EX_BEST_OFFERS"]},
        }) or []

        return books[0].get("runners", []) if books else []

    # ── Liquidity filter ─────────────────────────────────────────────────────

    def is_liquid(self, runner: dict) -> tuple[bool, dict]:
        """
        Liquidity proxy for Delayed Key (no totalMatched available).

        Passes when:
          1. sum(availableToBack sizes) >= 100€  → enough depth for reliable price
          2. (best_lay - best_back) / best_back  < 5%  → tight spread

        Returns (liquid: bool, metrics: dict).
        """
        ex    = runner.get("ex", {})
        backs = ex.get("availableToBack", [])
        lays  = ex.get("availableToLay",  [])

        total_back_size = sum(p["size"] for p in backs)
        best_back = backs[0]["price"] if backs else 0.0
        best_lay  = lays[0]["price"]  if lays  else 0.0

        if best_back and best_lay:
            spread_pct = (best_lay - best_back) / best_back
        else:
            spread_pct = 999.0

        liquid = (total_back_size >= _MIN_BACK_SIZE) and (spread_pct < _MAX_SPREAD_PCT)

        return liquid, {
            "best_back":       best_back,
            "best_lay":        best_lay,
            "total_back_size": round(total_back_size, 2),
            "spread_pct":      round(spread_pct * 100, 2),
            "liquid":          liquid,
        }
