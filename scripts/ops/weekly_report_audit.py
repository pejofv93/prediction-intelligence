"""SOLO LECTURA. Cuadra las cifras del informe semanal de una semana (por defecto la última
completa, lunes a lunes UTC) con sus fuentes. No escribe nada en Firestore.

  a) Sports: "Resueltas" (accuracy_log de la semana) frente a "cerradas" (shadow_trades
     sports con closed_at en la semana): desglose por deporte, mercado, si se envió y
     cuántas veces se cerró el mismo signal_id.
  b) Bankroll: P&L de shadow_trades por source (todas, no solo sports/polymarket) y
     50u + suma = saldo.
  c) Polymarket: "emitido real" (alerts_sent sent/sent_archived graduadas) frente al ledger
     crudo (shadow_trades polymarket cerradas): mercados distintos y solapes.
  e) BUY_NO graduadas en la semana: cuántas por status (sent/sent_archived/shadow).

En el log solo agregados. El detalle de las BUY_NO (mercado, precio, edge, resultado) va a
DETAIL_FILE, que el workflow sube como artefacto de 1 día (el repo es público).
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.firestore_client import col, iter_all  # noqa: E402
from shared.shadow_engine import _poly_alert_selection, _poly_decimal_odds  # noqa: E402

SENT = ("sent", "sent_archived")


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


def _window():
    end = os.environ.get("WEEK_END")  # AAAA-MM-DD (lunes)
    if end:
        week_end = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        week_end = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = week_end - timedelta(weeks=1)
    iso = (week_end - timedelta(days=1)).isocalendar()
    return start, week_end, f"{iso[0]}-W{iso[1]:02d}"


def _sport(t):
    sd = t.get("signal_data") or {}
    s = str(sd.get("sport") or "").lower()
    lg = str(sd.get("league") or t.get("category") or "").upper()
    if s in ("basketball", "nba") or lg in ("NBA", "ACB", "EUROLEAGUE"):
        return "baloncesto"
    if s == "tennis" or lg.startswith(("ATP", "WTA")):
        return "tenis"
    return "fútbol"


def main() -> int:
    start, end, week = _window()
    print(f"Semana {week}: {start:%d-%m %H:%M} → {end:%d-%m %H:%M} UTC")

    # ── a) Sports ──────────────────────────────────────────────────────────────────
    log = col("accuracy_log").document(week).get()
    log = log.to_dict() if log.exists else {}
    print(f"\n[a] accuracy_log {week}: predictions_total={log.get('predictions_total')} "
          f"correct={log.get('predictions_correct')}  (fútbol graduado por el aprendizaje "
          f"nocturno en esa semana, sin baloncesto)")

    trades = [d.to_dict() or {} for d in iter_all("shadow_trades")]
    sp = [t for t in trades if t.get("source") == "sports" and t.get("result") in ("win", "loss")
          and (c := _dt(t.get("closed_at"))) and start <= c < end]
    sent_ids = {str(a.get("match_id") or "") for a in
                (d.to_dict() or {} for d in iter_all("alerts_sent", where=("type", "==", "sports_sent")))}
    ids = Counter(str(t.get("signal_id")) for t in sp)
    print(f"[a] shadow_trades sports cerradas en la semana: {len(sp)}  "
          f"(signal_id distintos {len(ids)}, cerrados >1 vez {sum(1 for v in ids.values() if v > 1)})")
    print("    por deporte:", dict(Counter(_sport(t) for t in sp)))
    print("    por mercado:", dict(Counter(str(t.get('market') or '?') for t in sp)))
    print("    enviadas a Telegram:", sum(1 for t in sp if str(t.get('signal_id')) in sent_ids),
          "· no enviadas:", sum(1 for t in sp if str(t.get('signal_id')) not in sent_ids))
    opened = Counter("abierta antes de la semana" if (o := _dt(t.get("opened_at"))) and o < start
                     else "abierta en la semana" for t in sp)
    print("    apertura:", dict(opened))
    fut = [t for t in sp if _sport(t) == "fútbol"]
    print(f"    fútbol: {len(fut)} (pnl {sum(float(t.get('pnl_virtual') or 0) for t in fut):+.2f}u)")

    # ── b) Bankroll ────────────────────────────────────────────────────────────────
    by_src: dict = {}
    for t in trades:
        if t.get("pnl_virtual") is None:
            continue
        k = (str(t.get("source")), str(t.get("result")))
        n, p = by_src.get(k, (0, 0.0))
        by_src[k] = (n + 1, p + float(t.get("pnl_virtual") or 0))
    total = sum(p for _, p in by_src.values())
    print("\n[b] P&L de shadow_trades con pnl_virtual, por source y resultado:")
    for (s, r), (n, p) in sorted(by_src.items()):
        print(f"    {s:20} {r:8} n={n:5}  {p:+9.2f}u")
    print(f"    50u + {total:+.2f}u = saldo {50 + total:.2f}u")

    # ── c) Polymarket: emitido real frente a ledger crudo ──────────────────────────
    alerts = [d.to_dict() or {} for d in iter_all("alerts_sent", where=("type", "==", "polymarket"))]
    graded = [a for a in alerts if a.get("status") in SENT and a.get("market_id")
              and a.get("result") in ("win", "loss") and _poly_alert_selection(a)]
    pm = [t for t in trades if t.get("source") == "polymarket" and t.get("result") in ("win", "loss")]
    a_m = Counter(str(a["market_id"]) for a in graded)
    t_m = Counter(str(t.get("signal_id")) for t in pm)
    print(f"\n[c] emitido real: {len(graded)} envíos graduados, {len(a_m)} mercados "
          f"({sum(v - 1 for v in a_m.values())} reenvíos del mismo mercado)")
    print(f"    ledger crudo: {len(pm)} trades cerrados, {len(t_m)} mercados")
    print(f"    mercados en los dos: {len(set(a_m) & set(t_m))} · solo en alertas: "
          f"{len(set(a_m) - set(t_m))} · solo en el ledger: {len(set(t_m) - set(a_m))}")
    mov = [t for t in trades if t.get("source") == "polymarket_movement" and t.get("result") in ("win", "loss")]
    print(f"    (aparte, source=polymarket_movement: {len(mov)} cerradas; no entran en ninguna de las dos)")

    # ── e) BUY_NO graduadas en la semana ────────────────────────────────────────────
    wk = [a for a in alerts if a.get("market_id") and a.get("result") in ("win", "loss")
          and (r := _dt(a.get("resolved_at"))) and start <= r < end
          and _poly_alert_selection(a) == "BUY_NO"]
    print(f"\n[e] BUY_NO graduadas en la semana por status:",
          dict(Counter(f"{a.get('status')}/{a.get('result')}" for a in wk)))
    rows = []
    for a in sorted(wk, key=lambda a: _dt(a.get("sent_at")) or start):
        mp = a.get("market_price_yes", a.get("last_price"))
        rows.append({
            "status": a.get("status"),
            "enviada": f"{_dt(a.get('sent_at')):%Y-%m-%d %H:%M}" if _dt(a.get("sent_at")) else None,
            "resuelta": f"{_dt(a.get('resolved_at')):%Y-%m-%d}",
            "mercado": a.get("question") or (a.get("raw") or {}).get("question"),
            "market_id": a.get("market_id"),
            "precio_yes": mp,
            "cuota_no": round(_poly_decimal_odds("BUY_NO", float(mp)), 3) if mp is not None else None,
            "edge": a.get("edge"),
            "resultado": a.get("result"),
            "outcome": a.get("outcome"),
        })
    with open(os.environ.get("DETAIL_FILE", "detalle.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"    detalle de {len(rows)} BUY_NO en el artefacto (no en el log)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
