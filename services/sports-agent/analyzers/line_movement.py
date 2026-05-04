"""
Line Movement Detector + Sharp Money (Pinnacle reference).
Detecta movimientos significativos de cuota y dinero inteligente.
"""
import logging
from datetime import datetime, timezone, timedelta

from google.cloud.firestore_v1.base_query import FieldFilter

logger = logging.getLogger(__name__)


# ── BLOQUE 2: Line Movement ────────────────────────────────────────────────────


async def save_odds_snapshot(
    fixture_id: str,
    bookmaker: str,
    market_type: str,
    home_odds: float,
    draw_odds: float | None,
    away_odds: float,
) -> None:
    """
    Guarda snapshot de cuotas en Firestore (colección odds_history).
    Si ya existe doc para (fixture_id, bookmaker, market_type):
    - Actualizar odds_current
    - Append a odds_history (max 24 entradas)
    - Si odds_open no existe: setearlo como el primero
    Si no existe: crear doc nuevo con odds_open = odds_current = home/draw/away.
    """
    try:
        from shared.firestore_client import col

        now_iso = datetime.now(timezone.utc).isoformat()
        doc_id = f"{fixture_id}_{bookmaker}_{market_type}"
        ref = col("odds_history").document(doc_id)
        snap = ref.get()

        snapshot_entry = {
            "home_odds": home_odds,
            "draw_odds": draw_odds,
            "away_odds": away_odds,
            "recorded_at": now_iso,
        }

        if snap.exists:
            data = snap.to_dict() or {}
            history: list = data.get("odds_history", [])
            history.append(snapshot_entry)
            # Mantener máximo 24 entradas
            if len(history) > 24:
                history = history[-24:]

            update_payload: dict = {
                "odds_current": {
                    "home": home_odds,
                    "draw": draw_odds,
                    "away": away_odds,
                },
                "odds_history": history,
                "last_updated": now_iso,
            }
            # Si odds_open no existe en el doc existente, setearlo
            if "odds_open" not in data:
                update_payload["odds_open"] = {
                    "home": home_odds,
                    "draw": draw_odds,
                    "away": away_odds,
                }
            ref.update(update_payload)
        else:
            odds_block = {
                "home": home_odds,
                "draw": draw_odds,
                "away": away_odds,
            }
            ref.set(
                {
                    "fixture_id": fixture_id,
                    "bookmaker": bookmaker,
                    "market_type": market_type,
                    "odds_open": odds_block,
                    "odds_current": odds_block,
                    "odds_history": [snapshot_entry],
                    "recorded_at": now_iso,
                    "last_updated": now_iso,
                }
            )

        logger.debug(
            "line_movement: snapshot guardado fixture=%s bookmaker=%s market=%s",
            fixture_id, bookmaker, market_type,
        )
    except Exception as e:
        logger.warning("line_movement: error en save_odds_snapshot — %s", e)


