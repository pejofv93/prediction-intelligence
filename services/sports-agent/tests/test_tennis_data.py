"""Fase 1 tenis: lector xlsx (biblioteca estándar), sets, estados, torneos y nombres. Sin red."""
import zipfile
from io import BytesIO

from collectors import tennis_data as td

_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _xlsx(rows: list[list]) -> bytes:
    """xlsx mínimo: textos en sharedStrings, números en línea, celdas vacías omitidas."""
    strings: list[str] = []
    cells_xml = []
    for r_i, row in enumerate(rows, start=1):
        cells = []
        for c_i, v in enumerate(row):
            if v is None:
                continue
            ref = f"{chr(65 + c_i) if c_i < 26 else 'A' + chr(65 + c_i - 26)}{r_i}"
            if isinstance(v, str):
                strings.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
        cells_xml.append(f'<row r="{r_i}">{"".join(cells)}</row>')
    sheet = f'<?xml version="1.0"?><worksheet {_NS}><sheetData>{"".join(cells_xml)}</sheetData></worksheet>'
    sst = f'<?xml version="1.0"?><sst {_NS}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>"
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml", sst)
    return buf.getvalue()


_HEADER = ["ATP", "Location", "Tournament", "Date", "Series", "Court", "Surface", "Round",
           "Best of", "Winner", "Loser", "WRank", "LRank", "W1", "L1", "W2", "L2", "W3", "L3",
           "W4", "L4", "W5", "L5", "Wsets", "Lsets", "Comment", "B365W"]


def _row(num, tour_name, date_num, rnd, bo, w, l, sets, ws, ls, comment, surface="Hard"):
    flat = []
    for i in range(5):
        flat += list(sets[i]) if i < len(sets) else [None, None]
    return [num, "City", tour_name, date_num, "ATP250", "Outdoor", surface, rnd, bo, w, l, 10, 20,
            *flat, ws, ls, comment, 1.5]


def _rows():
    return [
        _HEADER,
        _row(1, "Brisbane", 46026, "1st Round", 3, "Alcaraz C.", "De Minaur A.", [(6, 4), (7, 6)], 2, 0, "Completed"),
        _row(1, "Brisbane", 46027, "2nd Round", 3, "Alcaraz C.", "Auger-Aliassime F.", [(6, 3), (2, 1)], 1, 0, "Retired"),
        _row(1, "Brisbane", 46028, "Quarterfinals", 3, "Alcaraz C.", "Zhang Zh.", [], 0, 0, "Walkover"),
        _row(2, "Australian Open", 46035, "1st Round", 5, "Sinner J.", "Bautista Agut R.",
             [(6, 7), (6, 4), (6, 4), (3, 6), (7, 6)], 3, 2, "Completed"),
        _row(2, "Australian Open", 46036, "2nd Round", 5, "Sinner J.", "Wang Xin.", [(6, 4), (6, 4), (6, 0), (0, 0)], 3, 0, "Completed"),
    ]


def test_lector_xlsx_cabeceras_numeros_y_textos():
    rows = td.read_xlsx_rows(_xlsx(_rows()))
    assert len(rows) == 5
    assert rows[0]["Winner"] == "Alcaraz C." and rows[0]["W1"] == 6.0
    assert rows[2].get("W1") is None            # celda omitida → no aparece
    assert td.excel_date(rows[0]["Date"]) == "2026-01-04"


def test_sets_y_estados():
    rows = td.read_xlsx_rows(_xlsx(_rows()))
    m = [td.normalize_match(r, "atp") for r in rows]
    assert m[0]["sets"] == [[6, 4], [7, 6]] and m[0]["total_games"] == 23 and m[0]["tiebreaks"] == 1
    assert m[0]["status"] == "completed" and m[0]["valid"]
    assert m[1]["status"] == "retired" and not m[1]["valid"] and m[1]["complete_sets"] == 1
    assert m[2]["status"] == "walkover" and m[2]["n_sets"] == 0
    assert m[3]["n_sets"] == 5 and m[3]["valid"] and m[3]["tiebreaks"] == 2
    assert m[4]["n_sets"] == 3 and m[4]["valid"]          # el 0-0 de relleno no cuenta


def test_set_terminado():
    for s in [(6, 0), (6, 4), (4, 6), (7, 5), (7, 6), (6, 7), (8, 6)]:
        assert td.set_is_complete(*s), s
    for s in [(6, 5), (5, 3), (2, 1), (7, 3), (0, 0)]:
        assert not td.set_is_complete(*s), s


def test_torneos_agrupados_y_contadores():
    docs, st = td.build_tournament_docs(td.read_xlsx_rows(_xlsx(_rows())), "atp", 2026)
    assert set(docs) == {"atp_2026_001_brisbane", "atp_2026_002_australian-open"}
    ao = docs["atp_2026_002_australian-open"]
    assert ao["best_of"] == 5 and ao["n_matches"] == 2 and ao["surface"] == "Hard"
    assert ao["start"] == "2026-01-13" and ao["end"] == "2026-01-14"
    assert st["status"] == {"completed": 3, "retired": 1, "walkover": 1}
    assert st["sets_incoherentes"] == 0 and st["sin_fecha_o_jugador"] == 0
    assert "B365W" not in str(docs)                       # sin cuotas
    assert td.doc_hash(ao) == td.doc_hash(dict(ao, updated_at="x"))


