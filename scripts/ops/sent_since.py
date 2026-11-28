"""SOLO LECTURA. Envíos de Sports registrados desde una fecha (alerts_sent, type=sports_sent): los
que suma la sección «Enviadas por nivel» del informe diario (shared/model_health.sent_tier_stats).

Por envío: fecha y hora del primer envío (UTC), fecha del partido, deporte, liga, mercado, nivel
(el del informe), si es 1X2 de fútbol (shared/config.is_football_1x2) y si salió después de activar
la sombra del 1X2 (deploy de 6436f3b, 23-nov-2026 09:46 UTC). Además, los reenvíos de envíos
anteriores (no suman en el informe) y cuántos 1X2 fueron a la sombra en el mismo periodo.
Nunca imprime equipos, selecciones ni ids. No escribe nada.
"""
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.config import is_football_1x2  # noqa: E402
from shared.firestore_client import iter_all  # noqa: E402
from shared.signal_tier import tier_of  # noqa: E402

SHADOW_ON = datetime(2026, 11, 23, 9, 46, tzinfo=timezone.utc)
FIELDS = ("sport", "league", "market_type", "intensity", "ev", "match_date", "prediction_id", "data_source")


def _as_utc_dt(v):
    """Timestamp de Firestore, datetime o texto ISO → datetime UTC (None si no se puede)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _fmt(d) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else "?"


def main() -> None:
    since = datetime.fromisoformat(os.environ.get("SINCE") or "2026-11-21").replace(tzinfo=timezone.utc)
    rows, resends = [], Counter()
    for doc in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        a = doc.to_dict() or {}
        sent = _as_utc_dt(a.get("sent_at"))
        last = _as_utc_dt(a.get("last_sent_at"))
        sport = str(a.get("sport") or "football").lower()
        if sent and sent >= since:
            in_report = sport not in ("tennis", "basketball", "nba")
            rows.append({"sent": sent, "match": str(a.get("match_date") or "")[:10], "sport": sport,
                         "league": a.get("league") or "?", "market": a.get("market_type") or "h2h",
                         "tier": tier_of(a.get("intensity"), a.get("ev")) if in_report else "-",
                         "in_report": in_report, "x12": is_football_1x2({k: a.get(k) for k in FIELDS}),
                         "intensity": "sí" if a.get("intensity") else "no", "resends": int(a.get("resends") or 0),
                         "correct": a.get("correct")})
        elif last and last >= since and int(a.get("resends") or 0):
            resends["1X2 fútbol" if is_football_1x2({k: a.get(k) for k in FIELDS}) else sport] += 1
    shadow = Counter()
    for doc in iter_all("alerts_sent", where=("type", "==", "sports_shadow")):
        a = doc.to_dict() or {}
        sent = _as_utc_dt(a.get("sent_at"))
        if sent and sent >= since:
            shadow["1X2 fútbol" if is_football_1x2({k: a.get(k) for k in FIELDS})
                   else str(a.get("sport") or "?")] += 1

    rows.sort(key=lambda r: r["sent"])
    print(f"Envíos nuevos (sports_sent) desde {since.date()}: {len(rows)}; en el informe por nivel "
          f"(sin tenis ni baloncesto): {sum(r['in_report'] for r in rows)}")
    print("primer envío UTC | partido | deporte | liga | mercado | nivel | 1X2 fútbol | tras la sombra | "
          "nivel guardado | reenvíos | resultado")
    for r in rows:
        after = r["sent"] >= SHADOW_ON
        res = {True: "acierto", False: "fallo"}.get(r["correct"], "pendiente")
        print(f"{_fmt(r['sent'])} | {r['match']} | {r['sport']} | {r['league']} | {r['market']} | {r['tier']} | "
              f"{'SÍ' if r['x12'] else 'no'} | {'sí' if after else 'no'} | {r['intensity']} | {r['resends']} | {res}")
    bad = [r for r in rows if r["x12"] and r["sent"] >= SHADOW_ON]
    print(f"\n1X2 de fútbol enviados DESPUÉS de activar la sombra ({_fmt(SHADOW_ON)} UTC): {len(bad)}")
    print("Por nivel (solo los del informe):", dict(Counter(r["tier"] for r in rows if r["in_report"])))
    print("Por deporte y mercado:", dict(Counter(f"{r['sport']}/{r['market']}" for r in rows)))
    print("Reenvíos desde esa fecha de envíos anteriores (no suman en el informe):", dict(resends) or "ninguno")
    print("En sombra (sports_shadow) desde esa fecha:", dict(shadow) or "ninguno")


if __name__ == "__main__":
    main()
