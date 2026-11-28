"""Fichas de baloncesto (bp-v1, Euroliga) y su graduación. Datos INVENTADOS (equipos de mentira).
Sin red ni Firestore: el feed, eur_games, el backtest y lo ya escrito se pasan a mano."""
import asyncio
import random
from datetime import date, datetime, timedelta, timezone

from analyzers import basket_preview as bp
from analyzers import basket_preview_live as bl

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
TEAMS = [("ZRV", "Zorrovia Norte"), ("QMR", "Quimeria Valle"), ("PRT", "Puerto Ficticio"),
         ("CLN", "Colina Inventada"), ("BAH", "Bahía Imaginaria"), ("RIB", "Ribera de Mentira")]


def _side(rng, pts=80):
    return {"pts": pts + rng.randint(-12, 12), "reb": 35 + rng.randint(-5, 5), "ast": 18 + rng.randint(-4, 4),
            "fg3m": 10 + rng.randint(-3, 3), "poss_est": 74.0 + rng.randint(-3, 3)}


def _docs():
    """eur_games inventados: E2025 entera y E2026 hasta el día anterior a NOW."""
    rng = random.Random(5)
    out = []
    for season, start in (("E2025", date(2025, 10, 1)), ("E2026", date(2026, 10, 1))):
        pairs = [(h, a) for h in TEAMS for a in TEAMS if h != a]
        for r in range(len(pairs) // 3):
            day = start + timedelta(days=4 * r)
            if day >= NOW.date():
                break
            for j, (h, a) in enumerate(pairs[r * 3:(r + 1) * 3]):
                out.append({"season": season, "gamecode": 100 * r + j, "date": day.isoformat(), "phase": "RS",
                            "round": r + 1, "home_code": h[0], "away_code": a[0], "home_name": h[1],
                            "away_name": a[1], "ot_periods": 0,
                            "home": _side(rng, 86 if h[0] == "ZRV" else 80), "away": _side(rng)})
    return out


def _feed_game(code, home, away, ko, status="confirmed"):
    return {"identifier": f"E2026_{code}", "code": code, "season": {"code": "E2026"}, "date": ko,
            "status": status, "phaseType": {"code": "RS"}, "round": {"round": 9},
            "home": {"code": home[0], "name": home[1]}, "away": {"code": away[0], "name": away[1]}}


FEED = [_feed_game(801, TEAMS[0], TEAMS[1], "2026-11-26T19:00:00.000Z"),
        _feed_game(802, TEAMS[2], TEAMS[3], "2026-11-27T19:45:00.000Z"),
        _feed_game(803, TEAMS[4], TEAMS[5], "2026-12-05T19:00:00.000Z"),             # fuera de la ventana
        _feed_game(700, TEAMS[1], TEAMS[0], "2026-11-20T19:00:00.000Z", "result")]  # ya jugado

BACKTEST = {"lines": {"pts_team": [75.5, 80.5, 85.5], "pts_total": [155.5, 160.5, 165.5],
                      "reb_team": [32.5, 35.5, 38.5], "reb_total": [67.5, 70.5, 73.5],
                      "ast_team": [15.5, 18.5, 21.5], "ast_total": [33.5, 36.5, 39.5],
                      "fg3m_team": [7.5, 10.5, 12.5], "fg3m_total": [17.5, 20.5, 23.5]},
            "labels": {"pts_team": "ventaja medida", "reb_team": "ventaja medida", "pts_total": "solo referencia"}}


def _run(**kw):
    kw.setdefault("feed", FEED)
    kw.setdefault("docs", _docs())
    kw.setdefault("backtest", BACKTEST)
    kw.setdefault("existing", {})
    return asyncio.run(bl.run_basket_previews(now=kw.pop("now", NOW), **kw))


def test_calendario_del_feed_solo_ventana_y_sin_jugar():
    fx = bl.fixtures_from_feed(FEED, NOW, 3)
    assert [f["match_id"] for f in fx] == ["EUR_E2026_801", "EUR_E2026_802"]
    f = fx[0]
    assert f["kickoff_utc"] == "2026-11-26T19:00:00+00:00" and f["date"] == "2026-11-26"
    assert (f["home_code"], f["home_team"], f["div"], f["league_name"]) == ("ZRV", "Zorrovia Norte", "EUR", "Euroliga")


def test_sin_backtest_no_escribe_y_no_falla():
    s = _run(backtest={})
    assert s["ok"] and "aviso" in s and s.get("would_write") is None


def test_feed_caido_es_fallo():
    s = _run(feed=[])
    assert not s["ok"] and s["errors"]


def test_fichas_dry_run_cuentan_sin_escribir(monkeypatch):
    escritos = []
    monkeypatch.setattr(bl, "_set_docs", lambda c, d: escritos.append((c, d)))
    s = _run()
    assert s["ok"] and s["dry_run"] and s["counts"]["nueva"] == 2 and s["would_write"] == 2
    assert s["edge_markets"] == ["pts_team", "reb_team"]
    assert not escritos
    assert "Zorrovia" not in repr(s)              # resumen sin equipos


def test_fichas_escriben_con_forma_de_mp_v1(monkeypatch):
    escritos = {}
    monkeypatch.setattr(bl, "_set_docs", lambda c, d: escritos.setdefault(c, {}).update(d))
    s = _run(dry_run=False)
    assert s["written"] == 2
    docs = escritos[bl.PREVIEWS_B]
    p = docs["EUR_E2026_801"]
    assert p["group"] == "basket" and p["div"] == "EUR" and p["model_version"] == "bp-v1"
    assert p["home_team"] == "Zorrovia Norte" and p["teams"]["home"]["team"] == "Zorrovia Norte"
    assert p["freeze_at"] == "2026-11-26T17:00:00+00:00" and p["data_cutoff"] < "2026-11-25"
    assert not p["low_sample"]
    blk = p["stats"]["pts"]["lines"]["home"]
    assert blk["label"] == "ventaja medida" and blk["highlight"]
    assert p["stats"]["ast"]["lines"]["home"]["label"] == "solo referencia"
    fixed = sorted(v["line"] for v in blk["lines"].values() if not v.get("extra"))
    assert fixed == BACKTEST["lines"]["pts_team"]
    # Segunda pasada sin cambios: igual; tras congelar: no se toca
    again = _run(existing=docs)
    assert again["counts"] == {"igual": 2}
    late = _run(existing=docs, now=datetime(2026, 11, 26, 17, 30, tzinfo=timezone.utc))
    assert late["counts"].get("congelada") == 1


def test_historial_solo_de_dias_anteriores():
    docs = _docs()
    # un partido de hoy ya cargado no entra (la ficha usaría datos del mismo día)
    hoy = {**docs[-1], "date": NOW.date().isoformat(), "gamecode": 9999}
    s1, s2 = _run(docs=docs), _run(docs=docs + [hoy])
    assert s1["history_games"] == s2["history_games"]


# ── Graduación ─────────────────────────────────────────────────────────────────

def _written_previews(monkeypatch):
    escritos = {}
    monkeypatch.setattr(bl, "_set_docs", lambda c, d: escritos.setdefault(c, {}).update(d))
    _run(dry_run=False)
    return escritos[bl.PREVIEWS_B]


def _game(code, home, away, pts=(90, 70)):
    return {"season": "E2026", "gamecode": code, "date": "2026-11-26", "home_code": home, "away_code": away,
            "home": {"pts": pts[0], "reb": 40, "ast": 20, "fg3m": 12, "poss_est": 75.0},
            "away": {"pts": pts[1], "reb": 30, "ast": 14, "fg3m": 6, "poss_est": 75.0}}


def test_graduacion_lineas_fijas_y_metricas(monkeypatch):
    previews = list(_written_previews(monkeypatch).values())
    later = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    results = {"E2026_801": _game(801, "ZRV", "QMR"),
               "E2026_802": _game(802, "QMR", "CLN")}        # equipos que no cuadran con la ficha
    out = {}
    s = asyncio.run(bl.run_basket_grade(dry_run=False, now=later, previews=previews, results=results, graded={},
                                        all_grades=[], write=lambda c, d: out.setdefault(c, {}).update(d)))
    assert s["ok"] and s["counts"] == {"graduada": 1, "anulada": 1} and s["n_graduated"] == 1
    g = out[bl.GRADES_B]["EUR_E2026_801"]
    assert g["status"] == "graduada" and g["group"] == "basket"
    assert out[bl.GRADES_B]["EUR_E2026_802"]["reason"] == "el partido guardado no cuadra con la ficha"
    pts_home = [ln for ln in g["lines"] if ln["market"] == "pts_team" and ln["side"] == "home"]
    assert sorted(ln["line"] for ln in pts_home) == BACKTEST["lines"]["pts_team"]   # sin la línea extra
    assert all(ln["y"] == int(90 > ln["line"]) and ln["p_team"] is not None for ln in pts_home)
    total = [ln for ln in g["lines"] if ln["market"] == "pts_total"]
    assert all(ln["value"] == 160 for ln in total)
    live = out["match_preview_metrics"][bl.LIVE_DOC]
    assert live["n_graduated"] == 1 and set(live["metrics"]) == {"vs_liga", "vs_equipo"}
    assert "Zorrovia" not in repr(live) and "Zorrovia" not in repr(s)


def test_graduacion_espera_y_sin_resultado(monkeypatch):
    previews = list(_written_previews(monkeypatch).values())
    soon = datetime(2026, 11, 27, 6, 0, tzinfo=timezone.utc)      # 801 empezó hace 11 h, 802 aún no
    s = asyncio.run(bl.run_basket_grade(now=soon, previews=previews, results={}, graded={}, all_grades=[]))
    assert s["counts"] == {"esperando eur_games": 1} and s["would_write"] == 0
    late = datetime(2026, 12, 5, 12, 0, tzinfo=timezone.utc)      # más de 7 días tras los dos
    s = asyncio.run(bl.run_basket_grade(now=late, previews=previews, results={}, graded={}, all_grades=[]))
    assert s["counts"] == {"sin resultado": 2}
    # ya graduada: no se repite
    s = asyncio.run(bl.run_basket_grade(now=late, previews=previews, results={},
                                        graded={"EUR_E2026_801": {}, "EUR_E2026_802": {}}, all_grades=[]))
    assert s["counts"] == {}


# ── Web de Tendencias ──────────────────────────────────────────────────────────

def test_exportacion_web_de_baloncesto(monkeypatch):
    from analyzers import match_preview_store as st
    previews = _written_previews(monkeypatch)
    w = st.web_match(previews["EUR_E2026_801"])
    assert w["group"] == "basket" and w["div"] == "EUR" and w["league_name"] == "Euroliga"
    assert w["model_version"] == "bp-v1" and w["home"] == "Zorrovia Norte"      # nombre del feed tal cual
    assert list(w["stats"]) == ["pts", "reb", "ast", "fg3m"]
    pts = w["stats"]["pts"]
    assert pts["blocks"]["home"]["label"] == "ventaja medida" and pts["blocks"]["home"]["highlight"]
    assert len(pts["exp"]) == 3 and pts["blocks"]["total"]["main"] is not None
    assert "gamecode" not in w and "home_code" not in w            # nada de ids de origen

    seen = []

    def fake_read(now, days, collection="match_previews"):
        seen.append(collection)
        return list(previews.values()) if collection == bl.PREVIEWS_B else []

    monkeypatch.setattr(st, "_read_upcoming_previews", fake_read)
    out = st.build_web_export(NOW)
    assert bl.PREVIEWS_B in seen and [m["id"] for m in out["matches"]] == ["EUR_E2026_801", "EUR_E2026_802"]


# ── NBA (28-nov, sin activar: solo la prueba sin escribir) ──────────────────────

def _nba_docs():
    """nba_games inventados: N2025 entera de fase regular y N2026 hasta el día anterior a NOW; uno
    de playoffs que no debe entrar en el historial."""
    rng = random.Random(9)
    out = []
    pairs = [(h, a) for h in TEAMS for a in TEAMS if h != a]
    for season, start in (("N2025", date(2025, 10, 22)), ("N2026", date(2026, 10, 21))):
        for r in range(40):
            day = start + timedelta(days=2 * r)
            if day >= NOW.date():
                break
            for j, (h, a) in enumerate(pairs[(3 * r) % len(pairs):(3 * r) % len(pairs) + 3]):
                out.append({"season": season, "game_id": f"{season}{r}{j}", "date": day.isoformat(),
                            "season_type": 2, "home_code": h[0], "away_code": a[0], "ot_periods": 0,
                            "home": _side(rng, 118 if h[0] == "ZRV" else 112), "away": _side(rng, 112)})
    out.append({**out[0], "game_id": "po1", "season_type": 3})
    return out


def _ev(eid, home, away, ko, stype=2, completed=False, status="STATUS_SCHEDULED"):
    return {"id": eid, "date": ko, "season": {"type": stype},
            "status": {"type": {"completed": completed, "name": status}},
            "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"abbreviation": home[0], "displayName": home[1]}},
                {"homeAway": "away", "team": {"abbreviation": away[0], "displayName": away[1]}}]}]}


