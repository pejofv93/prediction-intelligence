"""
Context enrichment for sports signals using web search + API-Football.

Enriquece señales con:
  - Lesiones y alineaciones (API-Football /injuries)
  - Carga de partidos / fatiga (Firestore)
  - Contexto de competición (jornada decisiva, rotaciones CL)
  - Síntesis IA (Groq) para validar o reducir la señal
"""
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Máximo de análisis de contexto por ciclo (evita agotar quota)
_MAX_CONTEXT_ANALYSES = 10
_context_analyses_this_cycle = 0

# Umbral mínimo de quota restante antes de llamar a API-Football
_MIN_QUOTA_REMAINING = 20

FOOTBALL_RAPID_API_KEY = os.environ.get("FOOTBALL_RAPID_API_KEY", "")


def _reset_cycle_counter() -> None:
    """Reinicia el contador por ciclo. Llamar al inicio de cada pipeline."""
    global _context_analyses_this_cycle
    _context_analyses_this_cycle = 0


async def enrich_signal_with_context(match: dict, signal: dict, quota_mgr=None) -> dict:
    """
    Enriquece una señal deportiva con contexto real.
    Devuelve signal modificado.
    Nunca falla — siempre devuelve el signal original en caso de error.
    Solo ejecuta si quota_mgr.can_call("api_sports") tiene > 20 req restantes.
    """
    global _context_analyses_this_cycle

    # ── 1. Verificar quota y límite por ciclo ─────────────────────────────────
    try:
        if quota_mgr is not None:
            if not quota_mgr.can_call("api_sports"):
                logger.debug("context_analyzer: api_sports sin quota — omitiendo enriquecimiento")
                return signal
            # Verificar remaining reportado
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            try:
                doc = quota_mgr._get_doc("api_sports", today)
                used = doc.get("used", 0)
                limit = 100  # api_sports daily limit
                remaining = limit - used
                if remaining < _MIN_QUOTA_REMAINING:
                    logger.debug(
                        "context_analyzer: api_sports remaining=%d < %d — omitiendo",
                        remaining, _MIN_QUOTA_REMAINING,
                    )
                    return signal
            except Exception:
                pass  # continuar si no se puede leer el doc

        if _context_analyses_this_cycle >= _MAX_CONTEXT_ANALYSES:
            logger.debug(
                "context_analyzer: límite de %d análisis por ciclo alcanzado", _MAX_CONTEXT_ANALYSES
            )
            return signal

        _context_analyses_this_cycle += 1
    except Exception as e:
        logger.warning("context_analyzer: error en verificación de quota — %s", e)

    # ── 2. Lesiones y alineaciones (API-Football) ─────────────────────────────
    injured_players: list[str] = []
    lineup_confirmed = False

    try:
        from analyzers.lineup_checker import fetch_injuries, fetch_lineups

        fixture_id = match.get("fixture_id") or match.get("match_id")
        api_key = FOOTBALL_RAPID_API_KEY

        if fixture_id and api_key:
            try:
                fixture_id_int = int(fixture_id)
            except (TypeError, ValueError):
                fixture_id_int = None

            if fixture_id_int:
                injuries = await fetch_injuries(fixture_id_int, api_key)
                if quota_mgr is not None:
                    try:
                        quota_mgr.track_call("api_sports")
                    except Exception:
                        pass

                # Extraer jugadores ausentes por lesión
                home_team_name = (match.get("home_team_name") or match.get("home_team", "")).lower()
                away_team_name = (match.get("away_team_name") or match.get("away_team", "")).lower()

                for inj in injuries:
                    player_name = inj.get("player_name", "")
                    team_name = (inj.get("team_name") or "").lower()
                    if player_name:
                        injured_players.append(player_name)

                    # Identificar ausencias clave: goleadores/asistidores principales
                    key_players: list[str] = []
                    if isinstance(match.get("key_players"), list):
                        key_players = [str(p).lower() for p in match["key_players"]]

                    if key_players and player_name.lower() in key_players:
                        prev_conf = float(signal.get("confidence", 1.0))
                        signal["confidence"] = round(prev_conf * 0.80, 4)
                        logger.info(
                            "context_analyzer: jugador clave %s ausente → confidence %.2f→%.2f",
                            player_name, prev_conf, signal["confidence"],
                        )

                    # Portero ausente
                    if inj.get("type", "").lower() in ("goalkeeper", "portero", "gk"):
                        signal["goalkeeper_absent"] = True
                        logger.info("context_analyzer: portero ausente detectado — %s", player_name)

                # Intentar confirmar alineación
                try:
                    lineups = await fetch_lineups(fixture_id_int, api_key)
                    if quota_mgr is not None:
                        try:
                            quota_mgr.track_call("api_sports")
                        except Exception:
                            pass
                    lineup_confirmed = bool(lineups.get("home_xi"))
                except Exception as e:
                    logger.debug("context_analyzer: fetch_lineups falló — %s", e)

        signal["lineup_confirmed"] = lineup_confirmed
        signal["injured_players"] = injured_players

    except Exception as e:
        logger.warning("context_analyzer: error en paso lesiones/alineaciones — %s", e)
        signal.setdefault("lineup_confirmed", False)
        signal.setdefault("injured_players", [])

    # ── 3. Carga de partidos (Fatiga) ─────────────────────────────────────────
    try:
        from shared.firestore_client import col

        cutoff_14d = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

        for team_field in ("home_team_id", "away_team_id"):
            team_id = match.get(team_field)
            if not team_id:
                continue
            try:
                # Buscar partidos del equipo en los últimos 14 días (como local o visitante)
                home_q = col("matches").where("home_team_id", "==", team_id).where(
                    "match_date", ">=", cutoff_14d
                )
                away_q = col("matches").where("away_team_id", "==", team_id).where(
                    "match_date", ">=", cutoff_14d
                )
                home_docs = list(home_q.stream())
                away_docs = list(away_q.stream())
                total_matches = len(home_docs) + len(away_docs)

                if total_matches >= 4:
                    prev_conf = float(signal.get("confidence", 1.0))
                    signal["confidence"] = round(prev_conf * 0.85, 4)
                    signal["fatigue_flag"] = True
                    logger.info(
                        "context_analyzer: %s %s jugó %d partidos en 14 días → fatiga detectada",
                        team_field, team_id, total_matches,
                    )
            except Exception as e:
                logger.debug("context_analyzer: error consultando fatiga para %s=%s — %s", team_field, team_id, e)

    except Exception as e:
        logger.warning("context_analyzer: error en paso fatiga — %s", e)

    # ── 4. Contexto de competición ────────────────────────────────────────────
    try:
        matchday = match.get("matchday") or match.get("round") or 0
        try:
            matchday = int(str(matchday).split()[-1]) if matchday else 0
        except (ValueError, TypeError):
            matchday = 0

        is_decisive = matchday >= 34
        if is_decisive:
            signal["is_decisive"] = True
            logger.debug("context_analyzer: jornada %d >= 34 → partido decisivo", matchday)

            # Motivación extra en h2h decisivo
            if signal.get("market_type") == "h2h":
                prev_kf = float(signal.get("kelly_fraction", 0.05))
                signal["kelly_fraction"] = round(prev_kf * 1.1, 4)
                logger.debug(
                    "context_analyzer: h2h decisivo → kelly_fraction %.3f→%.3f",
                    prev_kf, signal["kelly_fraction"],
                )
        else:
            signal.setdefault("is_decisive", False)

        # Detectar rotaciones por partido de Champions League en la misma semana
        from shared.firestore_client import col

        now_utc = datetime.now(timezone.utc)
        week_start = now_utc.isoformat()
        week_end = (now_utc + timedelta(days=7)).isoformat()

        for team_field in ("home_team_id", "away_team_id"):
            team_id = match.get(team_field)
            if not team_id:
                continue
            try:
                cl_home = list(
                    col("matches")
                    .where("home_team_id", "==", team_id)
                    .where("competition", "==", "CL")
                    .where("match_date", ">=", week_start)
                    .where("match_date", "<=", week_end)
                    .limit(1)
                    .stream()
                )
                cl_away = list(
                    col("matches")
                    .where("away_team_id", "==", team_id)
                    .where("competition", "==", "CL")
                    .where("match_date", ">=", week_start)
                    .where("match_date", "<=", week_end)
                    .limit(1)
                    .stream()
                )
                if cl_home or cl_away:
                    signal["rotation_risk"] = True
                    prev_kf = float(signal.get("kelly_fraction", 0.05))
                    signal["kelly_fraction"] = round(prev_kf * 0.5, 4)
                    logger.info(
                        "context_analyzer: %s %s tiene partido CL esta semana → rotation_risk, kelly %.3f→%.3f",
                        team_field, team_id, prev_kf, signal["kelly_fraction"],
                    )
                    break
            except Exception as e:
                logger.debug("context_analyzer: error consultando CL para %s=%s — %s", team_field, team_id, e)

    except Exception as e:
        logger.warning("context_analyzer: error en paso contexto competición — %s", e)

    # ── 5. Síntesis Groq ──────────────────────────────────────────────────────
    try:
        context_parts: list[str] = []

        if injured_players:
            context_parts.append(f"Lesionados ausentes: {', '.join(injured_players[:5])}")
        if signal.get("goalkeeper_absent"):
            context_parts.append("Portero titular ausente")
        if signal.get("fatigue_flag"):
            context_parts.append("Equipo con fatiga (>=4 partidos en 14 días)")
        if signal.get("rotation_risk"):
            context_parts.append("Riesgo de rotaciones por partido de Champions en la misma semana")
        if signal.get("is_decisive"):
            context_parts.append(f"Jornada decisiva (matchday {matchday})")

        if context_parts:
            contexto_str = ". ".join(context_parts)
            home_name = match.get("home_team_name") or match.get("home_team", "Local")
            away_name = match.get("away_team_name") or match.get("away_team", "Visitante")

            system = (
                "Eres un analista deportivo experto. "
                "Responde SOLO: CONFIRMA / REDUCE_50 / DESCARTA seguido de | "
                "y una razón en máximo 15 palabras en español."
            )
            user = (
                f"Señal: {signal.get('market_type')} en {home_name} vs {away_name}. "
                f"Selección: {signal.get('team_to_back')}. "
                f"Contexto: {contexto_str}. "
                "¿Hay factores que invaliden o refuercen la señal?"
            )

            try:
                import asyncio as _asyncio
                from shared.groq_client import analyze, GROQ_CALL_DELAY

                loop = _asyncio.get_event_loop()
                raw_response: str = await loop.run_in_executor(None, analyze, system, user, False)
                raw_response = (raw_response or "").strip()

                # Parsear: primera palabra es la acción, después | razón
                action_word = ""
                reason = ""
                if "|" in raw_response:
                    parts = raw_response.split("|", 1)
                    action_word = parts[0].strip().upper()
                    reason = parts[1].strip()
                else:
                    action_word = raw_response.split()[0].upper() if raw_response else "CONFIRMA"
                    reason = raw_response

                if action_word == "CONFIRMA":
                    signal["context_note"] = reason
                elif action_word == "REDUCE_50":
                    prev_kf = float(signal.get("kelly_fraction", 0.05))
                    signal["kelly_fraction"] = round(prev_kf * 0.5, 4)
                    signal["context_note"] = reason
                    signal["context_action"] = "REDUCE_50"
                elif action_word == "DESCARTA":
                    signal["context_action"] = "DESCARTA"
                    signal["context_note"] = reason
                else:
                    signal["context_note"] = raw_response

                logger.info(
                    "context_analyzer: Groq → %s | %s (fixture=%s)",
                    action_word, reason[:60], match.get("fixture_id") or match.get("match_id"),
                )

                # Respetar el delay entre llamadas Groq
                await _asyncio.sleep(GROQ_CALL_DELAY)

            except Exception as e:
                logger.warning("context_analyzer: Groq falló — %s", e)

        signal["context_analyzed"] = True

    except Exception as e:
        logger.warning("context_analyzer: error en síntesis Groq — %s", e)
        signal.setdefault("context_analyzed", False)

    # ── 6. Campos de resumen para Telegram ───────────────────────────────────
    try:
        signal["referee_note"] = None  # endpoint separado — pendiente implementar
        signal["context_summary"] = {
            "injured": signal.get("injured_players", []),
            "fatigue": signal.get("fatigue_flag", False),
            "rotation_risk": signal.get("rotation_risk", False),
            "is_decisive": signal.get("is_decisive", False),
            "action": signal.get("context_action", "CONFIRMA"),
            "note": signal.get("context_note", ""),
        }
    except Exception as e:
        logger.warning("context_analyzer: error construyendo context_summary — %s", e)

    return signal


