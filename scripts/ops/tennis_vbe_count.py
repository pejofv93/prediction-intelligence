"""SOLO LECTURA. ¿Cuántas señales de tenis del motor de valor salieron solo de las cuotas?

El colector de tenis (RapidAPI) no trae estadísticas desde mayo de 2026: tennis_analyzer va
por el camino «odds-only» (probabilidad sin margen de la cuota media frente a la mejor
cuota; docs de predictions con data_source=odds_only e id terminado en _oo).

  1) predictions de tenis desde SINCE por mes de created_at y data_source; cuántas pasaban
     el umbral de envío (edge > SPORTS_ALERT_EDGE). El análisis reescribe el doc del partido,
     así que cuenta partidos-lado, no pasadas.
  2) Enviadas desde el 22-oct-2026 (alerts_sent type=sports_sent, sport=tennis): por mes,
     odds-only (prediction_id _oo) o con estadísticas, reenvíos y graduadas (aciertos/n).
  3) Antes del 22-oct el bot solo guardaba el slot de dedup 24 h (alerts_sent type=sports,
     alert_key «local_vs_visitante_mercado_selección», un doc por clave, sent_at = último
     envío): se cuentan los slots cuya clave casa con un partido-lado de 1).

Solo agregados: ningún nombre de jugador ni partido. No escribe nada.
"""
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.config import SPORTS_ALERT_EDGE  # noqa: E402
from shared.firestore_client import iter_all  # noqa: E402

REGISTRY_FROM = datetime(2026, 10, 22, tzinfo=timezone.utc)


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


def _key(home, away, market, sel) -> str:
    """Misma clave que alert_manager._alert_key (deportes con equipos)."""
    return f"{str(home).lower().strip()}_vs_{str(away).lower().strip()}_{market}_{sel}"


def main() -> int:
    since = datetime.fromisoformat((os.environ.get("SINCE") or "2026-05-01")).replace(tzinfo=timezone.utc)
    print(f"Desde {since:%Y-%m-%d} · umbral de envío edge > {SPORTS_ALERT_EDGE}")

    # 1) predictions de tenis
    by_src: Counter = Counter()
    over: Counter = Counter()
    keys_oo: set[str] = set()
    keys_stats: set[str] = set()
    for snap in iter_all("predictions", where=("sport", "==", "tennis")):
        d = snap.to_dict() or {}
        t = _dt(d.get("created_at"))
        if t is None or t < since:
            continue
        src = "odds_only" if (d.get("data_source") == "odds_only" or snap.id.endswith("_oo")) else "estadísticas"
        m = f"{t:%Y-%m}"
        by_src[(m, src)] += 1
        if float(d.get("edge") or 0) > SPORTS_ALERT_EDGE:
            over[(m, src)] += 1
            k = _key(d.get("home_team", ""), d.get("away_team", ""),
                     d.get("market_type") or "h2h", d.get("team_to_back") or d.get("selection") or "")
            (keys_oo if src == "odds_only" else keys_stats).add(k)
    print("\n1) predictions de tenis (partido-lado; mes de created_at):")
    print("   mes      origen         docs  pasaban_umbral")
    for (m, src) in sorted(by_src):
        print(f"   {m}  {src:<13} {by_src[(m, src)]:5d}  {over[(m, src)]:5d}")
    print(f"   total: {sum(by_src.values())} docs · {sum(over.values())} por encima del umbral "
          f"(odds-only {sum(v for (m, s), v in over.items() if s == 'odds_only')})")

    # 2) registro de lo enviado (desde el 22-oct)
    sent: Counter = Counter()
    resends: Counter = Counter()
    graded: Counter = Counter()
    hits: Counter = Counter()
    for snap in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        d = snap.to_dict() or {}
        if str(d.get("sport") or "").lower() != "tennis":
            continue
        t = _dt(d.get("sent_at"))
        if t is None or t < since:
            continue
        src = "odds_only" if str(d.get("prediction_id") or "").endswith("_oo") else "estadísticas"
        m = f"{t:%Y-%m}"
        sent[(m, src)] += 1
        resends[(m, src)] += int(d.get("resends") or 0)
        if d.get("correct") is not None:
            graded[(m, src)] += 1
            hits[(m, src)] += 1 if d.get("correct") else 0
    print(f"\n2) Enviadas registradas (alerts_sent sports_sent, desde {REGISTRY_FROM:%d-%b}):")
    if not sent:
        print("   ninguna")
    for (m, src) in sorted(sent):
        print(f"   {m}  {src:<13} enviadas {sent[(m, src)]:4d} · reenvíos {resends[(m, src)]:3d} · "
              f"graduadas {graded[(m, src)]:3d} · aciertos {hits[(m, src)]:3d}")

    # 3) antes del 22-oct: slots de dedup 24 h que casan con partidos-lado de tenis
    legacy: Counter = Counter()
    for snap in iter_all("alerts_sent", where=("type", "==", "sports")):
        d = snap.to_dict() or {}
        k = d.get("alert_key") or ""
        t = _dt(d.get("sent_at"))
        if t is None or t < since or t >= REGISTRY_FROM:
            continue
        if k in keys_oo:
            legacy[(f"{t:%Y-%m}", "odds_only")] += 1
        elif k in keys_stats:
            legacy[(f"{t:%Y-%m}", "estadísticas")] += 1
    print("\n3) Antes del 22-oct: slots de dedup de tenis (≥1 envío por clave; mes del último envío):")
    if not legacy:
        print("   ninguno")
    for (m, src) in sorted(legacy):
        print(f"   {m}  {src:<13} {legacy[(m, src)]:4d}")
    print("   OJO: un slot = un partido-lado enviado al menos una vez; antes del 22-oct se "
          "reenviaba a diario, así que los mensajes fueron más. Los partidos cuyo doc de "
          "predictions se reescribió sin pasar el umbral no casan (cota inferior).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
