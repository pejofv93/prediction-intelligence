"""
Resumen diario de Tendencias por Telegram (08:00 UTC, hilo de Tendencias; decisión del usuario,
19-nov-2026). Fichas de partido del día (hora de Madrid): masculino (match_previews, mp-v1) y
femenino (match_previews_women, mp-w1; el grupo es privado).

Por partido y mercado: la línea principal de cada equipo (la de P(más) más cercana al 50 %, que es
donde el modelo mejora a la liga) y el total del partido como referencia. Solo estadísticas del
modelo: sin cuotas ni nada de apuestas. Texto plano (sin Markdown). Un mensaje por liga; si una
liga no cabe en un mensaje, se parte.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

WEB_URL = "https://tendencias-pi.pages.dev"
MAX_LEN = 3800                          # Telegram admite 4096
STATS = (("corners", "Córners"), ("cards", "Tarjetas"), ("fouls", "Faltas"),
         ("shots", "Remates"), ("sot", "A puerta"), ("goals", "Goles"))
DIV_NAMES = {"SP1": "LaLiga", "SP2": "Segunda", "I1": "Serie A", "E0": "Premier", "F1": "Ligue 1",
             "P1": "Primeira", "D1": "Bundesliga", "N1": "Eredivisie",
             "WLF": "Liga F", "WSL": "WSL", "WFBL": "Frauen-Bundesliga", "WSA": "Serie A fem.", "WD1": "D1 Arkema"}
DIV_ORDER = ("SP1", "SP2", "E0", "I1", "D1", "F1", "P1", "N1", "WLF", "WSL", "WFBL", "WSA", "WD1")
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
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


def match_block(p: dict) -> str:
    ko = madrid(_parse(p["kickoff_utc"]))
    out = [f"{ko:%H:%M} {p['home_team']} – {p['away_team']}"
           + ("  (muestra insuficiente)" if p.get("low_sample") else "")]
    for key, name in STATS:
        b = (p.get("stats") or {}).get(key) or {}
        if not b.get("available"):
            continue
        parts = []
        for side, who in (("home", "local"), ("away", "visit.")):
            ml = main_line((b.get("lines") or {}).get(side) or {})
            if ml:
                parts.append(f"{who} +{_line(ml['line'])}: {_pct(ml['p_model'])}")
        tot = main_line((b.get("lines") or {}).get("total") or {})
        txt = f"  {name}: " + " · ".join(parts)
        if tot:
            txt += f" (total +{_line(tot['line'])}: {_pct(tot['p_model'])})"
        out.append(txt)
    return "\n".join(out)


def build_messages(previews: list[dict], day: date) -> list[str]:
    """Un mensaje por liga (partido si no cabe). El último lleva el enlace a la web."""
    if not previews:
        return []
    by_div: dict[str, list[dict]] = defaultdict(list)
    for p in previews:
        by_div[p.get("div") or "?"].append(p)
    order = [d for d in DIV_ORDER if d in by_div] + sorted(d for d in by_div if d not in DIV_ORDER)
    dlabel = f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"
    msgs: list[str] = []
    for div in order:
        ms = sorted(by_div[div], key=lambda p: (p["kickoff_utc"], p["home_team"]))
        women = div.startswith("W")
        head = f"📊 Tendencias · {dlabel} · {'Femenino · ' if women else ''}{DIV_NAMES.get(div, div)}"
        body, cur = [], head + "\n% de que haya MÁS de la línea (modelo; solo estadísticas)"
        for p in ms:
            blk = match_block(p)
            if len(cur) + len(blk) + 2 > MAX_LEN:
                body.append(cur)
                cur = head + " (sigue)"
            cur += "\n\n" + blk
        body.append(cur)
        msgs.extend(body)
    msgs[-1] += f"\n\nFichas completas: {WEB_URL}/#partidos/{day.isoformat()}"
    return msgs


def read_day_previews(lo: datetime, hi: datetime) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    from shared.firestore_client import col
    out = []
    for coll in ("match_previews", "match_previews_women"):
        q = (col(coll).where(filter=FieldFilter("kickoff_utc", ">=", lo.isoformat()))
             .where(filter=FieldFilter("kickoff_utc", "<", hi.isoformat())))
        out.extend(s.to_dict() for s in q.stream())
    return out
