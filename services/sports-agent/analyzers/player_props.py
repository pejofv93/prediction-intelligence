"""
services/sports-agent/analyzers/player_props.py

Goleador probable — modelo Poisson con xG de understat.com

Fuentes:
  1. understat.com POST /main/getPlayersStats/ — xG por jugador, sin key, ~590 jugadores/liga
  2. Fallback: football-data.org /competitions/{code}/scorers — top 20 goleadores por liga

Fórmula:
  P(marca) = 1 - Poisson(0, total_xG / partidos_jugados)

Umbral: P > 0.25 sin comparar con bookmaker.
  The Odds API, odds-api.io y OddsPapi NO tienen mercados de goleadores para soccer en free tier.
  Señales marcadas como "no_bookmaker": True.

Ligas: PD, PL, BL1, SA, FL1
"""
import gzip
import json
import logging
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from scipy.stats import poisson as _poisson

from shared.config import FOOTBALL_API_KEY, SPORTS_ALERT_EDGE

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT  = 15.0
_SQUAD_SIZE    = 11.0
_MIN_GAMES     = 10      # mínimo partidos jugados para que el xG sea significativo
_P_THRESHOLD   = 0.25   # señal si P(marca) > 25%
_MAX_PER_TEAM  = 3      # máximo señales por equipo por partido
_UNDERSTAT_TTL = timedelta(hours=6)

# Mapeo liga interna → nombre understat
_UNDERSTAT_LEAGUE_MAP: dict[str, str] = {
    "PD":  "La_liga",
    "PL":  "EPL",
    "BL1": "Bundesliga",
    "SA":  "Serie_A",
    "FL1": "Ligue_1",
}

# Mapeo liga → código football-data.org (fallback)
_FDORG_LEAGUE_MAP: dict[str, str] = {
    "PD":  "PD",
    "PL":  "PL",
    "BL1": "BL1",
    "SA":  "SA",
    "FL1": "FL1",
}

# Cache: {liga: (fetched_at, {player_name_lower: player_dict})}
_UNDERSTAT_CACHE: dict[str, tuple[datetime, dict]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normaliza texto: minúsculas, sin tildes."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower())
        if unicodedata.category(c) != "Mn"
    )


def _team_matches(understat_team: str, match_team: str) -> bool:
    """
    True si comparten al menos una palabra significativa (≥4 chars) tras normalizar.
    Cubre: "Real Madrid" ↔ "Real Madrid CF", "Atlético" ↔ "Atletico Madrid".
    """
    a = _norm(understat_team)
    b = _norm(match_team)
    for word in a.split():
        if len(word) >= 4 and word in b:
            return True
    for word in b.split():
        if len(word) >= 4 and word in a:
            return True
    return False


def _p_score(total_xg: float, games: int) -> float:
    """P(jugador marca ≥1 gol) usando su xG por partido de la temporada."""
    xg_per_game = total_xg / max(1, games)
    return round(1.0 - float(_poisson.pmf(0, max(0.01, xg_per_game))), 4)


# ── Fuente 1: understat.com ───────────────────────────────────────────────────

