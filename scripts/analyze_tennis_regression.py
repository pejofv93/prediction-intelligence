"""
ANÁLISIS (solo lectura) — regresión 80/20 y umbral de divergencia en tenis.
NO modifica nada. Lee prodpredictions (sport=tennis) via Firestore REST.

Responde:
  1. Underdog (cuota>2.5) vs favorito: nº, win rate, P&L (flat 1u).
  2. ¿Pierden los underdogs sistemáticamente?
  3. Simula (a) bajar umbral divergencia a 0.05; (b) cambiar peso regresión
     model_p1 = w·fair + (1-w)·0.5 para w in {0.80, 0.90, 1.00}.
  4. Compara qué corta mejor las perdedoras sin tocar ganadoras.

Metodología P&L: flat 1 unidad por señal. win → +(odds-1), loss → -1.
  ROI = P&L_total / nº_apuestas.

Nota: en la ruta odds-only del analyzer, divergencia == edge (misma fórmula
  prob - 1/odds). Por eso bajar el umbral de divergencia = capar el edge.

Uso: python scripts/analyze_tennis_regression.py
Requiere: gcloud auth print-access-token --account pejocanal@gmail.com
"""
import json
import ssl
import subprocess
import urllib.request

PROJECT = "prediction-intelligence"
PREFIX = "prod"
ACCOUNT = "pejocanal@gmail.com"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

ODDS_ONLY_MIN_EDGE = 0.03
CUR_DIVERGENCE = 0.10


def _token() -> str:
    import sys
    if len(sys.argv) > 1 and sys.argv[1].startswith("ya29."):
        return sys.argv[1].strip()
    for exe in ("gcloud.cmd", "gcloud"):
        try:
            return subprocess.run(
                [exe, "auth", "print-access-token", "--account", ACCOUNT],
                capture_output=True, text=True, check=True, shell=False,
            ).stdout.strip()
        except FileNotFoundError:
            continue
    raise RuntimeError("gcloud no encontrado; pasa el token como argv[1]")


