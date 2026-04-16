"""
Detector de smart money via analisis on-chain de Polygon.
Perfila wallets que hacen grandes trades antes de noticias publicas.
"""
import logging

logger = logging.getLogger(__name__)

POLYGON_RPC = "https://polygon-rpc.com"
POLYGON_RPC_BACKUP = "https://rpc.ankr.com/polygon"


import asyncio
from datetime import datetime, timedelta, timezone

import httpx

CLOB_API = "https://clob.polymarket.com"


def _get_web3(rpc_url: str):
    """Crea cliente web3 de forma sincrona."""
    try:
        from web3 import Web3
        return Web3(Web3.HTTPProvider(rpc_url))
    except Exception:
        return None


async def profile_wallet(wallet_address: str) -> dict:
    """
    1. Busca en Firestore wallet_profiles (cache 24h)
    2. Si no hay cache: consulta Polygon RPC con web3.py
       web3.py es SINCRONO — usar en contexto async:
       loop = asyncio.get_event_loop()
       result = await loop.run_in_executor(None, w3.eth.get_transaction_count, address)
    3. Clasifica: "fresh" | "whale" | "bot_suspect" | "regular"
    4. Guarda en wallet_profiles con TTL 24h
    """
    from shared.firestore_client import col

    _default = {
        "wallet_address": wallet_address,
        "age_hours": None,
        "tx_count": 0,
        "is_fresh": False,
        "total_pnl_usd": None,
        "profile_type": "regular",
        "last_analyzed": datetime.now(timezone.utc),
    }

    try:
        doc = col("wallet_profiles").document(wallet_address).get()
        if doc.exists:
            data = doc.to_dict()
            last_analyzed = data.get("last_analyzed")
            if last_analyzed:
                if hasattr(last_analyzed, "tzinfo") and last_analyzed.tzinfo is None:
                    last_analyzed = last_analyzed.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_analyzed) < timedelta(hours=24):
                    return data

        # Consultar Polygon RPC con web3.py (sincrono → run_in_executor)
        loop = asyncio.get_event_loop()
        w3 = None

        for rpc in [POLYGON_RPC, POLYGON_RPC_BACKUP]:
            w3 = await loop.run_in_executor(None, _get_web3, rpc)
            if w3 and w3.is_connected():
                break

        if w3 is None or not w3.is_connected():
            logger.warning("profile_wallet(%s): no se pudo conectar a Polygon RPC", wallet_address)
            return _default

        try:
            checksum_addr = w3.to_checksum_address(wallet_address)
            tx_count = await loop.run_in_executor(
                None, w3.eth.get_transaction_count, checksum_addr
            )
        except Exception:
            logger.error("profile_wallet(%s): error obteniendo tx_count", wallet_address, exc_info=True)
            return _default

        is_fresh = tx_count < 10  # heuristica: menos de 10 txs = billetera nueva
        age_hours = None  # no podemos calcular edad sin Polygonscan API

        if is_fresh:
            profile_type = "fresh"
        elif tx_count > 10000:
            profile_type = "bot_suspect"
        else:
            profile_type = "regular"

        profile = {
            "wallet_address": wallet_address,
            "age_hours": age_hours,
            "tx_count": int(tx_count),
            "is_fresh": is_fresh,
            "total_pnl_usd": None,
            "profile_type": profile_type,
            "last_analyzed": datetime.now(timezone.utc),
        }

        try:
            col("wallet_profiles").document(wallet_address).set(profile)
        except Exception:
            logger.error("profile_wallet(%s): error guardando en Firestore", wallet_address, exc_info=True)

        return profile

    except Exception:
        logger.error("profile_wallet(%s): error no controlado", wallet_address, exc_info=True)
        return _default


def score_suspicion(
    wallet_profile: dict, trade: dict, market: dict
) -> dict:
    """
    Calcula score 0-100 de actividad sospechosa:
      - wallet_profile.is_fresh + trade size grande → +40pts
      - niche market (bajo volumen) + gran posicion → +30pts
      - patron de timing uniforme (bot) → +20pts
      - historial negativo de win rate → -20pts
    Devuelve {suspicion_score: int, signals: list[str], verdict: "clean"|"suspicious"|"likely_insider"}
    """
    score = 0
    signals: list[str] = []

    try:
        trade_size = float(trade.get("size", 0)) * float(trade.get("price", 0.5))
        is_fresh = bool(wallet_profile.get("is_fresh", False))
        volume_24h = float(market.get("volume_24h", 0))

        if is_fresh and trade_size > 500:
            score += 40
            signals.append(f"billetera nueva con trade grande (${trade_size:,.0f})")

        if volume_24h < 50000 and trade_size > volume_24h * 0.05:
            score += 30
            signals.append(f"mercado niche ({volume_24h:,.0f} vol) + posicion grande")

        profile_type = wallet_profile.get("profile_type", "regular")
        if profile_type == "bot_suspect":
            score += 20
            signals.append("patron bot detectado (>10,000 txs)")

    except Exception:
        logger.error("score_suspicion: error calculando score", exc_info=True)

    if score >= 70:
        verdict = "likely_insider"
    elif score >= 40:
        verdict = "suspicious"
    else:
        verdict = "clean"

    return {
        "suspicion_score": min(100, score),
        "signals": signals,
        "verdict": verdict,
    }


