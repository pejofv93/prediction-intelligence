"""
Colector Mundial 2026 (FIFA World Cup 2026) vía Sofascore.

Datos verificados:
  ✓ Tournament ID=16, Season ID=58210
  ✓ 48 partidos de fase de grupos + KO ya calendarizados (empieza 11 jun 2026)
  ✓ 12 grupos (A-L) con 48 selecciones asignadas
  ✓ /team/{sf_id}/events/last/0 funciona para selecciones nacionales
  ✓ xG disponible en EURO 2024 y Copa América 2024 (hasXg=True)
  ✗ xG NO disponible en qualifiers ni amistosos
  ✗ xG de los propios partidos del WC solo estará disponible una vez jugados

Estrategia de datos:
  1. Partidos WC 2026: Sofascore como fuente primaria (no football-data.org free tier)
  2. Historial de forma: últimos 40 eventos por selección (2 páginas)
  3. xG: de EURO 2024 / Copa América 2024 para selecciones europeas/sudamericanas
     Para otras (CAF, AFC, CONCACAF) solo form score sin xG
  4. IDs en Firestore: prefijo "sf_" + sf_team_id (ej. "sf_4748" para Brasil)
     para no colisionar con IDs de equipos de club

IDs confirmados de selecciones (extraídos del fixture WC 2026):
  Brasil=4748, España=4698, Alemania=4711, Francia=4481, México=4781, USA=4724
  Los 48 IDs completos se extraen dinámicamente desde los grupos del torneo.
"""
import asyncio
import logging
from datetime import datetime, timezone

from collectors.sofascore_client import (
    TOURNAMENTS,
    fetch_team_events,
    fetch_tournament_events,
    fetch_tournament_standings,
    fetch_tournament_seasons,
    fetch_event_statistics,
    normalize_name,
)
from shared.firestore_client import col

logger = logging.getLogger(__name__)

_WC26 = TOURNAMENTS["world_cup_2026"]
WC26_TOURNAMENT_ID = _WC26["id"]      # 16
WC26_SEASON_ID     = _WC26["seasons"][2026]   # 58210

# Prefijo para documentos Firestore de selecciones nacionales
_NS_PREFIX = "sf_"

# xG source tournaments (hasXg=True confirmado en research)
_XG_SOURCE_SEASONS = [
    (TOURNAMENTS["euro_2024"]["id"],         TOURNAMENTS["euro_2024"]["seasons"][2024]),         # EURO 2024
    (TOURNAMENTS["copa_america_2024"]["id"],  TOURNAMENTS["copa_america_2024"]["seasons"][2024]), # Copa América 2024
]

# Caché de proceso: {nombre_norm: sf_team_id} para las 48 selecciones
_WC_TEAM_MAP: dict[str, int] = {}
_WC_TEAM_MAP_LOADED = False

