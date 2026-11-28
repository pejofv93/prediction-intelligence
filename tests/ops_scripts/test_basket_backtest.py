"""basket_backtest: informe de solo agregados con partidos inventados (sin Firestore)."""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT))
import basket_backtest as bb  # noqa: E402


def _games(seasons=("E2024", "E2025")):
    rnd = random.Random(3)
    teams = [f"EQUIPO{i}" for i in range(6)]

    def side():
        return {"pts": 80 + rnd.randint(-15, 15), "reb": 35 + rnd.randint(-6, 6),
                "ast": 18 + rnd.randint(-5, 5), "fg3m": 10 + rnd.randint(-4, 4), "poss_est": 75.0}

    out = []
    for si, season in enumerate(seasons):
        r = 0
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                r += 1
                day = f"{2024 + si}-{1 + r // 28:02d}-{1 + r % 28:02d}"
                out.append({"season": season, "date": day, "phase": "RS", "round": r,
                            "home_code": h, "away_code": a, "ot_periods": 0,
                            "home": side(), "away": side()})
    return out


def test_informe_solo_agregados():
    games = bb.bp.games_from_docs(_games(), "EUR")
    text = "\n".join(bb.run(games, "EUR"))
    assert "calentamiento E2024" in text and "evaluadas ['E2025']" in text
    assert "media de la liga" in text and "media del equipo" in text
    assert "pts_total" in text and "fg3m_team" in text
    assert "EQUIPO" not in text           # ni un nombre de equipo en el log público


def test_una_temporada_no_basta():
    games = bb.bp.games_from_docs(_games(seasons=("E2025",)), "EUR")
    try:
        bb.run(games, "EUR")
    except ValueError as e:
        assert "2 temporadas" in str(e)
    else:
        raise AssertionError("debía fallar")


def test_orden_de_temporadas_numerico():
    games = [{"season": s} for s in ("A2025", "A2023", "A2024")]
    assert bb.split_seasons(games) == ("A2023", ["A2024", "A2025"])


def test_documento_del_backtest_solo_agregados():
    games = bb.bp.games_from_docs(_games(), "EUR")
    out = bb.compute(games, "EUR")
    doc = bb.backtest_doc(out, "EUR", "2026-11-25T00:00:00+00:00")
    assert doc["warmup"] == "E2024" and doc["seasons"] == ["E2025"]
    markets = {f"{s}_{sc}" for s in bb.bp.STATS for sc in ("team", "total")}
    assert set(doc["labels"]) == set(doc["lines"]) == markets
    assert set(doc["labels"].values()) <= {"ventaja medida", "solo referencia"}
    assert set(doc["metrics"]) == {"vs_liga", "vs_equipo"} and set(doc["metrics"]["vs_liga"]) == markets
    assert all(isinstance(v, list) for v in doc["lines"].values())
    assert "EQUIPO" not in repr(doc)          # ni un equipo en el documento


def test_informe_por_temporada_y_comparacion_sin_corregir():
    games = bb.bp.games_from_docs(_games(("E2023", "E2024", "E2025")), "EUR")
    out = bb.compute(games, "EUR")
    text = "\n".join(out["report"])
    assert "Por temporada" in text and "[E2024]" in text and "[E2025]" in text
    assert "Sin corregir el sesgo" in text and "Corrección de sesgo: sí" in text
    assert "EQUIPO" not in text
    doc = bb.backtest_doc(out, "EUR", "2026-11-26T00:00:00+00:00")
    assert doc["bias_correction"] is True


