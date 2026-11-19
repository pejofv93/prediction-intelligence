"""
Tabla de nombres de equipo para PANTALLA (web de Tendencias, fichas exportadas y resumen de
Telegram). Solo presentación: los ids, las claves internas (match_id, slug, home_team/away_team
guardados en Firestore) y lo que se gradúa siguen con el nombre de la fuente. Nada se persiste.

Las fuentes llaman a cada club a su manera:
  - football-data.co.uk (CSV, nombre interno de mp-v1, cups y la vista Equipos): "Ath Bilbao",
    "Espanol", "FC Koln", "M'gladbach", "Sp Lisbon", "Nott'm Forest"…
  - football-data.org (upcoming_matches, nombre oficial): "Athletic Club", "RCD Espanyol de
    Barcelona", "1. FC Köln", "Club Atlético de Madrid"… (solo llega si el emparejado falla)
  - Highlightly (fixtures de SP2, copas y femenino): "Athletic Club", "Espanyol", "FC Koln",
    "Bayern München", "Barcelona W"…
Todas se reducen a una clave (sin acentos, minúsculas, sin puntuación ni siglas societarias
FC/CF/SAD/CD/SV/VfB…, sin años) y la clave da el nombre de uso común en español: "Athletic",
"Espanyol", "Colonia", "Mönchengladbach", "Sporting de Portugal".

Colisiones: la clave conserva "real", "atletico", "athletic", "sporting", "b" de filial,
"cff"… ("Real Madrid" ≠ "Madrid CFF", "Real Sociedad B" ≠ "Real Sociedad"). Las fichas
femeninas miran primero su tabla propia (clubes solo femeninos) y quitan antes "W"/"Women"/
"Femenino"; el grupo ya dice "Femenino", así que "Barcelona W" sale "Barcelona".
Si un nombre no está, se enseña limpio (sin las siglas societarias de los extremos).

Las iniciales del escudo (círculo de color de la web) salen del nombre de pantalla con el mismo
criterio que trends_web/site/app.js: una palabra → sus 3 primeras letras; varias → la inicial de
las 3 primeras, saltando partículas en minúscula ("Sporting de Portugal" → "SP").
"""
import re
import unicodedata
from typing import Optional

# Siglas societarias, artículos y palabras de relleno que no distinguen clubes
_KEY_DROP = frozenset({
    "fc", "cf", "afc", "sad", "cd", "ud", "sd", "rcd", "rc", "sc", "ac", "as", "ssc", "ss", "us",
    "acf", "sv", "vfb", "vfl", "tsg", "bv", "fk", "sk", "cfc", "bsc", "club", "de", "del", "la",
    "el", "futbol", "football", "calcio", "sl", "sa", "fsv", "ogc", "aj", "sco",
})
# Lo que añade Highlightly (u otras fuentes) a un equipo femenino
_WOMEN_DROP = frozenset({"w", "women", "womens", "woman", "fem", "femenino", "femenina", "femminile",
                         "feminine", "feminin", "frauen", "ladies", "lfc", "wfc"})
# Siglas que se quitan de los extremos del nombre cuando no está en la tabla
_EDGE_DROP = frozenset({"fc", "cf", "afc", "sad", "cd", "ud", "sd", "rcd", "rc", "sc", "ac", "as",
                        "ssc", "ss", "us", "acf", "sv", "vfb", "vfl", "tsg", "bv", "fk", "sk", "cfc",
                        "bsc", "fsv", "ogc", "aj", "sco"})


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _words(name: str) -> list[str]:
    s = _ascii(name).replace("'", "")             # Nott'm → nottm, M'gladbach → mgladbach
    return "".join(c if c.isalnum() else " " for c in s).split()


def team_key(name: str, women: bool = False) -> str:
    """Clave de la tabla: sin acentos, minúsculas, sin puntuación, sin siglas ni años."""
    ws = [w for w in _words(name) if not w.isdigit() and w not in _KEY_DROP]
    if women:
        ws = [w for w in ws if w not in _WOMEN_DROP]
    return " ".join(ws)