# ---------------------------------------------------------------------------
# ELO inicial desde ranking FIFA junio 2026
# Clave: nombre exacto en The Odds API (se convierte a wc_{name.lower().replace(' ','_')})
# Valor: puntos FIFA junio 2026 — escala directamente compatible con DEFAULT_ELO=1500
# ---------------------------------------------------------------------------
_WC26_FIFA_ELO: dict[str, float] = {
    # UEFA (16 plazas)
    "Spain":                  1875.0,
    "France":                 1871.0,
    "England":                1828.0,
    "Portugal":               1768.0,
    "Netherlands":            1754.0,
    "Belgium":                1742.0,
    "Germany":                1736.0,
    "Croatia":                1715.0,
    "Switzerland":            1650.0,
    "Denmark":                1619.0,
    "Turkey":                 1606.0,
    "Austria":                1552.0,
    "Scotland":               1503.0,
    "Serbia":                 1502.0,
    "Czechia":                1490.0,
    "Bosnia and Herzegovina": 1475.0,
    "Slovakia":               1474.0,
    "Albania":                1452.0,
    # CONMEBOL (6 plazas)
    "Argentina":              1877.0,
    "Brazil":                 1766.0,
    "Colombia":               1698.0,
    "Uruguay":                1673.0,
    "Ecuador":                1599.0,
    "Venezuela":              1469.0,
    "Paraguay":               1505.0,
    "Bolivia":                1358.0,
    "Chile":                  1455.0,
    # CONCACAF (6 plazas + 3 anfitrones auto)
    "Mexico":                 1687.0,
    "USA":                    1671.0,
    "Canada":                 1559.0,
    "Panama":                 1539.0,
    "Costa Rica":             1443.0,
    "Honduras":               1415.0,
    "Haiti":                  1350.0,
    "Curaçao":                1355.0,
    "Jamaica":                1365.0,
    "Trinidad and Tobago":    1340.0,
    # AFC (8 plazas)
    "Japan":                  1662.0,
    "Iran":                   1620.0,
    "Korea Republic":         1592.0,
    "South Korea":            1592.0,  # alias The Odds API
    "Australia":              1579.0,
    "Uzbekistan":             1547.0,
    "Qatar":                  1430.0,
    "Iraq":                   1425.0,
    "Saudi Arabia":           1410.0,
    "Jordan":                 1390.0,
    "Indonesia":              1280.0,
    # CAF (9 plazas)
    "Morocco":                1755.0,
    "Senegal":                1684.0,
    "Algeria":                1571.0,
    "Nigeria":                1571.0,
    "Egypt":                  1562.0,
    "Ivory Coast":            1541.0,
    "Cote dIvoire":           1541.0,  # alias sin apóstrofo (Sofascore / FIFA)
    "Mali":                   1530.0,
    "Cameroon":               1481.0,
    "Ghana":                  1453.0,
    "Tunisia":                1476.0,
    "DR Congo":               1474.0,
    "Congo DR":               1474.0,  # alias FIFA
    "South Africa":           1415.0,
    "Tanzania":               1370.0,
    # OFC (1 plaza)
    "New Zealand":            1345.0,
    # UEFA (adicionales clasificados)
    "Sweden":                 1576.0,
    "Norway":                 1634.0,
    # CAF (adicionales)
    "Cape Verde":             1382.0,
    # CONCACAF aliases
    "United States":          1671.0,  # alias de "USA"
    "Curacao":                1355.0,  # alias sin ç (The Odds API)
    # UEFA aliases
    "Czech Republic":         1490.0,  # alias de "Czechia"
    "Bosnia & Herzegovina":   1475.0,  # alias de "Bosnia and Herzegovina"
}


async def init_wc26_national_elos() -> int:
    """
    Escribe ELO inicial (puntos FIFA junio 2026) en Firestore team_elo para las
    selecciones del WC 2026. Solo sobreescribe si el equipo tiene ELO default
    (1500.0) o si ya fue inicializado con fifa_init (actualización de ranking).
    Devuelve número de equipos escritos.
    """
    from datetime import datetime, timezone
    import asyncio

    now = datetime.now(timezone.utc)
    initialized = 0
    loop = asyncio.get_event_loop()

    for odds_name, elo_val in _WC26_FIFA_ELO.items():
        team_id = f"wc_{odds_name.lower().replace(' ', '_')}"
        try:
            doc_ref = col("team_elo").document(team_id)
            existing = await loop.run_in_executor(None, doc_ref.get)

            if existing.exists:
                data = existing.to_dict() or {}
                current = float(data.get("elo", 1500.0))
                source = data.get("source", "")
                # Conservar ELO si ya tiene historial real de partidos
                if abs(current - 1500.0) >= 1.0 and source not in ("fifa_init", ""):
                    logger.debug(
                        "init_wc26_elos: %s tiene ELO real %.0f (source=%s) — conservando",
                        team_id, current, source,
                    )
                    continue

            doc_ref.set({
                "team_id":    team_id,
                "team_name":  odds_name,
                "elo":        elo_val,
                "source":     "fifa_init",
                "updated_at": now,
                "elo_history": [],
            })
            initialized += 1
            logger.debug("init_wc26_elos: %s → %.0f", team_id, elo_val)

        except Exception:
            logger.warning("init_wc26_elos: error en %s", team_id, exc_info=True)

    logger.info("init_wc26_elos: %d selecciones WC 2026 inicializadas con ELO FIFA", initialized)
    return initialized