async def _fetch_understat_players(league: str) -> dict[str, dict]:
    """
    POST https://understat.com/main/getPlayersStats/
    Devuelve {player_name_lower: player_dict} para atacantes (position=F)
    con al menos _MIN_GAMES partidos. Cache _UNDERSTAT_TTL.

    Campos por jugador:
      name, team, position, games, minutes, goals, xg, assists, xa,
      xg_per90, xg_per_game, shots, key_passes
    """
    now = datetime.now(timezone.utc)
    cached = _UNDERSTAT_CACHE.get(league)
    if cached and (now - cached[0]) < _UNDERSTAT_TTL:
        return cached[1]

    understat_league = _UNDERSTAT_LEAGUE_MAP.get(league)
    if not understat_league:
        return {}

    for season in ("2025", "2024"):
        try:
            data = urllib.parse.urlencode({
                "league":  understat_league,
                "season":  season,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://understat.com/main/getPlayersStats/",
                data=data,
                method="POST",
                headers={
                    "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Content-Type":      "application/x-www-form-urlencoded",
                    "X-Requested-With":  "XMLHttpRequest",
                    "Accept-Encoding":   "identity",
                },
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            parsed = json.loads(raw.decode("utf-8"))
            players_raw = parsed.get("players", [])
            if not players_raw:
                logger.debug("understat(%s season=%s): lista vacía — probando anterior", league, season)
                continue

            result: dict[str, dict] = {}
            for p in players_raw:
                games = int(p.get("games", 0))
                if games < _MIN_GAMES:
                    continue
                if p.get("position", "") != "F":
                    continue
                mins  = int(p.get("time", 1)) or 1
                xg    = float(p.get("xG", 0))
                xa    = float(p.get("xA", 0))
                name  = p.get("player_name", "")
                result[_norm(name)] = {
                    "name":        name,
                    "team":        p.get("team_title", ""),
                    "position":    p.get("position", ""),
                    "games":       games,
                    "minutes":     mins,
                    "goals":       int(p.get("goals", 0)),
                    "xg":          round(xg, 4),
                    "assists":     int(p.get("assists", 0)),
                    "xa":          round(xa, 4),
                    "xg_per90":    round(xg / mins * 90, 4),
                    "xg_per_game": round(xg / games, 4),
                    "shots":       int(p.get("shots", 0)),
                    "key_passes":  int(p.get("key_passes", 0)),
                    "source":      "understat",
                }
            _UNDERSTAT_CACHE[league] = (now, result)
            logger.info("understat(%s season=%s): %d atacantes cargados", league, season, len(result))
            return result

        except Exception:
            logger.warning("understat(%s season=%s): fetch falló", league, season, exc_info=True)

    # Devolver caché antigua si existe aunque esté expirada
    if cached:
        logger.warning("understat(%s): usando caché expirada", league)
        return cached[1]
    return {}


# ── Fuente 2: football-data.org /scorers (fallback) ──────────────────────────

async def _fetch_fdorg_scorers(league: str) -> dict[str, dict]:
    """
    GET https://api.football-data.org/v4/competitions/{code}/scorers?limit=20
    Devuelve el mismo formato que _fetch_understat_players para ser intercambiable.
    goals_per_game usado como proxy de xG (sobreestima ligeramente).
    """
    if not FOOTBALL_API_KEY:
        return {}
    code = _FDORG_LEAGUE_MAP.get(league)
    if not code:
        return {}
    try:
        url = f"https://api.football-data.org/v4/competitions/{code}/scorers?limit=20"
        req = urllib.request.Request(
            url,
            headers={"X-Auth-Token": FOOTBALL_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))

        result: dict[str, dict] = {}
        for s in data.get("scorers", []):
            p     = s.get("player", {})
            name  = p.get("name", "")
            goals = int(s.get("goals", 0))
            played = max(1, int(s.get("playedMatches", 1)))
            if played < _MIN_GAMES:
                continue
            # proxy: goals como xG (top scorers suelen tener xG ≈ goals)
            gpg = goals / played
            result[_norm(name)] = {
                "name":        name,
                "team":        s.get("team", {}).get("shortName", ""),
                "position":    "F",
                "games":       played,
                "minutes":     played * 80,  # estimación
                "goals":       goals,
                "xg":          round(goals * 0.9, 3),  # pequeño descuento
                "assists":     int(s.get("assists", 0) or 0),
                "xa":          0.0,
                "xg_per90":    round(gpg * 90 / 80, 4),
                "xg_per_game": round(gpg, 4),
                "shots":       0,
                "key_passes":  0,
                "source":      "fdorg_scorers",
            }
        logger.info("fdorg_scorers(%s): %d jugadores cargados", league, len(result))
        return result

    except Exception:
        logger.warning("fdorg_scorers(%s): fetch falló", league, exc_info=True)
        return {}


# ── Señal sin bookmaker ───────────────────────────────────────────────────────

def _make_model_signal(
    match_id: str, home_team: str, away_team: str,
    league: str, match_date,
    player: dict, is_home_player: bool,
    prob: float, weights_version: int,
) -> dict:
    """
    Genera señal de goleador probable SIN comparar con bookmaker.
    odds = cuota implícita del modelo (1/prob).
    edge = 0 — sin validación externa.
    """
    from analyzers.value_bet_engine import kelly_criterion
    implied_odds = round(1.0 / max(0.01, prob), 2)
    team = home_team if is_home_player else away_team
    return {
        "match_id":        f"{match_id}_prop_{_norm(player['name'])[:18].replace(' ', '_')}",
        "home_team":       home_team,
        "away_team":       away_team,
        "sport":           "football",
        "league":          league,
        "market_type":     "anytime_scorer",
        "market":          "anytime_scorer",
        "selection":       player["name"],
        "team":            team,
        "odds":            implied_odds,
        "calculated_prob": prob,
        "edge":            0.0,
        "confidence":      round(prob, 4),
        "kelly_fraction":  0.0,
        "signals":         {},
        "factors": {
            "player_prob":   round(prob, 4),
            "xg_per_game":   player["xg_per_game"],
            "xg_per90":      player["xg_per90"],
            "games":         player["games"],
            "goals":         player["goals"],
        },
        "data_source":     f"player_props_{player['source']}",
        "odds_source":     "model_only",
        "no_bookmaker":    True,
        "match_date":      match_date,
        "weights_version": weights_version,
        "created_at":      datetime.now(timezone.utc),
        "result":          None,
        "correct":         None,
        "error_type":      None,
        "elo_sufficient":  False,
        "h2h_sufficient":  False,
    }


def _build_player_alert(
    player_name: str, player_team: str,
    home_team: str, away_team: str,
    prob: float, xg_per90: float, xg_per_game: float,
) -> str:
    """Formato Telegram específico para goleadores probables."""
    opponent = away_team if player_team in home_team or any(
        w in _norm(home_team) for w in _norm(player_team).split() if len(w) >= 4
    ) else home_team
    return (
        f"⚽ GOLEADOR PROBABLE\n"
        f"{player_name} ({player_team}) vs {opponent}\n"
        f"Prob. marcar: {prob * 100:.0f}%\n"
        f"xG/90: {xg_per90:.2f} | xG/partido: {xg_per_game:.2f}\n"
        f"⚠️ Sin validación de bookmaker"
    )


# ── Generación de señales ─────────────────────────────────────────────────────

async def generate_player_props_signals(
    enriched_match: dict,
    weights_version: int = 0,
) -> list[dict]:
    """
    Genera señales de goleador probable para un partido.

    Flujo:
    1. Carga atacantes de understat (con fallback a fdorg_scorers)
    2. Filtra jugadores que pertenecen a home_team o away_team
    3. P(marca) = 1 - Poisson(0, xG_total / partidos)
    4. Si P > 0.25: guarda en Firestore + alerta Telegram
    5. Máximo _MAX_PER_TEAM señales por equipo (ordenadas por P desc)
    """
    from shared.firestore_client import col
    from analyzers.value_bet_engine import _send_telegram_alert

    match_id   = str(enriched_match.get("match_id", ""))
    home_team  = enriched_match.get("home_team", "")
    away_team  = enriched_match.get("away_team", "")
    league     = enriched_match.get("league", "")
    match_date = enriched_match.get("match_date")

    if league not in _UNDERSTAT_LEAGUE_MAP:
        return []

    # 1. Cargar stats de jugadores (understat → fdorg fallback)
    players = await _fetch_understat_players(league)
    if not players:
        logger.info("player_props(%s): understat vacío — intentando fdorg_scorers", match_id)
        players = await _fetch_fdorg_scorers(league)
    if not players:
        logger.debug("player_props(%s): sin datos de jugadores para %s", match_id, league)
        return []

    # 2. Separar jugadores por equipo
    home_players: list[dict] = []
    away_players: list[dict] = []
    for p in players.values():
        if _team_matches(p["team"], home_team):
            home_players.append(p)
        elif _team_matches(p["team"], away_team):
            away_players.append(p)

    if not home_players and not away_players:
        logger.debug(
            "player_props(%s): sin jugadores localizados — home=%s away=%s",
            match_id, home_team, away_team,
        )
        return []

    # 3 & 4. Calcular P(marca) y generar señales
    predictions: list[dict] = []

    for is_home, team_players in ((True, home_players), (False, away_players)):
        candidates: list[tuple[float, dict]] = []
        for p in team_players:
            prob = _p_score(p["xg"], p["games"])
            if prob >= _P_THRESHOLD:
                candidates.append((prob, p))
            logger.info(
                "PLAYER_PROP: %s P=%.0f%% xG/90=%.2f partido=%s vs %s",
                p["name"], prob * 100, p["xg_per90"], home_team, away_team,
            )

        # Ordenar por P desc, limitar a _MAX_PER_TEAM
        candidates.sort(key=lambda x: x[0], reverse=True)
        for prob, p in candidates[:_MAX_PER_TEAM]:
            signal = _make_model_signal(
                match_id, home_team, away_team, league, match_date,
                p, is_home, prob, weights_version,
            )
            doc_id = signal["match_id"]
            try:
                col("predictions").document(doc_id).set(signal)
            except Exception:
                logger.error("player_props: error guardando %s", doc_id, exc_info=True)

            # Alerta Telegram si P > umbral de alerta (reutilizamos SPORTS_ALERT_EDGE como proxy)
            if prob >= max(_P_THRESHOLD, SPORTS_ALERT_EDGE + 0.5):
                alert_text = _build_player_alert(
                    p["name"], p["team"], home_team, away_team,
                    prob, p["xg_per90"], p["xg_per_game"],
                )
                await _send_telegram_alert({
                    **signal,
                    "telegram_text":  alert_text,
                    "market_emoji":   "⚽",
                    "intensity":      "🔥" if prob >= 0.45 else "✅",
                    "no_bookmaker":   True,
                })
            predictions.append(signal)

    if predictions:
        logger.info(
            "player_props(%s): %d señales — %s vs %s",
            match_id, len(predictions), home_team, away_team,
        )
    return predictions
