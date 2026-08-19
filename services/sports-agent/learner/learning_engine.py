"""
Motor de aprendizaje diario — ajusta pesos del modelo segun resultados reales.
Se ejecuta diariamente a las 02:00 UTC via learning-engine.yml.

Flujo:
  fetch_pending_results → check_result (football_api) → evaluate_prediction
  → update_weights → update_all_elos → actualiza model_weights + accuracy_log
"""
import asyncio
import logging
import unicodedata
from datetime import datetime, timedelta, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

from shared.config import DEFAULT_WEIGHTS, LEARNING_RATE, SUPPORTED_FOOTBALL_LEAGUES
from shared.firestore_client import col

logger = logging.getLogger(__name__)

# Tipos de error mapeados a los 4 signals del ensemble
ERROR_TYPES = [
    "poisson_overweighted",  # el modelo Poisson sobreestimo la probabilidad
    "elo_misleading",        # el rating ELO no reflejaba el estado real del equipo
    "form_misleading",       # la forma reciente era enganosa (lesiones, rotaciones)
    "h2h_irrelevant",        # el historial directo no era relevante para este partido
    "odds_inefficiency",     # la cuota era trampa (bookmaker tenia informacion privilegiada)
]

# Mapa de error_type → clave de weights para saber que peso reducir
# Solo aplica para data_source='statistical_model'
# Para groq_ai sports: no se ajustan pesos estadisticos
ERROR_TO_WEIGHT: dict[str, str | None] = {
    "poisson_overweighted": "poisson",
    "elo_misleading":       "elo",
    "form_misleading":      "form",
    "h2h_irrelevant":       "h2h",
    "odds_inefficiency":    None,  # no reduce ningun weight especifico
}

# Ligas de futbol con modelo estadistico completo
_FOOTBALL_LEAGUES = set(SUPPORTED_FOOTBALL_LEAGUES.keys())

# ── Parámetros por defecto de filtros de bloqueo ─────────────────────────────
_DEFAULT_FILTER_PARAMS: dict = {
    "HIGH_DRAW_PROB":   {"threshold": 0.30},
    "UNDERDOG_EXTREME": {"PD": 4.5, "SA": 4.5, "PL": 4.5, "BL1": 4.5, "FL1": 4.5},
    # Espejo de value_bet_engine._DEFAULT_FILTER_PARAMS — mantener en sincronía.
    "AWAY_ROAD_FORM": {
        "min_played":   4,
        "min_ppg":      1.00,
        "max_ppg_gap":  0.75,
        "min_gd":      -1.00,
        "no_data_conf": 0.70,
    },
}

# Límites máximos/mínimos que puede alcanzar cada parámetro por ajuste automático
_FILTER_PARAM_BOUNDS: dict = {
    "HIGH_DRAW_PROB":   {"threshold": (0.22, 0.40)},
    "UNDERDOG_EXTREME": {"PD": (3.5, 6.0), "SA": (3.5, 6.0), "PL": (3.5, 6.0),
                         "BL1": (3.5, 6.0), "FL1": (3.5, 6.0)},
    "AWAY_ROAD_FORM":   {"min_ppg": (0.70, 1.50), "max_ppg_gap": (0.40, 1.20),
                         "min_gd": (-1.50, -0.40), "no_data_conf": (0.65, 0.85)},
}

# Incremento por paso de ajuste
_FILTER_ADJUSTMENT_STEP: dict = {
    "HIGH_DRAW_PROB":   {"threshold": 0.02},
    "UNDERDOG_EXTREME": {"PD": 0.25, "SA": 0.25, "PL": 0.25, "BL1": 0.25, "FL1": 0.25},
    "AWAY_ROAD_FORM":   {"min_ppg": 0.05, "max_ppg_gap": 0.05,
                         "min_gd": 0.05, "no_data_conf": 0.02},
}

# Mapeo de market_type → bucket canónico para tracking de accuracy por mercado
_MARKET_BUCKETS: dict[str, str] = {
    "h2h":                      "1X2",
    "totals":                   "OVER_UNDER",
    "basketball_h1_totals":     "OVER_UNDER",
    "basketball_q1_totals":     "OVER_UNDER",
    "btts":                     "BTTS",
    "asian_handicap":           "ASIAN_HANDICAP",
    "spread":                   "ASIAN_HANDICAP",
    "basketball_h1_spread":     "ASIAN_HANDICAP",
    "double_chance":            "DOUBLE_CHANCE",
    # Sin bucket propio: se gradúan pero NO entran en accuracy_by_market.
    # Mejor no medirlos que medirlos en la cesta equivocada.
    "correct_score":            "CORRECT_SCORE",
    "ht_goals":                 "HT_GOALS",
    "corners_ou":               "CORNERS",
}


def _norm(s: str) -> str:
    """Normaliza string para comparación: strip, lower, sin acentos."""
    return (
        unicodedata.normalize("NFD", str(s).strip().lower())
        .encode("ascii", "ignore")
        .decode()
    )


# Sports cuyas predicciones expiradas siguen siendo graduables (fuente con histórico):
# basket (ESPN/Sofascore/Euroleague) y tenis. Fútbol expirado NO se reintenta.
_REGRADABLE_EXPIRED_SPORTS = {"basketball", "nba", "tennis"}
# Ventana máxima para reintentar expiradas — evita refetch infinito de partidos
# demasiado viejos para estar en el histórico de la fuente.
_EXPIRED_REGRADE_MAX_AGE = timedelta(days=60)


