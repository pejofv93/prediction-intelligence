"""
VERIFICACIÓN (solo lectura) del grader de basket.
Replica la lógica de get_basketball_result_by_teams + evaluate_prediction contra
las fuentes reales (ESPN / Sofascore ACB / Euroleague) para las predicciones h2h
de basket pendientes, y reporta cuántas se gradúan + WR/P&L real.

No escribe nada (ni Firestore ni nada). SSL verification off para esquivar Norton.
Uso: python scripts/verify_basket_grader.py <ACCESS_TOKEN>
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime, timedelta

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "https://firestore.googleapis.com/v1/projects/prediction-intelligence/databases/(default)/documents"
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

EUR_URL = ("https://feeds.incrowdsports.com/provider/euroleague-feeds/v2"
           "/competitions/E/seasons/E2025/games?limit=300")
SF_BASE = "https://api.sofascore.com/api/v1"
SF_ACB_T, SF_ACB_S = 264, 80922
SF_HDR = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:119.0) Gecko/20100101 Firefox/119.0",
          "Accept": "application/json", "Referer": "https://www.sofascore.com/"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        return json.loads(r.read().decode())


def norm(s):
    n = unicodedata.normalize("NFD", (s or "").lower().strip()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", n).strip()


def dec(v):
    if not isinstance(v, dict): return v
    for t in ["stringValue", "timestampValue"]:
        if t in v: return v[t]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    if "nullValue" in v: return None
    if "mapValue" in v: return {k: dec(x) for k, x in v["mapValue"].get("fields", {}).items()}
    return v


def q_predictions():
    body = {"structuredQuery": {"from": [{"collectionId": "prodpredictions"}],
            "orderBy": [{"field": {"fieldPath": "__name__"}}], "limit": 2000}}
    req = urllib.request.Request(BASE + ":runQuery", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    rows = json.loads(urllib.request.urlopen(req, context=ctx, timeout=90).read())
    return [{k: dec(v) for k, v in r["document"].get("fields", {}).items()}
            for r in rows if r.get("document")]


# ── Fuentes de resultados ─────────────────────────────────────────────────────
_CACHE = {}


def nba_finished(date_iso):
    key = "nba_" + date_iso
    if key in _CACHE: return _CACHE[key]
    espn_date = date_iso.replace("-", "")[:8]
    out = []
    try:
        data = _get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=" + espn_date)
    except Exception as e:
        print("  [warn] ESPN", date_iso, e); _CACHE[key] = []; return []
    for ev in data.get("events", []):
        for comp in ev.get("competitions", []):
            if comp.get("status", {}).get("type", {}).get("state") != "post":
                continue
            cs = comp.get("competitors", [])
            h = next((c for c in cs if c.get("homeAway") == "home"), {})
            a = next((c for c in cs if c.get("homeAway") == "away"), {})
            try: hs, as_ = float(h.get("score")), float(a.get("score"))
            except (TypeError, ValueError): continue
            out.append({"h": h.get("team", {}).get("displayName", ""), "a": a.get("team", {}).get("displayName", ""),
                        "hs": hs, "as": as_, "d": (comp.get("date") or ev.get("date") or "")[:10]})
    _CACHE[key] = out; return out


def acb_finished():
    if "acb" in _CACHE: return _CACHE["acb"]
    out = []
    for page in range(4):
        try:
            data = _get(f"{SF_BASE}/unique-tournament/{SF_ACB_T}/season/{SF_ACB_S}/events/last/{page}", SF_HDR)
        except Exception as e:
            print("  [warn] Sofascore ACB p", page, e); break
        for e in data.get("events", []):
            hs = (e.get("homeScore") or {}).get("current"); as_ = (e.get("awayScore") or {}).get("current")
            if hs is None or as_ is None: continue
            ts = e.get("startTimestamp")
            d = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            out.append({"h": e.get("homeTeam", {}).get("name", ""), "a": e.get("awayTeam", {}).get("name", ""),
                        "hs": float(hs), "as": float(as_), "d": d})
    _CACHE["acb"] = out; return out


def eur_finished():
    if "eur" in _CACHE: return _CACHE["eur"]
    out = []
    try:
        data = _get(EUR_URL)
    except Exception as e:
        print("  [warn] Euroleague", e); _CACHE["eur"] = []; return []
    for g in data.get("data", []):
        if (g.get("status") or "").lower() != "result": continue
        h, a = g.get("home", {}), g.get("away", {})
        hs, as_ = h.get("score"), a.get("score")
        if hs is None or as_ is None: continue
        out.append({"h": h.get("name", ""), "a": a.get("name", ""),
                    "hs": float(hs), "as": float(as_), "d": (g.get("date") or "")[:10]})
    _CACHE["eur"] = out; return out


def finished_for(league, date10):
    lg = (league or "").upper()
    if lg == "NBA":
        base = datetime.fromisoformat(date10)
        return [r for off in (-1, 0, 1) for r in nba_finished((base + timedelta(days=off)).date().isoformat())]
    if lg == "ACB": return acb_finished()
    if lg in ("EUROLEAGUE", "EUR"): return eur_finished()
    return []


def result_by_teams(league, home, away, date10):
    hn, an = norm(home), norm(away)
    for f in finished_for(league, date10):
        if f["d"] and date10 and f["d"] != date10: continue
        if f["hs"] == f["as"]: continue
        fh, fa = norm(f["h"]), norm(f["a"])
        if fh == hn and fa == an: return "HOME_WIN" if f["hs"] > f["as"] else "AWAY_WIN"
        if fh == an and fa == hn: return "AWAY_WIN" if f["hs"] > f["as"] else "HOME_WIN"
    return None


def main():
    allp = q_predictions()
    bk = [p for p in allp if str(p.get("sport", "")).lower() in ("basketball", "nba")
          and (p.get("market_type") or "h2h") == "h2h"]
    print(f"Predicciones basket h2h: {len(bk)}\n")

    rows = []
    for p in bk:
        league = p.get("league", ""); md = str(p.get("match_date", ""))[:10]
        home, away = p.get("home_team", ""), p.get("away_team", "")
        sel = p.get("selection", ""); odds = float(p.get("odds") or 0)
        res = result_by_teams(league, home, away, md)
        if res is None:
            rows.append((league, sel, odds, md, None, None)); continue
        backed = norm(sel)
        if backed == norm(home): correct = (res == "HOME_WIN")
        elif backed == norm(away): correct = (res == "AWAY_WIN")
        else: correct = None  # selection no casa con home/away
        rows.append((league, sel, odds, md, res, correct))

    graded = [r for r in rows if r[5] is not None]
    ungraded = [r for r in rows if r[5] is None]
    print(f"GRADUADAS: {len(graded)}/{len(bk)}   sin resolver: {len(ungraded)}\n")
    for lg in ("NBA", "ACB", "EUROLEAGUE"):
        g = [r for r in graded if (r[0] or "").upper() == lg]
        if not g: continue
        w = sum(1 for r in g if r[5]); pnl = sum((r[2] - 1) if r[5] else -1 for r in g)
        print(f"  [{lg:10}] graded={len(g):2} | WR={w/len(g):5.1%} ({w}/{len(g)}) | P&L={pnl:+.2f}u | ROI={pnl/len(g):+.1%}")
    if graded:
        w = sum(1 for r in graded if r[5]); pnl = sum((r[2] - 1) if r[5] else -1 for r in graded)
        print(f"  [{'TOTAL':10}] graded={len(graded):2} | WR={w/len(graded):5.1%} ({w}/{len(graded)}) | P&L={pnl:+.2f}u | ROI={pnl/len(graded):+.1%}")
    print("\n--- detalle graduadas ---")
    for lg, sel, odds, md, res, correct in graded:
        print(f"  {lg:10} {md} {sel:24} @{odds:5.2f} -> {res:9} correct={correct}")
    if ungraded:
        print("\n--- sin resolver (fuera de ventana histórica o sin match) ---")
        for lg, sel, odds, md, res, correct in ungraded[:20]:
            print(f"  {lg:10} {md} {sel:24} @{odds:5.2f}")


if __name__ == "__main__":
    main()