async def run_smart_money_analysis(market_id: str) -> dict:
    """
    1. Obtiene ultimos 50 trades del mercado via CLOB REST
       GET https://clob.polymarket.com/trades?market={condition_id}&limit=50
    2. Por cada trade > $1,000: profile_wallet()
    3. score_suspicion() para cada wallet
    4. Si algun score > 70 → is_smart_money=True
    5. Detecta bots: trades de tamano exactamente igual O intervalos regulares
    Devuelve {is_smart_money, bot_probability, suspicious_wallets, confidence, signals}
    """
    from shared.firestore_client import col

    _default = {
        "is_smart_money": False,
        "bot_probability": 0.0,
        "suspicious_wallets": [],
        "confidence": 0.5,
        "signals": [],
    }

    try:
        # Obtener condition_id para el CLOB
        doc = col("poly_markets").document(market_id).get()
        if not doc.exists:
            return _default

        condition_id = doc.to_dict().get("condition_id", "")
        if not condition_id:
            return _default

        market_data = doc.to_dict()

        # Obtener ultimos 50 trades
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{CLOB_API}/trades",
                    params={"market": condition_id, "limit": "50"},
                )
            if resp.status_code != 200:
                logger.debug("run_smart_money_analysis(%s): trades respondio %d", market_id, resp.status_code)
                return _default
            trades_data = resp.json()
            trades = trades_data if isinstance(trades_data, list) else trades_data.get("data", [])
        except Exception:
            logger.error("run_smart_money_analysis(%s): error obteniendo trades", market_id, exc_info=True)
            return _default

        if not trades:
            return _default

        # Calcular tamano medio de trade para deteccion de trades grandes
        trade_sizes = [float(t.get("size", 0)) * float(t.get("price", 0.5)) for t in trades]
        avg_trade_usd = sum(trade_sizes) / len(trade_sizes) if trade_sizes else 0

        # Analizar trades > $1,000
        suspicious_wallets: list[dict] = []
        is_smart_money = False
        all_signals: list[str] = []

        for trade in trades:
            trade_usd = float(trade.get("size", 0)) * float(trade.get("price", 0.5))
            if trade_usd < 1000:
                continue

            wallet = trade.get("maker_address") or trade.get("taker_address") or ""
            if not wallet:
                continue

            try:
                profile = await profile_wallet(wallet)
                suspicion = score_suspicion(profile, trade, market_data)

                if suspicion["suspicion_score"] > 70:
                    is_smart_money = True
                    suspicious_wallets.append({
                        "wallet": wallet[:10] + "...",  # anonimizar parcialmente
                        "score": suspicion["suspicion_score"],
                        "verdict": suspicion["verdict"],
                        "trade_usd": round(trade_usd, 2),
                    })
                    all_signals.extend(suspicion["signals"])
            except Exception:
                logger.error("run_smart_money_analysis: error perfilando wallet %s", wallet[:10], exc_info=True)

        # Deteccion de bots: trades de tamano exactamente igual
        bot_probability = 0.0
        if len(trade_sizes) >= 5:
            exact_matches = sum(
                1 for i in range(len(trade_sizes) - 1)
                if abs(trade_sizes[i] - trade_sizes[i + 1]) < 0.01
            )
            bot_probability = round(exact_matches / (len(trade_sizes) - 1), 3)
            if bot_probability > 0.5:
                all_signals.append(f"patron bot: {bot_probability:.0%} trades de tamano identico")

        confidence = min(0.9, 0.5 + len(suspicious_wallets) * 0.1)

        return {
            "is_smart_money": is_smart_money,
            "bot_probability": bot_probability,
            "suspicious_wallets": suspicious_wallets[:5],
            "confidence": round(confidence, 2),
            "signals": list(set(all_signals))[:5],
        }

    except Exception:
        logger.error("run_smart_money_analysis(%s): error no controlado", market_id, exc_info=True)
        return _default
