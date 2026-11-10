"""SOLO LECTURA. Revisión de Polymarket con gpt-oss (alertas desde el 2026-09-23).

Lee alerts_sent (type=polymarket), shadow_trades (source polymarket*), poly_predictions y
poly_model_weights/current, y consulta Gamma (API pública, solo GET). No escribe nada.
El log del workflow es público: imprime SOLO agregados (nada de ids, preguntas ni slugs).

1. Alertas por ventana (antes del categorizador v2 / solo categorizador / categorizador +
   dedup) y dirección: n, acierto, acierto esperado por precio, ROI plano a 1u con IC.
   "resent" se cuenta aparte y nunca como apuesta (el resolver no lo gradúa).
   "Limpia" = al enviarse el mercado no estaba cerrado ni el partido empezado (Gamma).
2. Calibración: edge declarado frente a realizado, confianza frente a acierto, Brier del
   modelo frente al del mercado.
3. Tope de confianza del tramo alto (groq_analyzer, Fix 4): BUY_YES con edge >= 0,15 sin
   alerta desde el 23-sep, por motivo probable, y cómo habrían salido. poly_predictions
   guarda solo el ÚLTIMO análisis de cada mercado: es una cota, no el histórico completo.
4. Cuántas alertas limpias y resueltas hay y si bastan para hablar de sobreestimación.
"""
import json
import os
import random
import re
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.firestore_client import col, iter_all  # noqa: E402

START = datetime(2026, 9, 23, tzinfo=timezone.utc)
CAT_V2 = datetime(2026, 10, 22, 7, 35, tzinfo=timezone.utc)
DEDUP = datetime(2026, 10, 22, 10, 18, tzinfo=timezone.utc)
WINDOWS = (("W1 antes categorizador (23-sep → 22-oct 07:35)", START, CAT_V2),
           ("W2 solo categorizador (22-oct 07:35 → 10:18)", CAT_V2, DEDUP),
           ("W3 categorizador + dedup (desde 22-oct 10:18)", DEDUP, None))
GAMMA = "https://gamma-api.polymarket.com/markets/"
BOOT = 4000
rng = random.Random(20261110)


# ── utilidades ────────────────────────────────────────────────────────────────

def as_dt(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        s = str(v).strip().replace("Z", "+00:00").replace(" ", "T", 1)
        if re.search(r"[+-]\d\d$", s):
            s += ":00"
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fnum(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def gamma_get(mid):
    try:
        rq = urllib.request.Request(GAMMA + str(mid), headers={"User-Agent": "pi-readonly-review"})
        with urllib.request.urlopen(rq, timeout=20) as r:
            raw = json.loads(r.read())
        return (raw[0] if raw else None) if isinstance(raw, list) else raw
    except Exception:
        return None


def gamma_outcome(raw):
    """YES / NO si el mercado está cerrado y decidido; None si no."""
    if not raw or raw.get("closed") is not True:
        return None
    op = raw.get("outcomePrices") or []
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except ValueError:
            return None
    try:
        y = float(op[0])
    except (IndexError, TypeError, ValueError):
        return None
    return "YES" if y > 0.9 else "NO" if y < 0.1 else None


def dirty_reason(raw, when):
    """Por qué el mercado ya no era apostable en `when` (None = limpio)."""
    if raw is None:
        return "sin_gamma"
    ct = as_dt(raw.get("closedTime"))
    if ct and when >= ct:
        return "cerrado"
    gs = as_dt(raw.get("gameStartTime"))
    if gs and when >= gs:
        return "partido_empezado"
    return None


def pct(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(q * len(s))))]


def boot_mean(vals):
    """IC 90 % por bootstrap de la media y P(media > 0)."""
    if len(vals) < 2:
        return None, None, None
    n = len(vals)
    ms = [sum(rng.choice(vals) for _ in range(n)) / n for _ in range(BOOT)]
    return pct(ms, 0.05), pct(ms, 0.95), sum(m > 0 for m in ms) / BOOT


def fmt_ci(lo, hi, p=None, mult=100, unit="%"):
    if lo is None:
        return "IC n/d"
    dec = 1 if unit else 3
    s = f"IC90 [{lo * mult:+.{dec}f}{unit}, {hi * mult:+.{dec}f}{unit}]"
    return s + (f" P(>0)={p:.0%}" if p is not None else "")