def _decode(v):
    """Firestore typed value → python."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    if "timestampValue" in v: return v["timestampValue"]
    if "nullValue" in v: return None
    if "mapValue" in v:
        return {k: _decode(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v:
        return [_decode(x) for x in v["arrayValue"].get("values", [])]
    return v


def _run_query(token, offset, limit=500):
    body = {
        "structuredQuery": {
            "from": [{"collectionId": f"{PREFIX}predictions"}],
            "where": {"fieldFilter": {
                "field": {"fieldPath": "sport"},
                "op": "EQUAL",
                "value": {"stringValue": "tennis"},
            }},
            "orderBy": [{"field": {"fieldPath": "__name__"}}],
            "offset": offset,
            "limit": limit,
        }
    }
    req = urllib.request.Request(
        f"{BASE}:runQuery",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, context=_ctx, timeout=60) as r:
        rows = json.loads(r.read())
    out = []
    for row in rows:
        doc = row.get("document")
        if not doc:
            continue
        fields = {k: _decode(v) for k, v in doc.get("fields", {}).items()}
        out.append(fields)
    return out


def _norm(s):
    import re, unicodedata
    n = unicodedata.normalize("NFD", (s or "").lower().strip()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", n).strip()


def main():
    token = _token()
    docs, offset = [], 0
    while True:
        batch = _run_query(token, offset)
        if not batch:
            break
        docs.extend(batch)
        offset += len(batch)
        if len(batch) < 500:
            break
    print(f"Total docs sport=tennis (todas las señales, todos los mercados): {len(docs)}")

    resolved = [d for d in docs if d.get("correct") is not None]
    print(f"  resueltas (correct != null): {len(resolved)}")

    h2h = [d for d in resolved if (d.get("market_type") or "h2h") == "h2h"]
    print(f"  resueltas h2h: {len(h2h)}")
    oo = [d for d in h2h if d.get("data_source") == "odds_only"]
    sm = [d for d in h2h if d.get("data_source") == "tennis_model"]
    print(f"    de ellas odds_only: {len(oo)} | tennis_model(stats): {len(sm)}")
    print(f"  por market_type: ", {})
    from collections import Counter
    print("  market_type breakdown:", dict(Counter((d.get('market_type') or 'h2h') for d in resolved)))
    print("  data_source breakdown (h2h):", dict(Counter(d.get('data_source') for d in h2h)))
    print()

    def pnl(d):
        odds = float(d.get("odds") or 0)
        if d.get("correct"):
            return odds - 1.0
        return -1.0

    def grp_stats(rows, label):
        n = len(rows)
        if n == 0:
            print(f"  [{label}] n=0")
            return
        w = sum(1 for d in rows if d.get("correct"))
        p = sum(pnl(d) for d in rows)
        avg_odds = sum(float(d.get("odds") or 0) for d in rows) / n
        print(f"  [{label}] n={n} | win_rate={w/n:.1%} ({w}/{n}) | "
              f"P&L={p:+.2f}u | ROI={p/n:+.1%} | cuota_media={avg_odds:.2f}")

    # ── 1 & 2: underdog vs favorito (h2h) ─────────────────────────────────────
    print("=== 1+2. UNDERDOG (cuota>2.5) vs FAVORITO (cuota<=2.5) — h2h resueltas ===")
    under = [d for d in h2h if float(d.get("odds") or 0) > 2.5]
    fav = [d for d in h2h if 0 < float(d.get("odds") or 0) <= 2.5]
    grp_stats(under, "UNDERDOG >2.5")
    grp_stats(fav, "FAVORITO <=2.5")
    grp_stats(h2h, "TODAS h2h")
    print()
    # odds-only es donde aplica la regresión
    print("  -- solo odds_only (donde la regresión 80/20 genera el edge) --")
    grp_stats([d for d in oo if float(d.get('odds') or 0) > 2.5], "odds_only UNDERDOG")
    grp_stats([d for d in oo if 0 < float(d.get('odds') or 0) <= 2.5], "odds_only FAVORITO")
    print()

    # banda de edge (en odds_only, edge == divergencia)
    print("  -- distribución de edge en odds_only h2h --")
    for lo, hi in [(0.03, 0.05), (0.05, 0.08), (0.08, 0.10), (0.10, 1.0)]:
        band = [d for d in oo if lo <= float(d.get("edge") or 0) < hi]
        grp_stats(band, f"edge [{lo:.2f},{hi:.2f})")
    print()

    # ── 3a: bajar umbral divergencia a 0.05 ───────────────────────────────────
    # divergencia == edge en h2h. Bajar a 0.05 corta señales con edge>0.05.
    print("=== 3a. SIMULACIÓN: umbral divergencia 0.10 -> 0.05 (h2h) ===")
    cut = [d for d in h2h if float(d.get("edge") or 0) > 0.05]
    kept = [d for d in h2h if float(d.get("edge") or 0) <= 0.05]
    cut_losers = [d for d in cut if not d.get("correct")]
    cut_winners = [d for d in cut if d.get("correct")]
    pnl_cut = sum(pnl(d) for d in cut)
    print(f"  señales cortadas (edge>0.05): {len(cut)} "
          f"(perdedoras {len(cut_losers)}, ganadoras {len(cut_winners)})")
    print(f"  P&L evitado al cortarlas: {-pnl_cut:+.2f}u "
          f"(si negativo el P&L cortado, esto es ahorro)")
    print(f"  P&L de las cortadas: {pnl_cut:+.2f}u")
    grp_stats(kept, "RESTANTES tras umbral 0.05")
    grp_stats(h2h, "ACTUAL (umbral 0.10)")
    # ¿caen McDonald/Virtanen/Jacquet?
    print("  nombres con edge en (0.05,0.10]:")
    for d in sorted(h2h, key=lambda x: -float(x.get("edge") or 0)):
        e = float(d.get("edge") or 0)
        if 0.05 < e <= 0.10:
            print(f"    {d.get('selection',''):28} edge={e:.3f} odds={float(d.get('odds') or 0):.2f} "
                  f"correct={d.get('correct')}")
    print()

    # ── 3b: cambiar peso regresión ────────────────────────────────────────────
    print("=== 3b. SIMULACIÓN: peso regresión w (solo odds_only h2h) ===")
    print("  model_backed = w*fair_backed + (1-w)*0.5 ; edge = model_backed - 1/odds")
    print("  fire si edge in [0.03, 0.10]\n")
    bad_fair = 0
    enriched = []
    for d in oo:
        sig = d.get("signals") or d.get("factors") or {}
        fair_p1 = sig.get("odds_fair_p1")
        if fair_p1 is None:
            bad_fair += 1
            continue
        backed_home = _norm(d.get("selection"))[:6] == _norm(d.get("home_team"))[:6]
        fair_backed = float(fair_p1) if backed_home else (1.0 - float(fair_p1))
        odds = float(d.get("odds") or 0)
        if odds <= 1:
            continue
        enriched.append({**d, "_fair_backed": fair_backed, "_odds": odds,
                         "_backed_home": backed_home})
    if bad_fair:
        print(f"  (omitidas {bad_fair} odds_only sin odds_fair_p1 en signals)")
    print(f"  odds_only h2h con fair disponible: {len(enriched)}\n")

    # sanity: ¿calculated_prob == 0.8*fair_backed + 0.1?
    mism = [e for e in enriched
            if abs(float(e.get("calculated_prob") or 0) - (0.8*e["_fair_backed"] + 0.1)) > 0.02]
    print(f"  sanity check (calc_prob ~= 0.8*fair+0.1): {len(enriched)-len(mism)}/{len(enriched)} OK\n")

    for w in (0.80, 0.90, 1.00):
        fired = []
        for e in enriched:
            model_b = w * e["_fair_backed"] + (1 - w) * 0.5
            new_edge = model_b - 1.0 / e["_odds"]
            if ODDS_ONLY_MIN_EDGE <= new_edge <= CUR_DIVERGENCE:
                fired.append(e)
        n = len(fired)
        wins = sum(1 for d in fired if d.get("correct"))
        p = sum(pnl(d) for d in fired)
        tag = " (ACTUAL)" if abs(w-0.80) < 1e-9 else ""
        if n:
            print(f"  w={w:.2f}{tag}: disparan {n} | WR={wins/n:.1%} | P&L={p:+.2f}u | ROI={p/n:+.1%}")
        else:
            print(f"  w={w:.2f}{tag}: disparan 0 (ningún edge sobrevive — edge fantasma eliminado)")
    print()

    # ── 4: comparación ────────────────────────────────────────────────────────
    print("=== 4. Comparación corte de perdedoras vs ganadoras (odds_only h2h) ===")
    base_losers = sum(1 for d in oo if not d.get("correct"))
    base_winners = sum(1 for d in oo if d.get("correct"))
    base_pnl = sum(pnl(d) for d in oo)
    print(f"  BASE odds_only h2h: {len(oo)} señales | ganadoras {base_winners} | "
          f"perdedoras {base_losers} | P&L {base_pnl:+.2f}u")
    # (a) umbral 0.05 sobre odds_only
    keep_a = [d for d in oo if float(d.get("edge") or 0) <= 0.05]
    la = sum(1 for d in keep_a if not d.get("correct"))
    wa = sum(1 for d in keep_a if d.get("correct"))
    print(f"  (a) umbral 0.05 -> quedan {len(keep_a)} | ganadoras {wa} (de {base_winners}) | "
          f"perdedoras {la} (de {base_losers}) | P&L {sum(pnl(d) for d in keep_a):+.2f}u")
    # (b) w=0.90 y w=1.0 ya calculados arriba


if __name__ == "__main__":
    main()