def test_enlaces_de_alldata_con_carpeta_aleatoria():
    html = ('<a HREF="abc-123_x/2026/2026.xlsx">x</a><a HREF="abc-123_x/2026w/2026.xlsx">'
            '<a HREF="abc-123_x/2011/2011.xls">')
    links = td.parse_alldata_links(html)
    assert links == {("atp", 2026): "https://www.tennis-data.co.uk/abc-123_x/2026/2026.xlsx",
                     ("wta", 2026): "https://www.tennis-data.co.uk/abc-123_x/2026w/2026.xlsx"}
    assert td.link_folder(links[("atp", 2026)]) == "abc-123_x"


def test_nombres_apellido_inicial():
    assert td.td_player_key("Auger-Aliassime F.") == "auger aliassime|f"
    assert td.td_player_key("Zhang Zh.") == "zhang|z"
    assert td.td_player_key("Friedsam A-L.") == "friedsam|a"
    index = {td.td_player_key(n) for n in ("Alcaraz C.", "De Minaur A.", "Bautista Agut R.",
                                            "Auger-Aliassime F.", "Sabalenka A.")}
    assert td.match_player("Carlos Alcaraz", index) == "alcaraz|c"
    assert td.match_player("Alex de Minaur", index) == "de minaur|a"
    assert td.match_player("Roberto Bautista Agut", index) == "bautista agut|r"
    assert td.match_player("Félix Auger-Aliassime", index) == "auger aliassime|f"
    assert td.match_player("Sabalenka, Aryna", index) == "sabalenka|a"
    assert td.match_player("Alcaraz C.", index) == "alcaraz|c"
    assert td.match_player("Novak Djokovic", index) is None


def test_anos_por_defecto():
    from datetime import date
    assert td.default_years(date(2026, 11, 19)) == [2024, 2025, 2026]


def test_emparejamiento_calendario_solo_agregados(monkeypatch):
    import sys
    import types

    class _Snap:
        def __init__(self, d):
            self._d = d

        def to_dict(self):
            return self._d

    class _Q:
        def where(self, filter=None):
            return self

        def stream(self):
            return [_Snap({"league": "ATP Finals", "home_team": "Carlos Alcaraz", "away_team": "Jannik Sinner"}),
                    _Snap({"league": "WTA Finals", "home_team": "A / B", "away_team": "C & D"}),
                    _Snap({"league": "ATP", "tournament": "ATP Challenger Montevideo",
                           "home_team": "Carlos Alcaraz", "away_team": "Juan Nadie"}),
                    _Snap({"league": "ATP", "tournament": "ITF M15 Monastir",
                           "home_team": "Pepe Uno", "away_team": "Pepe Dos"})]

    fake = types.ModuleType("shared.firestore_client")
    fake.col = lambda name: _Q()
    monkeypatch.setitem(sys.modules, "shared.firestore_client", fake)
    out = td.calendar_name_match({td.td_player_key("Alcaraz C.")})
    assert out["partidos"] == 4 and out["nombres"] == 2 and out["dobles_fuera"] == 2
    assert out["emparejan"] == 1 and out["pct"] == 50.0 and out["por_circuito"] == {"ATP": "1/2"}
    assert out["fuera_circuito_principal"] == "1/4"              # Challenger e ITF, aparte


def test_circuito_principal():
    assert td.is_main_tour({"league": "ATP_MADRID", "tournament": "Madrid Open"})
    assert td.is_main_tour({"league": "WTA", "tournament": "WTA Finals"})
    for t in ("Challenger Lima", "ITF W35 Sharm", "UTR Pro Tennis Series", "M25 Antalya", "Exhibition Mubadala",
              "Billie Jean King Cup - Playoffs", "Billie Jean King Cup - Billie Jean King Cup, Singles Matches",
              "Davis Cup - Finals", "Fed Cup"):
        assert not td.is_main_tour({"league": "ATP", "tournament": t}), t


def test_diario_año_en_curso_y_en_enero_tambien_el_anterior():
    from datetime import date
    assert td.daily_years(date(2026, 11, 21)) == [2026]
    assert td.daily_years(date(2027, 1, 10)) == [2026, 2027]
    assert td.daily_years(date(2027, 2, 1)) == [2027]


def test_apellido_compuesto_solo_con_un_unico_candidato():
    idx = {td.td_player_key("Maristany G."), td.td_player_key("Davidovich Fokina A.")}
    assert td.match_player("Maristany Zuleta de Reales, Guiomar", idx) == "maristany|g"
    assert td.match_player("Guiomar Maristany Zuleta de Reales", idx) == "maristany|g"
    assert td.match_player("Davidovich Fokina, Alejandro", idx) == "davidovich fokina|a"   # exacto primero
    assert td.match_player("Alejandro Davidovich Fokina", idx) == "davidovich fokina|a"
    assert td.match_player("Zolotareva, Anastasia", {td.td_player_key("Zolotareva R.")}) is None
    # dos claves que empiezan por el primer apellido con la misma inicial → no empareja
    two = {td.td_player_key("Gonzalez J."), td.td_player_key("Gonzalez Perez J.")}
    assert td.match_player("Gonzalez Daniele, Justina Maria", two) is None
    # una sola clave pero dos jugadores detrás (ATP y WTA) → no empareja
    one = {td.td_player_key("Gonzalez J.")}
    assert td.match_player("Gonzalez Daniele, Justina Maria", one, {"gonzalez|j": 2}) is None
    assert td.match_player("Gonzalez Daniele, Justina Maria", one, {"gonzalez|j": 1}) == "gonzalez|j"