# ── Tabla: (nombre de pantalla, variantes de las fuentes…) ──────────────────────
# Primera variante = nombre de football-data.co.uk cuando lo hay. Por país, porque los clubes
# suben y bajan (las segundas divisiones son historial de mp-v1 y pueden llegar a la ficha).
CLUBS: dict[str, list[tuple[str, ...]]] = {
    "ES": [
        ("Athletic", "Ath Bilbao", "Athletic Club", "Athletic Bilbao", "Athletic Club Bilbao"),
        ("Atlético", "Ath Madrid", "Atletico Madrid", "Club Atlético de Madrid", "Atlético de Madrid",
         "Atl. Madrid", "Atletico de Madrid"),
        ("Real Madrid", "Real Madrid", "Real Madrid CF"),
        ("Barcelona", "Barcelona", "FC Barcelona", "Barça"),
        ("Espanyol", "Espanol", "Espanyol", "RCD Espanyol de Barcelona", "RCD Espanyol", "Espanyol Barcelona"),
        ("Real Sociedad", "Sociedad", "Real Sociedad", "Real Sociedad de Fútbol"),
        ("Real Sociedad B", "Sociedad B", "Real Sociedad B", "Real Sociedad II", "Sanse"),
        ("Betis", "Betis", "Real Betis", "Real Betis Balompié"),
        ("Celta", "Celta", "Celta Vigo", "RC Celta de Vigo", "Celta de Vigo", "Real Club Celta de Vigo"),
        ("Celta B", "Celta B", "Celta Vigo B", "Celta Fortuna", "Celta de Vigo B"),
        ("Rayo Vallecano", "Vallecano", "Rayo Vallecano", "Rayo Vallecano de Madrid"),
        ("Alavés", "Alaves", "Deportivo Alavés", "Alavés"),
        ("Deportivo", "La Coruna", "Deportivo La Coruña", "RC Deportivo La Coruña", "RC Deportivo",
         "Deportivo de La Coruña", "Deportivo"),
        ("Valencia", "Valencia", "Valencia CF"),
        ("Villarreal", "Villarreal", "Villarreal CF"),
        ("Villarreal B", "Villarreal B", "Villarreal CF B"),
        ("Sevilla", "Sevilla", "Sevilla FC"),
        ("Getafe", "Getafe", "Getafe CF"),
        ("Osasuna", "Osasuna", "CA Osasuna", "Club Atlético Osasuna"),
        ("Elche", "Elche", "Elche CF"),
        ("Levante", "Levante", "Levante UD"),
        ("Málaga", "Malaga", "Málaga CF", "Málaga"),
        ("Racing", "Santander", "Racing Santander", "Racing de Santander", "Real Racing Club",
         "Real Racing Club de Santander"),
        ("Sporting", "Sp Gijon", "Sporting Gijon", "Sporting de Gijón", "Real Sporting de Gijón"),
        ("Girona", "Girona", "Girona FC"),
        ("Mallorca", "Mallorca", "RCD Mallorca"),
        ("Las Palmas", "Las Palmas", "UD Las Palmas"),
        ("Leganés", "Leganes", "CD Leganés", "Leganés"),
        ("Valladolid", "Valladolid", "Real Valladolid", "Real Valladolid CF"),
        ("Oviedo", "Oviedo", "Real Oviedo"),
        ("Granada", "Granada", "Granada CF"),
        ("Cádiz", "Cadiz", "Cádiz CF", "Cádiz"),
        ("Almería", "Almeria", "UD Almería", "Almería"),
        ("Eibar", "Eibar", "SD Eibar"),
        ("Huesca", "Huesca", "SD Huesca"),
        ("Zaragoza", "Zaragoza", "Real Zaragoza"),
        ("Tenerife", "Tenerife", "CD Tenerife"),
        ("Albacete", "Albacete", "Albacete Balompié", "Albacete BP"),
        ("Burgos", "Burgos", "Burgos CF"),
        ("Cartagena", "Cartagena", "FC Cartagena"),
        ("Castellón", "Castellon", "CD Castellón", "Castellón"),
        ("Córdoba", "Cordoba", "Córdoba CF", "Córdoba"),
        ("Eldense", "Eldense", "CD Eldense"),
        ("Racing de Ferrol", "Ferrol", "Racing Ferrol", "Racing Club Ferrol", "Racing de Ferrol"),
        ("Mirandés", "Mirandes", "CD Mirandés", "Mirandés"),
        ("Andorra", "Andorra", "FC Andorra"),
        ("Ceuta", "Ceuta", "AD Ceuta", "AD Ceuta FC"),
        ("Sabadell", "Sabadell", "CE Sabadell", "CE Sabadell FC"),
        ("Cultural Leonesa", "Cultural Leonesa", "Leonesa", "Cultural y Deportiva Leonesa"),
        ("Ponferradina", "Ponferradina", "SD Ponferradina"),
        ("Lugo", "Lugo", "CD Lugo"),
        ("Alcorcón", "Alcorcon", "AD Alcorcón", "Alcorcón"),
        ("Amorebieta", "Amorebieta", "SD Amorebieta"),
        ("Fuenlabrada", "Fuenlabrada", "CF Fuenlabrada"),
        ("Ibiza", "Ibiza", "UD Ibiza"),
        ("Logroñés", "Logrones", "UD Logroñés", "SD Logroñés"),
        ("Real Unión", "Real Union", "Real Unión"),
        ("Gimnàstic", "Gimnastic", "Gimnàstic de Tarragona", "Nastic", "Nàstic"),
        ("Murcia", "Murcia", "Real Murcia"),
        ("Hércules", "Hercules", "Hércules CF"),
        ("Alavés B", "Alaves B", "Deportivo Alavés B"),
        ("Athletic B", "Bilbao Athletic", "Athletic Club B"),
        ("Real Madrid Castilla", "Real Madrid Castilla", "Real Madrid B", "Castilla"),
        ("Barcelona B", "Barcelona B", "Barcelona Atletic", "Barça Atlètic"),
    ],
    "EN": [
        ("Arsenal", "Arsenal", "Arsenal FC"),
        ("Aston Villa", "Aston Villa", "Aston Villa FC"),
        ("Bournemouth", "Bournemouth", "AFC Bournemouth"),
        ("Brentford", "Brentford", "Brentford FC"),
        ("Brighton", "Brighton", "Brighton & Hove Albion", "Brighton and Hove Albion", "Brighton Hove"),
        ("Burnley", "Burnley", "Burnley FC"),
        ("Chelsea", "Chelsea", "Chelsea FC"),
        ("Crystal Palace", "Crystal Palace", "Crystal Palace FC"),
        ("Everton", "Everton", "Everton FC"),
        ("Fulham", "Fulham", "Fulham FC"),
        ("Ipswich", "Ipswich", "Ipswich Town", "Ipswich Town FC"),
        ("Leeds", "Leeds", "Leeds United", "Leeds United FC"),
        ("Leicester", "Leicester", "Leicester City", "Leicester City FC"),
        ("Liverpool", "Liverpool", "Liverpool FC"),
        ("Luton", "Luton", "Luton Town"),
        ("Manchester City", "Man City", "Manchester City", "Manchester City FC"),
        ("Manchester United", "Man United", "Manchester United", "Manchester United FC", "Man Utd"),
        ("Newcastle", "Newcastle", "Newcastle United", "Newcastle United FC"),
        ("Nottingham Forest", "Nott'm Forest", "Nottingham Forest", "Nottingham Forest FC", "Nottingham"),
        ("Sheffield United", "Sheffield United", "Sheffield Utd"),
        ("Sheffield Wednesday", "Sheffield Weds", "Sheffield Wednesday"),
        ("Southampton", "Southampton", "Southampton FC"),
        ("Sunderland", "Sunderland", "Sunderland AFC"),
        ("Tottenham", "Tottenham", "Tottenham Hotspur", "Tottenham Hotspur FC", "Spurs"),
        ("West Bromwich", "West Brom", "West Bromwich Albion"),
        ("West Ham", "West Ham", "West Ham United", "West Ham United FC"),
        ("Wolverhampton", "Wolves", "Wolverhampton Wanderers", "Wolverhampton Wanderers FC", "Wolverhampton"),
        ("Coventry", "Coventry", "Coventry City", "Coventry City FC"),
        ("Hull City", "Hull", "Hull City", "Hull City AFC"),
        ("Middlesbrough", "Middlesbrough", "Middlesbrough FC"),
        ("Norwich", "Norwich", "Norwich City"),
        ("Watford", "Watford", "Watford FC"),
        ("Stoke", "Stoke", "Stoke City"),
        ("QPR", "QPR", "Queens Park Rangers"),
        ("Swansea", "Swansea", "Swansea City"),
        ("Cardiff", "Cardiff", "Cardiff City"),
        ("Millwall", "Millwall", "Millwall FC"),
        ("Preston", "Preston", "Preston North End"),
        ("Blackburn", "Blackburn", "Blackburn Rovers"),
        ("Bristol City", "Bristol City", "Bristol City FC"),
        ("Derby", "Derby", "Derby County"),
        ("Plymouth", "Plymouth", "Plymouth Argyle"),
        ("Portsmouth", "Portsmouth", "Portsmouth FC"),
        ("Oxford United", "Oxford", "Oxford United"),
        ("Charlton", "Charlton", "Charlton Athletic"),
        ("Wrexham", "Wrexham", "Wrexham AFC"),
        ("Birmingham", "Birmingham", "Birmingham City"),
        ("Huddersfield", "Huddersfield", "Huddersfield Town"),
        ("Rotherham", "Rotherham", "Rotherham United"),
        ("Barnsley", "Barnsley", "Barnsley FC"),
        ("Reading", "Reading", "Reading FC"),
    ],
    "IT": [
        ("Atalanta", "Atalanta", "Atalanta BC", "Atalanta Bergamasca Calcio"),
        ("Bolonia", "Bologna", "Bologna FC 1909", "Bologna FC"),
        ("Cagliari", "Cagliari", "Cagliari Calcio"),
        ("Como", "Como", "Como 1907"),
        ("Empoli", "Empoli", "Empoli FC"),
        ("Fiorentina", "Fiorentina", "ACF Fiorentina"),
        ("Frosinone", "Frosinone", "Frosinone Calcio"),
        ("Genoa", "Genoa", "Genoa CFC"),
        ("Inter", "Inter", "FC Internazionale Milano", "Internazionale", "Inter Milan", "Inter Milano"),
        ("Juventus", "Juventus", "Juventus FC"),
        ("Lazio", "Lazio", "SS Lazio"),
        ("Lecce", "Lecce", "US Lecce"),
        ("Milan", "Milan", "AC Milan"),
        ("Monza", "Monza", "AC Monza"),
        ("Nápoles", "Napoli", "SSC Napoli"),
        ("Parma", "Parma", "Parma Calcio 1913", "Parma Calcio"),
        ("Roma", "Roma", "AS Roma"),
        ("Salernitana", "Salernitana", "US Salernitana 1919"),
        ("Sassuolo", "Sassuolo", "US Sassuolo Calcio", "US Sassuolo"),
        ("Torino", "Torino", "Torino FC"),
        ("Udinese", "Udinese", "Udinese Calcio"),
        ("Venezia", "Venezia", "Venezia FC"),
        ("Verona", "Verona", "Hellas Verona", "Hellas Verona FC"),
        ("Cremonese", "Cremonese", "US Cremonese"),
        ("Pisa", "Pisa", "AC Pisa 1909", "Pisa SC"),
        ("Spezia", "Spezia", "Spezia Calcio"),
        ("Sampdoria", "Sampdoria", "UC Sampdoria"),
        ("Palermo", "Palermo", "Palermo FC"),
        ("Bari", "Bari", "SSC Bari"),
        ("Catanzaro", "Catanzaro", "US Catanzaro 1929"),
        ("Cesena", "Cesena", "Cesena FC"),
        ("Módena", "Modena", "Modena FC"),
        ("Brescia", "Brescia", "Brescia Calcio"),
        ("Sudtirol", "Sudtirol", "FC Südtirol", "Südtirol"),
        ("Reggiana", "Reggiana", "AC Reggiana 1919"),
        ("Cittadella", "Cittadella", "AS Cittadella"),
        ("Carrarese", "Carrarese", "Carrarese Calcio"),
        ("Juve Stabia", "Juve Stabia", "SS Juve Stabia"),
        ("Mantova", "Mantova", "Mantova 1911"),
        ("Cosenza", "Cosenza", "Cosenza Calcio"),
        ("Avellino", "Avellino", "US Avellino 1912"),
        ("Padova", "Padova", "Calcio Padova"),
        ("Entella", "Virtus Entella", "Entella"),
    ],
    "DE": [
        ("Augsburgo", "Augsburg", "FC Augsburg"),
        ("Bayern", "Bayern Munich", "FC Bayern München", "Bayern München", "Bayern Munchen", "Bayern Múnich"),
        ("Dortmund", "Dortmund", "Borussia Dortmund", "BV Borussia 09 Dortmund"),
        ("Eintracht", "Ein Frankfurt", "Eintracht Frankfurt"),
        ("Colonia", "FC Koln", "1. FC Köln", "FC Köln", "Köln", "Koln", "Cologne"),
        ("Friburgo", "Freiburg", "SC Freiburg", "Sport-Club Freiburg"),
        ("Hamburgo", "Hamburg", "Hamburger SV", "Hamburger"),
        ("Heidenheim", "Heidenheim", "1. FC Heidenheim 1846", "FC Heidenheim"),
        ("Hoffenheim", "Hoffenheim", "TSG 1899 Hoffenheim", "TSG Hoffenheim"),
        ("Holstein Kiel", "Holstein Kiel", "KSV Holstein"),
        ("Leverkusen", "Leverkusen", "Bayer 04 Leverkusen", "Bayer Leverkusen"),
        ("Mönchengladbach", "M'gladbach", "Borussia Mönchengladbach", "Borussia Monchengladbach",
         "Borussia M'gladbach", "Gladbach"),
        ("Mainz", "Mainz", "1. FSV Mainz 05", "FSV Mainz 05", "Mainz 05"),
        ("RB Leipzig", "RB Leipzig", "Leipzig", "RasenBallsport Leipzig"),
        ("St. Pauli", "St Pauli", "FC St. Pauli", "FC St. Pauli 1910"),
        ("Stuttgart", "Stuttgart", "VfB Stuttgart"),
        ("Union Berlin", "Union Berlin", "1. FC Union Berlin"),
        ("Werder Bremen", "Werder Bremen", "SV Werder Bremen", "Werder"),
        ("Wolfsburgo", "Wolfsburg", "VfL Wolfsburg"),
        ("Bochum", "Bochum", "VfL Bochum 1848", "VfL Bochum"),
        ("Darmstadt", "Darmstadt", "SV Darmstadt 98"),
        ("Schalke", "Schalke 04", "FC Schalke 04", "Schalke"),
        ("Hertha", "Hertha", "Hertha BSC", "Hertha Berlin"),
        ("Elversberg", "Elversberg", "SV 07 Elversberg", "SV Elversberg"),
        ("Paderborn", "Paderborn", "SC Paderborn 07", "SC Paderborn"),
        ("Hannover", "Hannover", "Hannover 96"),
        ("Núremberg", "Nurnberg", "1. FC Nürnberg", "Nürnberg", "FC Nurnberg"),
        ("Karlsruhe", "Karlsruhe", "Karlsruher SC"),
        ("Kaiserslautern", "Kaiserslautern", "1. FC Kaiserslautern"),
        ("Fortuna Düsseldorf", "Fortuna Dusseldorf", "Fortuna Düsseldorf"),
        ("Greuther Fürth", "Greuther Furth", "SpVgg Greuther Fürth"),
        ("Magdeburgo", "Magdeburg", "1. FC Magdeburg"),
        ("Braunschweig", "Braunschweig", "Eintracht Braunschweig"),
        ("Arminia Bielefeld", "Bielefeld", "Arminia Bielefeld", "DSC Arminia Bielefeld"),
        ("Preußen Münster", "Preussen Munster", "SC Preußen Münster", "Munster"),
        ("Ulm", "Ulm", "SSV Ulm 1846"),
        ("Regensburg", "Regensburg", "SSV Jahn Regensburg", "Jahn Regensburg"),
        ("Dinamo Dresde", "Dresden", "Dynamo Dresden", "SG Dynamo Dresden"),
    ],
    "FR": [
        ("Angers", "Angers", "Angers SCO"),
        ("Auxerre", "Auxerre", "AJ Auxerre"),
        ("Brest", "Brest", "Stade Brestois 29", "Stade Brestois"),
        ("Le Havre", "Le Havre", "Le Havre AC", "Havre AC"),
        ("Lens", "Lens", "RC Lens", "Racing Club de Lens"),
        ("Lille", "Lille", "Lille OSC", "LOSC Lille"),
        ("Lorient", "Lorient", "FC Lorient"),
        ("Lyon", "Lyon", "Olympique Lyonnais", "Olympique Lyon"),
        ("Marsella", "Marseille", "Olympique de Marseille", "Olympique Marseille"),
        ("Metz", "Metz", "FC Metz"),
        ("Mónaco", "Monaco", "AS Monaco", "AS Monaco FC"),
        ("Montpellier", "Montpellier", "Montpellier HSC"),
        ("Nantes", "Nantes", "FC Nantes"),
        ("Niza", "Nice", "OGC Nice"),
        ("Paris FC", "Paris FC"),
        ("PSG", "Paris SG", "Paris Saint-Germain", "Paris Saint Germain", "Paris Saint-Germain FC", "PSG"),
        ("Reims", "Reims", "Stade de Reims"),
        ("Rennes", "Rennes", "Stade Rennais FC 1901", "Stade Rennais", "Stade Rennais FC"),
        ("Saint-Étienne", "St Etienne", "AS Saint-Étienne", "Saint-Etienne", "Saint Etienne"),
        ("Estrasburgo", "Strasbourg", "RC Strasbourg Alsace", "RC Strasbourg", "Racing Club de Strasbourg Alsace"),
        ("Toulouse", "Toulouse", "Toulouse FC"),
        ("Clermont", "Clermont", "Clermont Foot 63", "Clermont Foot"),
        ("Le Mans", "Le Mans", "Le Mans FC"),
        ("Troyes", "Troyes", "ESTAC Troyes", "Troyes AC"),
        ("Ajaccio", "Ajaccio", "AC Ajaccio"),
        ("Bastia", "Bastia", "SC Bastia"),
        ("Guingamp", "Guingamp", "En Avant Guingamp", "EA Guingamp"),
        ("Caen", "Caen", "Stade Malherbe Caen", "SM Caen"),
        ("Grenoble", "Grenoble", "Grenoble Foot 38"),
        ("Laval", "Laval", "Stade Lavallois"),
        ("Pau", "Pau FC", "Pau"),
        ("Annecy", "Annecy", "FC Annecy"),
        ("Dunkerque", "Dunkerque", "USL Dunkerque"),
        ("Amiens", "Amiens", "Amiens SC"),
        ("Rodez", "Rodez", "Rodez AF"),
        ("Red Star", "Red Star", "Red Star FC"),
        ("Burdeos", "Bordeaux", "Girondins de Bordeaux", "FC Girondins de Bordeaux"),
        ("Quevilly Rouen", "Quevilly Rouen", "QRM"),
        ("Boulogne", "Boulogne", "US Boulogne"),
        ("Nancy", "Nancy", "AS Nancy Lorraine"),
        ("Valenciennes", "Valenciennes", "Valenciennes FC"),
        ("Martigues", "Martigues", "FC Martigues"),
    ],
    "PT": [
        ("Arouca", "Arouca", "FC Arouca"),
        ("Benfica", "Benfica", "SL Benfica", "Sport Lisboa e Benfica"),
        ("Boavista", "Boavista", "Boavista FC"),
        ("Casa Pia", "Casa Pia", "Casa Pia AC"),
        ("Estoril", "Estoril", "GD Estoril Praia", "Estoril Praia"),
        ("Estrela Amadora", "Estrela", "CF Estrela da Amadora", "Estrela da Amadora", "Estrela Amadora"),
        ("Famalicão", "Famalicao", "FC Famalicão", "Famalicão"),
        ("Farense", "Farense", "SC Farense"),
        ("Gil Vicente", "Gil Vicente", "Gil Vicente FC"),
        ("Vitória Guimarães", "Guimaraes", "Vitória SC", "Vitoria Guimaraes", "Vitória Guimarães",
         "Vitória de Guimarães"),
        ("Moreirense", "Moreirense", "Moreirense FC"),
        ("Nacional", "Nacional", "CD Nacional", "Nacional da Madeira"),
        ("Oporto", "Porto", "FC Porto"),
        ("Rio Ave", "Rio Ave", "Rio Ave FC"),
        ("Santa Clara", "Santa Clara", "CD Santa Clara"),
        ("Braga", "Sp Braga", "SC Braga", "Sporting Clube de Braga", "Sporting Braga", "Braga"),
        ("Sporting de Portugal", "Sp Lisbon", "Sporting Clube de Portugal", "Sporting CP", "Sporting Lisbon",
         "Sporting Lisboa"),
        ("AVS", "AVS", "AVS Futebol SAD", "AFS"),
        ("Tondela", "Tondela", "CD Tondela"),
        ("Alverca", "Alverca", "FC Alverca"),
        ("Académico de Viseu", "Academico Viseu", "Académico de Viseu", "Academico de Viseu"),
        ("Marítimo", "Maritimo", "CS Marítimo", "Marítimo"),
        ("Chaves", "Chaves", "GD Chaves"),
        ("Portimonense", "Portimonense", "Portimonense SC"),
        ("Vizela", "Vizela", "FC Vizela"),
        ("Paços de Ferreira", "Pacos Ferreira", "FC Paços de Ferreira", "Paços Ferreira"),
        ("Belenenses", "Belenenses", "CF Os Belenenses"),
    ],
    "NL": [
        ("Ajax", "Ajax", "AFC Ajax", "Ajax Amsterdam"),
        ("AZ", "AZ Alkmaar", "AZ"),
        ("Feyenoord", "Feyenoord", "Feyenoord Rotterdam"),
        ("Fortuna Sittard", "For Sittard", "Fortuna Sittard"),
        ("Go Ahead Eagles", "Go Ahead Eagles", "Go Ahead Eagles Deventer"),
        ("Groningen", "Groningen", "FC Groningen"),
        ("Heerenveen", "Heerenveen", "SC Heerenveen"),
        ("Heracles", "Heracles", "Heracles Almelo"),
        ("NAC Breda", "NAC Breda", "NAC"),
        ("NEC", "Nijmegen", "NEC Nijmegen", "NEC"),
        ("PSV", "PSV Eindhoven", "PSV"),
        ("Sparta Rotterdam", "Sparta Rotterdam", "Sparta"),
        ("Twente", "Twente", "FC Twente", "FC Twente '65"),
        ("Utrecht", "Utrecht", "FC Utrecht"),
        ("RKC Waalwijk", "Waalwijk", "RKC Waalwijk", "RKC"),
        ("Willem II", "Willem II", "Willem II Tilburg"),
        ("PEC Zwolle", "Zwolle", "PEC Zwolle"),
        ("Almere City", "Almere City", "Almere City FC"),
        ("Excelsior", "Excelsior", "SBV Excelsior", "Excelsior Rotterdam"),
        ("Telstar", "Telstar", "SC Telstar"),
        ("Volendam", "Volendam", "FC Volendam"),
        ("Cambuur", "Cambuur", "SC Cambuur", "SC Cambuur-Leeuwarden"),
        ("ADO Den Haag", "Den Haag", "ADO Den Haag"),
        ("Vitesse", "Vitesse", "SBV Vitesse"),
        ("Emmen", "Emmen", "FC Emmen"),
        ("Roda JC", "Roda", "Roda JC Kerkrade"),
        ("De Graafschap", "De Graafschap", "Graafschap"),
    ],
}