async def fetch_pending_results() -> list[dict]:
    """
    Busca predicciones en Firestore donde:
    - result == None (aun sin evaluar), o
    - result == "expired" pero el sport es graduable (basket/tenis): el sweeper de
      48h (_cleanup_stale_predictions) las marcó expired antes de que existiera su
      grader; las fuentes de basket/tenis tienen histórico → recuperables.
    - match_date < now - 24h (el partido ya debio jugarse)
    Devuelve lista de predicciones pendientes de evaluar.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    expired_floor = now - _EXPIRED_REGRADE_MAX_AGE

    def _parse_md(match_date):
        if match_date is None:
            return None
        if isinstance(match_date, str):
            try:
                match_date = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
            except ValueError:
                return None
        if hasattr(match_date, "tzinfo") and match_date.tzinfo is None:
            match_date = match_date.replace(tzinfo=timezone.utc)
        return match_date

    try:
        # Query equality en result — el filtro de match_date se aplica en Python
        # para evitar requerir indice compuesto en Firestore
        docs = col("predictions").where(filter=FieldFilter("result", "==", None)).stream()
        pending = []
        for doc in docs:
            data = doc.to_dict()
            # Preservar el doc ID de Firestore para garantizar que el update posterior
            # use el ID correcto aunque el campo match_id almacenado difiera
            data["_firestore_doc_id"] = doc.id
            md = _parse_md(data.get("match_date"))
            if md is None:
                continue
            if md < cutoff:
                pending.append(data)

        # Segundo query: expiradas de sports graduables (basket/tenis) dentro de ventana.
        try:
            exp_docs = (
                col("predictions")
                .where(filter=FieldFilter("result", "==", "expired"))
                .stream()
            )
            regraded = 0
            for doc in exp_docs:
                data = doc.to_dict()
                if str(data.get("sport", "")).lower() not in _REGRADABLE_EXPIRED_SPORTS:
                    continue
                data["_firestore_doc_id"] = doc.id
                md = _parse_md(data.get("match_date"))
                if md is None or md >= cutoff or md < expired_floor:
                    continue
                pending.append(data)
                regraded += 1
            if regraded:
                logger.info("fetch_pending_results: %d expiradas basket/tenis re-encoladas", regraded)
        except Exception:
            logger.warning("fetch_pending_results: error leyendo expiradas regraduables", exc_info=True)

        logger.info("fetch_pending_results: %d predicciones pendientes", len(pending))
        return pending

    except Exception:
        logger.error("fetch_pending_results: error leyendo Firestore", exc_info=True)
        return []


def _base_match_id(match_id: str) -> str:
    """
    Extrae el ID base del partido eliminando el sufijo de mercado.
    Solo aplica cuando el primer segmento es numérico (predicciones de fútbol).
    Ejemplos: "12345_btts" → "12345", "12345_ou25_oaio" → "12345"
    Tennis/basketball quedan igual: "tennis_oapiio_xyz" → "tennis_oapiio_xyz"
    """
    parts = match_id.split("_", 1)
    if len(parts) > 1 and parts[0].isdigit():
        return parts[0]
    return match_id


async def check_result(match_id: str) -> str | None:
    """
    Busca resultado en match_results (Firestore) primero.
    Fallback a football_api.get_match_result() si no está en la colección.
    Devuelve "HOME_WIN" | "AWAY_WIN" | "DRAW" | None.
    Nota: col("match_results") aplica el prefijo FIRESTORE_COLLECTION_PREFIX
    automáticamente → prodmatch_results en producción. No usar col("prodmatch_results")
    porque añadiría el prefijo dos veces → prodprodmatch_results.
    """
    _WINNER_MAP = {"H": "HOME_WIN", "A": "AWAY_WIN", "D": "DRAW"}
    # Para predicciones con sufijo de mercado (ej: "12345_btts"), usar el ID base
    base_id = _base_match_id(match_id)
    try:
        doc = col("match_results").document(base_id).get()
        if doc.exists:
            winner = doc.to_dict().get("winner")
            mapped = _WINNER_MAP.get(winner)
            if mapped:
                logger.debug("check_result(%s): encontrado en match_results → %s", match_id, mapped)
                return mapped
    except Exception:
        logger.warning("check_result(%s): error leyendo match_results, usando API", match_id, exc_info=True)

    try:
        from collectors.football_api import get_match_result
        result = await get_match_result(base_id)
        if result is None:
            return None
        return result.get("result")
    except Exception:
        logger.error("check_result(%s): error consultando API", match_id, exc_info=True)
        return None


async def check_score(match_id: str) -> tuple[int, int] | None:
    """
    Marcador final (goles_local, goles_visitante) o None.

    Existe porque check_result() estrecha el resultado a "HOME_WIN"/"AWAY_WIN"/"DRAW" y
    tira los goles, de modo que BTTS, totales, hándicap y resultado exacto eran
    inevaluables aunque el dato estuviera guardado. `match_results` ya persiste
    goals_home/goals_away (los escribe firestore_writer.update_finished_matches).
    """
    base_id = _base_match_id(match_id)
    try:
        doc = col("match_results").document(base_id).get()
        if doc.exists:
            d = doc.to_dict()
            gh, ga = d.get("goals_home"), d.get("goals_away")
            if gh is not None and ga is not None:
                return int(gh), int(ga)
    except Exception:
        logger.warning("check_score(%s): error leyendo match_results", match_id, exc_info=True)

    try:
        from collectors.football_api import get_match_result
        result = await get_match_result(base_id)
        if result:
            gh, ga = result.get("goals_home"), result.get("goals_away")
            if gh is not None and ga is not None:
                return int(gh), int(ga)
    except Exception:
        logger.debug("check_score(%s): sin marcador vía API", match_id, exc_info=True)
    return None


# Mercados que se gradúan con el marcador final. El resto sigue por la comparación
# clásica contra el nombre del equipo (1X2, tenis, baloncesto).
_SCORE_GRADED_MARKETS: frozenset[str] = frozenset({
    "btts", "totals", "asian_handicap", "correct_score",
})


def _grade_with_score(prediction: dict, gh: int, ga: int) -> dict | None:
    """
    Gradúa un mercado alternativo con el marcador final.
    Devuelve {"correct": bool|None, "error_type": None} — correct=None significa PUSH
    (la línea cayó exacta): ni acierto ni fallo, se excluye del ROI.
    Devuelve None si el mercado no se gradúa por marcador o faltan datos.
    """
    market = str(prediction.get("market_type") or "").lower()
    if market not in _SCORE_GRADED_MARKETS:
        return None
    sel = str(prediction.get("selection") or "").strip()
    total = gh + ga

    if market == "btts":
        both = gh > 0 and ga > 0
        yes = sel.lower() in ("yes", "sí", "si", "btts yes", "btts sí", "btts si")
        return {"correct": both if yes else (not both), "error_type": None}

    if market == "totals":
        line = prediction.get("line")
        if line is None:
            return None
        line = float(line)
        if abs(total - line) < 1e-9:          # línea entera clavada → push
            return {"correct": None, "error_type": None}
        over = sel.lower().startswith("over")
        return {"correct": (total > line) if over else (total < line), "error_type": None}

    if market == "asian_handicap":
        point = prediction.get("line")
        if point is None:
            return None
        point = float(point)
        # `line` se guarda SIEMPRE en referencia al LOCAL: -0.5 = el local concede medio gol.
        # Respaldando al local:     margen = (gh - ga) + point
        # Respaldando al visitante: su hándicap es el opuesto → (ga - gh) - point
        backed_home = _norm(sel) == _norm(prediction.get("home_team", ""))
        adj = (gh - ga) + point if backed_home else (ga - gh) - point
        if abs(adj) < 1e-9:                    # hándicap entero clavado → push
            return {"correct": None, "error_type": None}
        return {"correct": adj > 0, "error_type": None}

    if market == "correct_score":
        return {"correct": sel.replace(" ", "") == f"{gh}-{ga}", "error_type": None}

    return None


def evaluate_prediction(prediction: dict, actual_result: str,
                       score: tuple[int, int] | None = None) -> dict:
    """
    Determina si la prediccion fue correcta y clasifica el error_type si fallo.

    Logica de clasificacion de errores:
    - Identifica que signal tenia el valor mas alto (factor dominante)
    - Mapea ese factor al tipo de error correspondiente
    - Si la prediccion fue correcta, error_type = None

    Devuelve {"correct": bool, "error_type": str | None}
    """
    # Mercados alternativos (BTTS, totales, hándicap, resultado exacto) se gradúan con el
    # marcador, no comparando la selección con el nombre de un equipo. Sin esta rama
    # "Over 2.5" o "BTTS Sí" nunca casaban con home/away y caían siempre como fallidas.
    if score is not None:
        graded = _grade_with_score(prediction, score[0], score[1])
        if graded is not None:
            return graded

    # Fútbol usa team_to_back; tenis/basket usan selection (nombre del equipo/jugador).
    # team_to_back tiene prioridad → no cambia la graduación de fútbol existente.
    backed = _norm(prediction.get("team_to_back") or prediction.get("selection") or "")
    home_team = _norm(prediction.get("home_team", ""))
    away_team = _norm(prediction.get("away_team", ""))
    home_id = str(prediction.get("home_team_id", "")).strip()
    away_id = str(prediction.get("away_team_id", "")).strip()

    # Determinar si la prediccion fue correcta (comparación normalizada).
    # Basket no tiene empate → actual_result solo es HOME_WIN/AWAY_WIN.
    if backed == home_team or backed == home_id:
        correct = (actual_result == "HOME_WIN")
    elif backed == away_team or backed == away_id:
        correct = (actual_result == "AWAY_WIN")
    else:
        # No se puede determinar — considerar incorrecto
        logger.warning(
            "evaluate_prediction: backed '%s' no coincide con home/away '%s'/'%s'",
            backed, home_team, away_team,
        )
        correct = False

    if correct:
        return {"correct": True, "error_type": None}

    # Clasificar tipo de error para data_source='statistical_model'
    data_source = prediction.get("data_source", "statistical_model")
    if data_source != "statistical_model":
        return {"correct": False, "error_type": None}

    factors = prediction.get("factors", {})
    if not factors:
        return {"correct": False, "error_type": "poisson_overweighted"}

    # El signal mas determinante (mayor valor) es el responsable del error
    signal_to_error = {
        "poisson": "poisson_overweighted",
        "elo":     "elo_misleading",
        "form":    "form_misleading",
        "h2h":     "h2h_irrelevant",
    }

    # Buscar el factor con mayor desviacion respecto al resultado real
    # Si el resultado fue lo contrario a lo predicho, el factor mas alto fue el "culpable"
    relevant_factors = {k: v for k, v in factors.items() if k in signal_to_error}
    if not relevant_factors:
        return {"correct": False, "error_type": "odds_inefficiency"}

    # Verificar si las cuotas eran sospechosamente bajas (odds_inefficiency)
    odds = prediction.get("odds", 2.0)
    edge = prediction.get("edge", 0.0)
    if edge > 0.15 and odds < 1.5:
        # Cuota muy baja con edge muy alto — sospechoso
        return {"correct": False, "error_type": "odds_inefficiency"}

    # Identificar el factor dominante
    dominant_factor = max(relevant_factors, key=lambda k: relevant_factors[k])
    error_type = signal_to_error.get(dominant_factor, "poisson_overweighted")

    return {"correct": False, "error_type": error_type}


def update_weights(
    error_type: str | None,
    top_factor: str,
    current_weights: dict,
    correct: bool,
) -> dict:
    """
    Ajusta los pesos del ensemble segun el resultado de la prediccion.

    Si fallo:  weights[ERROR_TO_WEIGHT[error_type]] *= (1 - LEARNING_RATE)
    Si acierto: weights[top_factor] *= (1 + LEARNING_RATE * 0.6)
    Si error_type == None o "odds_inefficiency" → no cambiar pesos.

    Normaliza para que sumen 1.0.
    Clampea cada peso entre 0.05 y 0.60.
    Devuelve nuevos pesos.
    """
    weights = dict(current_weights)

    if correct:
        # Reforzar el factor dominante (aprendizaje mas lento para evitar overfitting)
        if top_factor in weights:
            weights[top_factor] *= (1.0 + LEARNING_RATE * 0.6)
    else:
        # Reducir el peso del factor culpable del error
        if error_type is not None and error_type != "odds_inefficiency":
            weight_key = ERROR_TO_WEIGHT.get(error_type)
            if weight_key and weight_key in weights:
                weights[weight_key] *= (1.0 - LEARNING_RATE)

    # Clampear entre 0.05 y 0.60 antes de normalizar
    for k in weights:
        weights[k] = max(0.05, min(0.60, weights[k]))

    # Normalizar para que sumen 1.0
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 6) for k, v in weights.items()}

    return weights


def calculate_accuracy(predictions: list[dict]) -> float:
    """
    Devuelve tasa de acierto (0.0–1.0) de la lista dada.
    Solo cuenta predicciones con correct != None.
    """
    evaluated = [p for p in predictions if p.get("correct") is not None]
    if not evaluated:
        return 0.0
    correct_count = sum(1 for p in evaluated if p.get("correct") is True)
    return round(correct_count / len(evaluated), 4)


def _dedup_pending(pending: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    Deduplication de predicciones pendientes (BUG 2: dos señales por mismo partido).

    Agrupa por (home_norm, away_norm, date_day, market_type).
    Dentro de cada grupo con >1 predicciones:
      - Prioridad: ID numérico > WC26_SF_* > WC26_OA_* > cualquier otro
      - El primero (mayor prioridad) es el "primario" para actualizar pesos
      - Los demás son duplicados → se actualizarán en Firestore con el mismo
        result/correct/error_type pero NO contribuyen al weight update

    Returns:
        unique: una predicción por grupo (para weight update)
        dup_map: {primary_firestore_doc_id: [duplicate_predictions]}
    """
    from collections import defaultdict

    def _match_key(p: dict) -> tuple:
        home_n = _norm(p.get("home_team", ""))
        away_n = _norm(p.get("away_team", ""))
        date_d = str(p.get("match_date", ""))[:10]
        mkt = p.get("market_type") or "h2h"
        return (home_n, away_n, date_d, mkt)

    def _id_priority(p: dict) -> int:
        mid = str(p.get("match_id", ""))
        if mid and mid[0].isdigit():
            return 0   # ID numérico football-data.org — más resoluble
        if mid.startswith("WC26_SF_"):
            return 1   # Sofascore ID
        if mid.startswith("WC26_OA_"):
            return 2   # Odds API hash — menos resoluble
        return 3

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in pending:
        groups[_match_key(p)].append(p)

    unique: list[dict] = []
    dup_map: dict[str, list[dict]] = {}

    for key, preds in groups.items():
        if len(preds) == 1:
            unique.append(preds[0])
            continue
        preds_sorted = sorted(preds, key=_id_priority)
        primary = preds_sorted[0]
        duplicates = preds_sorted[1:]
        unique.append(primary)
        primary_id = str(primary.get("_firestore_doc_id") or primary.get("match_id") or "")
        if primary_id and duplicates:
            dup_map[primary_id] = duplicates

    if dup_map:
        total_dups = sum(len(v) for v in dup_map.values())
        logger.info(
            "_dedup_pending: %d grupos con duplicados, %d predicciones excluidas de weight update",
            len(dup_map), total_dups,
        )
    return unique, dup_map


