"""player_preview_show: bloque de jugadores y como mucho 1 píldora de jugador (datos inventados)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent"))
sys.path.insert(0, str(ROOT / "services" / "sports-agent" / "tests"))
sys.path.insert(0, str(ROOT))
import player_preview_show as ps  # noqa: E402
from test_player_preview import _season_docs  # noqa: E402


def test_bloque_y_una_pildora_por_partido():
    games = ps.pp.games_from_docs(_season_docs())
    hist, names = ps.pp.History(), {}
    for g in games:
        hist.update(g)
        for side in ("home", "away"):
            names.update({p["pid"]: p["name"] for p in g[side]["players"]})
    labels = {mk: ps.LABEL_EDGE for mk in ps.pp.MARKETS}
    home = ps.player_block(hist, names, "SP1", "2526", "home", 1, 2, labels)
    away = ps.player_block(hist, names, "SP1", "2526", "away", 2, 1, labels)
    assert [p["pid"] for p in home] == [101, 102] and home[0]["last_date"] and home[0]["markets"]["shots"]["star"]
    pill = ps.player_pill({"Equipo 1": home, "Equipo 2": away})
    assert pill is not None and ps.PILL_MIN <= pill["p"] <= ps.PILL_MAX
    assert ps.pill_text(pill).startswith("⭐ Jugador ")
    assert ps.player_pill({"Equipo 1": ps.player_block(hist, names, "SP1", "2526", "home", 1, 2, {})}) is None
    goal = ps.pill_text({"player": "Jugador 9", "team": "Equipo 1", "stat": "goals", "line": 0.5, "p": 0.61})
    assert goal == "⭐ Jugador 9 (Equipo 1) <b>marca</b> · <b>61 %</b>"


def test_emparejado_con_alias_de_produccion():
    teams = {"E0": {10: "Manchester United", 11: "Manchester City", 12: "Brentford"}}
    fx = [{"div": "E0", "home_team": "Man United", "away_team": "Brentford"},
          {"div": "E0", "home_team": "Arsenal", "away_team": "Man City"}]
    m = ps.team_map(teams, fx)
    assert m[("E0", "Man United")] == 10 and m[("E0", "Man City")] == 11 and m[("E0", "Brentford")] == 12
    assert ("E0", "Arsenal") not in m