# Clubes que solo existen en femenino (o que se llaman distinto) — se miran ANTES que CLUBS en
# las fichas femeninas. El resto ("Barcelona W", "Bayern München Women") sale de CLUBS tras
# quitar la marca de femenino.
WOMEN_CLUBS: dict[str, list[tuple[str, ...]]] = {
    "WLF": [
        ("Madrid CFF", "Madrid CFF", "Madrid Club de Fútbol Femenino"),
        ("Costa Adeje Tenerife", "Costa Adeje Tenerife", "UD Granadilla Tenerife", "Granadilla Tenerife",
         "UDG Tenerife", "Tenerife Egatesa", "Granadilla"),
        ("Badalona", "Badalona", "Levante Badalona", "Badalona Women", "CE Europa Badalona"),
        ("DUX Logroño", "DUX Logroño", "EdF Logroño", "Logroño", "DUX Logrono"),
        ("Alhama", "Alhama", "Alhama CF ElPozo", "Alhama ElPozo", "Alhama CF"),
        ("Sporting de Huelva", "Sporting Huelva", "Sporting Club de Huelva", "Sporting de Huelva"),
        ("Levante Las Planas", "Levante Las Planas", "Levante UD Las Planas"),
        ("Santa Teresa", "Santa Teresa", "Santa Teresa Badajoz"),
        ("Deportivo", "Deportivo Abanca", "Deportivo Abanca Femenino", "RC Deportivo Abanca"),
    ],
    "WSL": [
        ("London City Lionesses", "London City Lionesses", "London City"),
        ("Bristol City", "Bristol City Women"),
        ("Reading", "Reading Women"),
    ],
    "WFBL": [
        ("Essen", "SGS Essen", "Essen"),
        ("Turbine Potsdam", "Turbine Potsdam", "1. FFC Turbine Potsdam", "Potsdam"),
        ("Carl Zeiss Jena", "Carl Zeiss Jena", "FC Carl Zeiss Jena", "Jena"),
        ("Duisburgo", "MSV Duisburg", "Duisburg"),
        ("Sand", "SC Sand"),
    ],
    "WSA": [
        ("Ternana", "Ternana", "Ternana Women"),
        ("Pomigliano", "Pomigliano"),
    ],
    "WD1": [
        ("Fleury", "Fleury", "FC Fleury 91", "Fleury 91"),
        ("Dijon", "Dijon", "Dijon FCO", "Dijon FCO Women"),
        ("Lyon", "OL Lyonnes", "Olympique Lyonnais Féminin", "OL Feminin"),
        ("Saint-Étienne", "ASSE", "AS Saint-Étienne Féminines"),
        ("Soyaux", "Soyaux", "ASJ Soyaux"),
    ],
}


