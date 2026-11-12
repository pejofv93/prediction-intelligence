"""highlightly_players (Fase 2a): box score → doc, campos no fiables, emparejado con el CSV,
comparación, reparto de presupuesto y caché. Datos INVENTADOS con la estructura real de la API
(no se guardan respuestas reales en el repo). Sin red ni Firestore."""
import asyncio

import pytest

from collectors import highlightly_players as h


def _player(pid, name, pos, minutes, shots=0, sot=0, fc=0, fd=0, y=0, y2=None, r=0, saves=0, conceded=0):
    return {"id": pid, "name": name, "position": pos, "isSubstitute": False, "minutesPlayed": minutes,
            "matchRating": "7.0",
            "statistics": {"shotsTotal": shots, "shotsOnTarget": sot, "fouledOthers": fc, "fouledByOthers": fd,
                           "cardsYellow": y, "cardsSecondYellow": y if y2 is None else y2, "cardsRed": r,
                           "goalsSaved": saves, "goalsConceded": conceded, "goalsScored": 0}}


# Visitante PRIMERO, como hace a veces la API real
RAW = [
    {"team": {"id": 2, "name": "Visitante SV"}, "players": [
        _player(21, "Portero V", "Goalkeeper", 90, saves=4),
        _player(22, "Delantero V", "Forward", 90, shots=3, sot=1, fc=2, fd=1, y=1),
        _player(23, "Suplente V", "Goalkeeper", 0)]},
    {"team": {"id": 1, "name": "Local FC"}, "players": [
        _player(11, "Portero L", "Goalkeeper", 90, saves=1),
        _player(12, "Delantero L", "Forward", 70, shots=5, sot=3, fc=1, fd=3, r=1),
        _player(13, "Medio L", "Midfielder", 20, shots=1, fc=1, fd=0, y=1)]},
]
META = {"match_id": 999, "div": "D1", "season": "2627", "date": "2026-11-01",
        "home_team": "Local FC", "away_team": "Visitante SV", "home_team_id": 1, "away_team_id": 2}


def test_parse_ordena_local_visitante_por_id():
    doc = h.parse_box_score(RAW, META)
    assert [t["side"] for t in doc["teams"]] == ["home", "away"]
    assert doc["teams"][0]["team"] == "Local FC"


def test_parse_por_nombre_si_no_hay_id():
    meta = {**META, "home_team_id": None, "away_team_id": None}
    doc = h.parse_box_score(RAW, meta)
    assert doc["teams"][0]["team"] == "Local FC" and doc["teams"][1]["team"] == "Visitante SV"


def test_solo_jugadores_con_minutos():
    doc = h.parse_box_score(RAW, META)
    assert [p["player_id"] for p in doc["teams"][1]["players"]] == [21, 22]


def test_campos_no_fiables_fuera():
    doc = h.parse_box_score(RAW, META)
    p = doc["teams"][1]["players"][1]
    assert "goals_conceded" not in p and "second_yellows" not in p
    assert set(doc["unreliable_fields_dropped"]) == {"cardsSecondYellow", "goalsConceded"}
    # tarjetas = amarillas + rojas, sin sumar cardsSecondYellow (que copia la amarilla)
    assert p["cards"] == 1
    assert doc["teams"][0]["players"][1]["cards"] == 1  # solo roja


def test_totales_y_faltas_recibidas():
    home = h.parse_box_score(RAW, META)["teams"][0]["totals"]
    assert home["shots_total"] == 6 and home["shots_on_target"] == 3
    assert home["fouls_committed"] == 2 and home["fouls_drawn"] == 3
    assert home["saves"] == 1 and home["reds"] == 1


def test_box_score_vacio_o_incompleto():
    assert h.parse_box_score([], META) is None
    assert h.parse_box_score({"data": []}, META) is None
    assert h.parse_box_score(RAW[:1], META) is None


def test_comparacion_con_csv():
    doc = h.parse_box_score(RAW, META)
    fd = {"HomeTeam": "Local", "AwayTeam": "Visitante", "FTHG": 1, "FTAG": 2,
          "HS": 8, "AS": 3, "HST": 3, "AST": 1, "HF": 2, "AF": 2, "HY": 1, "AY": 1, "HR": 1, "AR": 0}
    out = h.compare_with_fdco(doc, fd, "2026-11-01_local_visitante")
    home, away = out["checks"]["home"], out["checks"]["away"]
    assert home["shots_suspect"] and home["shots_diff"] == -2      # 6 por jugador vs 8 en el CSV
    assert not away["shots_suspect"]
    assert home["saves_vs_derived"] == 1 - max(0, 1 - 2)           # derivadas 0 (autogol probable)
    assert away["saves_vs_derived"] == 4 - (3 - 1)                 # +2
    assert home["cards_diff"] == 0 and home["fouls_diff"] == 0
    assert out["fdco_match_key"] == "2026-11-01_local_visitante"


@pytest.mark.parametrize("hl,fd", [
    ("Eintracht Frankfurt", "Ein Frankfurt"), ("Hamburger SV", "Hamburg"),
    ("Racing Santander", "Santander"), ("Stade Brestois 29", "Brest"),
    ("Deportivo La Coruña", "La Coruna"), ("Newcastle United", "Newcastle"),
    ("Cadiz", "Cadiz"), ("PEC Zwolle", "Zwolle"),
])
def test_nombres_reales_emparejan(hl, fd):
    assert h.team_similarity(hl, fd) >= 0.75