# ── lectura ───────────────────────────────────────────────────────────────────

def load():
    alerts = []
    for d in iter_all("alerts_sent", where=("type", "==", "polymarket")):
        a = d.to_dict() or {}
        sent = as_dt(a.get("sent_at"))
        if sent and sent >= START:
            a["_sent"] = sent
            a["_id"] = d.id
            alerts.append(a)
    shadows = {}
    for d in iter_all("shadow_trades"):
        t = d.to_dict() or {}
        if str(t.get("source") or "").startswith("polymarket"):
            shadows[d.id] = t
    preds = []
    for d in iter_all("poly_predictions"):
        p = d.to_dict() or {}
        at = as_dt(p.get("analyzed_at"))
        if at and at >= START:
            p["_at"] = at
            p["_mid"] = str(p.get("market_id") or d.id)
            preds.append(p)
    w = col("poly_model_weights").document("current").get()
    weights = (w.to_dict() or {}) if w.exists else {}
    return alerts, shadows, preds, weights


def selection(a):
    for k in ("recommendation", "direction"):
        v = str(a.get(k) or "").upper()
        if v in ("BUY_YES", "BUY_NO"):
            return v
    try:
        e = float(str(a.get("alert_key") or "").rsplit("_", 1)[1])
        return "BUY_YES" if e >= 0 else "BUY_NO"
    except (IndexError, ValueError):
        return None


def enrich(a, shadows):
    """Precio YES, edge, confianza y prob. del modelo de lo ENVIADO: foto de alerts_sent
    (desde el 15-oct) o, si falta, el shadow_trade enlazado por el resolver."""
    t = shadows.get(str(a.get("shadow_trade_id") or "")) or {}
    sd = t.get("signal_data") or {}
    get = lambda k: fnum(a.get(k)) if fnum(a.get(k)) is not None else fnum(sd.get(k))  # noqa: E731
    mp, rp, edge, conf = get("market_price_yes"), get("real_prob"), get("edge"), get("confidence")
    if conf is None:
        conf = fnum(t.get("confidence"))
    if edge is None and mp is not None and rp is not None:
        edge = rp - mp
    if rp is None and mp is not None and edge is not None:
        rp = mp + edge
    return mp, rp, edge, conf


# ── métricas ──────────────────────────────────────────────────────────────────

def bet_rows(items):
    """items: dicts con dir, mp (precio YES), rp, edge, conf, outcome. Devuelve filas con
    p (precio pagado), hit, roi, edge declarado y realizado."""
    rows = []
    for it in items:
        mp, out, d = it["mp"], it["outcome"], it["dir"]
        if mp is None or out is None or not (0 < mp < 1):
            continue
        p = mp if d == "BUY_YES" else 1 - mp
        hit = 1 if (out == "YES") == (d == "BUY_YES") else 0
        rp = it.get("rp")
        model_side = None if rp is None else (rp if d == "BUY_YES" else 1 - rp)
        rows.append({**it, "p": p, "hit": hit, "roi": (1 / p - 1) if hit else -1.0,
                     "edge_decl": None if model_side is None else model_side - p,
                     "y": 1 if out == "YES" else 0})
    return rows


def line(label, rows):
    if not rows:
        return f"  {label:<10} n=0"
    n = len(rows)
    hit = sum(r["hit"] for r in rows) / n
    exp = sum(r["p"] for r in rows) / n
    roi = [r["roi"] for r in rows]
    lo, hi, p = boot_mean(roi)
    return (f"  {label:<10} n={n:<4} acierto={hit:6.1%} esperado={exp:6.1%} "
            f"ROI plano={sum(roi) / n * 100:+6.1f}% {fmt_ci(lo, hi, p)}")


def by_dir(rows):
    out = defaultdict(list)
    for r in rows:
        out[r["dir"]].append(r)
        out["TOTAL"].append(r)
    return out


