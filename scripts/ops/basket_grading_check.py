"""SOLO LECTURA (22-nov-2026). ¿Se gradúan las predicciones de baloncesto por su vía propia?

predictions de baloncesto (sport basketball/nba o liga NBA/ACB/EUROLEAGUE): graduadas y
pendientes por liga y mercado, y fecha de partido de la última graduada; alerts_sent de
baloncesto graduadas. Solo recuentos y fechas: el log es público (sin equipos ni cuotas).
"""
import os
import sys
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from shared.firestore_client import col  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

NOW = datetime.now(timezone.utc)


def _d(v) -> str:
    if v is None:
        return ""
    return v.isoformat()[:10] if hasattr(v, "isoformat") else str(v)[:10]


def _ts(v) -> str:
    if v is None:
        return ""
    return v.isoformat()[:16] if hasattr(v, "isoformat") else str(v)[:16]


def main() -> int:
    seen: dict[str, dict] = {}
    for field, vals in (("sport", ("basketball", "nba")), ("league", ("NBA", "ACB", "EUROLEAGUE"))):
        for v in vals:
            for s in col("predictions").where(filter=FieldFilter(field, "==", v)).stream():
                seen[s.id] = s.to_dict() or {}
    rows = list(seen.values())
    graded = [r for r in rows if r.get("result") not in (None, "expired")]
    expired = [r for r in rows if r.get("result") == "expired"]
    pending = [r for r in rows if r.get("result") is None]
    print(f"== predictions de baloncesto: {len(rows)} · graduadas {len(graded)} · "
          f"caducadas {len(expired)} · pendientes {len(pending)}")
    print(f"  graduadas por liga y mercado: "
          f"{dict(sorted(Counter((str(r.get('league')), str(r.get('market_type') or 'h2h')) for r in graded).items()))}")
    print(f"  pendientes por liga: {dict(sorted(Counter(str(r.get('league')) for r in pending).items()))}")
    ids_numericos = sum(1 for r in pending if str(r.get("match_id") or "").split("_", 1)[0].isdigit())
    print(f"  pendientes con id numérico (irían a football-data): {ids_numericos}")
    last = max((_d(r.get("match_date")) for r in graded), default="")
    print(f"  fecha de partido de la última graduada: {last}")
    by_day = Counter(_d(r.get("match_date")) for r in graded)
    print("  graduadas por fecha de partido (últimos 10 días con datos): "
          + ", ".join(f"{k[5:]}={by_day[k]}" for k in sorted(by_day)[-10:]))
    old_pending = Counter(_d(r.get("match_date")) for r in pending if _d(r.get("match_date")) < NOW.date().isoformat())
    print(f"  pendientes de partidos ya jugados, por día: {dict(sorted(old_pending.items())[-10:])}")
    stamp_fields = ("resolved_at", "graded_at", "checked_at", "updated_at")
    for f in stamp_fields:
        v = max((_ts(r.get(f)) for r in graded if r.get(f) is not None), default="")
        if v:
            print(f"  máximo {f} entre las graduadas: {v}")

    sent = [s.to_dict() or {} for s in
            col("alerts_sent").where(filter=FieldFilter("type", "==", "sports_sent")).stream()]
    sb = [a for a in sent if str(a.get("sport") or "").lower() in ("basketball", "nba")]
    sg = [a for a in sb if a.get("result") is not None]
    print(f"\n== alerts_sent de baloncesto: {len(sb)} · graduadas {len(sg)} · "
          f"último graded_at {max((_ts(a.get('graded_at')) for a in sg), default='')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
