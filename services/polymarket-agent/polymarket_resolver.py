"""
polymarket_resolver.py — Resuelve shadow_trades de Polymarket con resultado real.

Flujo:
  GET gamma-api /markets?closed=true  → mercados ya cerrados
  → busca en poly_predictions market_id + alerted=True sin result todavía
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

from shared.firestore_client import col
from shared.shadow_engine import update_trade_result

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
_HTTP_TIMEOUT = 20.0

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


async def resolve_closed_markets() -> dict:
    """
    Resuelve shadow_trades pendientes de Polymarket contra resultados reales.

    Devuelve {resolved, skipped_no_pred, skipped_unresolved, errors}.
    """
    now = datetime.now(timezone.utc)
    resolved = skipped_no_pred = skipped_unresolved = errors = 0

    # --- 1. Obtener mercados cerrados de Gamma API ---
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

            # --- 2. Buscar predicción alerted=True sin result ya asignado ---
            try:
                pred_doc = col("poly_predictions").document(market_id).get()
                if not pred_doc.exists:
                    skipped_no_pred += 1
                    continue
                pred_data = pred_doc.to_dict()
            except Exception:
                logger.error("POLY_RESOLVE: error leyendo poly_predictions/%s", market_id, exc_info=True)
                errors += 1
                continue

            if not pred_data.get("alerted"):
                skipped_no_pred += 1
                continue

            # Ya resuelto en una ejecución anterior
            if pred_data.get("result") in ("win", "loss"):
                continue

            recommendation = str(pred_data.get("recommendation") or "")
            if not recommendation or recommendation in ("WATCH", "PASS"):
                skipped_no_pred += 1
                continue

            # --- 3. Determinar outcome desde outcomePrices ---
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
                skipped_unresolved += 1
                continue

            # --- 4. Calcular win/loss ---
            result = _trade_result(recommendation, outcome)
            if result is None:
                skipped_no_pred += 1
                continue

            logger.info(
                "POLY_RESOLVE: %s — rec=%s vs outcome=%s → %s",
                market_id, recommendation, outcome, result,
            )

            # --- 5. Actualizar shadow_trade si existe ---
            try:
                shadow_docs = list(
                    col("shadow_trades")
                    .where("signal_id", "==", market_id)
                    .where("source", "==", "polymarket")
                    .limit(1)
                    .stream()
                )
            except Exception:
                logger.error("POLY_RESOLVE: error buscando shadow_trade para %s", market_id, exc_info=True)
                errors += 1
                continue

            if shadow_docs:
                trade_id = shadow_docs[0].id
                try:
                    await update_trade_result(trade_id, result)
                except Exception:
                    logger.error(
                        "POLY_RESOLVE: error en update_trade_result trade_id=%s", trade_id, exc_info=True,
                    )
                    errors += 1
                    continue
            else:
                logger.warning(
                    "POLY_RESOLVE: %s — sin shadow_trade pendiente, solo actualizando prediccion",
                    market_id,
                )

            # --- 6. Marcar poly_predictions como resuelto ---
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
                errors += 1
                continue

            resolved += 1

        except Exception:
            logger.error("POLY_RESOLVE: error procesando mercado %s", raw.get("id"), exc_info=True)
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
