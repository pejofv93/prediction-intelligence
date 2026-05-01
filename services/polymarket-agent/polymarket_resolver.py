"""
polymarket_resolver.py — Resuelve shadow_trades de Polymarket con resultado real.

Flujo:
  1. GET gamma-api /markets?closed=true  → top-100 por volumen
  2. GET gamma-api /markets/{id}         → lookup directo para alertados sin resultado
     que no aparecieron en el top-100 (mercados con poco volumen al cierre)
  → determina outcome (outcomePrices[0] > 0.9 → YES, < 0.1 → NO)
  → compara recommendation vs outcome → win/loss
  → update_trade_result(shadow_trade_id, win|loss)
  → actualiza poly_predictions con result + resolved_at

Se ejecuta diariamente a las 03:00 UTC via polymarket-resolve.yml.
"""
import json
import logging
from datetime import datetime, timezone

import httpx

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.firestore_client import col
from shared.shadow_engine import update_trade_result

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
_HTTP_TIMEOUT = 20.0
_MAX_DIRECT_LOOKUPS = 20

_YES_THRESHOLD = 0.9
_NO_THRESHOLD = 0.1


def _determine_outcome(outcome_prices: list) -> str | None:
    """
    Devuelve "YES" | "NO" | None si el mercado aún no está resuelto.
    outcome_prices[0] es el precio final de YES.
    """
    if not outcome_prices:
        return None
    try:
        price_yes_final = float(outcome_prices[0])
    except (ValueError, TypeError):
        return None
    if price_yes_final > _YES_THRESHOLD:
        return "YES"
    if price_yes_final < _NO_THRESHOLD:
        return "NO"
    return None  # Entre 0.1-0.9: no resuelto aún


def _trade_result(recommendation: str, outcome: str) -> str | None:
    """
    Mapea recommendation + outcome a "win" | "loss" | None (WATCH/PASS → skip).
    BUY_YES + YES → win
    BUY_YES + NO  → loss
    BUY_NO  + NO  → win
    BUY_NO  + YES → loss
    """
    if recommendation == "BUY_YES":
        return "win" if outcome == "YES" else "loss"
    if recommendation == "BUY_NO":
        return "win" if outcome == "NO" else "loss"
    return None  # WATCH, PASS o vacío → no se apostó


async def _resolve_market(market_id: str, raw: dict, now: datetime) -> tuple[int, int, int, int]:
    """
    Procesa un mercado ya obtenido de Gamma API.
    Returns (resolved, skipped_no_pred, skipped_unresolved, errors).
    """
    try:
        try:
            pred_doc = col("poly_predictions").document(market_id).get()
            if not pred_doc.exists:
                return 0, 1, 0, 0
            pred_data = pred_doc.to_dict()
        except Exception:
            logger.error("POLY_RESOLVE: error leyendo poly_predictions/%s", market_id, exc_info=True)
            return 0, 0, 0, 1

        if not pred_data.get("alerted"):
            return 0, 1, 0, 0

        if pred_data.get("result") in ("win", "loss"):
            return 0, 0, 0, 0  # ya resuelto en ejecución anterior

        recommendation = str(pred_data.get("recommendation") or "")
        if not recommendation or recommendation in ("WATCH", "PASS"):
            return 0, 1, 0, 0

        outcome_prices_raw = raw.get("outcomePrices") or []
        if isinstance(outcome_prices_raw, str):
            try:
                outcome_prices_raw = json.loads(outcome_prices_raw)
            except Exception:
                outcome_prices_raw = []

        outcome = _determine_outcome(outcome_prices_raw)
        if outcome is None:
            logger.debug(
                "POLY_RESOLVE: %s — outcomePrices sin resolver aun (%s)",
                market_id, outcome_prices_raw,
            )
            return 0, 0, 1, 0

        result = _trade_result(recommendation, outcome)
        if result is None:
            return 0, 1, 0, 0

        logger.info(
            "POLY_RESOLVE: %s — rec=%s vs outcome=%s → %s",
            market_id, recommendation, outcome, result,
        )

        try:
            shadow_docs = list(
                col("shadow_trades")
                .where(filter=FieldFilter("signal_id", "==", market_id))
                .where(filter=FieldFilter("source", "==", "polymarket"))
                .limit(1)
                .stream()
            )
        except Exception:
            logger.error("POLY_RESOLVE: error buscando shadow_trade para %s", market_id, exc_info=True)
            return 0, 0, 0, 1

        if shadow_docs:
            trade_id = shadow_docs[0].id
            try:
                await update_trade_result(trade_id, result)
            except Exception:
                logger.error(
                    "POLY_RESOLVE: error en update_trade_result trade_id=%s", trade_id, exc_info=True,
                )
                return 0, 0, 0, 1
        else:
            logger.warning(
                "POLY_RESOLVE: %s — sin shadow_trade pendiente, solo actualizando prediccion",
                market_id,
            )

        try:
            col("poly_predictions").document(market_id).update({
                "result": result,
                "outcome": outcome,
                "resolved_at": now,
            })
        except Exception:
            logger.error(
                "POLY_RESOLVE: error actualizando poly_predictions/%s", market_id, exc_info=True,
            )
            return 0, 0, 0, 1

        return 1, 0, 0, 0

    except Exception:
        logger.error("POLY_RESOLVE: error inesperado procesando %s", market_id, exc_info=True)
        return 0, 0, 0, 1


