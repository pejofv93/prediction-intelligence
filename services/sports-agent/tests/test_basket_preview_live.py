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