def _nba_docs(seasons=("N2024", "N2025"), per_season=180):
    """Docs con la forma de nba_games (sin round: la jornada es el día; season_type de ESPN)."""
    from datetime import date, timedelta
    rnd = random.Random(5)
    teams = [f"EQUIPO{i}" for i in range(10)]
    out = []
    for season in seasons:
        d0 = date(int(season[1:]), 10, 22)
        for i in range(per_season):
            h, a = rnd.sample(teams, 2)
            ot = 1 if i % 15 == 0 else 0
            stype = 2 if i < per_season - 12 else (5 if i < per_season - 8 else 3)

            def side():
                return {"pts": 112 + rnd.randint(-15, 15) + 10 * ot, "reb": 44 + rnd.randint(-6, 6),
                        "ast": 26 + rnd.randint(-5, 5), "fg3m": 13 + rnd.randint(-4, 4),
                        "poss_est": 99.0 + 10 * ot}
            out.append({"season": season, "game_id": f"{season}{i}",
                        "date": (d0 + timedelta(days=i // 4)).isoformat(), "season_type": stype,
                        "home_code": h, "away_code": a, "ot_periods": ot, "minutes": 240 + 25 * ot,
                        "home": side(), "away": side(), "source": "espn_summary"})
    # una de pretemporada y un All-Star que no deberían estar, por si acaso
    for st in (1, 4):
        out.append({**out[0], "game_id": f"x{st}", "season_type": st})
    return out


def test_nba_fases_regular_por_defecto():
    games = bb.bp.games_from_docs(_nba_docs(), "NBA")
    kept, counts = bb.filter_phases(games, "NBA")
    assert counts == {"ASG": 1, "PI": 8, "PO": 16, "PRE": 1, "RS": 336}
    assert len(kept) == 336 and {g["phase"] for g in kept} == {"RS"}
    kept, _ = bb.filter_phases(games, "NBA", "todas")
    assert {g["phase"] for g in kept} == {"RS", "PI", "PO"} and len(kept) == 360
    # las demás ligas no se filtran
    eur = bb.bp.games_from_docs(_games(), "EUR")
    assert bb.filter_phases(eur, "EUR")[0] == eur


def test_nba_jornada_por_dia_y_prorroga_sobre_48():
    games = bb.bp.games_from_docs(_nba_docs(), "NBA")
    g = next(g for g in games if g["ot"] == 1)
    assert g["group"] == f"{g['season']}_{g['date']}"
    # posesiones de un partido con una prórroga llevadas a 48 min: 109 × 48/53
    assert abs(bb.bp.game_poss(g) - 109.0 * 48 / 53) < 1e-9


def test_nba_recientes_en_el_informe_y_el_documento():
    games, _ = bb.filter_phases(bb.bp.games_from_docs(_nba_docs(("N2024", "N2025", "N2026")), "NBA"), "NBA")
    out = bb.compute(games, "NBA")
    text = "\n".join(out["report"])
    assert "temporadas recientes juntas ['N2025', 'N2026']" in text and "diferencia media ≤ 0" in text
    doc = bb.backtest_doc(out, "NBA", "2026-11-28T00:00:00+00:00")
    assert doc["recent_seasons"] == ["N2025", "N2026"] and set(doc["recent"]) == {"vs_liga", "vs_equipo"}
    assert "recent_seasons" in doc["rule"] and "EQUIPO" not in repr(doc)
    for mk, lab in doc["labels"].items():          # una ⭐ nunca con diferencia > 0 en las recientes
        if lab == bb.bp.LABEL_EDGE:
            assert all(doc["recent"][k][mk]["delta"] <= 0 for k in ("vs_liga", "vs_equipo"))
    # la Euroliga no lleva la condición
    eur = bb.backtest_doc(bb.compute(bb.bp.games_from_docs(_games(), "EUR"), "EUR"), "EUR", "x")
    assert "recent_seasons" not in eur and "recent_seasons" not in eur["rule"]


def test_nba_informe_solo_agregados():
    games, _ = bb.filter_phases(bb.bp.games_from_docs(_nba_docs(), "NBA"), "NBA")
    text = "\n".join(bb.run(games, "NBA"))
    assert "calentamiento N2024" in text and "evaluadas ['N2025']" in text
    assert "Con prórroga: 24" in text
    assert "'evaluados'" in text and "pts_total" in text
    assert "EQUIPO" not in text
