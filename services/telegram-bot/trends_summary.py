"""
Resumen diario de Tendencias por Telegram (hilo de Tendencias). Fichas de partido del día (hora de
Madrid): masculino (match_previews, mp-v1), copas (match_previews_cups), Europa
(match_previews_uefa, mp-e1) y femenino (match_previews_women, mp-w1). En copas y Europa la ⭐ solo
llega tras 50 graduados en directo con IC95 bajo 0 (antes, "medido en backtest": sin píldora).

Formato (20-nov-2026, decisión del usuario): HTML de Telegram, UN mensaje para el día (se parte
solo si no cabe; un partido nunca se corta). Por partido: icono + liga + hora, equipos en negrita,
píldoras SOLO de mercados con ventaja medida (⭐ = el modelo acierta más que la media de la liga,
medido). 21-nov-2026: sin el plegable del esperado (está en la web), las 3 mejores ⭐ por partido
y los partidos sin ninguna juntos en «Sin tendencias destacadas»; el largo se mide como Telegram
(sin etiquetas, UTF-16). Solo estadísticas del modelo: sin cuotas ni nada de apuestas.
"""
import html
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from shared.team_display_names import display_name

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
             "sot": "remates a puerta", "goals": "goles"}


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def names(p: dict) -> tuple[str, str]:
    """Nombres de pantalla en español ("Ath Bilbao" → "Athletic"); solo presentación."""
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
    for key, _ in STATS:
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
    if p.get("group") == "women" or div.startswith("W"):
        return f"👩 {DIV_NAMES.get(div, div)}"
    return f"⚽ {DIV_NAMES.get(div, div)}"


def match_block(p: dict, pills: list[dict] | None = None) -> str:
    """Partido con sus píldoras ⭐ (sin el esperado de cada mercado: está en la web)."""
    ko = madrid(_parse(p["kickoff_utc"]))
    home, away = names(p)
    pills = match_pills(p) if pills is None else pills
    out = [SEP, f"{group_icon_label(p)} · {ko:%H:%M}", f"<b>{esc(home)} – {esc(away)}</b>"]
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
    div = p.get("div") or ""
    return DIV_ORDER.index(div) if div in DIV_ORDER else len(DIV_ORDER) + 2


def build_messages(previews: list[dict], day: date) -> list[str]:
    """Resumen del día por hora de inicio: cada partido con alguna ⭐ con sus píldoras; los
    que no tienen ninguna, juntos al final en «Sin tendencias destacadas». Si no cabe en un
    mensaje se parte entre partidos (o entre nombres de la línea final). El último lleva el
    enlace a la web."""
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
    if tg_len(cur + link) > TG_LIMIT:
        msgs.append(cur)
        cur = cont
    msgs.append(cur + link)
    return msgs


def read_day_previews(lo: datetime, hi: datetime) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    out = []
    for coll in ("match_previews", "match_previews_cups", "match_previews_uefa", "match_previews_women"):
        q = (col(coll).where(filter=FieldFilter("kickoff_utc", ">=", lo.isoformat()))
             .where(filter=FieldFilter("kickoff_utc", "<", hi.isoformat())))
        out.extend(s.to_dict() for s in q.stream())
    return out