async def detect_line_movement(fixture_id: str) -> dict:
    """
    Lee todos los snapshots de odds_history para fixture_id.
    Para cada bookmaker/market calcula movimiento home/away y promedia cross-bookmaker.

    Returns:
    {
      has_movement: bool,
      type: "LINE_MOVEMENT_STRONG"|"LINE_MOVEMENT_MODERATE"|"LATE_MONEY"|"NONE",
      direction: "home"|"away"|"draw"|None,
      magnitude: float,
      late_money: bool,
      message: str
    }
    """
    result: dict = {
        "has_movement": False,
        "type": "NONE",
        "direction": None,
        "magnitude": 0.0,
        "late_money": False,
        "message": "Sin movimiento de línea detectado",
    }

    try:
        from shared.firestore_client import col

        docs = list(
            col("odds_history").where(filter=FieldFilter("fixture_id", "==", fixture_id)).stream()
        )

        if not docs:
            return result

        movements_home: list[float] = []
        movements_away: list[float] = []
        late_money_detected = False
        now_utc = datetime.now(timezone.utc)
        one_hour_ago = now_utc - timedelta(hours=1)

        for doc in docs:
            data = doc.to_dict() or {}
            odds_open = data.get("odds_open", {})
            odds_current = data.get("odds_current", {})
            history: list = data.get("odds_history", [])

            open_home = float(odds_open.get("home", 0) or 0)
            open_away = float(odds_open.get("away", 0) or 0)
            cur_home = float(odds_current.get("home", 0) or 0)
            cur_away = float(odds_current.get("away", 0) or 0)

            if open_home > 0 and cur_home > 0:
                mov_home = (open_home - cur_home) / open_home
                movements_home.append(mov_home)

            if open_away > 0 and cur_away > 0:
                mov_away = (open_away - cur_away) / open_away
                movements_away.append(mov_away)

            # Detectar late money: última entrada en <1h con >5% de movimiento
            if history:
                last_entry = history[-1]
                try:
                    rec_at = last_entry.get("recorded_at", "")
                    if rec_at:
                        if rec_at.endswith("Z"):
                            rec_at = rec_at[:-1] + "+00:00"
                        entry_dt = datetime.fromisoformat(rec_at)
                        if entry_dt.tzinfo is None:
                            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                        if entry_dt >= one_hour_ago:
                            last_home = float(last_entry.get("home_odds", 0) or 0)
                            if open_home > 0 and last_home > 0:
                                late_mov = abs((open_home - last_home) / open_home)
                                if late_mov > 0.05:
                                    late_money_detected = True
                except Exception:
                    pass

        avg_home = sum(movements_home) / len(movements_home) if movements_home else 0.0
        avg_away = sum(movements_away) / len(movements_away) if movements_away else 0.0

        # Determinar dirección y magnitud dominante
        if abs(avg_home) >= abs(avg_away):
            magnitude = abs(avg_home) * 100  # porcentaje
            # home odds bajaron → dinero hacia home
            direction = "home" if avg_home > 0 else "away"
        else:
            magnitude = abs(avg_away) * 100
            direction = "away" if avg_away > 0 else "home"

        # Clasificar
        if magnitude > 15:
            mov_type = "LINE_MOVEMENT_STRONG"
            has_movement = True
        elif magnitude > 8:
            mov_type = "LINE_MOVEMENT_MODERATE"
            has_movement = True
        elif late_money_detected:
            mov_type = "LATE_MONEY"
            has_movement = True
        else:
            mov_type = "NONE"
            has_movement = False

        if late_money_detected and mov_type in ("LINE_MOVEMENT_STRONG", "LINE_MOVEMENT_MODERATE"):
            # late_money coexiste con strong/moderate
            pass
        elif late_money_detected and mov_type == "NONE":
            mov_type = "LATE_MONEY"
            has_movement = True

        msg_parts = []
        if has_movement:
            msg_parts.append(f"Movimiento {mov_type} hacia {direction} ({magnitude:.1f}%)")
        if late_money_detected:
            msg_parts.append("Late money detectado en última hora")

        result.update(
            {
                "has_movement": has_movement,
                "type": mov_type,
                "direction": direction if has_movement else None,
                "magnitude": round(magnitude, 2),
                "late_money": late_money_detected,
                "message": ". ".join(msg_parts) if msg_parts else "Sin movimiento de línea detectado",
            }
        )

    except Exception as e:
        logger.warning("line_movement: error en detect_line_movement fixture=%s — %s", fixture_id, e)

    return result


