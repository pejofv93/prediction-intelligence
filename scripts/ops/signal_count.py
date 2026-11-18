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
Al final, desglose por versión, liga, mercado y nivel (fuerte/detectada/moderada, shared/signal_tier),
con los días que cubre cada versión en la ventana para comparar por día. No escribe nada.
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from shared.firestore_client import col, iter_all  # noqa: E402
from shared.signal_tier import tier_of  # noqa: E402

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
    seg_sent: Counter = Counter()
    seg_written: Counter = Counter()
    first_new = None

    def seg(d: dict) -> tuple:
        ev = d.get("ev") if d.get("ev") is not None else d.get("edge")
        return (d.get("poisson_fit") or "nelder-mead-2000", str(d.get("league") or "?"),
                str(d.get("market_type") or "h2h"), tier_of(d.get("intensity"), ev))

    for doc in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        d = doc.to_dict() or {}
        t = _dt(d.get("sent_at"))
        if t is None or t < since or not is_football(d):
            continue
        sent[(slot_of(t), d.get("poisson_fit") or "nelder-mead-2000")] += 1
        seg_sent[seg(d)] += 1
        if d.get("poisson_fit") and (first_new is None or t < first_new):
            first_new = t

    written: Counter = Counter()
    for doc in col("predictions").where(filter=FieldFilter("created_at", ">=", since)).stream():
        d = doc.to_dict() or {}
        t = _dt(d.get("created_at"))
        if t is None or not is_football(d):
            continue
        written[(slot_of(t), d.get("poisson_fit") or "nelder-mead-2000")] += 1
        seg_written[seg(d)] += 1
        if d.get("poisson_fit") and (first_new is None or t < first_new):
            first_new = t

    slots = sorted({k[0] for k in sent} | {k[0] for k in written})
    print(f"Ventana: últimos {days} días · franjas con algún envío o predicción: {len(slots)}")
    print("franja (UTC)       enviadas (NM / nuevo)   escritas (NM / nuevo)")
    for s in slots:
        print(f"{s:%d-%m %H:%M}        "
              f"{sent[(s, 'nelder-mead-2000')]:4d} / {sent[(s, 'lbfgs-l2-v1')]:<4d}          "
              f"{written[(s, 'nelder-mead-2000')]:4d} / {written[(s, 'lbfgs-l2-v1')]:<4d}")
    total_sent = sum(sent.values())
    print(f"Total enviadas: {total_sent} en {len(slots)} franjas")

    now = datetime.now(timezone.utc)
    split = first_new or now
    print()
    print(f"Días en la ventana: Nelder-Mead {(split - since).total_seconds() / 86400:.1f} · "
          f"nuevo (desde la primera señal lbfgs, {split:%d-%m %H:%M}) {(now - split).total_seconds() / 86400:.1f}")
    print("Por versión, liga, mercado y nivel (enviadas · escritas; escritas antiguas salen por debajo):")
    print(f"  {'versión':18} {'liga':6} {'mercado':14} {'nivel':10} {'env.':>4} {'escr.':>5}")
    for k in sorted(set(seg_sent) | set(seg_written)):
        print(f"  {k[0]:18} {k[1]:6} {k[2]:14} {k[3]:10} {seg_sent[k]:4d} {seg_written[k]:5d}")
    for fit in ("nelder-mead-2000", "lbfgs-l2-v1"):
        print(f"  TOTAL {fit:12} enviadas {sum(v for k, v in seg_sent.items() if k[0] == fit):3d} · "
              f"escritas {sum(v for k, v in seg_written.items() if k[0] == fit):3d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
