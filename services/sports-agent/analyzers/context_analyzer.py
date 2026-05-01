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
                home_q = col("matches").where(filter=FieldFilter("home_team_id", "==", team_id)).where(filter=FieldFilter("match_date", ">=", cutoff_14d))
                away_q = col("matches").where(filter=FieldFilter("away_team_id", "==", team_id)).where(filter=FieldFilter("match_date", ">=", cutoff_14d))
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
                    .where(filter=FieldFilter("home_team_id", "==", team_id))
                    .where(filter=FieldFilter("competition", "==", "CL"))
                    .where(filter=FieldFilter("match_date", ">=", week_start))
                    .where(filter=FieldFilter("match_date", "<=", week_end))
                    .limit(1)
                    .stream()
                )
                cl_away = list(
                    col("matches")
                    .where(filter=FieldFilter("away_team_id", "==", team_id))
                    .where(filter=FieldFilter("competition", "==", "CL"))
                    .where(filter=FieldFilter("match_date", ">=", week_start))
                    .where(filter=FieldFilter("match_date", "<=", week_end))
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
                    col("standings").where(filter=FieldFilter("team_id", "==", team_id)).limit(1).stream()
                )
                if not docs:
                    docs = list(
                        col("team_stats").where(filter=FieldFilter("team_id", "==", team_id)).limit(1).stream()
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
                    .where(filter=FieldFilter("home_team_id", "==", team_id))
                    .where(filter=FieldFilter("competition", "in", ["CL", "UCL"]))
                    .where(filter=FieldFilter("match_date", ">=", window_start))
                    .where(filter=FieldFilter("match_date", "<=", window_end))
                    .limit(1)
                    .stream()
                )
                cl_away = list(
                    col("matches")
                    .where(filter=FieldFilter("away_team_id", "==", team_id))
                    .where(filter=FieldFilter("competition", "in", ["CL", "UCL"]))
                    .where(filter=FieldFilter("match_date", ">=", window_start))
                    .where(filter=FieldFilter("match_date", "<=", window_end))
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
                            .where(filter=FieldFilter("competition", "==", comp))
                            .where(filter=FieldFilter("match_date", ">=", window_start))
                            .where(filter=FieldFilter("match_date", "<=", window_end_4))
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


# ── Árbitro: sesgo de penaltis con equipos grandes ────────────────────────────

_BIG_CLUBS: frozenset[str] = frozenset({
    "real madrid", "barcelona", "manchester city", "manchester united",
    "liverpool", "arsenal", "chelsea", "tottenham", "juventus", "inter",
    "milan", "ac milan", "napoli", "psg", "paris saint-germain",
    "bayern", "dortmund", "atletico", "atlético de madrid", "sevilla",
})


async def analyze_referee_penalty_bias(
    referee_name: str,
    api_key: str,
    quota_mgr=None,
) -> dict:
    """
    Analiza historial del árbitro para detectar sesgo de penaltis con equipos grandes.

    Fetch: GET https://v3.football.api-sports.io/fixtures
           ?referee={referee_name}&last=20
    Headers: x-rapidapi-key: {api_key}

    Por cada partido en el historial:
    - Si home_team o away_team está en _BIG_CLUBS: contar como "big_club_match"
    - Extraer penaltis pitados (fixture.events donde type=="Penalty")

    Calcula:
    - avg_penalties_big_clubs: media de penaltis por partido con grandes
    - avg_penalties_others: media de penaltis por partido sin grandes
    - penalty_bias: True si avg_big > avg_others * 1.4

    Returns:
    {
      referee_name: str,
      has_data: bool,
      avg_penalties_big: float,
      avg_penalties_others: float,
      penalty_bias: bool,
      bias_ratio: float,        # avg_big / avg_others
      n_big_matches: int,
      n_other_matches: int,
      note: str
    }

    Si no hay datos o falla: has_data=False, penalty_bias=False.
    """
    result: dict = {
        "referee_name": referee_name,
        "has_data": False,
        "avg_penalties_big": 0.0,
        "avg_penalties_others": 0.0,
        "penalty_bias": False,
        "bias_ratio": 1.0,
        "n_big_matches": 0,
        "n_other_matches": 0,
        "note": "",
    }

    if not referee_name or not api_key:
        return result

    if quota_mgr and not quota_mgr.can_call("api_sports"):
        logger.debug("analyze_referee_penalty_bias: sin quota api_sports")
        return result

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://v3.football.api-sports.io/fixtures",
                params={"referee": referee_name, "last": "20"},
                headers={
                    "x-rapidapi-key": api_key,
                    "x-rapidapi-host": "v3.football.api-sports.io",
                },
            )
        if quota_mgr:
            quota_mgr.track_call("api_sports")

        if resp.status_code != 200:
            logger.debug("analyze_referee_penalty_bias: API respondió %d", resp.status_code)
            return result

        fixtures = resp.json().get("response", [])
        if not fixtures:
            return result

        big_penalties: list[float] = []
        other_penalties: list[float] = []

        for fx in fixtures:
            fixture_info = fx.get("fixture", {})
            teams = fx.get("teams", {})
            events = fx.get("events", []) or []

            home_name = str(teams.get("home", {}).get("name", "")).lower()
            away_name = str(teams.get("away", {}).get("name", "")).lower()

            is_big = any(
                big in home_name or big in away_name
                for big in _BIG_CLUBS
            )

            pen_count = sum(
                1 for ev in events
                if str(ev.get("type", "")).lower() == "var"
                or "penalty" in str(ev.get("detail", "")).lower()
            )

            if is_big:
                big_penalties.append(float(pen_count))
            else:
                other_penalties.append(float(pen_count))

        avg_big = sum(big_penalties) / len(big_penalties) if big_penalties else 0.0
        avg_others = sum(other_penalties) / len(other_penalties) if other_penalties else 0.0
        bias_ratio = avg_big / avg_others if avg_others > 0 else 1.0
        penalty_bias = bias_ratio > 1.40 and len(big_penalties) >= 5

        result.update({
            "has_data": True,
            "avg_penalties_big": round(avg_big, 3),
            "avg_penalties_others": round(avg_others, 3),
            "penalty_bias": penalty_bias,
            "bias_ratio": round(bias_ratio, 3),
            "n_big_matches": len(big_penalties),
            "n_other_matches": len(other_penalties),
            "note": (
                f"📋 Árbitro pita {bias_ratio:.1f}x más penaltis con equipos grandes"
                if penalty_bias else ""
            ),
        })

        logger.info(
            "referee_penalty_bias: %s — big=%.2f others=%.2f ratio=%.2f bias=%s",
            referee_name, avg_big, avg_others, bias_ratio, penalty_bias,
        )

    except Exception as e:
        logger.warning("analyze_referee_penalty_bias: error — %s", e)

    return result