def _detect_odds_movement(match_id: str) -> dict:
    """
    Compara cuota actual vs 6h y 24h atrás para detectar SMART_MONEY o FADING.

    Returns:
    {
        "flag": "SMART_MONEY" | "FADING" | "NONE",
        "direction": "home" | "away" | None,
        "pct_change_6h": float,   # e.g. -0.12 para -12%
        "pct_change_24h": float,
        "timeframe": "6h" | "24h" | None,
        "message": str,
    }

    SMART_MONEY: pct_change_6h < -0.10 (cuota bajó >10% en 6h)
    FADING:      pct_change_24h > 0.15 (cuota subió >15% en 24h)
    """
    result: dict = {
        "flag": "NONE",
        "direction": None,
        "pct_change_6h": 0.0,
        "pct_change_24h": 0.0,
        "timeframe": None,
        "message": "",
    }

    try:
        from shared.firestore_client import col

        docs = list(
            col("odds_history").where(filter=FieldFilter("fixture_id", "==", match_id)).stream()
        )

        if not docs:
            return result

        now_utc = datetime.now(timezone.utc)
        target_6h = now_utc - timedelta(hours=6)
        target_24h = now_utc - timedelta(hours=24)
        tolerance = timedelta(hours=2)

        best_pct_6h = 0.0
        best_pct_24h = 0.0
        best_direction: str | None = None

        for doc in docs:
            data = doc.to_dict() or {}
            history: list = data.get("odds_history", [])
            odds_current = data.get("odds_current", {})

            cur_home = float(odds_current.get("home", 0) or 0)
            cur_away = float(odds_current.get("away", 0) or 0)

            if len(history) < 2 or cur_home <= 0:
                continue

            # Parsear timestamps de cada entrada
            parsed: list[tuple] = []  # (datetime, home_odds, away_odds)
            for entry in history:
                try:
                    rec_at = entry.get("recorded_at", "")
                    if not rec_at:
                        continue
                    if rec_at.endswith("Z"):
                        rec_at = rec_at[:-1] + "+00:00"
                    entry_dt = datetime.fromisoformat(rec_at)
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    h = float(entry.get("home_odds", 0) or 0)
                    a = float(entry.get("away_odds", 0) or 0)
                    if h > 0:
                        parsed.append((entry_dt, h, a))
                except Exception:
                    continue

            if not parsed:
                continue

            # Encontrar entrada más cercana a 6h atrás
            def _closest(target_dt: datetime) -> tuple | None:
                best_entry = None
                best_delta = None
                for p in parsed:
                    delta = abs(p[0] - target_dt)
                    if best_delta is None or delta < best_delta:
                        best_delta = delta
                        best_entry = p
                if best_entry and abs(best_entry[0] - target_dt) <= tolerance:
                    return best_entry
                return None

            entry_6h = _closest(target_6h)
            entry_24h = _closest(target_24h)

            # Calcular pct_change_6h
            if entry_6h is not None:
                h6 = entry_6h[1]
                a6 = entry_6h[2]
                if h6 > 0:
                    pc6_home = (cur_home - h6) / h6
                    if abs(pc6_home) > abs(best_pct_6h):
                        best_pct_6h = pc6_home
                        # Bajada de cuota home → dinero en home
                        best_direction = "home" if pc6_home < 0 else "away"
                if a6 > 0 and cur_away > 0:
                    pc6_away = (cur_away - a6) / a6
                    # Solo actualizar dirección desde away si es más significativo
                    if abs(pc6_away) > abs(best_pct_6h):
                        best_pct_6h = pc6_away
                        best_direction = "away" if pc6_away < 0 else "home"

            # Calcular pct_change_24h
            if entry_24h is not None:
                h24 = entry_24h[1]
                a24 = entry_24h[2]
                if h24 > 0:
                    pc24_home = (cur_home - h24) / h24
                    if abs(pc24_home) > abs(best_pct_24h):
                        best_pct_24h = pc24_home

                if a24 > 0 and cur_away > 0:
                    pc24_away = (cur_away - a24) / a24
                    if abs(pc24_away) > abs(best_pct_24h):
                        best_pct_24h = pc24_away

        result["pct_change_6h"] = round(best_pct_6h, 4)
        result["pct_change_24h"] = round(best_pct_24h, 4)

        # Clasificar flag
        if best_pct_6h < -0.10:
            result["flag"] = "SMART_MONEY"
            result["direction"] = best_direction
            result["timeframe"] = "6h"
            result["message"] = (
                f"Cuota bajó {abs(best_pct_6h):.0%} en 6h "
                f"— posible smart money ({best_direction})"
            )
        elif best_pct_24h > 0.15:
            result["flag"] = "FADING"
            result["direction"] = best_direction
            result["timeframe"] = "24h"
            result["message"] = (
                f"Cuota subió {abs(best_pct_24h):.0%} en 24h "
                f"— bookmaker alargando ({best_direction})"
            )

        logger.debug(
            "_detect_odds_movement(%s): flag=%s pct_6h=%.3f pct_24h=%.3f",
            match_id, result["flag"], best_pct_6h, best_pct_24h,
        )

    except Exception as e:
        logger.warning("_detect_odds_movement: error match_id=%s — %s", match_id, e)

    return result


