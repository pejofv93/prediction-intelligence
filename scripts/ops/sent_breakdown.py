"""SOLO LECTURA. ¿Dónde se pierde lo ENVIADO en Sports desde el 22-oct?

Universo (el mismo que usa el informe diario):
  - Fútbol y tenis: alerts_sent type=sports_sent (un doc por predicción y selección; un
    reenvío no es apuesta nueva). Cuota, EV y nivel del PRIMER envío. Graduado por
    learning_engine.grade_sent_alerts (correct True/False; push y pendientes fuera).
  - Baloncesto: no escribe sports_sent; va por predictions.alerted == True con resultado,
    creadas desde el 22-oct.
  - Tenis: se registra pero no hay fuente de resultados → solo se cuenta.

Para cada dimensión (deporte, liga, mercado, nivel, tramo de cuota, semana, liga×mercado)
imprime: n graduadas, acierto, acierto esperado por la cuota (media de 1/cuota, con margen
de la casa), ROI a 1u con IC95 (normal sobre el resultado por apuesta) y, donde hay, la
cuota de cierre aproximada: último snapshot de odds_history (1X2) antes del inicio, media
entre casas → CLV = cuota enviada / cierre − 1.

Veredicto por segmento: "pierde claro" si el IC95 del ROI queda entero bajo 0; "muestra
corta" si n < 30; si no, "sin evidencia". Solo agregados: nunca ids, equipos ni selecciones.
No escribe nada.
"""
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from shared.firestore_client import col, iter_all  # noqa: E402

SINCE = datetime(2026, 10, 22, tzinfo=timezone.utc)
MIN_N = 30
Z95 = 1.96
ODDS_BANDS = ((1.0, 1.6, "<1,60"), (1.6, 2.0, "1,60-2,00"), (2.0, 2.5, "2,00-2,50"),
              (2.5, 3.5, "2,50-3,50"), (3.5, 1e9, ">=3,50"))


def as_dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def tier(intensity, ev) -> str:
    """Nivel que se vio en la alerta (shared/signal_tier), el mismo del informe y el historial."""
    from shared.signal_tier import tier_of
    return tier_of(intensity, ev)


def band(odds: float) -> str:
    for lo, hi, lab in ODDS_BANDS:
        if lo <= odds < hi:
            return lab
    return "?"


def is_basket(p: dict) -> bool:
    return (str(p.get("sport") or "").lower() in ("basketball", "nba")
            or str(p.get("league") or "").upper() in ("NBA", "ACB", "EUROLEAGUE"))


def closing_odds(match_id: str, side: str, kickoff):
    """Media entre casas del último snapshot 1X2 antes del inicio → (cuota, minutos antes)."""
    if not match_id or side not in ("home", "draw", "away") or kickoff is None:
        return None, None
    vals, mins = [], []
    try:
        docs = list(col("odds_history").where(filter=FieldFilter("fixture_id", "==", match_id)).stream())
    except Exception:
        return None, None
    for d in docs:
        h = d.to_dict() or {}
        best = None
        for e in h.get("odds_history") or []:
            t = as_dt(e.get("recorded_at"))
            o = e.get(f"{side}_odds")
            if t is None or t > kickoff or not o or float(o) <= 1:
                continue
            if best is None or t > best[0]:
                best = (t, float(o))
        if best:
            vals.append(best[1])
            mins.append((kickoff - best[0]).total_seconds() / 60)
    if not vals:
        return None, None
    return sum(vals) / len(vals), min(mins)


def h2h_side(a: dict):
    sel = str(a.get("selection") or a.get("team_to_back") or "").strip().lower()
    if not sel:
        return None
    if sel == str(a.get("home_team") or "").strip().lower():
        return "home"
    if sel == str(a.get("away_team") or "").strip().lower():
        return "away"
    if sel in ("draw", "empate", "x"):
        return "draw"
    return None


def collect():
    bets, counts = [], defaultdict(int)
    field_names = defaultdict(int)
    for d in iter_all("alerts_sent", where=("type", "==", "sports_sent")):
        a = d.to_dict() or {}
        sent_at = as_dt(a.get("sent_at"))
        if sent_at is None or sent_at < SINCE:
            counts["fuera de fecha"] += 1
            continue
        for k in a:
            if any(s in k.lower() for s in ("clos", "clv", "open")):
                field_names[f"alerts_sent.{k}"] += 1
        sport = str(a.get("sport") or "football").lower()
        counts[f"enviadas {sport}"] += 1
        if a.get("correct") not in (True, False):
            counts[f"sin graduar {sport} ({a.get('result') or 'pendiente'})"] += 1
            continue
        try:
            odds = float(a.get("odds") or 0)
        except (TypeError, ValueError):
            odds = 0.0
        if odds <= 1:
            counts["graduada sin cuota (fuera)"] += 1
            continue
        bets.append({
            "sport": sport, "league": a.get("league") or "?",
            "market": str(a.get("market_type") or "h2h").lower(), "ev": a.get("ev"),
            "intensity": a.get("intensity"),
            "odds": odds, "ok": a["correct"] is True, "at": sent_at,
            "match_id": str(a.get("match_id") or ""), "kickoff": as_dt(a.get("match_date")),
            "side": h2h_side(a) if str(a.get("market_type") or "h2h").lower() == "h2h" else None,
        })

    for d in col("predictions").where(filter=FieldFilter("alerted", "==", True)).stream():
        p = d.to_dict() or {}
        if not is_basket(p):
            continue
        created = as_dt(p.get("created_at"))
        if created is None or created < SINCE:
            continue
        counts["enviadas basketball (marca alerted)"] += 1
        if p.get("correct") not in (True, False) or p.get("push"):
            counts["sin graduar basketball"] += 1
            continue
        try:
            odds = float(p.get("odds") or 0)
        except (TypeError, ValueError):
            odds = 0.0
        if odds <= 1:
            counts["graduada sin cuota (fuera)"] += 1
            continue
        bets.append({
            "sport": "basketball", "league": p.get("league") or "?",
            "market": str(p.get("market_type") or "h2h").lower(),
            "ev": p.get("ev") if p.get("ev") is not None else p.get("edge"),
            "intensity": p.get("intensity"),
            "odds": odds, "ok": p["correct"] is True, "at": created,
            "match_id": "", "kickoff": None, "side": None,
        })
    return bets, counts, field_names