def calib(rows, title):
    print(f"\n  {title}")
    er = [r for r in rows if r["edge_decl"] is not None]
    if er:
        for name, grp in (("todas", er), *[(d, [r for r in er if r["dir"] == d]) for d in ("BUY_YES", "BUY_NO")]):
            if not grp:
                continue
            decl = sum(r["edge_decl"] for r in grp) / len(grp)
            real = [r["hit"] - r["p"] for r in grp]
            gap = [r["edge_decl"] - (r["hit"] - r["p"]) for r in grp]
            lo, hi, _ = boot_mean(gap)
            print(f"    edge {name:<8} n={len(grp):<4} declarado={decl:+.3f} realizado={sum(real) / len(grp):+.3f} "
                  f"sobreestimación={sum(gap) / len(gap):+.3f} {fmt_ci(lo, hi, None, 1, '')}")
        print("    por tramo de edge declarado:")
        for lo_e, hi_e in ((0, 0.12), (0.12, 0.15), (0.15, 0.25), (0.25, 9)):
            g = [r for r in er if lo_e <= abs(r["edge_decl"]) < hi_e]
            if g:
                print(f"      [{lo_e:.2f},{min(hi_e, 1):.2f}) n={len(g):<4} declarado={sum(r['edge_decl'] for r in g) / len(g):+.3f} "
                      f"realizado={sum(r['hit'] - r['p'] for r in g) / len(g):+.3f} acierto={sum(r['hit'] for r in g) / len(g):.0%}")
    cr = [r for r in rows if r.get("conf") is not None]
    if cr:
        print("    confianza declarada vs acierto:")
        for lo_c, hi_c in ((0, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.80), (0.80, 1.01)):
            g = [r for r in cr if lo_c <= r["conf"] < hi_c]
            if g:
                print(f"      conf [{lo_c:.2f},{min(hi_c, 1):.2f}) n={len(g):<4} media={sum(r['conf'] for r in g) / len(g):.3f} "
                      f"acierto={sum(r['hit'] for r in g) / len(g):.1%} esperado precio={sum(r['p'] for r in g) / len(g):.1%}")
    br = [r for r in rows if r.get("rp") is not None and 0 <= r["rp"] <= 1]
    if br:
        bm = [(r["rp"] - r["y"]) ** 2 for r in br]
        bk = [(r["mp"] - r["y"]) ** 2 for r in br]
        diff = [m - k for m, k in zip(bm, bk)]
        lo, hi, p = boot_mean(diff)
        print(f"    Brier (sobre YES) n={len(br)}: modelo={sum(bm) / len(bm):.4f} mercado={sum(bk) / len(bk):.4f} "
              f"modelo-mercado={sum(diff) / len(diff):+.4f} IC90 [{lo:+.4f}, {hi:+.4f}] P(modelo peor)={p:.0%}"
              if lo is not None else f"    Brier n={len(br)}: muestra insuficiente")


# ── principal ─────────────────────────────────────────────────────────────────