def apply_line_movement_to_signal(signal: dict, movement: dict) -> dict:
    """
    Ajusta confidence según movimiento de cuota.
    - STRONG o MODERATE y direction == team apostado: confidence *= 1.15
    - STRONG o MODERATE y direction != team apostado: confidence *= 0.85
    - LATE_MONEY hacia equipo apostado: confidence *= 1.10 (adicional)
    - Añadir campos: line_movement_type, line_movement_magnitude, line_movement_direction
    Clampar confidence a [0.0, 1.0]. Nunca falla.
    """
    try:
        mov_type = movement.get("type", "NONE")
        direction = movement.get("direction")
        team_to_back = signal.get("team_to_back", "")
        confidence = float(signal.get("confidence", 1.0))

        # Normalizar team_to_back a "home"/"away"/"draw" si es posible
        team_lower = str(team_to_back).lower()
        team_dir = None
        if team_lower in ("home", "local"):
            team_dir = "home"
        elif team_lower in ("away", "visitante"):
            team_dir = "away"
        elif team_lower in ("draw", "empate"):
            team_dir = "draw"

        if mov_type in ("LINE_MOVEMENT_STRONG", "LINE_MOVEMENT_MODERATE"):
            if direction is not None and team_dir is not None:
                if direction == team_dir:
                    confidence *= 1.15
                    logger.debug(
                        "line_movement: %s a favor → confidence *= 1.15 → %.4f",
                        mov_type, confidence,
                    )
                else:
                    confidence *= 0.85
                    logger.debug(
                        "line_movement: %s en contra → confidence *= 0.85 → %.4f",
                        mov_type, confidence,
                    )

        if movement.get("late_money") and direction is not None and team_dir is not None:
            if direction == team_dir:
                confidence *= 1.10
                logger.debug(
                    "line_movement: late money a favor → confidence *= 1.10 → %.4f", confidence
                )

        signal["confidence"] = round(min(max(confidence, 0.0), 1.0), 4)
        signal["line_movement_type"] = mov_type
        signal["line_movement_magnitude"] = movement.get("magnitude", 0.0)
        signal["line_movement_direction"] = direction

    except Exception as e:
        logger.warning("line_movement: error en apply_line_movement_to_signal — %s", e)

    return signal


def format_late_money_alert(fixture: dict, movement: dict) -> str:
    """
    Formato Telegram para late money.
    """
    try:
        league = fixture.get("league_name") or fixture.get("league", "Liga desconocida")
        home = fixture.get("home_team_name") or fixture.get("home_team", "Local")
        away = fixture.get("away_team_name") or fixture.get("away_team", "Visitante")
        direction = movement.get("direction", "")
        team_label = home if direction == "home" else (away if direction == "away" else direction)

        # Intentar obtener odds open/current del movimiento si están disponibles
        odds_open = movement.get("odds_open", "?")
        odds_current = movement.get("odds_current", "?")
        try:
            odds_open_fmt = f"{float(odds_open):.2f}"
        except (TypeError, ValueError):
            odds_open_fmt = str(odds_open)
        try:
            odds_current_fmt = f"{float(odds_current):.2f}"
        except (TypeError, ValueError):
            odds_current_fmt = str(odds_current)

        return (
            f"⚡ LATE MONEY DETECTADO | ⚽ {league}\n"
            f"{home} vs {away}\n"
            f"Cuota {team_label}: {odds_open_fmt} → {odds_current_fmt} (última hora)\n"
            f"Posible información privilegiada\n"
            f"⚠️ Apuesta responsablemente."
        )
    except Exception as e:
        logger.warning("line_movement: error en format_late_money_alert — %s", e)
        return "⚡ LATE MONEY DETECTADO\n⚠️ Apuesta responsablemente."


# ── Odds Drift (apertura vs actual) ──────────────────────────────────────────