def test_match_fdco_row_elige_el_partido_correcto():
    rows = [{"HomeTeam": "Union Berlin", "AwayTeam": "FC Koln"},
            {"HomeTeam": "Ein Frankfurt", "AwayTeam": "Hamburg"}]
    assert h.match_fdco_row("Eintracht Frankfurt", "Hamburger SV", rows)["HomeTeam"] == "Ein Frankfurt"
    assert h.match_fdco_row("Bayern Munich", "Dortmund", rows) is None


def test_presupuesto():
    assert h.requests_available(used_counter=17, remaining_reported=83) == 73   # 83 − 10 reserva
    assert h.requests_available(used_counter=5, remaining_reported=40) == 30    # la API manda si dice menos
    assert h.requests_available(used_counter=95, remaining_reported=None, limit=h.HL_DAILY_LIMIT) == 0
    assert h.split_budget(73, 48) == (48, 25)
    assert h.split_budget(20, 48) == (20, 0)


def test_limite_pro_hasta_la_fecha_y_gratuito_despues():
    from datetime import timedelta
    assert h.hl_daily_limit(h.HL_PRO_UNTIL) == 7500
    assert h.hl_daily_limit(h.HL_PRO_UNTIL + timedelta(days=1)) == 100
    # Con PRO, la cabecera de la API sigue mandando si dice menos (p. ej. plan ya caducado).
    assert h.requests_available(used_counter=0, remaining_reported=60, limit=7500) == 50


def test_pick_backfill_actual_primero_y_reciente_primero():
    pending = [
        {"match_id": 1, "season": "2526", "date": "2026-05-31"},
        {"match_id": 2, "season": "2627", "date": "2026-09-01"},
        {"match_id": 3, "season": "2627", "date": "2026-11-02"},
        {"match_id": 4, "season": "2627", "date": "2026-10-01", "box_status": "done"},
        {"match_id": 5, "season": "2627", "date": "2026-10-15", "attempts": 3, "box_status": "error"},
    ]
    assert [m["match_id"] for m in h.pick_backfill(pending, 10, "2627")] == [3, 2, 1]
    assert [m["match_id"] for m in h.pick_backfill(pending, 1, "2627")] == [3]


def test_estado_del_box_score():
    assert h.next_box_status({"x": 1}, finished=True, attempts=0) == ("done", 1)
    assert h.next_box_status(None, finished=False, attempts=0) == ("pending", 0)
    assert h.next_box_status(None, finished=True, attempts=0) == ("error", 1)
    assert h.next_box_status(None, finished=True, attempts=2) == ("empty", 3)


def test_listado_a_meta():
    m = {"id": 7, "date": "2026-11-01T16:30:00.000Z",
         "homeTeam": {"id": 1, "name": "Local FC"}, "awayTeam": {"id": 2, "name": "Visitante SV"},
         "state": {"description": "Finished", "score": {"current": "1 - 2"}},
         "league": {"id": 67162, "season": 2026}}
    meta = h.match_meta_from_listing(m, "D1", "2627")
    assert meta["date"] == "2026-11-01" and meta["finished"] and meta["hl_league_id"] == 67162
    assert meta["box_status"] is None and meta["attempts"] == 0


def test_temporada_highlightly():
    assert h.hl_season("2627") == 2026 and h.hl_season("2526") == 2025


class _Resp:
    def __init__(self, status, payload=None, remaining="50"):
        self.status_code, self._p, self.headers = status, payload, {"x-ratelimit-requests-remaining": remaining}

    def json(self):
        return self._p


class _Client:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    async def get(self, url, headers=None):
        self.calls.append(url)
        return self.resp


class _Quota:
    def __init__(self, used=0, remaining=None):
        self.doc = {"used": used, "remaining_reported": remaining}
        self.tracked = []

    def _get_doc(self, key):
        return self.doc

    def track_call(self, api, remaining=None):
        self.tracked.append((api, remaining))


def test_hl_get_cuenta_la_llamada_y_guarda_lo_que_queda():
    q, c = _Quota(used=3, remaining=97), _Client(_Resp(200, {"data": []}, remaining="96"))
    assert asyncio.run(h.hl_get(c, "k", "/matches?x=1", q)) == {"data": []}
    assert q.tracked == [("highlightly", "96")]


def test_hl_get_no_llama_sin_presupuesto():
    q, c = _Quota(used=h.hl_daily_limit()), _Client(_Resp(200, {}))   # contador propio agotado
    with pytest.raises(h.HighlightlyBudgetExhausted):
        asyncio.run(h.hl_get(c, "k", "/matches?x=1", q))
    assert c.calls == []
    q2 = _Quota(used=10, remaining=0)
    with pytest.raises(h.HighlightlyBudgetExhausted):
        asyncio.run(h.hl_get(_Client(_Resp(200, {})), "k", "/m", q2))


def test_hl_get_429_para():
    with pytest.raises(h.HighlightlyBudgetExhausted):
        asyncio.run(h.hl_get(_Client(_Resp(429)), "k", "/m", _Quota()))