def main():
    alerts, shadows, preds, weights = load()
    print(f"alerts_sent polymarket desde 23-sep: {len(alerts)} · shadow_trades poly: {len(shadows)} · "
          f"poly_predictions analizadas desde 23-sep: {len(preds)}")

    yes_cands = [p for p in preds
                 if str(p.get("recommendation") or "").upper() == "BUY_YES"
                 and (fnum(p.get("edge")) or 0) >= 0.15]
    mids = {str(a.get("market_id")) for a in alerts if a.get("market_id")} | {p["_mid"] for p in yes_cands}
    with ThreadPoolExecutor(max_workers=8) as ex:
        gamma = dict(zip(mids, ex.map(gamma_get, mids)))
    print(f"Gamma: {len(mids)} mercados consultados, {sum(v is None for v in gamma.values())} sin respuesta")

    # ── 1 y 2: alertas ──
    items, status_count = [], defaultdict(lambda: defaultdict(int))
    for a in alerts:
        w = next(i for i, (_, s, e) in enumerate(WINDOWS) if a["_sent"] >= s and (e is None or a["_sent"] < e))
        st = str(a.get("status") or "")
        status_count[w][st] += 1
        if st not in ("sent", "sent_archived"):
            continue
        mp, rp, edge, conf = enrich(a, shadows)
        raw = gamma.get(str(a.get("market_id")))
        res = a.get("result")
        out = a.get("outcome") if res in ("win", "loss") else None
        items.append({"w": w, "dir": selection(a), "mp": mp, "rp": rp, "edge": edge, "conf": conf,
                      "outcome": out if out in ("YES", "NO") else None,
                      "dirty": dirty_reason(raw, a["_sent"]), "graded": res in ("win", "loss")})

    # Comprobación de los cortes con los propios datos (solo fechas y recuentos).
    cv2 = sorted(a["_sent"] for a in alerts if a.get("categorizer_version") == 2)
    slot_re = re.compile(r"_BUY_(YES|NO)(_r\d+)?$")
    slot = sorted(a["_sent"] for a in alerts if slot_re.search(str(a.get("alert_key") or a["_id"])))
    print("\nCortes: categorizador v2 desde %s, dedup desde %s" % (CAT_V2.isoformat(), DEDUP.isoformat()))
    print(f"  primera alerta con categorizer_version=2: {cv2[0].isoformat() if cv2 else 'ninguna'} · "
          f"con v2 antes del corte: {sum(t < CAT_V2 for t in cv2)}")
    print(f"  primera alerta con clave de slot (mercado_dirección): {slot[0].isoformat() if slot else 'ninguna'} · "
          f"con clave de slot antes del corte: {sum(t < DEDUP for t in slot)} · "
          f"sin v2 después del corte: {sum(a['_sent'] >= CAT_V2 and a.get('categorizer_version') != 2 for a in alerts)}")

    print("\n=== 1. ALERTAS POR VENTANA ===")
    for i, (name, _, _) in enumerate(WINDOWS):
        sc = status_count[i]
        its = [x for x in items if x["w"] == i]
        print(f"\n{name}")
        print("  estados: " + ", ".join(f"{k or 'sin_estado'}={v}" for k, v in sorted(sc.items())) +
              f" · apuestas (sent/archived)={len(its)} graduadas={sum(x['graded'] for x in its)} "
              f"pendientes={sum(not x['graded'] for x in its)}")
        dirty = defaultdict(int)
        for x in its:
            dirty[x["dirty"] or "limpia"] += 1
        print("  al enviar: " + ", ".join(f"{k}={v}" for k, v in sorted(dirty.items())))
        sin_precio = sum(x["graded"] and x["mp"] is None for x in its)
        if sin_precio:
            print(f"  graduadas sin precio recuperable (fuera del ROI): {sin_precio}")
        for scope, flt in (("todas", lambda x: True), ("limpias", lambda x: x["dirty"] is None)):
            rows = bet_rows([x for x in its if flt(x)])
            print(f"  [{scope}]")
            g = by_dir(rows)
            for d in ("BUY_YES", "BUY_NO", "TOTAL"):
                print(line(d, g.get(d, [])))

    print("\nTOTAL gpt-oss (las tres ventanas)")
    for scope, flt in (("todas", lambda x: True), ("limpias", lambda x: x["dirty"] is None)):
        rows = bet_rows([x for x in items if flt(x)])
        print(f"  [{scope}]")
        g = by_dir(rows)
        for d in ("BUY_YES", "BUY_NO", "TOTAL"):
            print(line(d, g.get(d, [])))

    print("\n=== 2. CALIBRACIÓN (alertas graduadas) ===")
    all_rows = bet_rows(items)
    clean_rows = [r for r in all_rows if r["dirty"] is None]
    calib(all_rows, "Todas las graduadas")
    calib(clean_rows, "Solo limpias")
    for i, (name, _, _) in enumerate(WINDOWS):
        g = [r for r in clean_rows if r["w"] == i]
        if g:
            calib(g, f"Limpias · {name}")

    # ── 3: tope de confianza ──
    print("\n=== 3. TOPE DE CONFIANZA DEL TRAMO ALTO (BUY_YES, edge ≥ 0,15) ===")
    bb = (weights.get("accuracy_by_bucket") or {}).get("high") or {}
    bn, bacc = int(bb.get("n", 0) or 0), float(bb.get("accuracy", 0) or 0)
    cap = round(min(1.0, max(0.55, bacc + 0.15)), 4) if bn >= 50 and bacc > 0 else None
    print(f"accuracy_by_bucket.high vigente: n={bn} acc={bacc:.1%} → tope={cap} (mínimo base BUY_YES 0,60; con volume_spike 0,55)")
    sent_yes = {str(a.get("market_id")) for a in alerts
                if selection(a) == "BUY_YES" and str(a.get("status")) in ("sent", "sent_archived", "resent")}
    motives, rows3 = defaultdict(int), defaultdict(list)
    for p in yes_cands:
        if p["_mid"] in sent_yes:
            motives["alertada igualmente"] += 1
            continue
        mp, conf = fnum(p.get("market_price_yes")), fnum(p.get("confidence")) or 0
        spike = bool(p.get("volume_spike"))
        end = as_dt(p.get("end_date_iso"))
        raw = gamma.get(p["_mid"])
        if mp is not None and mp < 0.40:
            m = "anti-longshot (mp_yes<0,40)"
        elif end and (end - p["_at"]).days > 60:
            m = "cierra en >60 días"
        elif conf < (0.55 if spike else 0.60):
            m = ("conf en el tope" if cap is not None and abs(conf - cap) < 0.0005
                 else "conf bajo el mínimo (tope + iliquidez u otra)")
        else:
            m = "otro (slot/mercado cerrado/no llegó a check)"
        motives[m] += 1
        rows3[m].append({"dir": "BUY_YES", "mp": mp, "rp": fnum(p.get("real_prob")), "conf": conf,
                         "outcome": gamma_outcome(raw), "dirty": dirty_reason(raw, p["_at"])})
    print(f"Candidatas (último análisis de cada mercado): {len(yes_cands)}")
    for m, c in sorted(motives.items(), key=lambda kv: -kv[1]):
        print(f"  {m}: {c}")
    print("Resultado de las descartadas, al precio de su último análisis:")
    for m, its in sorted(rows3.items(), key=lambda kv: -len(kv[1])):
        res = sum(x["outcome"] is not None for x in its)
        print(f"  {m}: resueltas {res}/{len(its)}")
        for scope, flt in (("todas", lambda x: True), ("limpias", lambda x: x["dirty"] is None)):
            print(line(scope, bet_rows([x for x in its if flt(x)])))
    blocked = rows3.get("conf en el tope", []) + rows3.get("conf bajo el mínimo (tope + iliquidez u otra)", [])
    br = bet_rows([x for x in blocked if x["dirty"] is None])
    if br:
        print("  Bloqueadas por confianza, limpias, por precio:")
        for lo_p, hi_p in ((0.40, 0.55), (0.55, 0.70), (0.70, 1.0)):
            g = [r for r in br if lo_p <= r["mp"] < hi_p]
            print(line(f"mp[{lo_p:.2f},{hi_p:.2f})", g))
        calib(br, "Calibración de las bloqueadas limpias")

    # ── 4: muestra ──
    print("\n=== 4. MUESTRA LIMPIA Y RESUELTA ===")
    er = [r for r in clean_rows if r["edge_decl"] is not None]
    print(f"Alertas limpias graduadas: {len(clean_rows)} (con edge declarado: {len(er)}) · "
          f"BUY_YES={sum(r['dir'] == 'BUY_YES' for r in clean_rows)} BUY_NO={sum(r['dir'] == 'BUY_NO' for r in clean_rows)}")
    print(f"Limpias pendientes de resolver: {sum(x['dirty'] is None and not x['graded'] for x in items)}")
    if er:
        gap = [r["edge_decl"] - (r["hit"] - r["p"]) for r in er]
        lo, hi, p = boot_mean(gap)
        sd = (sum((g - sum(gap) / len(gap)) ** 2 for g in gap) / max(1, len(gap) - 1)) ** 0.5
        need = int((1.645 * sd / max(1e-9, abs(sum(gap) / len(gap)))) ** 2) + 1
        print(f"Sobreestimación media del edge: {sum(gap) / len(gap):+.3f} {fmt_ci(lo, hi, p, 1, '')} · "
              f"desv. típica {sd:.3f} → n para que el IC90 excluya 0 con este efecto ≈ {need}")


if __name__ == "__main__":
    main()