async def calculate_odds_drift(fixture_id: str) -> dict:
    """
    Calcula el drift entre cuota de apertura y actual para un fixture.
    opening_value = (odds_open - odds_current) / odds_open

    Thresholds:
    - odds bajaron >15% desde apertura (opening_value > 0.15):
      "PROCESSED" — mercado ya procesó información → reducir confidence
    - odds subieron >10% desde apertura (opening_value < -0.10):
      "DRIFTED_UP" — posible valor residual → boost confidence

    Returns:
    {
      has_data: bool,
      direction: "home"|"away"|"draw"|None,
      drift_home: float,   # positivo = cuota bajó (más dinero), negativo = cuota subió
      drift_away: float,
      type: "PROCESSED"|"DRIFTED_UP"|"NONE",
      team: "home"|"away"|None,  # qué equipo tiene el drift más significativo
      message: str
    }
    """
    result: dict = {
        "has_data": False,
        "drift_home": 0.0,
        "drift_away": 0.0,
        "type": "NONE",
        "team": None,
        "message": "",
    }
    try:
        from shared.firestore_client import col

        docs = list(col("odds_history").where(filter=FieldFilter("fixture_id", "==", fixture_id)).stream())
        if not docs:
            return result

        drifts_home: list[float] = []
        drifts_away: list[float] = []

        for doc in docs:
            data = doc.to_dict() or {}
            odds_open = data.get("odds_open", {})
            odds_cur = data.get("odds_current", {})

            o_home = float(odds_open.get("home", 0) or 0)
            o_away = float(odds_open.get("away", 0) or 0)
            c_home = float(odds_cur.get("home", 0) or 0)
            c_away = float(odds_cur.get("away", 0) or 0)

            if o_home > 0 and c_home > 0:
                drifts_home.append((o_home - c_home) / o_home)
            if o_away > 0 and c_away > 0:
                drifts_away.append((o_away - c_away) / o_away)

        if not drifts_home and not drifts_away:
            return result

        avg_drift_home = sum(drifts_home) / len(drifts_home) if drifts_home else 0.0
        avg_drift_away = sum(drifts_away) / len(drifts_away) if drifts_away else 0.0

        result["has_data"] = True
        result["drift_home"] = round(avg_drift_home, 4)
        result["drift_away"] = round(avg_drift_away, 4)

        # El drift más significativo determina el tipo
        max_drift = max(abs(avg_drift_home), abs(avg_drift_away))
        dominant_team = "home" if abs(avg_drift_home) >= abs(avg_drift_away) else "away"
        dominant_drift = avg_drift_home if dominant_team == "home" else avg_drift_away

        if dominant_drift > 0.15:
            result["type"] = "PROCESSED"
            result["team"] = dominant_team
            result["message"] = (
                f"Cuota {dominant_team} bajó {dominant_drift:.0%} desde apertura "
                f"→ mercado ya procesó información"
            )
        elif dominant_drift < -0.10:
            result["type"] = "DRIFTED_UP"
            result["team"] = dominant_team
            result["message"] = (
                f"Cuota {dominant_team} subió {abs(dominant_drift):.0%} desde apertura "
                f"→ posible valor residual"
            )

        logger.debug(
            "calculate_odds_drift fixture=%s: home=%.3f away=%.3f type=%s",
            fixture_id, avg_drift_home, avg_drift_away, result["type"],
        )
    except Exception as e:
        logger.warning("calculate_odds_drift: error fixture=%s — %s", fixture_id, e)

    return result


def apply_odds_drift_to_signal(signal: dict, drift: dict) -> dict:
    """
    Ajusta confidence según drift de cuota (apertura vs actual).

    - Si drift.type == "PROCESSED" y drift.team == equipo apostado:
      confidence *= 0.85 (el mercado ya procesó la info, la ventana se cerró)
    - Si drift.type == "DRIFTED_UP" y drift.team == equipo apostado:
      confidence *= 1.10 (hay valor residual no procesado)
    - Añade campo odds_drift al signal.
    Clampa confidence a [0.0, 1.0]. Nunca falla.
    """
    try:
        if not drift.get("has_data") or drift.get("type") == "NONE":
            return signal

        confidence = float(signal.get("confidence", 0.65))
        team_to_back = str(signal.get("team_to_back", "")).lower()
        drift_team = drift.get("team")
        drift_type = drift.get("type", "NONE")

        team_dir = None
        if team_to_back in ("home", "local"):
            team_dir = "home"
        elif team_to_back in ("away", "visitante"):
            team_dir = "away"

        if drift_team and team_dir and drift_team == team_dir:
            if drift_type == "PROCESSED":
                confidence = min(1.0, max(0.0, confidence * 0.85))
                logger.debug("odds_drift: PROCESSED en equipo apostado → confidence=%.4f", confidence)
            elif drift_type == "DRIFTED_UP":
                confidence = min(1.0, max(0.0, confidence * 1.10))
                logger.debug("odds_drift: DRIFTED_UP en equipo apostado → confidence=%.4f", confidence)

        signal["confidence"] = round(confidence, 4)
        signal["odds_drift"] = {
            "type": drift_type,
            "team": drift_team,
            "drift_home_pct": round(drift.get("drift_home", 0) * 100, 1),
            "drift_away_pct": round(drift.get("drift_away", 0) * 100, 1),
            "message": drift.get("message", ""),
        }
    except Exception as e:
        logger.warning("apply_odds_drift_to_signal: error — %s", e)

    return signal


# ── BLOQUE 9: Sharp Money (Pinnacle) ──────────────────────────────────────────


