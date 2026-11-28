"""
Resumen diario de Tendencias por Telegram (hilo de Tendencias). Fichas de partido del día (hora de
Madrid): masculino (match_previews, mp-v1), copas (match_previews_cups), Europa
(match_previews_uefa, mp-e1), femenino (match_previews_women, mp-w1) y baloncesto
(match_previews_basket, bp-v1, Euroliga y NBA). En copas la ⭐ solo llega tras 50 graduados en directo con
IC95 bajo 0 (antes, "medido en backtest": sin píldora). Europa (desde el 28-nov), como las ligas: ⭐ en
los mercados con ventaja medida en su backtest (match_preview_metrics/mp-e1_backtest; la etiqueta va
guardada en la ficha). Baloncesto, como las ligas de fútbol: ⭐ en los mercados con ventaja medida en
su backtest (frente a la media de la liga y del equipo). NBA (desde el 28-nov): entran los partidos
de la noche de hoy en EE. UU. (su «date» es el día de EE. UU.; en Madrid empiezan de madrugada, del
día siguiente), con la hora marcada «(madrugada)»; así llegan antes de jugarse.

Formato (20-nov-2026, decisión del usuario): HTML de Telegram, UN mensaje para el día (se parte
solo si no cabe; un partido nunca se corta). Por partido: icono + liga + hora, equipos en negrita,
píldoras SOLO de mercados con ventaja medida (⭐ = el modelo acierta más que la media de la liga,
medido). 21-nov-2026: sin el plegable del esperado (está en la web), las 3 mejores ⭐ por partido
y los partidos sin ninguna juntos en «Sin tendencias destacadas»; el largo se mide como Telegram
(sin etiquetas, UTF-16). Solo estadísticas del modelo: sin cuotas ni nada de apuestas.
"""
import html
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from shared.team_display_names import display_name

logger = logging.getLogger(__name__)

WEB_URL = "https://tendencias-pi.pages.dev"
MAX_LEN = 3800                          # Telegram admite 4096
TG_LIMIT = 4096                         # el enlace final puede usar el margen (no se parte solo por él)
TG_PILLS_MAX = 3                        # en Telegram, las 3 mejores ⭐ por partido (la web enseña hasta 5)