def stats(sub: list[dict]) -> dict:
    n = len(sub)
    pnl = [(b["odds"] - 1) if b["ok"] else -1.0 for b in sub]
    roi = sum(pnl) / n
    se = statistics.stdev(pnl) / math.sqrt(n) if n > 1 else float("inf")
    clv = [b["clv"] for b in sub if b.get("clv") is not None]
    lo, hi = roi - Z95 * se, roi + Z95 * se
    verdict = "muestra corta" if n < MIN_N else ("PIERDE CLARO" if hi < 0 else
                                                 "gana claro" if lo > 0 else "sin evidencia")
    return {"n": n, "hit": sum(b["ok"] for b in sub) / n, "exp": sum(1 / b["odds"] for b in sub) / n,
            "odds": statistics.median(b["odds"] for b in sub), "roi": roi, "lo": lo, "hi": hi,
            "pnl": sum(pnl), "clv_n": len(clv), "clv": (sum(clv) / len(clv)) if clv else None,
            "verdict": verdict}


def line(label: str, s: dict) -> str:
    clv = f" CLV {s['clv']:+.1%} (n={s['clv_n']})" if s["clv"] is not None else ""
    return (f"  {label[:34]:34s} n={s['n']:4d} acierto={s['hit']:4.0%} esperado={s['exp']:4.0%}"
            f" cuota_med={s['odds']:.2f} ROI={s['roi']:+6.1%} IC95=[{s['lo']:+.0%},{s['hi']:+.0%}]"
            f" uds={s['pnl']:+6.1f}{clv}  → {s['verdict']}")


def main() -> None:
    bets, counts, field_names = collect()

    # Cuota de cierre aproximada (solo 1X2 de fútbol, que es lo que guarda odds_history)
    cache, mins_before = {}, []
    for b in bets:
        if b["sport"] != "football" or b["market"] != "h2h" or not b["side"]:
            continue
        key = (b["match_id"], b["side"])
        if key not in cache:
            cache[key] = closing_odds(b["match_id"], b["side"], b["kickoff"])
        close, mins = cache[key]
        if close:
            b["clv"] = b["odds"] / close - 1
            mins_before.append(mins)

    print(f"Universo: enviadas Sports desde {SINCE.date()} (fútbol/tenis alerts_sent; baloncesto marca alerted).")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    print(f"  graduadas con cuota (lo que se analiza): {len(bets)}")
    print("  campos tipo cierre/apertura en alerts_sent: "
          + (", ".join(f"{k} ({v})" for k, v in sorted(field_names.items())) or "ninguno"))
    h2h_fb = [b for b in bets if b["sport"] == "football" and b["market"] == "h2h"]
    print(f"  cierre aproximado (odds_history): {len(mins_before)} de {len(h2h_fb)} 1X2 de fútbol;"
          + (f" último snapshot, mediana {statistics.median(mins_before):.0f} min antes del inicio"
             if mins_before else " sin datos"))
    print("  Acierto esperado = media de 1/cuota (incluye el margen de la casa: el real sin margen es algo menor).\n")

    if not bets:
        print("Nada graduado: no hay análisis.")
        return
    print("== TOTAL")
    print(line("todas", stats(bets)))

    dims = {
        "deporte": lambda b: b["sport"],
        "nivel": lambda b: tier(b["intensity"], b["ev"]),
        "tramo de cuota": lambda b: band(b["odds"]),
        "mercado": lambda b: f"{b['sport'][:4]}·{b['market']}",
        "liga": lambda b: f"{b['sport'][:4]}·{b['league']}",
        "semana": lambda b: b["at"].strftime("%G-W%V"),
        "nivel × cuota": lambda b: f"{tier(b['intensity'], b['ev'])} · {band(b['odds'])}",
        "liga × mercado (n>=10)": lambda b: f"{b['league']} · {b['market']}",
    }
    for name, key in dims.items():
        groups = defaultdict(list)
        for b in bets:
            groups[key(b)].append(b)
        print(f"\n== {name}")
        rows = sorted(groups.items(), key=lambda kv: -len(kv[1]))
        hidden = 0
        for val, sub in rows:
            if name.startswith("liga × mercado") and len(sub) < 10:
                hidden += len(sub)
                continue
            print(line(str(val), stats(sub)))
        if hidden:
            print(f"  (en celdas con n<10: {hidden})")

    # Resumen de veredictos (sin semana ni cruces)
    print("\n== Segmentos que PIERDEN CLARO (IC95 del ROI entero bajo 0, n>=30)")
    any_ = False
    for name in ("deporte", "nivel", "tramo de cuota", "mercado", "liga", "nivel × cuota"):
        groups = defaultdict(list)
        for b in bets:
            groups[dims[name](b)].append(b)
        for val, sub in groups.items():
            s = stats(sub)
            if s["verdict"] == "PIERDE CLARO":
                any_ = True
                print(f"  [{name}]" + line(str(val), s))
    if not any_:
        print("  ninguno")


if __name__ == "__main__":
    main()
