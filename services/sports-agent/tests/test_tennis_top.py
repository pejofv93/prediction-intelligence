"""Tenis top F0: lista cerrada de categorías, exclusiones por nombre y agregados (datos inventados)."""
from analyzers import tennis_top as tt
from tests.tennis_fake import make_docs


def _doc(series, name="Torneo X", tour="atp"):
    return {"tour": tour, "series": series, "tournament": name, "location": "Ciudad"}


def test_lista_cerrada_de_series():
    for s, cat in (("Grand Slam", "grand_slam"), ("Masters 1000", "1000"), ("WTA1000", "1000"),
                   ("ATP500", "500"), ("ATP 500", "500"), ("WTA250", "250"), ("Masters Cup", "finals"),
                   ("Tour Championships", "finals"), ("Premier Mandatory", "1000"), ("Premier 5", "1000"),
                   ("Premier", "500"), ("International", "250")):
        assert tt.tournament_category(_doc(s)) == (cat, "top"), s
    for s in ("Challenger", "Olympics", "", None, "ITF", "Elite"):
        assert tt.tournament_category(_doc(s)) == (None, "serie_fuera_de_lista"), s


def test_exclusiones_por_nombre_aunque_la_serie_sea_top():
    for n in ("United Cup", "ATP Cup", "Davis Cup Finals", "Billie Jean King Cup", "Laver Cup",
              "Hopman Cup", "Olympic Games", "Next Gen Finals", "NextGen ATP Finals"):
        assert tt.tournament_category(_doc("ATP250", n)) == (None, "excluido_por_nombre"), n
    assert tt.is_top(_doc("Masters Cup", "ATP Finals"))       # las Finals sí
    assert tt.is_top(_doc("Tour Championships", "WTA Finals", "wta"))


def test_partidos_planos_y_resumenes():
    docs = make_docs(weeks=8)
    todos, top = tt.flat_matches(docs, top_only=False), tt.flat_matches(docs)
    assert len(todos) - len(top) == 2                          # United Cup y Olympics fuera
    m = top[0]
    assert m["w"].startswith(m["tour"] + "|") and m["w_games"] + m["l_games"] == m["total_games"]
    ss = tt.series_summary(docs)
    assert ss[("atp", "ATP250")]["motivos"] == {"top": 2, "excluido_por_nombre": 1}
    assert ss[("atp", "ATP250")]["fuera"] == {"United Cup"}
    assert ss[("atp", "Olympics")]["categoria"] is None
    sh = tt.shape_summary(top)
    assert sum(v["partidos"] for v in sh.values()) == len(top)
    assert sum(v["status"]["walkover"] for v in sh.values()) == 16
    assert ("atp", "Hard", 5) in sh                            # Grand Slam ATP al mejor de 5


def test_histograma_solo_recuentos():
    docs = make_docs(weeks=60)
    top = tt.flat_matches(docs)
    ref = max(m["date"] for m in top)
    h = tt.player_surface_histogram(top, ref, 24, 8)
    v = h[("atp", "Hard")]
    assert sum(v["buckets"].values()) == v["jugadores"] <= 24
    assert 0 < v["partidos_ambos_min"] <= v["partidos_ultimo_año"]
    assert "Inventado" not in repr(h)                          # ningún nombre de jugador