async def _discover_wc26_season_id() -> int:
    """
    Intenta descubrir el season_id activo para WC 2026 via /unique-tournament/16/seasons.
    Devuelve el ID de la temporada más reciente (mayor ID). Fallback: 58210.
    """
    try:
        seasons = await fetch_tournament_seasons(WC26_TOURNAMENT_ID)
        if seasons:
            def _year_val(s: dict) -> int:
                raw = str(s.get("year") or "0")
                # "2025/2026" → 2026, "2026" → 2026, 2026 → 2026
                part = raw.split("/")[-1].strip()
                try:
                    return int(part)
                except ValueError:
                    return 0

            for s in seasons:
                if _year_val(s) >= 2026:
                    sid = int(s.get("id", 0))
                    if sid and sid != WC26_SEASON_ID:
                        logger.info("sofascore_wc: season autodiscovered → %d (antes %d)", sid, WC26_SEASON_ID)
                    return sid if sid else WC26_SEASON_ID
            # ninguno con year>=2026, usar el de mayor ID
            by_id = sorted(seasons, key=lambda s: int(s.get("id", 0) or 0), reverse=True)
            sid = int(by_id[0].get("id", WC26_SEASON_ID) or WC26_SEASON_ID)
            logger.info("sofascore_wc: season por mayor ID → %d", sid)
            return sid
    except Exception:
        logger.debug("sofascore_wc: fallo autodiscovery de season", exc_info=True)
    return WC26_SEASON_ID