async def _resolve_wc26_extra(prediction: dict) -> str | None:
    """
    Resolución alternativa para WC26_OA_* y WC26_SF_* que check_result no puede manejar
    (IDs no numéricos → get_match_result() los ignora).

    Intenta en orden:
    1. Predicción hermana en Firestore (mismo home+away+fecha, result ya resuelto)
    2. match_results en Firestore, búsqueda por nombre de equipo
    3. football_api WC endpoint por equipos + fecha (football-data.org)

    Solo se llama para predicciones WC26_* cuyo partido ya debería haber terminado.
    """
    home_team = str(prediction.get("home_team") or "")
    away_team = str(prediction.get("away_team") or "")
    match_date = prediction.get("match_date")

    if not home_team or not away_team or not match_date:
        return None

    date_str = str(match_date)[:10]  # YYYY-MM-DD
    home_n = _norm(home_team)
    away_n = _norm(away_team)
    _WINNER_MAP = {"H": "HOME_WIN", "A": "AWAY_WIN", "D": "DRAW"}

    # 1. Predicción hermana ya resuelta (mismo home+away+fecha, result != None)
    try:
        sibling_docs = list(
            col("predictions")
            .where(filter=FieldFilter("home_team", "==", home_team))
            .limit(20)
            .stream()
        )
        for doc in sibling_docs:
            data = doc.to_dict()
            result = data.get("result")
            if not result or result in (None, "expired"):
                continue
            if (
                _norm(data.get("away_team", "")) == away_n
                and str(data.get("match_date", ""))[:10] == date_str
            ):
                logger.info(
                    "_resolve_wc26_extra: hermana encontrada en predictions — %s vs %s → %s",
                    home_team, away_team, result,
                )
                return result
    except Exception:
        logger.warning("_resolve_wc26_extra: error en búsqueda de hermana", exc_info=True)

    # 2. match_results por nombre de equipo
    try:
        mr_docs = list(
            col("match_results")
            .where(filter=FieldFilter("home_team", "==", home_team))
            .limit(20)
            .stream()
        )
        for doc in mr_docs:
            data = doc.to_dict()
            if (
                _norm(data.get("away_team", "")) == away_n
                and str(data.get("match_date", ""))[:10] == date_str
            ):
                winner = data.get("winner")
                mapped = _WINNER_MAP.get(winner)
                if mapped:
                    logger.info(
                        "_resolve_wc26_extra: match_results — %s vs %s → %s",
                        home_team, away_team, mapped,
                    )
                    return mapped
    except Exception:
        logger.warning("_resolve_wc26_extra: error en búsqueda match_results", exc_info=True)

    # 3. football-data.org WC endpoint por equipos + fecha
    try:
        from collectors.football_api import get_wc26_result_by_teams
        result = await get_wc26_result_by_teams(home_team, away_team, date_str)
        if result:
            logger.info(
                "_resolve_wc26_extra: football_api WC — %s vs %s → %s",
                home_team, away_team, result,
            )
            return result
    except Exception:
        logger.warning("_resolve_wc26_extra: error en football_api WC", exc_info=True)

    logger.debug(
        "_resolve_wc26_extra: sin resultado para %s vs %s (%s)",
        home_team, away_team, date_str,
    )
    return None


