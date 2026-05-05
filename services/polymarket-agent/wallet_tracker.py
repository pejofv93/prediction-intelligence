"""
Tracker de wallets inteligentes en Polymarket.
Identifica traders con win_rate > 65% y > 10 trades.

Usa data-api.polymarket.com/trades?market={conditionId} — endpoint público, sin auth.
Campos de respuesta: proxyWallet, side (BUY/SELL), outcome (Yes/No), size (USD), timestamp (unix int).
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col

logger = logging.getLogger(__name__)

_DATA_API = "https://data-api.polymarket.com"
_MIN_TRADES = 10
_MIN_WIN_RATE = 0.65
_MIN_USD = 5_000.0
_WINDOW_H = 6


async def _get_condition_id(market_id: str) -> str:
    """Lee condition_id (hex) desde poly_markets Firestore."""
    try:
        doc = col("poly_markets").document(str(market_id)).get()
        return doc.to_dict().get("condition_id", "") if doc.exists else ""
    except Exception:
        return ""


async def get_top_traders(market_id: str) -> list[dict]:
    """
    Llama data-api GET /trades?market={condition_id}&limit=100 (público).
    Agrupa por proxyWallet, estima win_rate desde outcome=Yes/BUY combos,
    y guarda smart wallets en poly_smart_wallets.
    """
    try:
        condition_id = await _get_condition_id(market_id)
        if not condition_id:
            logger.debug("get_top_traders(%s): sin condition_id", market_id)
            return []
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_DATA_API}/trades",
                params={"market": condition_id, "limit": "100"},
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            logger.debug("get_top_traders(%s): data-api %d", market_id, resp.status_code)
            return []

        raw = resp.json()
        trades = raw if isinstance(raw, list) else raw.get("data", [])
        if not trades:
            return []

        by_wallet: dict[str, dict] = {}
        for t in trades:
            addr = t.get("proxyWallet") or ""
            if not addr or len(addr) < 10:
                continue
            rec = by_wallet.setdefault(addr, {"n": 0, "wins": 0, "usd": 0.0})
            rec["n"] += 1
            rec["usd"] += float(t.get("size") or 0)
            # Estimación de win: compró YES (outcome=Yes, side=BUY) a precio < 0.45
            price = float(t.get("price") or 0)
            outcome = str(t.get("outcome") or "").lower()
            side = str(t.get("side") or "").upper()
            if side == "BUY" and outcome == "yes" and 0 < price < 0.45:
                rec["wins"] += 1

        smart: list[dict] = []
        now = datetime.now(timezone.utc)
        for addr, stats in by_wallet.items():
            if stats["n"] < _MIN_TRADES:
                continue
            win_rate = round(stats["wins"] / stats["n"], 4)
            if win_rate < _MIN_WIN_RATE:
                continue
            doc = {
                "address": addr,
                "market_id": market_id,
                "trades": stats["n"],
                "win_rate": win_rate,
                "total_usd": round(stats["usd"], 2),
                "updated_at": now,
            }
            smart.append(doc)
            try:
                col("poly_smart_wallets").document(f"{addr[:8]}_{market_id[:8]}").set(doc, merge=True)
            except Exception:
                pass

        logger.info(
            "get_top_traders(%s): %d smart wallets / %d traders",
            market_id, len(smart), len(by_wallet),
        )
        return smart

    except Exception:
        logger.warning("get_top_traders(%s): error — devolviendo vacío", market_id, exc_info=True)
        return []


async def check_wallet_activity(market_id: str, recommendation: str) -> dict:
    """
    Comprueba si alguna smart wallet conocida operó en las últimas 6h.
    Usa data-api /trades — público, sin auth.
    Si coincide con recomendación: confidence_adj = +0.10
    Si va en contra: confidence_adj = -0.10
    """
    _default = {"whale_signal": False, "confidence_adj": 0.0, "message": ""}
    try:
        docs = list(
            col("poly_smart_wallets")
            .where(filter=FieldFilter("market_id", "==", market_id))
            .limit(20)
            .stream()
        )
        if not docs:
            return _default

        known = {d.to_dict()["address"]: d.to_dict() for d in docs}

        condition_id = await _get_condition_id(market_id)
        if not condition_id:
            return _default

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_DATA_API}/trades",
                params={"market": condition_id, "limit": "50"},
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            return _default

        raw = resp.json()
        trades = raw if isinstance(raw, list) else raw.get("data", [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_WINDOW_H)

        for t in trades:
            addr = t.get("proxyWallet") or ""
            if addr not in known:
                continue

            # timestamp es unix int en data-api
            ts_raw = t.get("timestamp")
            try:
                if isinstance(ts_raw, (int, float)):
                    ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
                else:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except Exception:
                continue

            usd = float(t.get("size") or 0)
            if usd < _MIN_USD:
                continue

            # outcome field: "Yes" → YES, "No" → NO; side: BUY/SELL
            outcome = str(t.get("outcome") or "").upper()
            side = str(t.get("side") or "").upper()
            if side == "BUY":
                direction = outcome if outcome in ("YES", "NO") else "YES"
            else:
                direction = "NO" if outcome == "YES" else "YES"

            win_rate = float(known[addr].get("win_rate", 0))
            short = addr[:6] + "..." + addr[-4:]

            rec = recommendation.upper()
            if (direction == "YES" and rec == "BUY_YES") or (direction == "NO" and rec == "BUY_NO"):
                adj = 0.10
                align = "a favor"
            else:
                adj = -0.10
                align = "en contra"

            logger.info(
                "check_wallet_activity(%s): whale=%s win_rate=%.0f%% dir=%s usd=$%.0f adj=%+.2f",
                market_id, short, win_rate * 100, direction, usd, adj,
            )
            return {
                "whale_signal": True,
                "confidence_adj": adj,
                "message": (
                    f"🐋 Whale {short} ({win_rate:.0%} accuracy) "
                    f"compró {direction} ${usd:,.0f} — {align}"
                ),
            }

        return _default
    except Exception:
        logger.warning("check_wallet_activity(%s): error", market_id, exc_info=True)
        return _default
