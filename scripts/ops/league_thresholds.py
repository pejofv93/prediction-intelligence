"""SOLO LECTURA. ¿Qué mínimo de EV por liga aplica de verdad el generador de fútbol?

value_bet_engine.generate_signal usa _load_backtest_thresholds(): parte de LEAGUE_MIN_EDGE
(shared/config.py) y lo PISA con model_weights/backtest_thresholds[<nombre liga>]["h2h"]
de Firestore (lo escribe backtest_engine.run_backtest). Este script imprime:
  1. el documento model_weights/backtest_thresholds (solo números por liga y mercado);
  2. el mínimo resultante por código de liga frente al de la config;
  3. el último backtest_results h2h por liga (ROI, apuestas, umbral recomendado, fecha);
  4. el min_ev que el generador guardó en filter_blocks BELOW_THRESHOLD desde SINCE
     (el umbral aplicado en producción, por liga);
  5. envíos 1X2 de fútbol (alerts_sent sports_sent) desde SINCE con EV por debajo del mínimo
     de la config: liga, EV, mínimo config, mínimo efectivo y versión del Poisson.
Nada de equipos ni cuotas: el log del workflow es público.
"""
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.config import LEAGUE_MIN_EDGE, SPORTS_MIN_EDGE  # noqa: E402

# Copia de value_bet_engine._LEAGUE_NAME_TO_CODE (el test comprueba que siguen iguales).
LEAGUE_NAME_TO_CODE = {
    "Premier League": "PL",
    "La Liga": "PD",
    "Bundesliga": "BL1",
    "Serie A": "SA",
    "Ligue 1": "FL1",
}


def effective_thresholds(doc: dict | None) -> dict[str, float]:
    """Misma fusión que value_bet_engine.merge_backtest_thresholds: max(config, Firestore h2h)."""
    merged = dict(LEAGUE_MIN_EDGE)
    for league_name, markets in (doc or {}).items():
        code = LEAGUE_NAME_TO_CODE.get(league_name)
        if not code or not isinstance(markets, dict) or markets.get("h2h") is None:
            continue
        try:
            thr = float(markets["h2h"])
        except (TypeError, ValueError):
            continue
        merged[code] = max(thr, float(LEAGUE_MIN_EDGE.get(code, SPORTS_MIN_EDGE)))
    return merged


def below_config(ev: float, league: str, effective: dict[str, float]) -> dict | None:
    """Envío con EV ≤ mínimo de la config: devuelve el detalle agregado, si no None."""
    cfg = LEAGUE_MIN_EDGE.get(league, SPORTS_MIN_EDGE)
    if ev > cfg:
        return None
    return {"league": league or "?", "ev": round(ev, 4), "min_config": cfg,
            "min_efectivo": effective.get(league, SPORTS_MIN_EDGE)}


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


def main() -> int:
    from shared.firestore_client import col, iter_all

    since = datetime.fromisoformat(os.environ.get("SINCE") or "2026-11-14").replace(tzinfo=timezone.utc)

    snap = col("model_weights").document("backtest_thresholds").get()
    doc = snap.to_dict() if snap.exists else None
    print("== 1. model_weights/backtest_thresholds ==")
    if not doc:
        print("  (no existe: se usa LEAGUE_MIN_EDGE de la config)")
    else:
        for name in sorted(doc):
            m = doc[name]
            nums = {k: v for k, v in m.items() if isinstance(v, (int, float))} if isinstance(m, dict) else m
            print(f"  {name}: {nums}")
    upd = getattr(snap, "update_time", None)
    if upd:
        print(f"  última escritura del documento: {upd}")

    eff = effective_thresholds(doc)
    print("\n== 2. Mínimo de EV por liga: config → efectivo ==")
    for code in sorted(set(LEAGUE_MIN_EDGE) | set(eff)):
        c, e = LEAGUE_MIN_EDGE.get(code), eff.get(code)
        print(f"  {code:4} config={c}  efectivo={e}" + ("   <-- DISTINTO" if c != e else ""))
    print(f"  resto de ligas: SPORTS_MIN_EDGE={SPORTS_MIN_EDGE}")

    print("\n== 3. Último backtest_results h2h por liga ==")
    last: dict[str, dict] = {}
    for s in iter_all("backtest_results"):
        r = s.to_dict() or {}
        if r.get("market") != "h2h":
            continue
        t = _dt(r.get("created_at"))
        lg = str(r.get("league") or "?")
        if t and (lg not in last or t > last[lg]["_t"]):
            last[lg] = {**r, "_t": t}
    for lg in sorted(last):
        r = last[lg]
        print(f"  {lg}: {r['_t']:%Y-%m-%d %H:%M} roi={r.get('roi')} n_bets={r.get('n_bets')} "
              f"temporadas={r.get('season')} umbral_recomendado={r.get('threshold_recommended')}")
    if not last:
        print("  (sin resultados h2h)")

    print(f"\n== 4. min_ev aplicado en filter_blocks BELOW_THRESHOLD desde {since:%Y-%m-%d} ==")
    applied: dict[str, Counter] = defaultdict(Counter)
    for s in col("filter_blocks").where("blocked_at", ">=", since).stream():
        b = s.to_dict() or {}
        if b.get("filter_name") != "BELOW_THRESHOLD" or b.get("min_ev") is None:
            continue
        applied[str(b.get("league") or "?")][round(float(b["min_ev"]), 4)] += 1
    for lg in sorted(applied):
        print(f"  {lg}: " + ", ".join(f"{v} ×{n}" for v, n in sorted(applied[lg].items())))
    if not applied:
        print("  (sin bloqueos en la ventana)")

    print(f"\n== 5. Envíos 1X2 fútbol desde {since:%Y-%m-%d} con EV ≤ mínimo de la config ==")
    n_total = 0
    for s in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        a = s.to_dict() or {}
        t = _dt(a.get("sent_at"))
        if t is None or t < since or str(a.get("sport") or "football").lower() not in ("football", "soccer"):
            continue
        if str(a.get("market_type") or "h2h") != "h2h" or a.get("ev") is None:
            continue
        n_total += 1
        row = below_config(float(a["ev"]), str(a.get("league") or ""), eff)
        if row:
            print(f"  {t:%Y-%m-%d %H:%M} {row['league']} ev={row['ev']} min_config={row['min_config']} "
                  f"min_efectivo={row['min_efectivo']} nivel={a.get('intensity') or '-'} "
                  f"poisson={a.get('poisson_fit') or 'nelder-mead-2000'}")
    print(f"  envíos 1X2 fútbol en la ventana: {n_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