async def _resolve_basket_extra(prediction: dict) -> str | None:
    """
    Resolución de basket (h2h/moneyline) que check_result no maneja: sus match_id
    (ESPN numérico, ACB_SF_*, EUR_*) no son resolubles por football_api.
    Solo h2h — totals/spread necesitan marcador final (fase posterior).

    Intenta en orden (mismo patrón que _resolve_wc26_extra):
    1. Predicción hermana ya resuelta (mismo home+away+fecha, result != None)
    2. Fuente de resultados del league (NBA→ESPN, ACB→Sofascore, EUR→Euroleague)
    Devuelve 'HOME_WIN' | 'AWAY_WIN' | None.
    """
    market = prediction.get("market_type") or "h2h"
    if market != "h2h":
        return None

    home_team = str(prediction.get("home_team") or "")
    away_team = str(prediction.get("away_team") or "")
    league = str(prediction.get("league") or "")
    match_date = prediction.get("match_date")
    if not home_team or not away_team or not match_date:
        return None

    date_str = str(match_date)[:10]
    home_n = _norm(home_team)
    away_n = _norm(away_team)

    # 1. Predicción hermana ya resuelta (mismo home+away+fecha) — evita refetch.
    try:
        sibling_docs = list(
            col("predictions")
            .where(filter=FieldFilter("home_team", "==", home_team))
            .limit(20)
            .stream()
        )
        for doc in sibling_docs:
            data = doc.to_dict()
            result = data.get("result")
            if not result or result in (None, "expired"):
                continue
            if result not in ("HOME_WIN", "AWAY_WIN"):
                continue  # solo resultados de ganador válidos para h2h
            if (
                _norm(data.get("away_team", "")) == away_n
                and str(data.get("match_date", ""))[:10] == date_str
            ):
                logger.info(
                    "_resolve_basket_extra: hermana resuelta — %s vs %s → %s",
                    home_team, away_team, result,
                )
                return result
    except Exception:
        logger.warning("_resolve_basket_extra: error buscando hermana", exc_info=True)

    # 2. Fuente de resultados del league (pasa match_id para resolución ACB por id TheSportsDB).
    try:
        from collectors.basketball_collector import get_basketball_result_by_teams
        result = await get_basketball_result_by_teams(
            league, home_team, away_team, date_str,
            match_id=str(prediction.get("match_id") or ""),
        )
        if result:
            logger.info(
                "_resolve_basket_extra: %s — %s vs %s → %s",
                league, home_team, away_team, result,
            )
            return result
    except Exception:
        logger.warning("_resolve_basket_extra: error consultando fuente basket", exc_info=True)

    logger.debug(
        "_resolve_basket_extra: sin resultado para %s vs %s (%s, %s)",
        home_team, away_team, date_str, league,
    )
    return None


def _get_week_label(dt: datetime) -> str:
    """Devuelve etiqueta de semana ISO: ej. '2025-W14'."""
    iso_cal = dt.isocalendar()
    return f"{iso_cal.year}-W{iso_cal.week:02d}"


def _top_factor(signals: dict) -> str:
    """Devuelve la clave del signal con mayor valor en el prediction.factors."""
    valid = {k: v for k, v in signals.items() if k in DEFAULT_WEIGHTS}
    if not valid:
        return "poisson"
    return max(valid, key=lambda k: valid[k])


