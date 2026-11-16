"""SOLO LECTURA. ¿Cuántas señales de fútbol salen por pasada de análisis?

Pasadas programadas de sports-analyze: 01, 07, 13 y 19 UTC (GitHub a veces las retrasa
o se salta alguna). Cada envío se asigna a la franja de 6 h que empieza en la última hora
programada anterior a su envío.

  - Enviadas: alerts_sent type=sports_sent, fútbol, por sent_at (primer envío; un reenvío
    no cuenta como señal nueva). Separadas por poisson_fit (sin campo = Nelder-Mead).
  - Escritas: predictions de fútbol por created_at. OJO: el análisis reescribe el doc del
    partido en cada pasada, así que las franjas antiguas salen por debajo; sirve solo para
    la última franja.

Ventana: últimos DAYS días (por defecto 8). Solo agregados: franja, número y versión.
No escribe nada.
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from shared.firestore_client import col, iter_all  # noqa: E402

SLOTS = (1, 7, 13, 19)


def _dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def slot_of(t: datetime) -> datetime:
    """Inicio de la franja de 6 h (01/07/13/19 UTC) que contiene t."""
    h = max((s for s in SLOTS if s <= t.hour), default=None)
    if h is None:                      # 00:xx → franja de las 19 del día anterior
        t = t - timedelta(days=1)
        h = 19
    return t.replace(hour=h, minute=0, second=0, microsecond=0)


def is_football(d: dict) -> bool:
    return str(d.get("sport") or "football").lower() in ("football", "soccer")


def main() -> int:
    days = int(os.environ.get("DAYS") or 8)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    sent: Counter = Counter()
    for doc in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        d = doc.to_dict() or {}
        t = _dt(d.get("sent_at"))
        if t is None or t < since or not is_football(d):
            continue
        sent[(slot_of(t), d.get("poisson_fit") or "nelder-mead-2000")] += 1

    written: Counter = Counter()
    for doc in col("predictions").where(filter=FieldFilter("created_at", ">=", since)).stream():
        d = doc.to_dict() or {}
        t = _dt(d.get("created_at"))
        if t is None or not is_football(d):
            continue
        written[(slot_of(t), d.get("poisson_fit") or "nelder-mead-2000")] += 1

    slots = sorted({k[0] for k in sent} | {k[0] for k in written})
    print(f"Ventana: últimos {days} días · franjas con algún envío o predicción: {len(slots)}")
    print("franja (UTC)       enviadas (NM / nuevo)   escritas (NM / nuevo)")
    for s in slots:
        print(f"{s:%d-%m %H:%M}        "
              f"{sent[(s, 'nelder-mead-2000')]:4d} / {sent[(s, 'lbfgs-l2-v1')]:<4d}          "
              f"{written[(s, 'nelder-mead-2000')]:4d} / {written[(s, 'lbfgs-l2-v1')]:<4d}")
    total_sent = sum(sent.values())
    print(f"Total enviadas: {total_sent} en {len(slots)} franjas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