async def detect_sharp_money(fixture_id: str, team_to_back: str) -> dict:
    """
    Compara cuota Pinnacle vs media del resto de bookmakers para fixture_id.

    Returns:
    {
      has_pinnacle: bool,
      pinnacle_odds: float | None,
      market_avg_odds: float | None,
      divergence_pct: float,
      sharp_signal: "CONFIRMS"|"CONTRADICTS"|"NEUTRAL",
      message: str
    }
    """
    result: dict = {
        "has_pinnacle": False,
        "pinnacle_odds": None,
        "market_avg_odds": None,
        "divergence_pct": 0.0,
        "sharp_signal": "NEUTRAL",
        "message": "Sin datos de Pinnacle",
    }

    try:
        from shared.firestore_client import col

        docs = list(
            col("odds_history").where(filter=FieldFilter("fixture_id", "==", fixture_id)).stream()
        )

        if not docs:
            return result

        # Determinar qué odds leer (home o away) según team_to_back
        team_lower = str(team_to_back).lower()
        if team_lower in ("home", "local"):
            odds_key = "home"
        elif team_lower in ("away", "visitante"):
            odds_key = "away"
        else:
            odds_key = "home"  # default

        pinnacle_odds: float | None = None
        other_odds: list[float] = []

        for doc in docs:
            data = doc.to_dict() or {}
            bookmaker = (data.get("bookmaker") or "").lower()
            odds_current = data.get("odds_current", {})
            val = float(odds_current.get(odds_key, 0) or 0)
            if val <= 0:
                continue

            if "pinnacle" in bookmaker:
                pinnacle_odds = val
            else:
                other_odds.append(val)

        if pinnacle_odds is None:
            result["has_pinnacle"] = False
            result["sharp_signal"] = "NEUTRAL"
            return result

        result["has_pinnacle"] = True
        result["pinnacle_odds"] = pinnacle_odds

        if not other_odds:
            result["sharp_signal"] = "NEUTRAL"
            result["message"] = f"Pinnacle: {pinnacle_odds:.2f} — sin otros bookmakers para comparar"
            return result

        avg_others = sum(other_odds) / len(other_odds)
        result["market_avg_odds"] = round(avg_others, 4)

        divergence_pct = ((pinnacle_odds - avg_others) / avg_others) * 100
        result["divergence_pct"] = round(divergence_pct, 2)

        # Pinnacle < avg_others * 0.95 → mercado apuesta más en Pinnacle → sharp money hacia favorito
        if pinnacle_odds < avg_others * 0.95:
            sharp_signal = "CONFIRMS"
            msg = (
                f"Pinnacle {pinnacle_odds:.2f} < media {avg_others:.2f} "
                f"({divergence_pct:.1f}%) → sharp money confirma {team_to_back}"
            )
        elif pinnacle_odds > avg_others * 1.05:
            sharp_signal = "CONTRADICTS"
            msg = (
                f"Pinnacle {pinnacle_odds:.2f} > media {avg_others:.2f} "
                f"({divergence_pct:.1f}%) → sharp money contradice {team_to_back}"
            )
        else:
            sharp_signal = "NEUTRAL"
            msg = (
                f"Pinnacle {pinnacle_odds:.2f} ≈ media {avg_others:.2f} "
                f"({divergence_pct:.1f}%) — sin divergencia significativa"
            )

        result["sharp_signal"] = sharp_signal
        result["message"] = msg

        logger.debug("line_movement: detect_sharp_money fixture=%s → %s", fixture_id, sharp_signal)

    except Exception as e:
        logger.warning("line_movement: error en detect_sharp_money fixture=%s — %s", fixture_id, e)

    return result


def apply_sharp_money_to_signal(signal: dict, sharp: dict) -> dict:
    """
    Ajusta confidence según señal de sharp money.
    - CONFIRMS: confidence *= 1.20
    - CONTRADICTS: confidence *= 0.75
    - NEUTRAL: sin cambio
    Añade campo sharp_signal. Nunca falla.
    """
    try:
        sharp_signal = sharp.get("sharp_signal", "NEUTRAL")
        confidence = float(signal.get("confidence", 1.0))

        if sharp_signal == "CONFIRMS":
            confidence *= 1.20
            logger.debug(
                "line_movement: sharp CONFIRMS → confidence *= 1.20 → %.4f", confidence
            )
        elif sharp_signal == "CONTRADICTS":
            confidence *= 0.75
            logger.debug(
                "line_movement: sharp CONTRADICTS → confidence *= 0.75 → %.4f", confidence
            )

        signal["confidence"] = round(min(max(confidence, 0.0), 1.0), 4)
        signal["sharp_signal"] = sharp_signal

    except Exception as e:
        logger.warning("line_movement: error en apply_sharp_money_to_signal — %s", e)

    return signal