def _build(groups: dict[str, list[tuple[str, ...]]], women: bool) -> tuple[dict[str, str], list[tuple]]:
    table: dict[str, str] = {}
    collisions: list[tuple] = []
    for entries in groups.values():
        for display, *variants in entries:
            for v in (display, *variants):
                k = team_key(v, women)
                if not k:
                    continue
                if k in table and table[k] != display:
                    collisions.append((k, table[k], display))
                    continue
                table.setdefault(k, display)
    return table, collisions


_MEN, _MEN_COLLISIONS = _build(CLUBS, women=False)
_WOMEN, _WOMEN_COLLISIONS = _build(WOMEN_CLUBS, women=True)
# Las variantes de CLUBS también sirven en femenino quitando la marca ("Barcelona W")
_MEN_W = {team_key(k, True): v for k, v in _MEN.items() if team_key(k, True)}


def collisions() -> list[tuple]:
    """(clave, nombre ya puesto, nombre descartado) — vacío si la tabla está bien (lo mira un test)."""
    return _MEN_COLLISIONS + _WOMEN_COLLISIONS


def _clean(name: str, women: bool) -> str:
    """Nombre fuera de la tabla: sin siglas societarias ni años en los extremos (ni marca de femenino)."""
    ws = (name or "").split()
    drop = _EDGE_DROP | (_WOMEN_DROP if women else frozenset())

    def junk(w: str) -> bool:
        k = "".join(_words(w))
        return k in drop or k.isdigit()
    while len(ws) > 1 and junk(ws[0]):
        ws.pop(0)
    while len(ws) > 1 and junk(ws[-1]):
        ws.pop()
    return " ".join(ws) or (name or "").strip()


