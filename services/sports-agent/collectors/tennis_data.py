"""
Tenis en Tendencias — Fase 1: historial de resultados (juegos por set) desde tennis-data.co.uk.

Fuente: https://www.tennis-data.co.uk/alldata.php (uso personal, no comercial). Los ficheros
cuelgan de una carpeta con nombre aleatorio que puede cambiar, así que SIEMPRE se lee antes
alldata.php y se sacan de ahí los enlaces: ATP `{dir}/{año}/{año}.xlsx`, WTA `{dir}/{año}w/{año}.xlsx`.
Solo circuito principal (sin Challenger ni ITF); van 4-5 días por detrás.

Lector xlsx con la biblioteca estándar (zipfile + xml): sin openpyxl ni pandas.

Escribe (solo con dry_run=False):
  tennis_tournaments/{tour}_{year}_{nº}_{slug}   un doc por torneo con matches[] (sin cuotas)
  tennis_data_state/{tour}_{year}                 hash por torneo + carpeta detectada → solo
                                                  se reescribe lo nuevo o cambiado

Retiradas, walkovers y 'Awarded' se guardan con su comentario y status != completed: la
graduación los dará por ANULADOS; los sets terminados de una retirada sí valen para el modelo.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tennis-data.co.uk/"
ALLDATA_URL = BASE_URL + "alldata.php"
_HTTP_TIMEOUT = 60
_UA = {"User-Agent": "Mozilla/5.0 (uso personal; Tendencias)"}

TOURS = ("atp", "wta")
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

STATUS_BY_COMMENT = {
    "completed": "completed",
    "retired": "retired",
    "walkover": "walkover",
    "awarded": "awarded",
    "disqualified": "awarded",
    "sched": "unknown",
}


# ── Enlaces de alldata.php ──────────────────────────────────────────────────

_LINK_RE = re.compile(r'href="([^"]*?/(\d{4})(w?)/\d{4}\.xlsx)"', re.IGNORECASE)


def parse_alldata_links(html: str) -> dict[tuple[str, int], str]:
    """{(tour, año): url absoluta} solo de los .xlsx (los .xls antiguos no se leen)."""
    out: dict[tuple[str, int], str] = {}
    for href, year, w in _LINK_RE.findall(html or ""):
        tour = "wta" if w else "atp"
        url = href if href.lower().startswith("http") else BASE_URL + href.lstrip("/")
        out.setdefault((tour, int(year)), url)
    return out


def link_folder(url: str) -> str:
    """Carpeta aleatoria del enlace (para avisar si cambia)."""
    path = url.replace(BASE_URL, "")
    return path.split("/", 1)[0] if "/" in path else ""


# ── Lector xlsx (biblioteca estándar) ──────────────────────────────────────

def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.findall("m:si", _NS):
        out.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
    return out


def _first_sheet_path(z: zipfile.ZipFile) -> str:
    names = z.namelist()
    if "xl/worksheets/sheet1.xml" in names:
        return "xl/worksheets/sheet1.xml"
    sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    if not sheets:
        raise ValueError("xlsx sin hojas")
    return sheets[0]


def read_xlsx_rows(content: bytes) -> list[dict]:
    """Primera hoja como lista de dicts {cabecera: valor}. Números como float, texto como str."""
    with zipfile.ZipFile(BytesIO(content)) as z:
        strings = _shared_strings(z)
        root = ET.fromstring(z.read(_first_sheet_path(z)))
    rows: list[list] = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        vals: dict[int, object] = {}
        for c in row.findall("m:c", _NS):
            idx = _col_index(c.get("r", "A"))
            t = c.get("t")
            if t == "inlineStr":
                vals[idx] = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
                continue
            v = c.find("m:v", _NS)
            if v is None or v.text is None:
                continue
            if t == "s":
                vals[idx] = strings[int(v.text)]
            elif t in ("str", "e"):
                vals[idx] = v.text
            elif t == "b":
                vals[idx] = v.text == "1"
            else:
                try:
                    vals[idx] = float(v.text)
                except ValueError:
                    vals[idx] = v.text
        if vals:
            width = max(vals) + 1
            rows.append([vals.get(i) for i in range(width)])
    if not rows:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = {header[i]: r[i] for i in range(min(len(header), len(r))) if header[i]}
        if any(v not in (None, "") for v in d.values()):
            out.append(d)
    return out


# ── Normalización ───────────────────────────────────────────────────────────

def excel_date(v) -> Optional[str]:
    """Número de fecha de Excel (sistema 1900) o texto → 'AAAA-MM-DD'."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        if v < 1:
            return None
        return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def slugify(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "x"


def set_is_complete(w: int, l: int) -> bool:
    """Set terminado con las reglas de hoy: 6-0..6-4, 7-5, 7-6 (tie-break, también el
    super tie-break del 6-6 en el quinto de los Grand Slam, que se anota 7-6). Un set
    largo sin tie-break (p. ej. 8-6) se admite como terminado si la diferencia es 2."""
    hi, lo = max(w, l), min(w, l)
    if hi == 6 and lo <= 4:
        return True
    if hi == 7 and lo in (5, 6):
        return True
    return hi > 7 and hi - lo == 2


def match_status(comment) -> str:
    c = str(comment or "").strip().lower()
    for key, status in STATUS_BY_COMMENT.items():
        if c.startswith(key):
            return status
    return "completed" if not c else "unknown"


def normalize_match(row: dict, tour: str) -> Optional[dict]:
    """Fila de tennis-data → partido normalizado. None si falta lo básico (fecha o jugadores)."""
    d = excel_date(row.get("Date"))
    winner, loser = str(row.get("Winner") or "").strip(), str(row.get("Loser") or "").strip()
    if not d or not winner or not loser:
        return None
    max_sets = 5 if tour == "atp" else 3
    sets: list[list[int]] = []
    for i in range(1, max_sets + 1):
        w, l = _int(row.get(f"W{i}")), _int(row.get(f"L{i}"))
        if not w and not l:   # vacío o 0-0 de relleno
            break
        sets.append([w or 0, l or 0])
    status = match_status(row.get("Comment"))
    complete_sets = [s for s in sets if set_is_complete(*s)]
    # En una retirada el último set suele quedar a medias: solo cuentan los terminados.
    bad_sets = 0 if status != "completed" else len(sets) - len(complete_sets)
    best_of = _int(row.get("Best of")) or 3
    m = {
        "date": d,
        "round": str(row.get("Round") or "").strip(),
        "winner": winner,
        "loser": loser,
        "wrank": _int(row.get("WRank")),
        "lrank": _int(row.get("LRank")),
        "sets": sets,
        "wsets": _int(row.get("Wsets")),
        "lsets": _int(row.get("Lsets")),
        "n_sets": len(sets),
        "total_games": sum(a + b for a, b in sets),
        "tiebreaks": sum(1 for a, b in sets if max(a, b) == 7 and min(a, b) == 6),
        "complete_sets": len(complete_sets),
        "comment": str(row.get("Comment") or "").strip(),
        "status": status,
    }
    if status == "completed":
        need = best_of // 2 + 1
        m["valid"] = bad_sets == 0 and max(m["wsets"] or 0, m["lsets"] or 0) == need
    else:
        m["valid"] = False
    return m


def tournament_key(row: dict, tour: str) -> tuple:
    """Nº del torneo en el año (columna ATP/WTA) + nombre: único dentro del fichero."""
    num = _int(row.get("ATP" if tour == "atp" else "WTA"))
    return (num, str(row.get("Tournament") or "").strip())


def build_tournament_docs(rows: Iterable[dict], tour: str, year: int) -> tuple[dict[str, dict], dict]:
    """Agrupa las filas por torneo → {doc_id: doc}; devuelve también contadores de control."""
    groups: dict[tuple, dict] = {}
    stats = {"rows": 0, "matches": 0, "sin_fecha_o_jugador": 0, "sets_incoherentes": 0,
             "status": {}, "surface": {}}
    for row in rows:
        stats["rows"] += 1
        m = normalize_match(row, tour)
        if m is None:
            stats["sin_fecha_o_jugador"] += 1
            continue
        stats["matches"] += 1
        stats["status"][m["status"]] = stats["status"].get(m["status"], 0) + 1
        if m["status"] == "completed" and not m["valid"]:
            stats["sets_incoherentes"] += 1
        key = tournament_key(row, tour)
        g = groups.get(key)
        if g is None:
            surface = str(row.get("Surface") or "").strip()
            g = groups[key] = {
                "tour": tour, "year": year, "num": key[0], "tournament": key[1],
                "location": str(row.get("Location") or "").strip(),
                "series": str(row.get("Series") or row.get("Tier") or "").strip(),
                "surface": surface,
                "court": str(row.get("Court") or "").strip(),
                "best_of": _int(row.get("Best of")) or 3,
                "matches": [],
            }
        g["matches"].append(m)
        stats["surface"][g["surface"]] = stats["surface"].get(g["surface"], 0) + 1
    docs: dict[str, dict] = {}
    for g in groups.values():
        g["matches"].sort(key=lambda x: (x["date"], x["round"]))
        dates = [x["date"] for x in g["matches"]]
        g["start"], g["end"] = min(dates), max(dates)
        g["n_matches"] = len(g["matches"])
        g["source"] = "tennis-data.co.uk"
        num = f"{g['num']:03d}" if isinstance(g["num"], int) else "000"
        docs[f"{tour}_{year}_{num}_{slugify(g['tournament'])}"] = g
    return docs, stats


def doc_hash(doc: dict) -> str:
    payload = json.dumps({k: v for k, v in doc.items() if k != "updated_at"},
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


# ── Nombres: «Apellido I.» (tennis-data) frente a «Nombre Apellido» (calendario) ──

def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z]+", " ", t).split())


def td_player_key(name: str) -> Optional[str]:
    """'Auger-Aliassime F.' → 'auger aliassime|f'; 'De Minaur A.' → 'de minaur|a'."""
    s = str(name or "").strip()
    m = re.match(r"^(.*?)\s+([A-Za-z])[A-Za-z]*(?:[.\-][A-Za-z.\-]*)?\s*$", s)
    if not m:
        return None
    surname = _norm(m.group(1))
    return f"{surname}|{m.group(2).lower()}" if surname else None


def candidate_keys(full_name: str) -> list[str]:
    """Claves posibles de un nombre completo de calendario ('Carlos Alcaraz', 'Alex de Minaur',
    'Alcaraz, Carlos', 'Alcaraz C.'): cada corte nombre|apellido, de más largo a más corto."""
    s = str(full_name or "").strip()
    if not s:
        return []
    if "," in s:
        last, first = [p.strip() for p in s.split(",", 1)]
        k_last, k_first = _norm(last), _norm(first)
        return [f"{k_last}|{k_first[0]}"] if k_last and k_first else []
    direct = td_player_key(s)
    keys = [direct] if direct and s.endswith(".") else []
    tokens = _norm(s).split()
    for i in range(1, len(tokens)):
        keys.append(f"{' '.join(tokens[i:])}|{tokens[0][0]}")
    return keys


def match_player(full_name: str, index: set[str]) -> Optional[str]:
    for k in candidate_keys(full_name):
        if k in index:
            return k
    return None


# ── Descarga y escritura ────────────────────────────────────────────────────

async def fetch_links(client: httpx.AsyncClient) -> dict[tuple[str, int], str]:
    r = await client.get(ALLDATA_URL, headers=_UA)
    r.raise_for_status()
    return parse_alldata_links(r.text)


async def fetch_xlsx(client: httpx.AsyncClient, url: str) -> list[dict]:
    r = await client.get(url, headers=_UA)
    r.raise_for_status()
    return read_xlsx_rows(r.content)


def _write_changed(state_id: str, docs: dict[str, dict], folder: str, dry_run: bool) -> int:
    from shared.firestore_client import col, get_client

    state_ref = col("tennis_data_state").document(state_id)
    snap = state_ref.get()
    old = (snap.to_dict() or {}).get("hashes", {}) if snap.exists else {}
    new_hashes = {doc_id: doc_hash(doc) for doc_id, doc in docs.items()}
    changed = [d for d, h in new_hashes.items() if old.get(d) != h]
    if dry_run or not changed:
        return len(changed)
    now = datetime.now(timezone.utc).isoformat()
    client = get_client()
    for i in range(0, len(changed), 100):
        batch = client.batch()
        for doc_id in changed[i:i + 100]:
            batch.set(col("tennis_tournaments").document(doc_id), {**docs[doc_id], "updated_at": now})
        batch.commit()
    state_ref.set({"hashes": {**old, **new_hashes}, "folder": folder, "updated_at": now})
    return len(changed)


def default_years(today: Optional[date] = None) -> list[int]:
    y = (today or datetime.now(timezone.utc).date()).year
    return [y - 2, y - 1, y]


async def run_tennis_data(years: Optional[list[int]] = None, tours: Iterable[str] = TOURS,
                          dry_run: bool = True) -> dict:
    """Carga (o en dry_run solo cuenta) los torneos de los años pedidos. 1 + 2·años peticiones."""
    years = years or default_years()
    summary: dict = {"ok": True, "dry_run": dry_run, "years": years, "folder": None,
                     "files": {}, "written": {}, "errors": []}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            links = await fetch_links(client)
        except Exception as e:
            return {**summary, "ok": False, "errors": [f"alldata.php: {e}"]}
        for tour in tours:
            for year in years:
                key = f"{tour}_{year}"
                url = links.get((tour, year))
                if not url:
                    summary["errors"].append(f"{key}: sin enlace en alldata.php")
                    continue
                summary["folder"] = link_folder(url)
                try:
                    rows = await fetch_xlsx(client, url)
                    docs, stats = build_tournament_docs(rows, tour, year)
                    stats["tournaments"] = len(docs)
                    dates = [m["date"] for d in docs.values() for m in d["matches"]]
                    stats["first_date"], stats["last_date"] = (min(dates), max(dates)) if dates else (None, None)
                    summary["files"][key] = stats
                    summary["written"][key] = _write_changed(key, docs, summary["folder"], dry_run)
                except Exception as e:
                    logger.error("tennis_data: error en %s", key, exc_info=True)
                    summary["errors"].append(f"{key}: {e}")
    summary["ok"] = not summary["errors"]
    logger.info("tennis_data: %s", summary)
    return summary