async def resolve_closed_markets() -> dict:
    """
    Resuelve shadow_trades pendientes de Polymarket contra resultados reales.

    Paso 1: top-100 mercados cerrados por volumen (cubre mercados populares).
    Paso 2: lookup directo por ID para alertados que no aparecen en el top-100
            (mercados con poco volumen al cerrarse, como BTC April dips).

    Devuelve {resolved, skipped_no_pred, skipped_unresolved, errors}.
    """
    now = datetime.now(timezone.utc)
    resolved = skipped_no_pred = skipped_unresolved = errors = 0
    processed_ids: set[str] = set()

    # --- Paso 1: Top-100 cerrados por volumen ---
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed": "true",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": "100",
                },
            )
        if resp.status_code != 200:
            logger.error("POLY_RESOLVE: Gamma API respondio %d", resp.status_code)
            return {"resolved": 0, "skipped_no_pred": 0, "skipped_unresolved": 0, "errors": 1}
        raw_data = resp.json()
        raw_markets = (
            raw_data if isinstance(raw_data, list)
            else raw_data.get("markets", raw_data.get("data", []))
        )
    except Exception:
        logger.error("POLY_RESOLVE: error de red", exc_info=True)
        return {"resolved": 0, "skipped_no_pred": 0, "skipped_unresolved": 0, "errors": 1}

    logger.info("POLY_RESOLVE: %d mercados cerrados obtenidos de Gamma API", len(raw_markets))

    for raw in raw_markets:
        try:
            market_id = str(raw.get("id") or raw.get("market_id") or "")
            if not market_id:
                continue
            processed_ids.add(market_id)
            r, sn, su, e = await _resolve_market(market_id, raw, now)
            resolved += r
            skipped_no_pred += sn
            skipped_unresolved += su
            errors += e
        except Exception:
            logger.error("POLY_RESOLVE: error procesando mercado %s", raw.get("id"), exc_info=True)
            errors += 1

    # --- Paso 2: Lookup directo para alertados no encontrados en top-100 ---
    try:
        pending_docs = list(col("poly_predictions").where(filter=FieldFilter("alerted", "==", True)).stream())
    except Exception:
        logger.error("POLY_RESOLVE: error consultando poly_predictions pendientes", exc_info=True)
        pending_docs = []

    missing_ids = [
        doc.id for doc in pending_docs
        if doc.id not in processed_ids
        and doc.to_dict().get("result") not in ("win", "loss")
    ][:_MAX_DIRECT_LOOKUPS]

    if missing_ids:
        logger.info(
            "POLY_RESOLVE: %d mercados alertados sin resultado no estaban en top-100 — lookup directo",
            len(missing_ids),
        )
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            for market_id in missing_ids:
                try:
                    resp = await client.get(f"{GAMMA_API}/markets/{market_id}")
                    if resp.status_code == 404:
                        logger.debug(
                            "POLY_RESOLVE: %s — 404 en Gamma (mercado aún activo o eliminado)",
                            market_id,
                        )
                        skipped_unresolved += 1
                        continue
                    if resp.status_code != 200:
                        logger.warning(
                            "POLY_RESOLVE: %s — Gamma respondio %d en lookup directo",
                            market_id, resp.status_code,
                        )
                        errors += 1
                        continue
                    raw = resp.json()
                    if isinstance(raw, list):
                        raw = raw[0] if raw else {}
                    r, sn, su, e = await _resolve_market(market_id, raw, now)
                    resolved += r
                    skipped_no_pred += sn
                    skipped_unresolved += su
                    errors += e
                except Exception:
                    logger.error(
                        "POLY_RESOLVE: error en lookup directo %s", market_id, exc_info=True,
                    )
                    errors += 1

    logger.info(
        "POLY_RESOLVE: completado — resolved=%d skipped_no_pred=%d skipped_unresolved=%d errors=%d",
        resolved, skipped_no_pred, skipped_unresolved, errors,
    )
    return {
        "resolved": resolved,
        "skipped_no_pred": skipped_no_pred,
        "skipped_unresolved": skipped_unresolved,
        "errors": errors,
    }
