"""
scripts/poly_real_roi.py

Mide el ROI REAL de Polymarket separando:
  - LEDGER CRUDO  : todos los shadow_trades source=polymarket resueltos
                    (incluye señales legacy nunca alertadas → contaminación)
  - EMITIDO REAL  : solo las realmente alertadas (poly_predictions.alerted == True)
                    join por shadow_trade.signal_id == poly_predictions doc id

Y simula el efecto de des-contaminar el LEARNING: muestra accuracy por dirección
y el min_edge que `poly_learning_engine._new_threshold` produciría con el ledger
crudo vs solo-alertadas, para ver si los umbrales aprendidos se reajustan.

Solo lectura. No escribe nada en Firestore.

Uso:
    python scripts/poly_real_roi.py

Requiere: gcloud auth print-access-token --account pejocanal@gmail.com activo.
Re-ejecutable cuando resuelvan más pendientes para re-medir la muestra limpia.
"""
import json
import ssl
import subprocess
import sys
import urllib.request

PROJECT = "prediction-intelligence"
ACCOUNT = "pejocanal@gmail.com"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"

# Límites del learner (espejo de poly_learning_engine.py)
_EDGE_MIN = 0.07
_EDGE_MAX = 0.40
_MIN_SAMPLE = 5

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_TOK = [None]


def _tok():
    if not _TOK[0]:
        r = subprocess.run(
            ["gcloud", "auth", "print-access-token", f"--account={ACCOUNT}"],
            capture_output=True, text=True, shell=(sys.platform == "win32"),
        )
        _TOK[0] = r.stdout.strip()
        if not _TOK[0]:
            raise RuntimeError(f"Sin token para {ACCOUNT}: {r.stderr}")
    return _TOK[0]


def _req(url):
    rq = urllib.request.Request(url, headers={"Authorization": f"Bearer {_tok()}"})
    with urllib.request.urlopen(rq, timeout=60, context=_ctx) as r:
        return json.loads(r.read())


def _fv(o):
    if not isinstance(o, dict):
        return o
    for t in ["stringValue", "booleanValue", "timestampValue", "nullValue"]:
        if t in o:
            return o[t]
    if "integerValue" in o:
        return int(o["integerValue"])
    if "doubleValue" in o:
        return float(o["doubleValue"])
    if "mapValue" in o:
        return {k: _fv(v) for k, v in o["mapValue"].get("fields", {}).items()}
    if "arrayValue" in o:
        return [_fv(v) for v in o["arrayValue"].get("values", [])]
    return None


def _fields(d):
    return {k: _fv(v) for k, v in d.get("fields", {}).items()}


def _list(coll):
    docs, pt = [], None
    while True:
        url = f"{BASE}/{coll}?pageSize=300" + (f"&pageToken={pt}" if pt else "")
        d = _req(url)
        docs.extend(d.get("documents", []))
        pt = d.get("nextPageToken")
        if not pt:
            break
    return docs


def _mp_yes(t):
    sd = t.get("signal_data") or {}
    v = sd.get("market_price_yes")
    if v is None:
        v = t.get("market_price_yes")
    try:
        v = float(v)
        if 0 < v < 1:
            return v
    except Exception:
        pass
    return None


def _agg(rows):
    n = len(rows)
    w = sum(1 for r in rows if r["result"] == "win")
    pnl = sum(r["pnl"] for r in rows)
    stake = sum(r["stake"] for r in rows)
    return {
        "n": n, "wins": w,
        "wr": (100 * w / n) if n else 0.0,
        "pnl": pnl, "stake": stake,
        "roi": (100 * pnl / stake) if stake else 0.0,
    }


def _line(label, rows):
    a = _agg(rows)
    print(f"  {label:34s} n={a['n']:3d} W={a['wins']:3d} "
          f"WR={a['wr']:5.1f}% PnL={a['pnl']:+7.2f}u stake={a['stake']:6.2f}u ROI={a['roi']:+6.1f}%")


def _sim_min_edge(rows):
    """Replica _new_threshold (rama n>=MIN_SAMPLE) para una dirección.
    Devuelve (accuracy, n, min_win_edge, new_edge_estimado)."""
    n = len(rows)
    wins = [r for r in rows if r["result"] == "win"]
    acc = (len(wins) / n) if n else 0.0
    win_edges = sorted(abs(r["edge"]) for r in wins if r["edge"] is not None)
    if n >= _MIN_SAMPLE and win_edges:
        min_win = min(win_edges)
        new_edge = min_win if acc >= 0.70 else min_win + 0.02
        if acc >= 0.85:
            new_edge = max(_EDGE_MIN, min_win - 0.01)
        new_edge = round(max(_EDGE_MIN, min(_EDGE_MAX, new_edge)), 4)
        return acc, n, (min_win if win_edges else None), new_edge
    return acc, n, (min(win_edges) if win_edges else None), None


def main():
    print("Cargando Firestore (prodshadow_trades + prodpoly_predictions)...", file=sys.stderr)
    # Alertados autoritativos
    pp = _list("prodpoly_predictions")
    alerted_ids = set()
    for d in pp:
        f = _fields(d)
        if f.get("alerted") is True:
            alerted_ids.add(d["name"].split("/")[-1])

    st = _list("prodshadow_trades")
    rows = []
    for d in st:
        t = _fields(d)
        if t.get("source") != "polymarket" or t.get("result") not in ("win", "loss"):
            continue
        edge = t.get("edge")
        sd = t.get("signal_data") or {}
        if edge is None:
            edge = sd.get("edge")
        rows.append({
            "sel": (t.get("selection") or "").upper(),
            "result": t.get("result"),
            "pnl": float(t.get("pnl_virtual") or 0),
            "stake": float(t.get("virtual_stake") or 0.5),
            "mp": _mp_yes(t),
            "edge": float(edge) if edge is not None else None,
            "alerted": t.get("signal_id") in alerted_ids,
        })

    crudo = rows
    real = [r for r in rows if r["alerted"]]
    no_alert = [r for r in rows if not r["alerted"]]

    print("\n" + "=" * 80)
    print("POLYMARKET — ROI REAL (ledger crudo vs emitido real)   [solo lectura]")
    print("=" * 80)
    print(f"poly_predictions.alerted=True: {len(alerted_ids)} | shadow_trades resueltos: {len(crudo)}")
    print()
    _line("LEDGER CRUDO (todo)", crudo)
    _line("EMITIDO REAL (alertado)", real)
    _line("contaminación (no alertado)", no_alert)

    print("\n--- Emitido real por dirección ---")
    for s in ("BUY_YES", "BUY_NO"):
        _line(s, [r for r in real if s in r["sel"]])

    print("\n--- Simulación umbrales learning (crudo vs des-contaminado) ---")
    print("  ¿Cambian los min_edge aprendidos al quitar las fantasma?\n")
    for s in ("BUY_YES", "BUY_NO"):
        cr = _sim_min_edge([r for r in crudo if s in r["sel"]])
        rl = _sim_min_edge([r for r in real if s in r["sel"]])
        print(f"  [{s}]")
        print(f"    CRUDO          : acc={cr[0]*100:5.1f}% n={cr[1]:3d} "
              f"min_win_edge={cr[2]} -> min_edge={cr[3]}")
        print(f"    DES-CONTAMINADO: acc={rl[0]*100:5.1f}% n={rl[1]:3d} "
              f"min_win_edge={rl[2]} -> min_edge={rl[3]}")
    print("\n[fin]")


if __name__ == "__main__":
    main()