BOARDS = {
    "20261125": {"events": [_ev("401", TEAMS[0], TEAMS[1], "2026-11-26T00:30Z"),          # 25-nov en EE. UU.
                            _ev("400", TEAMS[2], TEAMS[3], "2026-11-25T03:00Z", completed=True,
                                status="STATUS_FINAL")]},
    "20261126": {"events": [_ev("402", TEAMS[2], TEAMS[3], "2026-11-27T01:00Z"),
                            _ev("403", TEAMS[4], TEAMS[5], "2026-11-27T02:00Z", status="STATUS_POSTPONED"),
                            _ev("404", TEAMS[4], TEAMS[5], "2026-11-27T03:00Z", stype=1)]},     # pretemporada
    "20261201": {"events": [_ev("405", TEAMS[1], TEAMS[0], "2026-12-02T00:00Z")]},            # fuera de la ventana
}


def test_nba_calendario_de_espn_fase_regular_y_ventana():
    fx = bl.fixtures_from_espn(BOARDS, NOW, 3)
    assert [f["match_id"] for f in fx] == ["NBA_N2026_401", "NBA_N2026_402"]
    f = fx[0]
    assert f["date"] == "2026-11-25" and f["kickoff_utc"] == "2026-11-26T00:30:00+00:00"
    assert (f["div"], f["league_name"], f["home_code"], f["home_team"]) == ("NBA", "NBA", "ZRV", "Zorrovia Norte")