async def _load_wc26_team_map() -> dict[str, int]:
    """
    Extrae {nombre_normalizado: sf_team_id} de los grupos del WC 2026.
    Cubre las 48 selecciones participantes.
    """
    global _WC_TEAM_MAP, _WC_TEAM_MAP_LOADED
    if _WC_TEAM_MAP_LOADED:
        return _WC_TEAM_MAP

    try:
        season_id = await _discover_wc26_season_id()
        rows = await fetch_tournament_standings(WC26_TOURNAMENT_ID, season_id)
        for row in rows:
            team = row.get("team") or {}
            sf_id = team.get("id")
            if not sf_id:
                continue
            for field in ("name", "shortName"):
                raw = team.get(field) or ""
                if raw:
                    _WC_TEAM_MAP[normalize_name(raw)] = int(sf_id)
        logger.info("sofascore_wc: team map WC 2026 → %d selecciones", len(_WC_TEAM_MAP) // 2)
    except Exception:
        logger.warning("sofascore_wc: error cargando team map WC 2026", exc_info=True)

    # Completar con IDs confirmados para garantizar cobertura mínima
    _FALLBACK_IDS = {
        "brazil": 4748, "brasil": 4748,
        "spain": 4698, "espana": 4698,
        "germany": 4711, "alemania": 4711,
        "france": 4481, "francia": 4481,
        "mexico": 4781, "mexico": 4781,
        "united states": 4724, "usa": 4724, "estados unidos": 4724,
    }
    for k, v in _FALLBACK_IDS.items():
        if k not in _WC_TEAM_MAP:
            _WC_TEAM_MAP[k] = v

    _WC_TEAM_MAP_LOADED = True
    return _WC_TEAM_MAP


async def collect_wc2026_matches() -> list[dict]:
    """
    Devuelve todos los partidos calendarizados del WC 2026 (fase de grupos + KO).
    Páginas 0-4 = ~80 eventos (48 grupos + 32 KO).
    Formato compatible con upcoming_matches en Firestore.
    """
    season_id = await _discover_wc26_season_id()
    result: list[dict] = []
    for page in range(5):
        events = await fetch_tournament_events(
            WC26_TOURNAMENT_ID, season_id, page=page, direction="next"
        )
        if not events:
            break
        for e in events:
            ht = e.get("homeTeam") or {}
            at = e.get("awayTeam") or {}
            ts = e.get("startTimestamp")
            match_date = (
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                if ts else ""
            )
            status_type = (e.get("status") or {}).get("type", "notstarted")
            round_name = (e.get("roundInfo") or {}).get("name", "")
            try:
                result.append({
                    "match_id": f"WC26_SF_{e['id']}",
                    "home_team_id": f"{_NS_PREFIX}{ht['id']}",
                    "away_team_id": f"{_NS_PREFIX}{at['id']}",
                    "home_team": ht.get("name", ""),
                    "away_team": at.get("name", ""),
                    "home_sf_id": int(ht["id"]),
                    "away_sf_id": int(at["id"]),
                    "league": "WC26",
                    "sport": "football",
                    "source": "sofascore",
                    "match_date": match_date,
                    "round": round_name,
                    "status": "FINISHED" if status_type == "finished" else "SCHEDULED",
                })
            except (KeyError, TypeError):
                continue
        if len(events) < 10:
            break  # última página

    logger.info("sofascore_wc: %d partidos WC 2026 calendarizados", len(result))
    return result


async def _fetch_national_team_xg(sf_team_id: int, max_xg: int = 5) -> tuple[float | None, float | None, int]:
    """
    Extrae xG medio de los últimos max_xg partidos con hasXg=True para una selección.
    Solo EURO 2024 y Copa América 2024 tienen xG para selecciones en Sofascore.
    Devuelve (xg_for, xg_against, n_matches).
    """
    events = await fetch_team_events(sf_team_id, page=0)
    if not events:
        return None, None, 0

    xg_for: list[float] = []
    xg_against: list[float] = []
    calls = 0

    for e in events:
        if calls >= max_xg:
            break
        if not e.get("hasXg"):
            continue
        event_id = e.get("id")
        if not event_id:
            continue
        try:
            stats_data = await fetch_event_statistics(event_id)
            for group in stats_data.get("statistics", []):
                if group.get("period") != "ALL":
                    continue
                for item in group.get("groups", []):
                    for stat in item.get("statisticsItems", []):
                        if stat.get("name", "").lower() in ("expected goals", "xg", "xgoals"):
                            try:
                                h_val = float(stat.get("home") or 0)
                                a_val = float(stat.get("away") or 0)
                                is_home = (e.get("homeTeam") or {}).get("id") == sf_team_id
                                xg_for.append(h_val if is_home else a_val)
                                xg_against.append(a_val if is_home else h_val)
                            except (TypeError, ValueError):
                                pass
            calls += 1
            await asyncio.sleep(0.15)
        except Exception:
            logger.debug("sofascore_wc: xG event %d error", event_id, exc_info=True)

    if xg_for:
        return (
            round(sum(xg_for) / len(xg_for), 3),
            round(sum(xg_against) / len(xg_against), 3),
            len(xg_for),
        )
    return None, None, 0


def _compute_form_from_events(sf_team_id: int, events: list[dict]) -> dict:
    """
    Calcula form score ponderado (decaimiento temporal) desde los últimos 30 eventos.
    Solo partidos terminados. W=3 D=1 L=0, peso mayor a los más recientes.
    """
    results: list[str] = []
    for e in events[:30]:
        if (e.get("status") or {}).get("type") != "finished":
            continue
        wc = e.get("winnerCode")
        is_home = (e.get("homeTeam") or {}).get("id") == sf_team_id
        if wc == 0:
            results.append("D")
        elif (wc == 1 and is_home) or (wc == 2 and not is_home):
            results.append("W")
        else:
            results.append("L")

    if not results:
        return {}

    # Pesos decrecientes (partidos más recientes = más peso)
    weights = [1.0 / (i + 1) for i in range(len(results))]
    total_w = sum(weights)
    score_map = {"W": 3, "D": 1, "L": 0}
    weighted_pts = sum(score_map[r] * w for r, w in zip(results, weights))
    form_score = round((weighted_pts / (total_w * 3)) * 100, 1)

    # Racha actual
    streak_type = results[0]
    streak_count = 0
    for r in results:
        if r == streak_type:
            streak_count += 1
        else:
            break

    return {
        "form_score": form_score,
        "last_10": results[:10],
        "streak": {"type": streak_type.lower(), "count": streak_count},
        "sf_form_matches": len(results),
    }


async def collect_national_team_stats(sf_team_ids: list[int], team_name_map: dict[int, str]) -> None:
    """
    Para cada selección nacional, recoge historial reciente + xG y guarda en Firestore.
    Clave Firestore: "sf_{sf_team_id}" para no colisionar con IDs de club.

    sf_team_ids: lista de Sofascore team IDs de las selecciones
    team_name_map: {sf_team_id: "nombre equipo"} para el log
    """
    processed = 0
    for sf_id in sf_team_ids:
        team_name = team_name_map.get(sf_id, f"Team_{sf_id}")
        doc_key = f"{_NS_PREFIX}{sf_id}"
        try:
            # Historial: 2 páginas = ~40 partidos
            events: list[dict] = []
            for page in range(2):
                page_events = await fetch_team_events(sf_id, page=page)
                if not page_events:
                    break
                events.extend(page_events)
                if len(page_events) < 15:
                    break
                await asyncio.sleep(0.1)

            if not events:
                logger.debug("sofascore_wc: sin historial para %s (sf_id=%d)", team_name, sf_id)
                continue

            form_data = _compute_form_from_events(sf_id, events)
            if not form_data:
                continue

            # xG (solo para selecciones con EURO/Copa América recientes)
            xg_for, xg_against, xg_n = await _fetch_national_team_xg(sf_id)

            doc: dict = {
                "team_name": team_name,
                "sf_team_id": sf_id,
                "league": "WC26",
                "sport": "football",
                "source": "sofascore_wc",
                "updated_at": datetime.now(timezone.utc),
                **form_data,
            }
            if xg_for is not None:
                doc["sf_xg_for"] = xg_for
                doc["sf_xg_against"] = xg_against
                doc["sf_xg_matches"] = xg_n
                doc["sf_form_score"] = form_data.get("form_score", 50.0)

            col("team_stats").document(doc_key).set(doc, merge=True)
            processed += 1
            logger.info(
                "sofascore_wc: %s (sf=%d) form=%.0f%% last10=%s xG=%s",
                team_name, sf_id,
                form_data.get("form_score", 0),
                "".join(form_data.get("last_10", [])[:5]),
                f"{xg_for:.2f}/{xg_against:.2f}" if xg_for else "n/a",
            )
            await asyncio.sleep(0.2)

        except Exception:
            logger.warning("sofascore_wc: error procesando %s", team_name, exc_info=True)

    logger.info("sofascore_wc: %d/%d selecciones guardadas en Firestore", processed, len(sf_team_ids))


async def run_wc2026_collection() -> list[dict]:
    """
    Pipeline completo WC 2026:
      1. Carga team map (48 selecciones) desde grupos del torneo
      2. Fetch todos los partidos calendarizados
      3. Extrae todos los sf_team_ids únicos del fixture
      4. Recoge historial + xG para cada selección
      5. Guarda en Firestore y devuelve upcoming_matches

    Punto de entrada desde main.py.
    """
    logger.info("sofascore_wc: iniciando colección WC 2026")

    # 1. Team map
    team_map = await _load_wc26_team_map()

    # 2. Partidos calendarizados
    matches = await collect_wc2026_matches()
    if not matches:
        logger.warning("sofascore_wc: sin partidos WC 2026 — ¿torneo no iniciado aún?")
        return []

    # 3. IDs únicos de selecciones + nombres
    team_name_map: dict[int, str] = {}
    for m in matches:
        for side, name_key in (("home_sf_id", "home_team"), ("away_sf_id", "away_team")):
            sf_id = m.get(side)
            name = m.get(name_key, "")
            if sf_id and name:
                team_name_map[sf_id] = name

    logger.info("sofascore_wc: %d selecciones identificadas en el fixture", len(team_name_map))

    # 4. Stats por selección
    if team_name_map:
        await collect_national_team_stats(list(team_name_map.keys()), team_name_map)

    return matches