async def run_daily_learning() -> None:
    """
    Pipeline completo de aprendizaje diario:

    1. fetch_pending_results()
    2. Por cada prediccion: check_result() → evaluate_prediction() → update_weights()
    3. update_all_elos() con los partidos verificados (actualiza ELOs)
    4. Actualiza doc 'current' en model_weights con nuevos pesos + nueva version
    5. Calcula accuracy de la semana actual
    6. Guarda/actualiza accuracy_log para la semana actual
    7. Actualiza cada prediction en Firestore con result, correct, error_type
    """
    now = datetime.now(timezone.utc)
    current_week = _get_week_label(now)

    # --- 1. Obtener predicciones pendientes ---
    pending = await fetch_pending_results()
    if not pending:
        logger.info("run_daily_learning: sin predicciones pendientes")
        return

    logger.info("run_daily_learning: %d predicciones pendientes antes de dedup", len(pending))

    # Deduplicar por (home, away, fecha, mercado) — BUG 2: dos señales por mismo partido
    pending, _dup_map = _dedup_pending(pending)
    logger.info("run_daily_learning: procesando %d predicciones únicas", len(pending))

    # --- 2. Cargar pesos actuales ---
    _prev_conf_data: dict = {}
    try:
        weights_doc = col("model_weights").document("current").get()
        if weights_doc.exists:
            data = weights_doc.to_dict()
            current_weights = data.get("weights", dict(DEFAULT_WEIGHTS))
            current_version = int(data.get("version", 0))
            _prev_conf_data = data.get("accuracy_by_confidence", {})
        else:
            current_weights = dict(DEFAULT_WEIGHTS)
            current_version = 0
    except Exception:
        logger.error("run_daily_learning: error leyendo model_weights — usando defaults", exc_info=True)
        current_weights = dict(DEFAULT_WEIGHTS)
        current_version = 0

    weights_start = dict(current_weights)

    # --- 3. Procesar cada prediccion ---
    processed_predictions: list[dict] = []
    finished_matches_for_elo: list[dict] = []
    accuracy_by_league: dict[str, list[bool]] = {k: [] for k in _FOOTBALL_LEAGUES}
    accuracy_by_market: dict[str, list[bool]] = {
        "1X2": [], "OVER_UNDER": [], "BTTS": [], "ASIAN_HANDICAP": [], "DOUBLE_CHANCE": []
    }
    accuracy_by_confidence: dict[str, list[bool]] = {
        "65_70": [], "70_80": [], "80_90": [], "90_99": []
    }

    # 3a. Paralelizar todas las llamadas check_result (I/O bound → asyncio.gather)
    _match_ids = [str(p.get("match_id", "")) for p in pending]
    _raw_results = list(await asyncio.gather(
        *[check_result(mid) for mid in _match_ids],
        return_exceptions=True,
    ))
    logger.info(
        "run_daily_learning: check_result paralelo completado — %d/%d con resultado",
        sum(1 for r in _raw_results if r is not None and not isinstance(r, Exception)),
        len(_raw_results),
    )

    # Marcadores: necesarios para graduar BTTS/totales/hándicap/resultado exacto.
    # Solo se piden para predicciones de esos mercados — el 1X2 no los usa.
    _needs_score = [
        i for i, pr in enumerate(pending)
        if str(pr.get("market_type") or "").lower() in _SCORE_GRADED_MARKETS
    ]
    _scores: list[tuple[int, int] | None] = [None] * len(pending)
    if _needs_score:
        _fetched = await asyncio.gather(
            *[check_score(_match_ids[i]) for i in _needs_score],
            return_exceptions=True,
        )
        for i, sc in zip(_needs_score, _fetched):
            _scores[i] = sc if isinstance(sc, tuple) else None
        logger.info(
            "run_daily_learning: marcadores para mercados alternativos — %d/%d obtenidos",
            sum(1 for i in _needs_score if _scores[i] is not None), len(_needs_score),
        )

    # BUG 1: second-pass para WC26_OA_* / WC26_SF_* IDs no numéricos que check_result ignoró.
    # Solo se intenta para predicciones sin resultado cuyo partido ya debería estar terminado.
    _wc26_indices = [
        i for i, (p, r) in enumerate(zip(pending, _raw_results))
        if (r is None or isinstance(r, Exception))
        and str(p.get("match_id", "")).startswith("WC26_")
    ]
    if _wc26_indices:
        logger.info(
            "run_daily_learning: %d predicciones WC26 sin resultado — intentando resolución extra",
            len(_wc26_indices),
        )
        _wc26_extra = await asyncio.gather(
            *[_resolve_wc26_extra(pending[i]) for i in _wc26_indices],
            return_exceptions=True,
        )
        resolved_extra = 0
        for idx, extra_r in zip(_wc26_indices, _wc26_extra):
            if isinstance(extra_r, str):
                _raw_results[idx] = extra_r
                resolved_extra += 1
        logger.info(
            "run_daily_learning: WC26 extra-resolution — %d/%d resueltos",
            resolved_extra, len(_wc26_indices),
        )

    # Second-pass basket: tenis/basket no los resuelve check_result (solo fútbol).
    # Resolvemos basket h2h por nombres+fecha contra la fuente del league.
    _basket_indices = [
        i for i, (p, r) in enumerate(zip(pending, _raw_results))
        if (r is None or isinstance(r, Exception))
        and (
            str(p.get("sport", "")).lower() in ("basketball", "nba")
            or str(p.get("league", "")).upper() in ("NBA", "ACB", "EUROLEAGUE")
        )
        and (p.get("market_type") or "h2h") == "h2h"
    ]
    if _basket_indices:
        logger.info(
            "run_daily_learning: %d predicciones basket h2h sin resultado — resolución extra",
            len(_basket_indices),
        )
        _basket_extra = await asyncio.gather(
            *[_resolve_basket_extra(pending[i]) for i in _basket_indices],
            return_exceptions=True,
        )
        resolved_basket = 0
        for idx, extra_r in zip(_basket_indices, _basket_extra):
            if isinstance(extra_r, str):
                _raw_results[idx] = extra_r
                resolved_basket += 1
        logger.info(
            "run_daily_learning: basket extra-resolution — %d/%d resueltos",
            resolved_basket, len(_basket_indices),
        )

    groq_predictions_count = 0  # contador de predicciones groq_ai procesadas hoy

    # 3b. Procesar resultados en orden (weight updates son acumulativos)
    for prediction, actual_result, _score in zip(pending, _raw_results, _scores):
        match_id = prediction.get("match_id", "")
        league = prediction.get("league", "")

        try:
            if isinstance(actual_result, Exception):
                logger.error(
                    "run_daily_learning: check_result(%s) excepción — %s",
                    match_id, actual_result,
                )
                continue
            if actual_result is None:
                # Partido sin resultado todavia — omitir
                continue

            # Evaluar prediccion
            evaluation = evaluate_prediction(prediction, actual_result, score=_score)
            correct = evaluation["correct"]
            error_type = evaluation["error_type"]

            # PUSH (la línea cayó exacta): ni acierto ni fallo. Se marca resuelta pero se
            # excluye de pesos, accuracy y ROI — contarla como fallo sesgaría las métricas.
            if correct is None:
                logger.info(
                    "run_daily_learning: PUSH %s (%s %s) — excluida de métricas",
                    match_id, prediction.get("market_type"), prediction.get("selection"),
                )
                try:
                    col("predictions").document(match_id).update(
                        {"result": actual_result, "correct": None, "push": True}
                    )
                except Exception:
                    logger.warning("run_daily_learning: error marcando push %s", match_id, exc_info=True)
                continue

            # Identificar factor dominante
            factors = prediction.get("factors", {})
            top = _top_factor(factors)

            # Ajustar pesos para todas las predicciones resueltas — groq_ai incluida
            data_source = prediction.get("data_source", "statistical_model")
            if data_source == "groq_ai":
                groq_predictions_count += 1
            current_weights = update_weights(error_type, top, current_weights, correct)

            # Guardar para actualizacion de ELOs (partidos de futbol verificados)
            if league in _FOOTBALL_LEAGUES and prediction.get("home_team_id") and prediction.get("away_team_id"):
                finished_matches_for_elo.append({
                    "home_team_id": prediction.get("home_team_id"),
                    "away_team_id": prediction.get("away_team_id"),
                    "result": actual_result,
                    "date": str(prediction.get("match_date", "")),
                })

            # Acumular accuracy por liga
            if league in accuracy_by_league:
                accuracy_by_league[league].append(correct)

            # Acumular accuracy por tipo de mercado
            # OJO con el default: era "1X2", así que CUALQUIER mercado sin mapear caía en
            # la cesta del 1X2 y envenenaba la única métrica que hoy es fiable. market_type
            # se guarda a veces en mayúsculas ("CORRECT_SCORE"), de ahí el .lower().
            # Sin mapeo → cadena vacía → el guard de abajo lo descarta.
            market_type = str(prediction.get("market_type") or "h2h").lower()
            bucket = _MARKET_BUCKETS.get(market_type, "")
            if bucket in accuracy_by_market:
                accuracy_by_market[bucket].append(correct)
            elif not bucket:
                logger.debug(
                    "run_daily_learning: market_type '%s' sin bucket — fuera de accuracy_by_market",
                    prediction.get("market_type"),
                )

            # Acumular accuracy por bucket de confianza (calibración)
            _conf = float(prediction.get("confidence") or 0.0)
            if 0.65 <= _conf < 0.70:
                accuracy_by_confidence["65_70"].append(correct)
            elif 0.70 <= _conf < 0.80:
                accuracy_by_confidence["70_80"].append(correct)
            elif 0.80 <= _conf < 0.90:
                accuracy_by_confidence["80_90"].append(correct)
            elif _conf >= 0.90:
                accuracy_by_confidence["90_99"].append(correct)

            # Actualizar el documento prediction en Firestore
            processed_predictions.append({
                "match_id": match_id,
                # _firestore_doc_id garantiza el doc ID real aunque match_id difiera
                "_firestore_doc_id": prediction.get("_firestore_doc_id") or match_id,
                "result": actual_result,
                "correct": correct,
                "error_type": error_type,
            })

            # Sincronizar shadow_trade — crea el doc si no existe, luego lo resuelve
            try:
                from shared.shadow_engine import track_new_signal, update_trade_result
                shadow_result = "win" if correct else "loss"
                existing = list(
                    col("shadow_trades")
                    .where(filter=FieldFilter("signal_id", "==", str(match_id)))
                    .where(filter=FieldFilter("source", "==", "sports"))
                    .limit(1)
                    .stream()
                )
                if existing:
                    trade_id = existing[0].id
                    created = False
                else:
                    trade_id = await track_new_signal(prediction, "sports")
                    created = True
                await update_trade_result(trade_id, shadow_result)
                logger.info(
                    "run_daily_learning: shadow_trade %s → %s (%s trade_id=%s)",
                    match_id, shadow_result, "created" if created else "updated", trade_id,
                )
            except Exception:
                logger.error(
                    "run_daily_learning: error sincronizando shadow_trade para %s",
                    match_id, exc_info=True,
                )

            logger.debug(
                "run_daily_learning: %s → %s | correct=%s error=%s",
                match_id, actual_result, correct, error_type,
            )

        except Exception:
            logger.error(
                "run_daily_learning: error procesando prediccion %s", match_id, exc_info=True
            )

    # --- 3c. Propagar result/correct/error_type a predicciones duplicadas (BUG 2) ---
    # Los duplicados fueron excluidos del weight update pero deben recibir el resultado
    # en Firestore para no quedar atascados eternamente en pending.
    if _dup_map:
        _primary_results: dict[str, dict] = {
            str(p.get("_firestore_doc_id") or p.get("match_id") or ""): p
            for p in processed_predictions
        }
        propagated = 0
        for primary_id, dups in _dup_map.items():
            primary_proc = _primary_results.get(primary_id)
            if not primary_proc:
                continue
            payload = {
                "result":     primary_proc["result"],
                "correct":    primary_proc["correct"],
                "error_type": primary_proc["error_type"],
                "_resolved_from_duplicate": primary_id,
            }
            for dup in dups:
                dup_doc_id = str(dup.get("_firestore_doc_id") or dup.get("match_id") or "")
                if not dup_doc_id:
                    continue
                try:
                    col("predictions").document(dup_doc_id).update(payload)
                    propagated += 1
                except Exception:
                    logger.warning(
                        "run_daily_learning: error propagando resultado a duplicado %s", dup_doc_id, exc_info=True
                    )
        if propagated:
            logger.info("run_daily_learning: resultado propagado a %d predicciones duplicadas", propagated)

    # --- 4. Actualizar ELOs ---
    if finished_matches_for_elo:
        try:
            from enrichers.elo_rating import update_all_elos
            await update_all_elos(finished_matches_for_elo)
            logger.info("run_daily_learning: ELOs actualizados para %d partidos", len(finished_matches_for_elo))
        except Exception:
            logger.error("run_daily_learning: error actualizando ELOs", exc_info=True)

    if not processed_predictions:
        logger.info("run_daily_learning: ninguna prediccion pudo resolverse hoy")
        return

    # --- 5. Guardar model_weights actualizado ---
    # Calcular accuracy por liga
    acc_by_league = {
        league: round(sum(results) / len(results), 4) if results else 0.0
        for league, results in accuracy_by_league.items()
    }

    # Calcular accuracy por tipo de mercado
    acc_by_market = {
        bucket: round(sum(results) / len(results), 4) if results else None
        for bucket, results in accuracy_by_market.items()
    }

    # Calcular calibración acumulativa por bucket de confianza (merge con histórico)
    acc_by_confidence: dict[str, dict] = {}
    for _bkt in ["65_70", "70_80", "80_90", "90_99"]:
        _today_results = accuracy_by_confidence.get(_bkt, [])
        _prev = _prev_conf_data.get(_bkt, {"count": 0, "correct": 0})
        _total = int(_prev.get("count", 0)) + len(_today_results)
        _correct = int(_prev.get("correct", 0)) + sum(1 for r in _today_results if r)
        acc_by_confidence[_bkt] = {
            "count": _total,
            "correct": _correct,
            "rate": round(_correct / _total, 4) if _total > 0 else None,
        }
    logger.info(
        "run_daily_learning: calibración confianza — %s",
        {k: v["rate"] for k, v in acc_by_confidence.items() if v["rate"] is not None},
    )

    new_version = current_version + 1
    total_in_db, correct_in_db = _get_historical_counts()

    try:
        col("model_weights").document("current").set({
            "version": new_version,
            "updated": now,
            "weights": current_weights,
            "accuracy_by_league": acc_by_league,
            "accuracy_by_market": acc_by_market,
            "accuracy_by_confidence": acc_by_confidence,
            "blacklisted_leagues": [],
            "min_edge_threshold": 0.08,
            "min_confidence": 0.65,
            "total_predictions": total_in_db + len(processed_predictions),
            "correct_predictions": correct_in_db + sum(
                1 for p in processed_predictions if p.get("correct")
            ),
            "groq_predictions_count": groq_predictions_count,
        })
        logger.info(
            "run_daily_learning: model_weights actualizado → version %d pesos=%s",
            new_version, current_weights,
        )
    except Exception:
        logger.error("run_daily_learning: error guardando model_weights", exc_info=True)

    # --- 6. Actualizar accuracy_log de la semana ---
    week_predictions = [p for p in processed_predictions]
    week_accuracy = calculate_accuracy(
        [{"correct": p.get("correct")} for p in week_predictions]
    )

    # Buscar accuracy de la semana anterior para el delta del reporte
    prev_week_accuracy = _get_prev_week_accuracy(current_week)

    try:
        acc_log_ref = col("accuracy_log").document(current_week)
        acc_log_doc = acc_log_ref.get()

        if acc_log_doc.exists:
            existing = acc_log_doc.to_dict()
            total_prev = existing.get("predictions_total", 0)
            correct_prev = existing.get("predictions_correct", 0)
            total_new = total_prev + len(week_predictions)
            correct_new = correct_prev + sum(1 for p in week_predictions if p.get("correct"))
            updated_accuracy = round(correct_new / total_new, 4) if total_new > 0 else 0.0

            acc_log_ref.update({
                "predictions_total": total_new,
                "predictions_correct": correct_new,
                "accuracy": updated_accuracy,
                "accuracy_by_league": acc_by_league,
                "accuracy_by_market": acc_by_market,
                "weights_end": current_weights,
                "prev_week_accuracy": prev_week_accuracy,
            })
        else:
            correct_count = sum(1 for p in week_predictions if p.get("correct"))
            acc_log_ref.set({
                "week": current_week,
                "predictions_total": len(week_predictions),
                "predictions_correct": correct_count,
                "accuracy": week_accuracy,
                "prev_week_accuracy": prev_week_accuracy,
                "accuracy_by_league": acc_by_league,
                "accuracy_by_market": acc_by_market,
                "weights_start": weights_start,
                "weights_end": current_weights,
                "created_at": now,
            })

        logger.info(
            "run_daily_learning: accuracy_log[%s] actualizado — accuracy=%.1f%%",
            current_week, week_accuracy * 100,
        )
    except Exception:
        logger.error("run_daily_learning: error guardando accuracy_log", exc_info=True)

    # --- 7. Actualizar cada prediction con result/correct/error_type ---
    for upd in processed_predictions:
        payload = {
            "result": upd["result"],
            "correct": upd["correct"],
            "error_type": upd["error_type"],
        }
        mid = upd["match_id"]
        # Usar el Firestore doc ID real (preservado en fetch_pending_results)
        # para evitar 404 si el campo match_id almacenado difiere del doc ID
        doc_id = upd.get("_firestore_doc_id") or mid
        try:
            col("predictions").document(str(doc_id)).update(payload)
        except Exception:
            logger.error(
                "run_daily_learning: error actualizando prediction %s (doc_id=%s)", mid, doc_id, exc_info=True
            )
        # Actualizar también {doc_id}_synthetic si existe
        try:
            synthetic_ref = col("predictions").document(f"{doc_id}_synthetic")
            snap = synthetic_ref.get()
            if snap.exists:
                synthetic_ref.update(payload)
                logger.debug("run_daily_learning: %s_synthetic actualizado", doc_id)
        except Exception:
            logger.warning(
                "run_daily_learning: error actualizando %s_synthetic", doc_id, exc_info=True
            )

    # --- 8. Marcar predicciones obsoletas (>48h sin resultado) ---
    try:
        cutoff_48h = now - timedelta(hours=48)
        stale_docs = list(
            col("predictions")
            .where(filter=FieldFilter("result", "==", None))
            .stream()
        )
        obsolete_count = 0
        for doc in stale_docs:
            data = doc.to_dict()
            match_date = data.get("match_date")
            if match_date is None:
                continue
            if isinstance(match_date, str):
                try:
                    match_date = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
                except Exception:
                    continue
            if hasattr(match_date, "tzinfo") and match_date.tzinfo is None:
                match_date = match_date.replace(tzinfo=timezone.utc)
            if match_date < cutoff_48h and not data.get("obsolete"):
                try:
                    col("predictions").document(doc.id).update({"obsolete": True})
                    obsolete_count += 1
                except Exception:
                    pass
        if obsolete_count:
            logger.info(
                "run_daily_learning: %d predicciones marcadas obsoletas (>48h sin resultado)",
                obsolete_count,
            )
    except Exception:
        logger.warning("run_daily_learning: error marcando predicciones obsoletas", exc_info=True)

    logger.info(
        "run_daily_learning: completado — %d procesadas, accuracy semana=%.1f%%",
        len(processed_predictions), week_accuracy * 100,
    )

    # --- 8. Graduación diaria de bloqueos + evaluación semanal de filtros ---
    # La graduación va a diario aunque la evaluación sea semanal: match_results se limpia
    # a las 48h, así que un bloqueo que no se gradúa hoy puede quedarse sin veredicto.
    try:
        await grade_filter_blocks()
    except Exception:
        logger.error("run_daily_learning: error graduando bloqueos de filtro", exc_info=True)

    try:
        await _maybe_evaluate_filters(now)
    except Exception:
        logger.error("run_daily_learning: error evaluando filtros", exc_info=True)