async def analyze_motivation(match: dict, signal: dict) -> dict:
    """
    Análisis de motivación basado en standings y calendario.

    1. STANDINGS (Firestore): posición en clasificación → rotation_risk o motivation_boost.
    2. CHAMPIONS SANDWICH: partido CL en ±3 días → rotation_risk HIGH + kelly reducido.
    3. FINAL DE TEMPORADA: jornada >= 34 → leve boost de confianza.

    Returns signal modificado. Nunca falla.
    """
    try:
        from shared.firestore_client import col
        from datetime import datetime, timezone, timedelta

        now_utc = datetime.now(timezone.utc)

        # ── 1. Standings ──────────────────────────────────────────────────────
        for team_field in ("home_team_id", "away_team_id"):
            team_id = match.get(team_field)
            if not team_id:
                continue
            try:
                docs = list(
                    col("standings").where("team_id", "==", team_id).limit(1).stream()
                )
                if not docs:
                    docs = list(
                        col("team_stats").where("team_id", "==", team_id).limit(1).stream()
                    )
                if docs:
                    data = docs[0].to_dict() or {}
                    position = data.get("position") or data.get("rank") or 0
                    total_teams = data.get("total_teams") or 20

                    relegation_zone_threshold = total_teams - 3
                    cl_zone_threshold = 4

                    if position > 0:
                        pts_to_relegation = max(0, (total_teams - position) * 2)
                        pts_to_cl = max(0, (cl_zone_threshold - position) * 2)

                        if pts_to_relegation > 15 and pts_to_cl > 15:
                            signal["rotation_risk"] = signal.get("rotation_risk") or "possible"
                            logger.debug(
                                "analyze_motivation: %s lejos de objetivos → rotation_risk=possible",
                                team_id,
                            )
                        elif position > relegation_zone_threshold or pts_to_relegation <= 5:
                            signal["motivation_boost"] = True
                            logger.debug(
                                "analyze_motivation: %s en zona descenso → motivation_boost",
                                team_id,
                            )
            except Exception as e:
                logger.debug(
                    "analyze_motivation: error standings %s=%s — %s", team_field, team_id, e
                )

        # ── 2. Champions Sandwich ─────────────────────────────────────────────
        window_start = (now_utc - timedelta(days=3)).isoformat()
        window_end = (now_utc + timedelta(days=3)).isoformat()

        for team_field in ("home_team_id", "away_team_id"):
            team_id = match.get(team_field)
            if not team_id:
                continue
            try:
                cl_home = list(
                    col("matches")
                    .where("home_team_id", "==", team_id)
                    .where("competition", "in", ["CL", "UCL"])
                    .where("match_date", ">=", window_start)
                    .where("match_date", "<=", window_end)
                    .limit(1)
                    .stream()
                )
                cl_away = list(
                    col("matches")
                    .where("away_team_id", "==", team_id)
                    .where("competition", "in", ["CL", "UCL"])
                    .where("match_date", ">=", window_start)
                    .where("match_date", "<=", window_end)
                    .limit(1)
                    .stream()
                )
                if cl_home or cl_away:
                    if signal.get("rotation_risk") == "HIGH":
                        continue
                    signal["rotation_risk"] = "HIGH"
                    prev_kf = float(signal.get("kelly_fraction", 0.05))
                    signal["kelly_fraction"] = round(prev_kf * 0.4, 4)
                    existing_note = signal.get("context_note") or ""
                    signal["context_note"] = existing_note + " | ⚠️ RIESGO ROTACIÓN CL"
                    logger.info(
                        "analyze_motivation: %s=%s tiene CL en ±3 días → rotation_risk HIGH, kelly %.4f",
                        team_field, team_id, signal["kelly_fraction"],
                    )
                    break
            except Exception as e:
                logger.debug(
                    "analyze_motivation: error CL sandwich %s=%s — %s", team_field, team_id, e
                )

        # ── 3. Final de temporada ─────────────────────────────────────────────
        matchday = match.get("matchday") or match.get("round") or match.get("jornada") or 0
        season_end_keywords = {"34", "35", "36", "37", "38"}
        try:
            matchday_int = int(str(matchday).split()[-1])
        except (ValueError, TypeError):
            matchday_int = 0

        is_season_finale = matchday_int >= 34 or any(
            kw in str(matchday) for kw in season_end_keywords
        )

        if is_season_finale:
            signal["is_season_finale"] = True
            confidence = float(signal.get("confidence", 1.0))
            confidence *= 1.05
            signal["confidence"] = round(min(max(confidence, 0.0), 1.0), 4)
            logger.debug(
                "analyze_motivation: final temporada → confidence *= 1.05 → %.4f",
                signal["confidence"],
            )

    except Exception as e:
        logger.warning("analyze_motivation: error general — %s", e)

    return signal