def test_nba_fichas_sin_escribir_con_nba_games(monkeypatch):
    escritos = []
    monkeypatch.setattr(bl, "_set_docs", lambda c, d: escritos.append((c, d)))
    nba_bt = {**BACKTEST, "lines": {"pts_team": [105.5, 112.5, 118.5], "pts_total": [215.5, 224.5, 232.5],
                                    "reb_team": [32.5, 35.5, 38.5], "reb_total": [67.5, 70.5, 73.5],
                                    "ast_team": [15.5, 18.5, 21.5], "ast_total": [33.5, 36.5, 39.5],
                                    "fg3m_team": [7.5, 10.5, 12.5], "fg3m_total": [17.5, 20.5, 23.5]}}
    got = {}
    s = _run(league="NBA", feed=BOARDS, docs=_nba_docs(), backtest=nba_bt, collect=got)
    assert s["ok"] and s["league"] == "NBA" and s["fixtures_window"] == 2 and s["would_write"] == 2
    assert s["history_games"] == len([d for d in _nba_docs() if d["season_type"] == 2])   # sin playoffs
    assert not escritos and "Zorrovia" not in repr(s)
    p = got["NBA_N2026_401"]
    assert p["group"] == "basket" and p["div"] == "NBA" and p["labels_source"] == "bp-v1-NBA_backtest"
    assert p["data_cutoff"] < "2026-11-25" and p["stats"]["pts"]["lines"]["home"]["highlight"]


def test_nba_scoreboard_caido_es_fallo_y_sin_backtest_aviso():
    s = _run(league="NBA", feed={}, docs=_nba_docs())
    assert not s["ok"] and "ESPN" in s["errors"][0]
    s = _run(league="NBA", feed=BOARDS, docs=_nba_docs(), backtest={})
    assert s["ok"] and "bp-v1-NBA_backtest" in s["aviso"]