def _get_historical_counts() -> tuple[int, int]:
    """Lee totales historicos de model_weights para acumularlos correctamente."""
    try:
        doc = col("model_weights").document("current").get()
        if doc.exists:
            data = doc.to_dict()
            return int(data.get("total_predictions", 0)), int(data.get("correct_predictions", 0))
    except Exception:
        pass
    return 0, 0


def _get_prev_week_accuracy(current_week: str) -> float | None:
    """Lee accuracy de la semana anterior desde accuracy_log."""
    try:
        # Calcular etiqueta de la semana anterior
        now = datetime.now(timezone.utc)
        prev_week_dt = now - timedelta(weeks=1)
        prev_week = _get_week_label(prev_week_dt)

        doc = col("accuracy_log").document(prev_week).get()
        if doc.exists:
            return doc.to_dict().get("accuracy")
    except Exception:
        logger.error("_get_prev_week_accuracy: error leyendo Firestore", exc_info=True)
    return None


def _adjust_filter_params(filter_name: str, params: dict, direction: str) -> dict:
    """
    Ajusta un único paso los parámetros de un filtro.
    direction: "relax" (menos agresivo) | "tighten" (más agresivo)
    """
    new_params = dict(params)
    bounds = _FILTER_PARAM_BOUNDS.get(filter_name, {})
    steps = _FILTER_ADJUSTMENT_STEP.get(filter_name, {})

    if filter_name == "HIGH_DRAW_PROB":
        step = steps.get("threshold", 0.02)
        lo, hi = bounds.get("threshold", (0.22, 0.40))
        cur = params.get("threshold", 0.30)
        # relax → aumentar umbral (bloquea menos partidos con posible empate)
        # tighten → reducir umbral (bloquea más partidos con posible empate)
        new_params["threshold"] = (
            min(hi, round(cur + step, 3)) if direction == "relax"
            else max(lo, round(cur - step, 3))
        )

    elif filter_name == "UNDERDOG_EXTREME":
        for lk in ["PD", "SA", "PL", "BL1", "FL1"]:
            if lk not in params:
                continue
            step = steps.get(lk, 0.25)
            lo, hi = bounds.get(lk, (3.5, 7.0))
            cur = params[lk]
            # relax → subir umbral de cuota (permite underdogs más grandes)
            # tighten → bajar umbral (bloquea underdogs más pequeños)
            new_params[lk] = (
                min(hi, round(cur + step, 2)) if direction == "relax"
                else max(lo, round(cur - step, 2))
            )

    elif filter_name == "AWAY_ROAD_FORM":
        # relax → exigir menos al visitante (menos ppg, más gap tolerado, peor gd,
        #          menos confianza sin muestra) → pasan más señales a visitante.
        # tighten → lo contrario. min_played no se auto-ajusta: cambiar el tamaño de
        #          muestra mueve señales entre las dos ramas del gate y haría ilegible
        #          la serie histórica de bloqueos.
        _sign = -1.0 if direction == "relax" else 1.0
        for _key, _default in (
            ("min_ppg", 1.00), ("min_gd", -1.00), ("no_data_conf", 0.70),
        ):
            if _key not in params:
                continue
            step = steps.get(_key, 0.05)
            lo, hi = bounds.get(_key, (-99.0, 99.0))
            new_params[_key] = max(lo, min(hi, round(params[_key] + _sign * step, 3)))
        if "max_ppg_gap" in params:
            # max_ppg_gap va al revés: subirlo tolera más ventaja del local (relax).
            step = steps.get("max_ppg_gap", 0.05)
            lo, hi = bounds.get("max_ppg_gap", (0.40, 1.20))
            new_params["max_ppg_gap"] = max(
                lo, min(hi, round(params["max_ppg_gap"] - _sign * step, 3))
            )

    return new_params


