"""
Detección de arbitraje comparando cuotas entre bookmakers.

Flujo:
  find_arbitrage(markets) → lista de oportunidades ARB
  detect_and_store_arbitrage(odds_data) → persiste en Firestore + devuelve lista
  format_arb_telegram(arb) → string formateado para enviar por Telegram
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def calculate_overround(odds_list: list[float]) -> float:
    """
    Suma de probabilidades implícitas.
    Si overround < 1.0 → existe oportunidad de arbitraje.
    """
    return sum(1.0 / o for o in odds_list if o > 1.0)


def find_arbitrage(markets: list[dict]) -> list[dict]:
    """
    Recibe lista de mercados con estructura:
      {match, home, away, league,
       bookmakers: [{name, home_odds, draw_odds, away_odds}]}

    Para cada partido:
      1. best_home = max(home_odds de todos los bookmakers)
      2. best_draw = max(draw_odds) si disponible
      3. best_away = max(away_odds)
      4. overround = 1/best_home + 1/best_away (+ 1/best_draw si existe)
      5. Si overround < 1.0: arb detectado

    Devuelve lista de dicts con detalles del arbitraje.
    """
    results: list[dict] = []

    for market in markets:
        try:
            bookmakers: list[dict] = market.get("bookmakers") or []
            if len(bookmakers) < 2:
                continue

            best_home_odds: float = 0.0
            best_home_book: str = ""
            best_away_odds: float = 0.0
            best_away_book: str = ""
            best_draw_odds: float = 0.0
            best_draw_book: str = ""
            has_draw = False

            for bm in bookmakers:
                name = bm.get("name", "unknown")

                ho = float(bm.get("home_odds") or 0)
                ao = float(bm.get("away_odds") or 0)
                do = float(bm.get("draw_odds") or 0)

                if ho > best_home_odds:
                    best_home_odds = ho
                    best_home_book = name
                if ao > best_away_odds:
                    best_away_odds = ao
                    best_away_book = name
                if do > 1.0:
                    has_draw = True
                    if do > best_draw_odds:
                        best_draw_odds = do
                        best_draw_book = name

            if best_home_odds <= 1.0 or best_away_odds <= 1.0:
                continue

            odds_for_overround: list[float] = [best_home_odds, best_away_odds]
            if has_draw and best_draw_odds > 1.0:
                odds_for_overround.append(best_draw_odds)

            overround = calculate_overround(odds_for_overround)

            if overround >= 1.0:
                continue  # No hay arbitraje

            profit_pct = (1.0 / overround - 1.0) * 100
            arb_type = "3way" if has_draw and best_draw_odds > 1.0 else "2way"

            # Calcular stakes para bankroll de referencia de 100€
            bankroll = 100.0
            stake_home = round((bankroll / overround) * (1.0 / best_home_odds), 2)
            stake_away = round((bankroll / overround) * (1.0 / best_away_odds), 2)
            stakes: dict = {"home": stake_home, "away": stake_away}

            arb: dict = {
                "match": market.get("match", ""),
                "league": market.get("league", ""),
                "home": market.get("home", ""),
                "away": market.get("away", ""),
                "best_home_odds": round(best_home_odds, 3),
                "best_home_book": best_home_book,
                "best_away_odds": round(best_away_odds, 3),
                "best_away_book": best_away_book,
                "overround": round(overround, 4),
                "profit_pct": round(profit_pct, 2),
                "stakes": stakes,
                "arb_type": arb_type,
            }

            if arb_type == "3way":
                stake_draw = round((bankroll / overround) * (1.0 / best_draw_odds), 2)
                arb["best_draw_odds"] = round(best_draw_odds, 3)
                arb["best_draw_book"] = best_draw_book
                arb["stakes"]["draw"] = stake_draw

            results.append(arb)
            logger.info(
                "arbitrage_detector: ARB %s — %s vs %s | overround=%.4f profit=+%.2f%%",
                arb_type, market.get("home"), market.get("away"), overround, profit_pct,
            )

        except Exception as e:
            logger.warning("arbitrage_detector.find_arbitrage: error procesando mercado — %s", e)

    return results


async def detect_and_store_arbitrage(odds_data: list[dict]) -> list[dict]:
    """
    Ejecuta find_arbitrage, guarda resultados en Firestore col("arb_opportunities")
    con TTL field: expires_at = now + 2h.
    Devuelve lista de arb encontrados.
    """
    arbs = find_arbitrage(odds_data)

    if not arbs:
        logger.info("arbitrage_detector: ninguna oportunidad de arb detectada")
        return []

    try:
        from shared.firestore_client import col

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=2)).isoformat()

        for arb in arbs:
            try:
                arb_id = str(uuid.uuid4())
                doc = {
                    "arb_id": arb_id,
                    "detected_at": now.isoformat(),
                    "expires_at": expires_at,
                    **arb,
                }
                col("arb_opportunities").document(arb_id).set(doc)
                logger.debug("arbitrage_detector: guardado arb_id=%s", arb_id)
            except Exception as e:
                logger.warning("arbitrage_detector: error guardando arb en Firestore — %s", e)

        logger.info("arbitrage_detector: %d oportunidades guardadas en Firestore", len(arbs))

    except Exception as e:
        logger.error("arbitrage_detector.detect_and_store_arbitrage: error Firestore — %s", e)

    return arbs


def format_arb_telegram(arb: dict) -> str:
    """Formatea una oportunidad de arbitraje para Telegram con emoji 💎."""
    league = arb.get("league", "")
    home = arb.get("home", "")
    away = arb.get("away", "")
    profit_pct = arb.get("profit_pct", 0.0)
    arb_type = arb.get("arb_type", "2way")
    stakes = arb.get("stakes", {})

    best_home_book = arb.get("best_home_book", "?")
    best_home_odds = arb.get("best_home_odds", 0.0)
    best_away_book = arb.get("best_away_book", "?")
    best_away_odds = arb.get("best_away_odds", 0.0)

    stake_home = stakes.get("home", 0.0)
    stake_away = stakes.get("away", 0.0)

    lines: list[str] = [
        f"💎 ARBITRAJE DETECTADO | {league}",
        f"{home} vs {away}",
        f"Back: {best_home_book} @ *{best_home_odds}* ({stake_home:.0f}€)",
        f"Lay: {best_away_book} @ *{best_away_odds}* ({stake_away:.0f}€)",
    ]

    if arb_type == "3way":
        best_draw_book = arb.get("best_draw_book", "?")
        best_draw_odds = arb.get("best_draw_odds", 0.0)
        stake_draw = stakes.get("draw", 0.0)
        lines.append(f"Empate: {best_draw_book} @ *{best_draw_odds}* ({stake_draw:.0f}€)")

    lines.append(f"Beneficio garantizado: *+{profit_pct:.1f}%*")
    lines.append("⚠️ Apuesta responsablemente. No es asesoramiento financiero.")

    return "\n".join(lines)


def build_arb_prediction(arb: dict, match_id: str) -> dict:
    """Construye un dict de prediction con market_type=ARBITRAGE para guardar en Firestore."""
    from datetime import datetime, timezone
    return {
        "match_id": f"{match_id}_arb",
        "home_team": arb.get("home", ""),
        "away_team": arb.get("away", ""),
        "league": arb.get("league", ""),
        "sport": "football",
        "market_type": "ARBITRAGE",
        "selection": f"{arb.get('best_home_book')} / {arb.get('best_away_book')}",
        "odds": round(arb.get("best_home_odds", 0.0), 3),
        "edge": round(arb.get("profit_pct", 0.0) / 100, 4),
        "confidence": 1.0,
        "profit_pct": arb.get("profit_pct", 0.0),
        "arb_type": arb.get("arb_type", "2way"),
        "overround": arb.get("overround", 0.0),
        "stakes": arb.get("stakes", {}),
        "data_source": "arbitrage_detector",
        "created_at": datetime.now(timezone.utc),
        "result": None,
        "correct": None,
    }