def display_name(name: Optional[str], group: Optional[str] = None) -> str:
    """Nombre de pantalla en español. group: "women" para fichas femeninas (mp-w1); el resto
    (None, "men", "cups") usa la tabla de clubes."""
    if not name:
        return name or ""
    women = group == "women"
    k = team_key(name, women)
    if women:
        hit = _WOMEN.get(k) or _MEN_W.get(k)
    else:
        hit = _MEN.get(k)
    return hit or _clean(name, women)


def known(name: Optional[str], group: Optional[str] = None) -> bool:
    """True si el nombre está en la tabla (no sale por el respaldo de nombre limpio)."""
    k = team_key(name or "", group == "women")
    return bool(k) and (k in _MEN if group != "women" else (k in _WOMEN or k in _MEN_W))


_PUNCT = re.compile(r"[^\w ]|_")


def badge_initials(display: str) -> str:
    """Iniciales del escudo (las mismas que initials() de trends_web/site/app.js)."""
    ws = _PUNCT.sub(" ", display or "").split()
    if not ws:
        return "?"
    if len(ws) > 1:
        ws = [w for w in ws if not w.islower()] or ws          # sin "de", "da"…
    return (ws[0][:3] if len(ws) == 1 else "".join(w[0] for w in ws[:3])).upper()


def coverage() -> dict[str, int]:
    """Clubes de la tabla por país (masculino) y por liga femenina."""
    return {**{c: len(v) for c, v in CLUBS.items()}, **{d: len(v) for d, v in WOMEN_CLUBS.items()}}