# Filtros con parámetros auto-ajustables. El resto de nombres que aparezcan en
# filter_blocks se gradúan y se reportan igual, pero no se tocan automáticamente:
# son cortes estructurales (EDGE_ZERO, BELOW_THRESHOLD…) cuyo umbral vive en otro sitio.
_TUNABLE_FILTERS: set[str] = set(_DEFAULT_FILTER_PARAMS)

# Margen tras el inicio del partido antes de intentar graduar un bloqueo.
_FILTER_BLOCK_GRADE_DELAY = timedelta(hours=3)


async def grade_filter_blocks(max_age_days: int = 30, max_docs: int = 3000) -> dict:
    """
    Gradúa los bloqueos registrados en filter_blocks: resuelve el resultado real del
    partido y escribe en el propio doc si la señal bloqueada HABRÍA acertado.

    Sin esto, filter_blocks es una lista de cortes sin veredicto: se sabe qué se dejó de
    apostar pero no si dejarlo fue acierto o error, que es justo lo que hay que medir para
    decidir si un filtro se queda, se relaja o se va.

    Escribe en cada doc graduado:
      result          — "HOME_WIN" | "AWAY_WIN" | "DRAW"
      would_have_won  — True si el equipo bloqueado ganó (None si no se pudo emparejar)
      graded_at       — timestamp

    Devuelve un resumen {"pendientes", "graduados", "aciertos", "sin_resultado"}.
    Nunca lanza.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    summary = {"pendientes": 0, "graduados": 0, "aciertos": 0,
               "sin_resultado": 0, "sin_emparejar": 0}

    # Un solo filtro de Firestore + criba en memoria: combinar "blocked_at >=" con
    # "result == None" exigiría un índice compuesto, y el despliegue de índices de este
    # proyecto no es fiable. Orden ascendente para que el tope de max_docs se lleve los
    # bloqueos más antiguos, que son justo los que ya tienen resultado.
    try:
        docs = list(
            col("filter_blocks")
            .where(filter=FieldFilter("blocked_at", ">=", cutoff))
            .order_by("blocked_at")
            .limit(max_docs)
            .stream()
        )
    except Exception:
        logger.error("grade_filter_blocks: error leyendo filter_blocks", exc_info=True)
        return summary

    # Solo partidos que ya deberían haber terminado. Sin match_date no se puede saber,
    # así que se intenta igual: check_result devolverá None si aún no hay resultado.
    pending: list[tuple[str, dict]] = []
    for doc in docs:
        data = doc.to_dict() or {}
        if data.get("result") is not None:
            continue  # ya graduado
        md = data.get("match_date")
        if isinstance(md, str):
            try:
                md = datetime.fromisoformat(md.replace("Z", "+00:00"))
            except Exception:
                md = None
        if md is not None and hasattr(md, "tzinfo"):
            if md.tzinfo is None:
                md = md.replace(tzinfo=timezone.utc)
            if md > now - _FILTER_BLOCK_GRADE_DELAY:
                continue  # aún no ha terminado
        pending.append((doc.id, data))

    summary["pendientes"] = len(pending)
    if not pending:
        logger.info("grade_filter_blocks: nada que graduar")
        return summary

    mids = sorted({str(d.get("match_id", "")) for _, d in pending if d.get("match_id")})
    sem = asyncio.Semaphore(8)

    async def _bounded(mid: str):
        async with sem:
            try:
                return await check_result(mid)
            except Exception:
                return None

    raw = await asyncio.gather(*[_bounded(m) for m in mids], return_exceptions=True)
    results_map = {m: (r if isinstance(r, str) else None) for m, r in zip(mids, raw)}

    updates: list[tuple[str, dict]] = []
    for doc_id, data in pending:
        actual = results_map.get(str(data.get("match_id", "")))
        if actual is None:
            summary["sin_resultado"] += 1
            continue
        ttb  = _norm(str(data.get("team_to_back", "")))
        home = _norm(str(data.get("home_team", "")))
        away = _norm(str(data.get("away_team", "")))
        if ttb and ttb == home:
            won = actual == "HOME_WIN"
        elif ttb and ttb == away:
            won = actual == "AWAY_WIN"
        else:
            # Guardar el resultado igualmente: sirve para depurar el emparejamiento
            # y evita re-consultar el mismo partido en cada pasada.
            summary["sin_emparejar"] += 1
            updates.append((doc_id, {"result": actual, "would_have_won": None,
                                     "grade_error": "team_unmatched", "graded_at": now}))
            continue
        summary["graduados"] += 1
        if won:
            summary["aciertos"] += 1
        updates.append((doc_id, {"result": actual, "would_have_won": won, "graded_at": now}))

    # Escritura por lotes — mismo motivo que en el registro (value_bet_engine).
    if updates:
        try:
            from shared.firestore_client import get_client
            client = get_client()
            for i in range(0, len(updates), 400):
                batch = client.batch()
                for doc_id, payload in updates[i:i + 400]:
                    batch.set(col("filter_blocks").document(doc_id), payload, merge=True)
                batch.commit()
        except Exception:
            logger.error("grade_filter_blocks: error escribiendo graduaciones", exc_info=True)

    _wr = (summary["aciertos"] / summary["graduados"]) if summary["graduados"] else 0.0
    logger.info(
        "grade_filter_blocks: %d pendientes → %d graduados (%d aciertos, %.1f%% win rate), "
        "%d sin resultado, %d sin emparejar",
        summary["pendientes"], summary["graduados"], summary["aciertos"], _wr * 100,
        summary["sin_resultado"], summary["sin_emparejar"],
    )
    return summary


async def evaluate_filter_performance() -> None:
    """
    Evalúa el rendimiento de cada filtro de bloqueo en las últimas 4 semanas.

    Para cada filtro:
      - Cuenta los bloqueos con resultado de partido conocido
      - Calcula win_rate: fracción de señales bloqueadas que habrían acertado
      - win_rate > 0.45 → relajar filtro (bloqueaba buenas señales)
      - win_rate < 0.30 → endurecer filtro (bloqueos eran correctos)
      - Requiere mínimo 10 bloqueos y 5 con resultado para ajustar

    Guarda resultado en model_weights/filter_performance.
    """
    now = datetime.now(timezone.utc)
    cutoff_4w = now - timedelta(weeks=4)

    logger.info(
        "evaluate_filter_performance: inicio — ventana 4 semanas desde %s",
        cutoff_4w.date(),
    )

    # --- 0. Graduar primero los bloqueos que ya tengan resultado ---
    try:
        await grade_filter_blocks()
    except Exception:
        logger.error("evaluate_filter_performance: error graduando bloqueos", exc_info=True)

    # --- 1. Leer todos los filter_blocks de las últimas 4 semanas ---
    # Se agrupan TODOS los nombres de filtro presentes, no solo los auto-ajustables:
    # los cortes estructurales (EDGE_ZERO, BELOW_THRESHOLD, LOW_POISSON…) no se tocan
    # solos, pero su win rate es justo lo que hay que ver para decidirlos a mano.
    blocks_by_filter: dict[str, list[dict]] = {k: [] for k in _DEFAULT_FILTER_PARAMS}
    try:
        docs = list(
            col("filter_blocks")
            .where(filter=FieldFilter("blocked_at", ">=", cutoff_4w))
            .stream()
        )
    except Exception:
        logger.error("evaluate_filter_performance: error leyendo filter_blocks", exc_info=True)
        return

    for doc in docs:
        data = doc.to_dict()
        fname = data.get("filter_name")
        if not fname:
            continue
        blocks_by_filter.setdefault(fname, []).append(data)

    logger.info(
        "evaluate_filter_performance: bloques encontrados — %s",
        {k: len(v) for k, v in sorted(blocks_by_filter.items())},
    )

    # --- 2. Resultados: usar la graduación ya escrita y consultar solo lo que falte ---
    missing_ids: set[str] = set()
    for blocks in blocks_by_filter.values():
        for b in blocks:
            if b.get("result") is None and b.get("match_id"):
                missing_ids.add(str(b["match_id"]))

    results_map: dict[str, str | None] = {}
    if missing_ids:
        mid_list = sorted(missing_ids)
        _sem = asyncio.Semaphore(8)

        async def _bounded_check(mid: str):
            async with _sem:
                try:
                    return await check_result(mid)
                except Exception:
                    return None

        raw_results = await asyncio.gather(
            *[_bounded_check(mid) for mid in mid_list],
            return_exceptions=True,
        )
        results_map = {
            mid: (r if isinstance(r, str) else None)
            for mid, r in zip(mid_list, raw_results)
        }

    if not blocks_by_filter or not any(blocks_by_filter.values()):
        logger.info("evaluate_filter_performance: sin bloqueos — saltando")
        return

    # --- 3. Cargar parámetros actuales (con fallback a defaults) ---
    current_params: dict = {k: dict(v) for k, v in _DEFAULT_FILTER_PARAMS.items()}
    try:
        fp_doc = col("model_weights").document("filter_performance").get()
        if fp_doc.exists:
            stored = fp_doc.to_dict().get("params", {})
            for fname, fparams in stored.items():
                if fname in current_params and isinstance(fparams, dict):
                    current_params[fname].update(fparams)
    except Exception:
        logger.warning(
            "evaluate_filter_performance: error leyendo params actuales — usando defaults"
        )

    # --- 4. Evaluar y ajustar cada filtro ---
    filter_stats: dict[str, dict] = {}
    updated_params: dict[str, dict] = {k: dict(v) for k, v in current_params.items()}

    for filter_name, blocks in sorted(blocks_by_filter.items()):
        _tunable = filter_name in _TUNABLE_FILTERS
        if len(blocks) < 10:
            logger.info(
                "evaluate_filter_performance: %s — %d bloques (min 10) — sin ajuste",
                filter_name, len(blocks),
            )
            filter_stats[filter_name] = {
                "blocks": len(blocks), "evaluated": 0, "tunable": _tunable,
                "win_rate": None, "action": "skip_insufficient_data",
            }
            continue

        wins = 0
        evaluated = 0
        _pnl = 0.0          # unidades a stake plano 1u sobre las señales bloqueadas
        for b in blocks:
            # Preferir la graduación ya escrita por grade_filter_blocks.
            correct = b.get("would_have_won")
            if correct is None:
                actual = b.get("result") or results_map.get(str(b.get("match_id", "")))
                if actual is None:
                    continue
                ttb = _norm(str(b.get("team_to_back", "")))
                home = _norm(str(b.get("home_team", "")))
                away = _norm(str(b.get("away_team", "")))
                if ttb == home:
                    correct = actual == "HOME_WIN"
                elif ttb == away:
                    correct = actual == "AWAY_WIN"
                else:
                    continue  # equipo no identificable — omitir
            evaluated += 1
            if correct:
                wins += 1
                try:
                    _pnl += float(b.get("odds") or 0.0) - 1.0
                except (TypeError, ValueError):
                    pass
            else:
                _pnl -= 1.0

        if evaluated < 5:
            logger.info(
                "evaluate_filter_performance: %s — solo %d evaluados con resultado — sin ajuste",
                filter_name, evaluated,
            )
            filter_stats[filter_name] = {
                "blocks": len(blocks), "evaluated": evaluated, "tunable": _tunable,
                "win_rate": None, "action": "skip_no_results",
            }
            continue

        win_rate = wins / evaluated
        roi = _pnl / evaluated
        action = "no_change"

        if not _tunable:
            # Corte estructural: se mide y se reporta, pero su umbral no vive aquí.
            action = "report_only"
        elif win_rate > 0.45:
            action = "relax"
            updated_params[filter_name] = _adjust_filter_params(
                filter_name, current_params[filter_name], direction="relax"
            )
        elif win_rate < 0.30:
            action = "tighten"
            updated_params[filter_name] = _adjust_filter_params(
                filter_name, current_params[filter_name], direction="tighten"
            )

        logger.info(
            "evaluate_filter_performance: %s — %d bloques, %d evaluados, "
            "win_rate=%.1f%% roi=%+.1f%% (%+.2fu) → %s",
            filter_name, len(blocks), evaluated, win_rate * 100,
            roi * 100, _pnl, action,
        )
        filter_stats[filter_name] = {
            "blocks": len(blocks),
            "evaluated": evaluated,
            "wins": wins,
            "win_rate": round(win_rate, 4),
            # ROI a stake plano de lo que se dejó de apostar: un filtro puede tener win
            # rate alto y seguir siendo correcto si cortaba cuotas malas, y al revés.
            "roi": round(roi, 4),
            "pnl_units": round(_pnl, 2),
            "tunable": _tunable,
            "action": action,
            "params_before": current_params.get(filter_name),
            "params_after": updated_params.get(filter_name),
        }

    # --- 5. Guardar resultado en Firestore ---
    try:
        col("model_weights").document("filter_performance").set({
            "params": updated_params,
            "last_evaluated": now,
            "stats": filter_stats,
        })
        logger.info(
            "evaluate_filter_performance: guardado — acciones=%s",
            {k: v.get("action", "?") for k, v in filter_stats.items()},
        )
    except Exception:
        logger.error(
            "evaluate_filter_performance: error guardando filter_performance", exc_info=True
        )


async def _maybe_evaluate_filters(now: datetime) -> None:
    """Ejecuta evaluate_filter_performance si han pasado ≥7 días desde la última evaluación."""
    try:
        fp_doc = col("model_weights").document("filter_performance").get()
        if fp_doc.exists:
            last_eval = fp_doc.to_dict().get("last_evaluated")
            if last_eval is not None:
                if hasattr(last_eval, "tzinfo") and last_eval.tzinfo is None:
                    last_eval = last_eval.replace(tzinfo=timezone.utc)
                if (now - last_eval).days < 7:
                    logger.info(
                        "_maybe_evaluate_filters: evaluación reciente (%s) — saltando",
                        last_eval.date(),
                    )
                    return
    except Exception:
        pass  # si falla la lectura, ejecutar igualmente
    await evaluate_filter_performance()