def apply_referee_bias_to_signal(signal: dict, referee_bias: dict) -> dict:
    """
    Ajusta señales de corners/tarjetas según sesgo del árbitro.
    - penalty_bias=True y apostando al equipo grande: boost confidence × 1.08
      (el árbitro favorece a equipos grandes en penaltis)
    - Para señales de market_type en ("corners", "bookings", "cards"):
      Solo ajusta si hay sesgo confirmado.
    Nunca falla.
    """
    try:
        if not referee_bias.get("penalty_bias"):
            return signal

        market_type = str(signal.get("market_type", "")).lower()
        team_to_back = str(signal.get("team_to_back", "")).lower()

        is_big_team = any(big in team_to_back for big in _BIG_CLUBS)
        is_relevant_market = market_type in (
            "corners", "bookings", "cards", "asian_handicap", "h2h", "btts"
        )

        if is_relevant_market and is_big_team:
            confidence = float(signal.get("confidence", 0.65))
            confidence = min(1.0, max(0.0, confidence * 1.08))
            signal["confidence"] = round(confidence, 4)
            logger.debug("referee_bias: boost × 1.08 para %s (penalty_bias)", team_to_back)

        signal["referee_penalty_bias"] = {
            "detected": True,
            "ratio": referee_bias.get("bias_ratio", 1.0),
            "note": referee_bias.get("note", ""),
        }
    except Exception as e:
        logger.warning("apply_referee_bias_to_signal: error — %s", e)

    return signal