def tg_len(text: str) -> int:
    """Largo como lo cuenta Telegram: texto ya sin etiquetas HTML, en unidades UTF-16."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2


STATS = (("corners", "Córners"), ("cards", "Tarjetas"), ("fouls", "Faltas"),
         ("shots", "Remates"), ("sot", "A puerta"), ("goals", "Goles"))
DIV_NAMES = {"SP1": "LaLiga", "SP2": "Segunda", "I1": "Serie A", "E0": "Premier", "F1": "Ligue 1",
             "P1": "Primeira", "D1": "Bundesliga", "N1": "Eredivisie",
             "WLF": "Liga F", "WSL": "WSL", "WFBL": "Frauen-Bundesliga", "WSA": "Serie A fem.", "WD1": "D1 Arkema"}
DIV_ORDER = ("SP1", "SP2", "E0", "I1", "D1", "F1", "P1", "N1", "WLF", "WSL", "WFBL", "WSA", "WD1")
UEFA_NAMES = {"CL": "Champions League", "EL": "Europa League", "ECL": "Conference League"}
# Baloncesto (bp-v1): sus estadísticas y el nombre de la liga si la ficha no lo trae.
BASKET_STATS = (("pts", "Puntos"), ("reb", "Rebotes"), ("ast", "Asistencias"), ("fg3m", "Triples"))
BASKET_NAMES = {"EUR": "Euroliga", "NBA": "NBA"}
# NBA: el resumen lee las fichas de baloncesto hasta estas horas después del fin del día de Madrid
# (los partidos de la noche de EE. UU. empiezan entre las 00:00 y las ~05:00 UTC).
NBA_NIGHT_H = 12
SEP = "━━━━━━━━━━━━━━"
STAR_LEGEND = "⭐ = el modelo acierta más que la media de la liga"
WEEKDAYS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


# ── Hora de Madrid sin depender de tzdata (CET/CEST: último domingo de marzo y de octubre, 01:00 UTC) ──
def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def madrid_offset(utc_dt: datetime) -> timedelta:
    y = utc_dt.year
    start = datetime.combine(_last_sunday(y, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(y, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return timedelta(hours=2 if start <= utc_dt < end else 1)


def madrid(utc_dt: datetime) -> datetime:
    return (utc_dt + madrid_offset(utc_dt)).replace(tzinfo=None)


def day_window(now: datetime) -> tuple[date, datetime, datetime]:
    """(día de Madrid de `now`, inicio y fin de ese día en UTC)."""
    d = madrid(now).date()
    lo = datetime.combine(d, datetime.min.time(), timezone.utc)
    lo -= madrid_offset(lo)
    hi = datetime.combine(d + timedelta(days=1), datetime.min.time(), timezone.utc)
    hi -= madrid_offset(hi)
    return d, lo, hi


def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _line(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def main_line(block: dict) -> Optional[dict]:
    """Línea guardada como principal o, en fichas antiguas, la de P(más) más cercana al 50 %."""
    lines = list((block.get("lines") or {}).values())
    if not lines:
        return None
    ml = block.get("main_line")
    hit = [v for v in lines if v["line"] == ml] if ml is not None else []
    return hit[0] if hit else min(lines, key=lambda v: (abs(v["p_model"] - 0.5), v["line"]))


def _pct(p: float) -> str:
    return f"{round(p * 100)} %"


# Píldoras (las mismas que la ficha de la web): SOLO mercados por equipo con "ventaja medida" (backtest
# en ligas; en copas y femenino, además 50 graduadas en directo con IC95 bajo 0). En cada mercado y
# equipo, la línea MÁS ALTA con el modelo entre el 60 % y el 90 %, sin las de 0,5 salvo "marca al
# menos 1 gol"; como mucho 5, de mayor a menor %. Todas llevan ⭐ (la ventaja está medida).
EDGE = "ventaja medida"
PILL_MIN, PILL_MAX, PILL_MAX_N = 0.60, 0.90, 5
PILL_NOUN = {"corners": "córners", "cards": "tarjetas", "fouls": "faltas", "shots": "remates",
             "sot": "remates a puerta", "goals": "goles",
             "pts": "puntos", "reb": "rebotes", "ast": "asistencias", "fg3m": "triples"}


def is_basket(p: dict) -> bool:
    return p.get("group") == "basket"


def is_nba(p: dict) -> bool:
    return is_basket(p) and (p.get("league") or p.get("div")) == "NBA"


def in_summary_day(p: dict, day: date, lo: datetime, hi: datetime) -> bool:
    """¿Entra la ficha en el resumen de `day`? NBA: si su día de EE. UU. es `day` (los partidos de
    esta noche, aunque en Madrid sean de madrugada del día siguiente). El resto: si empieza dentro
    del día de Madrid [lo, hi)."""
    if is_nba(p):
        return str(p.get("date") or "")[:10] == day.isoformat()
    return lo <= _parse(p["kickoff_utc"]) < hi


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def names(p: dict) -> tuple[str, str]:
    """Nombres de pantalla en español ("Ath Bilbao" → "Athletic"); solo presentación. Baloncesto: el
    nombre del feed tal cual (la tabla es de clubes de fútbol)."""
    if is_basket(p):
        return p["home_team"], p["away_team"]
    return display_name(p["home_team"], p.get("group")), display_name(p["away_team"], p.get("group"))


def _edge(blk: dict) -> bool:
    return (blk.get("market_label") or blk.get("label")) == EDGE


def pill_line_ok(key: str, line: float) -> bool:
    """Fuera las líneas de 0,5 salvo la de goles ("marca al menos 1 gol")."""
    return line != 0.5 or key == "goals"


def match_pills(p: dict) -> list[dict]:
    if p.get("low_sample"):
        return []
    out = []
    home, away = names(p)
    for key, _ in (BASKET_STATS if is_basket(p) else STATS):
        b = (p.get("stats") or {}).get(key) or {}
        if not b.get("available"):
            continue
        for side, team in (("home", home), ("away", away)):
            blk = (b.get("lines") or {}).get(side) or {}
            if not _edge(blk):
                continue
            lines = [v for v in (blk.get("lines") or {}).values()
                     if PILL_MIN <= v["p_model"] <= PILL_MAX and pill_line_ok(key, v["line"])]
            if not lines:
                continue
            v = max(lines, key=lambda x: x["line"])
            out.append({"team": team, "key": key, "line": v["line"], "p": v["p_model"]})
    return sorted(out, key=lambda x: -x["p"])[:PILL_MAX_N]


def pill_html(x: dict) -> str:
    if x["key"] == "goals" and x["line"] == 0.5:
        body = f"{esc(x['team'])} <b>marca</b> al menos 1 gol"
    else:
        body = f"{esc(x['team'])} más de <b>{_line(x['line'])}</b> {PILL_NOUN[x['key']]}"
    return f"⭐ {body} · <b>{_pct(x['p'])}</b>"


def group_icon_label(p: dict) -> str:
    if p.get("group") == "cups":
        return f"🏆 {p.get('cup') or 'Copa'}"
    if p.get("group") == "uefa":
        return f"🌍 {p.get('comp_name') or UEFA_NAMES.get(p.get('comp') or p.get('div'), 'Europa')}"
    div = p.get("div") or "?"
    if is_basket(p):
        return f"🏀 {p.get('league_name') or BASKET_NAMES.get(div, div)}"
    if p.get("group") == "women" or div.startswith("W"):
        return f"👩 {DIV_NAMES.get(div, div)}"
    return f"⚽ {DIV_NAMES.get(div, div)}"


def match_block(p: dict, pills: list[dict] | None = None) -> str:
    """Partido con sus píldoras ⭐ (sin el esperado de cada mercado: está en la web)."""
    ko = madrid(_parse(p["kickoff_utc"]))
    home, away = names(p)
    pills = match_pills(p) if pills is None else pills
    # NBA: la hora de Madrid cae en la madrugada siguiente al día del resumen
    late = " (madrugada)" if is_nba(p) and ko.date().isoformat() > str(p.get("date") or "")[:10] else ""
    out = [SEP, f"{group_icon_label(p)} · {ko:%H:%M}{late}", f"<b>{esc(home)} – {esc(away)}</b>"]
    out += [pill_html(x) for x in pills]
    return "\n".join(out)


def pair_label(p: dict) -> str:
    """'A–B' para la línea final; femenino y copas con su icono para no confundirlos."""
    home, away = names(p)
    icon = group_icon_label(p).split(" ", 1)[0]
    return f"{'' if icon == '⚽' else icon + ' '}{esc(home)}–{esc(away)}"


def _group_rank(p: dict) -> int:
    if p.get("group") in ("uefa", "cups"):
        return len(DIV_ORDER) + (0 if p.get("group") == "uefa" else 1)
    if is_basket(p):                    # a la misma hora, detrás del fútbol
        return len(DIV_ORDER) + 2
    div = p.get("div") or ""
    return DIV_ORDER.index(div) if div in DIV_ORDER else len(DIV_ORDER) + 3


BASKET_FAIL = "⚠️ Baloncesto: no se pudieron montar sus fichas de hoy."
UEFA_FAIL = "⚠️ Europa: no se pudieron leer sus fichas de hoy."


def _basket_ok(p: dict) -> bool:
    """¿Se puede montar esta ficha de baloncesto? (todo lo que build_messages le va a pedir)."""
    try:
        str(p["kickoff_utc"]), str(p["home_team"])
        match_block(p, match_pills(p)[:TG_PILLS_MAX])
        pair_label(p)
        _group_rank(p)
        return True
    except Exception:
        logger.warning("trends_summary: ficha de baloncesto sin montar (se omite)", exc_info=True)
        return False


def build_messages(previews: list[dict], day: date, avisos: Optional[list[str]] = None) -> list[str]:
    """Resumen del día por hora de inicio: cada partido con alguna ⭐ con sus píldoras; los
    que no tienen ninguna, juntos al final en «Sin tendencias destacadas». Si no cabe en un
    mensaje se parte entre partidos (o entre nombres de la línea final). El último lleva el
    enlace a la web.

    Un fallo al leer (avisos, de read_day_previews) o al montar el baloncesto no afecta al resto:
    esas fichas se quitan y el último mensaje lleva un aviso."""
    avisos = list(avisos or [])
    ok = [p for p in previews if not is_basket(p) or _basket_ok(p)]
    if len(ok) < len(previews) and BASKET_FAIL not in avisos:
        avisos.append(BASKET_FAIL)
    previews = ok
    if not previews:
        return []
    ms = sorted(previews, key=lambda p: (p["kickoff_utc"], _group_rank(p), p["home_team"]))
    dlabel = f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"
    pills = [(p, match_pills(p)[:TG_PILLS_MAX]) for p in ms]
    with_pills = [(p, pl) for p, pl in pills if pl]
    without = [p for p, pl in pills if not pl]
    n = len(ms)
    head = (f"<b>📊 TENDENCIAS · {dlabel}</b>\n{n} partido{'s' if n != 1 else ''}"
            f" · {len(with_pills)} con ⭐\n{STAR_LEGEND}")
    cont = f"<b>📊 TENDENCIAS · {dlabel}</b> (sigue)"
    link = f'\n\n<a href="{WEB_URL}/#partidos/{day.isoformat()}">Fichas completas en la web</a>'
    msgs: list[str] = []
    cur = head

    def add(piece: str, sep: str = "\n") -> None:
        nonlocal cur
        if tg_len(cur + sep + piece) > MAX_LEN:
            msgs.append(cur)
            cur = cont
            sep = "\n"
        cur += sep + piece

    for p, pl in with_pills:
        add(match_block(p, pl))
    if without:
        add(SEP)
        add("<b>Sin tendencias destacadas:</b> " + pair_label(without[0]))
        for p in without[1:]:
            add(pair_label(p), ", ")
    if avisos:
        add("\n".join(esc(a) for a in avisos), "\n\n")
    if tg_len(cur + link) > TG_LIMIT:
        msgs.append(cur)
        cur = cont
    msgs.append(cur + link)
    return msgs


def read_day_previews(lo: datetime, hi: datetime, avisos: Optional[list[str]] = None) -> list[dict]:
    """Fichas del día de todas las colecciones. Si el baloncesto o Europa no se pueden leer, se sigue
    sin ellos y se deja el aviso en `avisos` (el fútbol, como siempre). Baloncesto: se lee hasta
    NBA_NIGHT_H horas más tarde y se filtra con in_summary_day (la NBA de esta noche entra; la de
    anoche, ya jugada, no)."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    day = madrid(lo).date()
    out = []
    for coll in ("match_previews", "match_previews_cups", "match_previews_uefa", "match_previews_women",
                 "match_previews_basket"):
        basket = coll == "match_previews_basket"
        top = hi + timedelta(hours=NBA_NIGHT_H) if basket else hi
        q = (col(coll).where(filter=FieldFilter("kickoff_utc", ">=", lo.isoformat()))
             .where(filter=FieldFilter("kickoff_utc", "<", top.isoformat())))
        fail = {"match_previews_basket": BASKET_FAIL, "match_previews_uefa": UEFA_FAIL}.get(coll)
        if fail is None:
            out.extend(s.to_dict() for s in q.stream())
            continue
        try:
            docs = [s.to_dict() for s in q.stream()]
            out.extend([p for p in docs if in_summary_day(p, day, lo, hi)] if basket else docs)
        except Exception:
            logger.warning("trends_summary: no se pudieron leer las fichas de %s", coll, exc_info=True)
            if avisos is not None:
                avisos.append(fail)
    return out
