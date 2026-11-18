"""SOLO LECTURA. ¿Por qué el Poisson nuevo envía señales 📊 moderadas y el anterior ninguna?

Para cada envío de fútbol 1X2 (alerts_sent sports_sent) desde SINCE:
  - nivel guardado en el envío (intensity; antes del 14-nov-2026 no se guardaba y se reconstruye
    sobre el EV con los cortes 0,15 / 0,08 de shared/signal_tier);
  - nivel que le habría puesto el generador (value_bet_engine, regla sin cambios desde agosto):
      🔥 EV > 0,20 y conf > 0,80 y cuota < 5
      ✅ EV > 0,12 y conf > 0,70 (0,65 en CL/EL/ECL/WC/WC26) y cuota < 6
      📊 EV > mínimo de la liga y conf > 0,65 y cuota < 4 (< 6 en copas internacionales)
    con EV, confianza y cuota de la predicción (predictions/{prediction_id}).
Log: solo agregados por versión. Detalle de las 📊 (probabilidad, cuota, edge, EV, confianza,
mínimo de la liga, regla) en DETAIL_FILE, artefacto de 1 día (el repo es público).
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.config import LEAGUE_MIN_EDGE, SPORTS_MIN_EDGE  # noqa: E402
from shared.firestore_client import col, iter_all  # noqa: E402
from shared.signal_tier import emoji_from_ev  # noqa: E402

INTL = {"CL", "EL", "ECL", "WC", "WC26"}


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


def generator_tier(ev: float, conf: float, odds: float, league: str) -> tuple[str, str]:
    intl = league in INTL
    min_edge = LEAGUE_MIN_EDGE.get(league, SPORTS_MIN_EDGE)
    if ev > 0.20 and conf > 0.80 and odds < 5.0:
        return "🔥", "fuerte: EV>0,20 conf>0,80 cuota<5"
    if ev > 0.12 and conf > (0.65 if intl else 0.70) and odds < 6.0:
        return "✅", f"detectada: EV>0,12 conf>{0.65 if intl else 0.70} cuota<6"
    if ev > min_edge and conf > 0.65 and odds < (6.0 if intl else 4.0):
        return "📊", f"moderada: EV>{min_edge} (mínimo {league or 'global'}) conf>0,65 cuota<{6 if intl else 4}"
    return "—", "no cumple ningún nivel con los datos guardados"


def main() -> int:
    since = datetime.fromisoformat(os.environ.get("SINCE") or "2026-10-25").replace(tzinfo=timezone.utc)
    agg: Counter = Counter()
    rows = []
    for doc in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        a = doc.to_dict() or {}
        t = _dt(a.get("sent_at"))
        if t is None or t < since or str(a.get("sport") or "football").lower() not in ("football", "soccer"):
            continue
        if str(a.get("market_type") or "h2h") != "h2h":
            continue
        fit = a.get("poisson_fit") or "nelder-mead-2000"
        p = None
        for pid in (a.get("prediction_id"), a.get("match_id")):
            if pid:
                s = col("predictions").document(str(pid)).get()
                if s.exists:
                    p = s.to_dict() or {}
                    break
        stored = a.get("intensity") if a.get("intensity") in ("🔥", "✅", "📊") else None
        stored_src = "guardado" if stored else "reconstruido_EV"
        stored = stored or emoji_from_ev(a.get("ev"))
        if p is None:
            agg[(fit, stored_src, stored, "sin predicción")] += 1
            continue
        ev = float(p.get("ev") or a.get("ev") or 0)
        conf = float(p.get("confidence") or 0)
        odds = float(p.get("odds") or a.get("odds") or 0)
        gen, rule = generator_tier(ev, conf, odds, str(p.get("league") or a.get("league") or ""))
        agg[(fit, stored_src, stored, gen)] += 1
        if gen == "📊" or stored == "📊":
            rows.append({"versión": fit, "enviada": f"{t:%Y-%m-%d %H:%M}", "liga": a.get("league"),
                         "partido": f"{a.get('home_team')} vs {a.get('away_team')}",
                         "selección": a.get("team_to_back") or a.get("selection"),
                         "prob_modelo": p.get("calculated_prob"), "cuota": odds, "edge": p.get("edge"),
                         "ev": ev, "confianza": conf, "data_quality": p.get("data_quality"),
                         "mínimo_liga": LEAGUE_MIN_EDGE.get(str(p.get("league") or ""), SPORTS_MIN_EDGE),
                         "nivel_guardado": stored, "origen_nivel": stored_src, "nivel_generador": gen,
                         "regla": rule})
    print(f"Envíos 1X2 de fútbol desde {since:%Y-%m-%d}, por versión · origen del nivel · nivel guardado · "
          f"nivel del generador:")
    for k, v in sorted(agg.items()):
        print(f"  {k[0]:18} {k[1]:16} guardado {k[2]}  generador {k[3]}  ×{v}")
    with open(os.environ.get("DETAIL_FILE", "detalle.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"Detalle de {len(rows)} envíos 📊 (guardados o según el generador) en el artefacto, no en el log.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