async def detect_rotation_risk(match: dict, signal: dict) -> dict:
    """
    Detección dedicada de riesgo de rotaciones.
    Busca fixtures de CL/EL/ECL del equipo en Firestore en ventana ±4 días.

    Returns signal modificado. Nunca falla.
    """
    try:
        from shared.firestore_client import col
        from datetime import datetime, timezone, timedelta

        now_utc = datetime.now(timezone.utc)
        home_id = match.get("home_team_id")
        away_id = match.get("away_team_id")

        team_ids = [tid for tid in (home_id, away_id) if tid]
        if not team_ids:
            return signal

        european_comps = ["CL", "UCL", "EL", "ECL", "Europa League", "Champions League"]

        window_start = (now_utc - timedelta(days=3)).isoformat()
        window_end_4 = (now_utc + timedelta(days=4)).isoformat()

        rotation_risk_level: str | None = None
        european_match_label: str | None = None

        for team_id in team_ids:
            for home_away_field in ("home_team_id", "away_team_id"):
                for comp in european_comps:
                    try:
                        docs = list(
                            col("matches")
                            .where(home_away_field, "==", team_id)
                            .where("competition", "==", comp)
                            .where("match_date", ">=", window_start)
                            .where("match_date", "<=", window_end_4)
                            .limit(1)
                            .stream()
                        )
                        if docs:
                            doc_data = docs[0].to_dict() or {}
                            match_date_str = doc_data.get("match_date", "")
                            try:
                                if match_date_str.endswith("Z"):
                                    match_date_str = match_date_str[:-1] + "+00:00"
                                european_dt = datetime.fromisoformat(match_date_str)
                                if european_dt.tzinfo is None:
                                    european_dt = european_dt.replace(tzinfo=timezone.utc)
                                diff_days = abs((european_dt - now_utc).total_seconds() / 86400)
                                level = "HIGH" if diff_days <= 3 else "MODERATE"
                            except Exception:
                                level = "HIGH"

                            if rotation_risk_level != "HIGH":
                                rotation_risk_level = level

                            home_name = doc_data.get("home_team_name", "")
                            away_name = doc_data.get("away_team_name", "")
                            european_match_label = f"{comp}: {home_name} vs {away_name}"
                    except Exception as e:
                        logger.debug(
                            "detect_rotation_risk: error query %s=%s comp=%s — %s",
                            home_away_field, team_id, comp, e,
                        )

            if rotation_risk_level:
                break

        if rotation_risk_level:
            kelly_adj = 0.4 if rotation_risk_level == "HIGH" else 0.6
            prev_kf = float(signal.get("kelly_fraction", 0.05))
            signal["kelly_fraction"] = round(prev_kf * kelly_adj, 4)
            signal["rotation_risk_level"] = rotation_risk_level
            warning_label = european_match_label or "partido europeo"
            signal["rotation_warning"] = f"⚠️ RIESGO ROTACIÓN: {warning_label}"
            logger.info(
                "detect_rotation_risk: level=%s kelly %.4f→%.4f (%s)",
                rotation_risk_level, prev_kf, signal["kelly_fraction"], warning_label,
            )
        else:
            signal.setdefault("rotation_risk_level", None)

    except Exception as e:
        logger.warning("detect_rotation_risk: error general — %s", e)

    return signal
