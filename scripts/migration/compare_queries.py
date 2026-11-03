"""Compara consultas sobre Firestore real y sobre pg_store, en el MISMO instante.

Uso (lo lanza pg-store-test con compare=true):
  python scripts/migration/compare_queries.py --dump DIR

DIR es un volcado recién hecho por firestore_dump.py y ya cargado en Postgres con
load_jsonl_pg.py. Las consultas a Firestore van con readTime = el del volcado; sin PITR,
Firestore solo deja leer la última hora, así que este paso tiene que correr justo después
del volcado (el script se para solo si se acerca el límite).

Casos:
  - "codigo": las formas de consulta que usan los servicios (colección, campos, operadores,
    order_by, límites y la paginación de iter_all por __name__ con start_after), con
    valores tomados de los propios datos.
  - "sonda": en cada colección, los 2 campos de primer nivel con más mezcla de ausente /
    null / tipos distintos: ==, == null, !=, != null, in, not-in, <, >=, order_by asc/desc
    y order_by + start_after.
Para cada caso: recuento (agregación en Firestore, count() en pg_store) y lista ordenada de
ids (hasta CAP) en ambos lados.

Solo lectura en Firestore. El log es público: imprime nombres de colección y de campo, el
tipo de los valores usados y recuentos; nunca valores ni ids de documento.
"""
import argparse, collections, concurrent.futures as cf, datetime as dt, hashlib, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import firestore_dump as fd  # noqa: E402
import pg_store as ps  # noqa: E402
from load_jsonl_pg import parse_ts, rest_to_py  # noqa: E402

CAP = 200            # ids comparados por caso
CURSOR_POS = 50      # posición del documento usado como cursor en start_after
WORKERS = 8
SCALAR = ("nullValue", "booleanValue", "integerValue", "doubleValue", "timestampValue", "stringValue")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
FS_OP = {"==": "EQUAL", "!=": "NOT_EQUAL", "<": "LESS_THAN", "<=": "LESS_THAN_OR_EQUAL",
         ">": "GREATER_THAN", ">=": "GREATER_THAN_OR_EQUAL", "in": "IN", "not-in": "NOT_IN"}
NULL = {"nullValue": None}


def S(x):
    return {"stringValue": x}


def B(x):
    return {"booleanValue": x}


# Formas de consulta del código (services/, shared/, scripts/), sin prefijo de colección.
# Tokens de valor: "top"/"top2" (valor más común / dos más comunes), "rare" (valor menos
# común), "med"/"q1"/"q3" (cuantiles del tipo dominante), "prefix" (rango de prefijo de un
# string: >= base, < base+""). Se calculan sobre los docs que cumplen los "==" previos.
# (colección, filtros, orders, límite, start_after en la posición CURSOR_POS del caso base)
CODE = [
    ("predictions", [("result", "==", NULL)], [], None, False),
    ("predictions", [("result", "!=", NULL)], [], None, False),
    ("predictions", [("result", "==", S("expired"))], [], None, False),
    ("predictions", [("alerted", "==", B(True))], [], None, False),
    ("predictions", [("league", "==", "top"), ("market", "==", "top")], [], None, False),
    ("predictions", [("home_team", "==", "top")], [], None, False),
    ("predictions", [("edge", ">", "med"), ("result", "==", NULL)], [("edge", "DESC")], 50, False),
    ("predictions", [("result", "==", NULL)], [("unified_score", "DESC")], 50, False),
    ("predictions", [], [("created_at", "DESC")], CAP, True),
    ("predictions", [("created_at", ">=", "q1"), ("created_at", "<", "q3")], [], None, False),
    ("predictions", [("match_id", ">=", "prefix"), ("match_id", "<", "prefix")], [], None, False),
    ("poly_predictions", [("analyzed_at", ">=", "med")], [("analyzed_at", "DESC")], None, True),
    ("poly_predictions", [("analyzed_at", ">=", "q1"), ("analyzed_at", "<", "q3")], [], None, False),
    ("poly_predictions", [("edge", ">", "med")], [("edge", "DESC")], 20, False),
    ("poly_predictions", [("category", "==", S("crypto")), ("alerted", "==", B(False))], [("edge", "DESC")], 20, False),
    ("poly_predictions", [("alerted", "==", B(False))], [("edge", "DESC")], 20, False),
    ("poly_predictions", [("alerted", "==", B(True))], [], None, False),
    ("poly_price_history", [("market_id", "==", "top"), ("timestamp", ">=", "med")], [("timestamp", "ASC")], None, True),
    ("poly_price_history", [("market_id", "==", "top"), ("timestamp", ">=", "q1"), ("timestamp", "<=", "q3")],
     [("timestamp", "ASC")], None, False),
    ("poly_price_history", [("market_id", "==", "rare"), ("timestamp", ">=", "q1")], [("timestamp", "ASC")], None, False),
    ("poly_price_history", [("timestamp", "<", "med")], [], None, False),
    ("enriched_markets", [("enriched_at", "<", "med")], [], None, False),
    ("enriched_markets", [], [("enriched_at", "DESC")], CAP, True),
    ("enriched_markets", [], [("volume_24h", "DESC")], CAP, True),
    ("poly_markets", [], [("updated_at", "DESC")], CAP, True),
    ("poly_markets", [], [("volume_24h", "DESC")], CAP, True),
    ("alerts_sent", [("type", "==", S("polymarket"))], [("__name__", "ASC")], CAP, True),
    ("alerts_sent", [("type", "==", S("sports_sent"))], [("__name__", "ASC")], CAP, True),
    ("alerts_sent", [("market_id", "==", "top")], [], None, False),
    ("alerts_sent", [("sent_at", ">=", "med")], [], None, False),
    ("matches", [("home_team_id", "==", "top"), ("match_date", ">=", "med")], [], None, False),
    ("matches", [("away_team_id", "==", "top"), ("match_date", ">=", "med")], [], None, False),
    ("matches", [("home_team_id", "==", "top"), ("competition", "==", "top"),
                 ("match_date", ">=", "q1"), ("match_date", "<=", "q3")], [], None, False),
    ("matches", [("home_team_id", "==", "top"), ("competition", "in", "top2"),
                 ("match_date", ">=", "q1"), ("match_date", "<=", "q3")], [], None, False),
    ("upcoming_matches", [("status", "in", [S("SCHEDULED"), S("TIMED")])], [], None, False),
    ("upcoming_matches", [("league", "==", "top"), ("status", "in", [S("SCHEDULED"), S("TIMED")])], [], None, False),
    ("upcoming_matches", [("sport", "==", S("tennis")), ("status", "in", [S("SCHEDULED"), S("TIMED")])], [], None, False),
    ("upcoming_matches", [("status", "==", S("SCHEDULED"))], [], None, False),
    ("upcoming_matches", [("source", "==", S("allsports_uefa"))], [], None, False),
    ("enriched_matches", [("status", "in", [S("SCHEDULED"), S("TIMED")])], [], None, False),
    ("shadow_trades", [("result", "in", [S("win"), S("loss")])], [("closed_at", "DESC")], CAP, True),
    ("shadow_trades", [("result", "!=", NULL)], [], None, False),
    ("shadow_trades", [("alerted", "==", B(True))], [], None, False),
    ("shadow_trades", [("signal_id", "==", "top"), ("source", "==", "top")], [], None, False),
    ("trend_signals", [("graded", "==", B(False))], [], None, False),
    ("trend_signals", [("pattern_type", "in", [S("series"), S("model")]), ("graded", "==", B(False))], [], None, False),
    ("trend_signals", [("pattern_type", "==", S("rolling")), ("graded", "==", B(False))], [], None, False),
    ("team_stats", [("team_id", "==", "top")], [], 1, False),
    ("team_stats", [("team_name", "==", "top")], [], None, False),
    ("team_stats", [("name", "==", "top")], [], None, False),
    ("standings", [("team_id", "==", "top")], [], 1, False),
    ("odds_history", [("fixture_id", "==", "top")], [], None, False),
    ("market_correlations", [("market_a", "==", "top")], [], None, False),
    ("market_correlations", [("market_b", "==", "top")], [], None, False),
    ("match_results", [("league", "==", S("CL"))], [], None, False),
    ("match_results", [("home_team", "==", "top")], [], None, False),
    ("accuracy_log", [("week", "==", "top")], [], None, False),
    ("accuracy_log", [], [("week", "DESC")], CAP, False),
    ("arb_opportunities", [("expires_at", ">", "med")], [("expires_at", "DESC")], None, False),
    ("binance_snapshots", [("symbol", "==", S("BTCUSDT"))], [("recorded_at", "DESC")], 1, False),
    ("filter_blocks", [("blocked_at", ">=", "med")], [("blocked_at", "ASC")], None, True),
    ("corners_signals", [("generated_at", ">=", "med")], [], None, False),
    ("trend_daily_reports", [], [("cutoff", "DESC")], 1, False),
    ("trend_weekly_reports", [], [("cutoff", "DESC")], 1, False),
    ("bets", [], [("created_at", "DESC")], CAP, False),
    ("team_corner_stats", [("league", "==", "top")], [], None, False),
    ("poly_smart_wallets", [("market_id", "==", "top")], [], None, False),
]


# ------------------------------------------------------------------ valores
def vkey(v):
    return json.dumps(v, sort_keys=True)


def vtype(v):
    return next(iter(v))


def sortable(v):
    t, x = next(iter(v.items()))
    if t == "integerValue":
        return int(x)
    if t == "doubleValue":
        return float(x)
    if t == "timestampValue":
        return parse_ts(x)
    return x


def usable(v):
    t, x = next(iter(v.items()))
    return t in SCALAR and not (t == "doubleValue" and x in ("NaN", "Infinity", "-Infinity"))


def profile(docs):
    prof = collections.defaultdict(lambda: {"present": 0, "null": 0, "types": collections.Counter(),
                                            "vals": collections.Counter()})
    for fields in docs.values():
        for k, v in fields.items():
            if not NAME_RE.match(k):
                continue
            p = prof[k]
            p["present"] += 1
            p["types"][vtype(v)] += 1
            if vtype(v) == "nullValue":
                p["null"] += 1
            elif usable(v):
                p["vals"][vkey(v)] += 1
    return prof


def resolve(token, field, docs):
    """Valor (o lista) REST para el token, calculado sobre `docs`. None si no hay datos."""
    if not isinstance(token, str):
        return token
    vals = collections.Counter()
    for fields in docs.values():
        v = fields.get(field)
        if v is not None and vtype(v) != "nullValue" and usable(v):
            vals[vkey(v)] += 1
    if not vals:
        return None
    if token == "top":
        return json.loads(vals.most_common(1)[0][0])
    if token == "rare":
        return json.loads(vals.most_common()[-1][0])
    if token == "top2":
        return [json.loads(k) for k, _ in vals.most_common(2)]
    by_type = collections.defaultdict(list)
    for k in vals:
        v = json.loads(k)
        t = vtype(v)
        by_type["number" if t in ("integerValue", "doubleValue") else t].append(v)
    dom = max(by_type.values(), key=len)
    dom.sort(key=sortable)
    if token == "prefix":
        strs = by_type.get("stringValue")
        if not strs:
            return None
        base = strs[len(strs) // 2]["stringValue"]
        base = base[: max(1, len(base) // 2)]
        return [S(base), S(base + "")]
    q = {"med": 0.5, "q1": 0.25, "q3": 0.75}[token]
    return dom[min(len(dom) - 1, int(len(dom) * q))]


def build_code_case(cid_short, filters, orders, limit, cursor, docs):
    subset = docs
    out = []
    prefix = None
    for field, op, token in filters:
        if token == "prefix":
            if prefix is None:
                prefix = resolve("prefix", field, subset)
                if prefix is None:
                    return None
            v = prefix[0] if op in (">=", ">") else prefix[1]
        else:
            v = resolve(token, field, subset)
            if v is None:
                return None
        out.append((field, op, v, token if isinstance(token, str) else "literal"))
        if op == "==":
            k = vkey(v)
            subset = {i: f for i, f in docs.items() if f.get(field) is not None and vkey(f[field]) == k}
    return {"kind": "codigo", "filters": out, "orders": orders, "limit": limit, "cursor": cursor}


def probe_cases(docs, prof):
    n = len(docs)
    cands = []
    for k, p in prof.items():
        if len(p["vals"]) < 2:
            continue
        nn_types = {("number" if t in ("integerValue", "doubleValue") else t) for t in p["types"] if t != "nullValue"}
        score = 2 * (p["null"] > 0) + 2 * (p["present"] < n) + (len(nn_types) > 1)
        cands.append((score, p["present"], k))
    cands.sort(reverse=True)
    cases = []
    for _, _, f in cands[:2]:
        p = prof[f]
        top2 = [json.loads(k) for k, _ in p["vals"].most_common(2)]
        x = top2[0]
        med = resolve("med", f, docs)
        base = [
            ([(f, "==", x, "top")], [], None, False),
            ([(f, "!=", x, "top")], [], None, False),
            ([(f, "!=", NULL, "literal")], [], None, False),
            ([(f, "in", top2, "top2")], [], None, False),
            ([(f, "not-in", top2, "top2")], [], None, False),
            ([(f, "<", med, "med")], [], None, False),
            ([(f, ">=", med, "med")], [], None, False),
            ([], [(f, "ASC")], CAP, True),
            ([], [(f, "DESC")], CAP, True),
        ]
        if p["null"]:
            base.insert(1, ([(f, "==", NULL, "literal")], [], None, False))
        for fl, od, lim, cur in base:
            cases.append({"kind": "sonda", "filters": fl, "orders": od, "limit": lim, "cursor": cur})
    cases.append({"kind": "sonda", "filters": [], "orders": [("__name__", "ASC")], "limit": CAP, "cursor": True})
    return cases


# ------------------------------------------------------------------ ejecución
def fs_structured(cid, case, limit, cursor_values=None):
    sq = {"from": [{"collectionId": cid}]}
    fl = []
    for f, op, v, _ in case["filters"]:
        if op in ("==", "!=") and not isinstance(v, list) and vtype(v) == "nullValue":
            fl.append({"unaryFilter": {"field": {"fieldPath": f}, "op": "IS_NULL" if op == "==" else "IS_NOT_NULL"}})
        else:
            val = {"arrayValue": {"values": v}} if op in ("in", "not-in") else v
            fl.append({"fieldFilter": {"field": {"fieldPath": f}, "op": FS_OP[op], "value": val}})
    if fl:
        sq["where"] = fl[0] if len(fl) == 1 else {"compositeFilter": {"op": "AND", "filters": fl}}
    if case["orders"]:
        sq["orderBy"] = [{"field": {"fieldPath": f}, "direction": "DESCENDING" if d == "DESC" else "ASCENDING"}
                         for f, d in case["orders"]]
    if cursor_values is not None:
        sq["startAt"] = {"values": cursor_values, "before": False}
    if limit is not None:
        sq["limit"] = limit
    return sq


def fs_run(cid, case, rt, cursor_values=None):
    lim = case["limit"]
    sq = fs_structured(cid, case, lim, cursor_values)
    agg = {"structuredAggregationQuery": {"structuredQuery": sq, "aggregations": [{"alias": "n", "count": {}}]},
           "readTime": rt}
    d = fd.call(f"{fd.API}/{fd.DB}/documents:runAggregationQuery", agg)
    n = int(d[0]["result"]["aggregateFields"]["n"]["integerValue"])
    sq_ids = dict(fs_structured(cid, case, min(lim, CAP) if lim else CAP, cursor_values))
    sq_ids["select"] = {"fields": [{"fieldPath": "__name__"}]}
    d = fd.call(f"{fd.API}/{fd.DB}/documents:runQuery", {"structuredQuery": sq_ids, "readTime": rt})
    return n, [x["document"]["name"].rsplit("/", 1)[1] for x in d if "document" in x]


def pg_run(client, cid, case, cursor=None):
    q = client.collection(cid)
    for f, op, v, _ in case["filters"]:
        val = [rest_to_py(i) for i in v] if op in ("in", "not-in") else rest_to_py(v)
        q = q.where(filter=ps.FieldFilter(f, op, val))
    for f, d in case["orders"]:
        q = q.order_by(f, direction="DESCENDING" if d == "DESC" else "ASCENDING")
    if cursor is not None:
        q = q.start_after(cursor)
    lim = case["limit"]
    qc = q.limit(lim) if lim else q
    n = qc.count().get()[0][0].value
    ids = [s.id for s in q.limit(min(lim, CAP) if lim else CAP).stream()]
    return n, ids


def fname(f):
    return f if f == "__name__" or SAFE_RE.match(f) else "campo#" + hashlib.sha256(f.encode()).hexdigest()[:8]


def describe(case):
    parts = []
    for f, op, v, tok in case["filters"]:
        if isinstance(v, list):
            t = "[" + ",".join(vtype(i).replace("Value", "") for i in v) + "]"
        else:
            t = "null" if vtype(v) == "nullValue" else vtype(v).replace("Value", "")
        parts.append(f"{fname(f)} {op} {t}" + ("" if tok == "literal" else f"({tok})"))
    for f, d in case["orders"]:
        parts.append(f"order {fname(f)} {d}")
    if case["limit"]:
        parts.append(f"limit {case['limit']}")
    if case.get("after"):
        parts.append(f"start_after pos {case['after']}")
    return " · ".join(parts) or "(colección entera)"


def op_family(case):
    if case.get("after"):
        return "start_after"
    ops = [op for _, op, _, _ in case["filters"]]
    for o in ("not-in", "!=", "in", "<", "<=", ">", ">=", "=="):
        if o in ops:
            return o
    return "order_by" if case["orders"] else "todo"


def compare(fs, pg):
    (nf, idf), (np_, idp) = fs, pg
    if nf == np_ and idf == idp:
        return True, f"fs {nf} pg {np_} · ids {len(idf)} iguales y en el mismo orden"
    sf, sp = set(idf), set(idp)
    msg = f"fs {nf} pg {np_} · ids comparados fs {len(idf)} pg {len(idp)}: faltan en pg {len(sf - sp)}, sobran {len(sp - sf)}"
    if sf == sp and idf != idp:
        k = next(i for i, (a, b) in enumerate(zip(idf, idp)) if a != b)
        msg += f", orden distinto desde la posición {k}"
    return False, msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    a = ap.parse_args()
    manifest = json.load(open(os.path.join(a.dump, "manifest.json"), encoding="utf-8"))
    rt = manifest["readTime"]
    deadline = parse_ts(rt) + dt.timedelta(minutes=57)
    client = ps.PgClient(os.environ["PG_DSN"])
    prefix = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "prod")
    code_by_coll = collections.defaultdict(list)
    for c, *rest in CODE:
        code_by_coll[prefix + c].append(rest)

    totals = collections.Counter()
    by_family = collections.defaultdict(collections.Counter)
    diffs = []
    stopped = False
    print(f"readTime={rt}; límite para consultar Firestore: {deadline:%H:%M:%S}Z", flush=True)

    for cid in sorted(manifest["collections"]):
        if stopped:
            break
        if "_backup_" in cid:
            continue
        docs = {}
        with open(os.path.join(a.dump, manifest["collections"][cid]["file"]), encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                docs[rec["id"]] = rec.get("fields", {})
        if not docs:
            continue
        cases = []
        for filters, orders, limit, cursor in code_by_coll.get(cid, []):
            c = build_code_case(cid, filters, orders, limit, cursor, docs)
            if c is None:
                totals["sin datos"] += 1
                print(f"--  {cid} | caso de código sin datos para calcular el valor", flush=True)
            else:
                cases.append(c)
        cases += probe_cases(docs, profile(docs))

        def run(case, cursor_doc=None):
            if dt.datetime.now(dt.timezone.utc) > deadline:
                return case, "tiempo", None
            fs_cur = pg_cur = None
            if cursor_doc is not None:
                fields = docs[cursor_doc]
                vals = []
                for f, _ in case["orders"]:
                    if f != "__name__":
                        vals.append(fields[f])
                vals.append({"referenceValue": f"{fd.DB}/documents/{cid}/{cursor_doc}"})
                fs_cur = vals
                pg_cur = client.collection(cid).document(cursor_doc).get()  # como iter_all: snapshot
            try:
                fs = fs_run(cid, case, rt, fs_cur)
            except RuntimeError as e:
                return case, ("sin índice" if "FAILED_PRECONDITION" in str(e) or "index" in str(e).lower()
                              else "error firestore " + str(e)[:12]), None
            try:
                pg = pg_run(client, cid, case, pg_cur)
            except Exception as e:  # noqa: BLE001
                return case, f"error pg_store {type(e).__name__}", fs
            return case, compare(fs, pg), fs

        with cf.ThreadPoolExecutor(WORKERS) as ex:
            first = list(ex.map(run, cases))
            second = []
            for case, res, fs in first:
                if case["cursor"] and isinstance(res, tuple) and fs and len(fs[1]) > 1:
                    pos = min(CURSOR_POS, len(fs[1]) - 2)
                    c2 = dict(case, after=pos + 1)
                    second.append((c2, fs[1][pos]))
            second = list(ex.map(lambda t: run(*t), second))

        for case, res, _ in first + second:
            fam = op_family(case)
            label = f"{cid} | {case['kind']} | {describe(case)}"
            if res == "tiempo":
                stopped = True
                totals["sin tiempo"] += 1
                continue
            if isinstance(res, str):
                totals[res.split(" ")[0] if res.startswith("error") else res] += 1
                by_family[fam][res if not res.startswith("error") else "error"] += 1
                print(f"--  {label} | {res}", flush=True)
                if res.startswith("error"):
                    diffs.append(label + " | " + res)
                continue
            ok, msg = res
            totals["iguales" if ok else "DISTINTOS"] += 1
            by_family[fam]["iguales" if ok else "DISTINTOS"] += 1
            print(f"{'OK ' if ok else 'DIF'} {label} | {msg}", flush=True)
            if not ok:
                diffs.append(f"{label} | {msg}")

    print("\nResumen por forma de consulta:")
    for fam in sorted(by_family):
        print(f"  {fam:12s} " + ", ".join(f"{k} {v}" for k, v in sorted(by_family[fam].items())))
    print("Total: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))
    if stopped:
        print("AVISO: se agotó la hora de readTime; los casos restantes no se han comparado.")
    if diffs:
        print(f"\n{len(diffs)} casos con diferencias o errores:")
        for d in diffs:
            print("  " + d)
    print("SIN DIFERENCIAS" if not diffs and not stopped else "HAY DIFERENCIAS O CASOS SIN COMPARAR", flush=True)
    sys.exit(0 if not diffs and not stopped else 1)


if __name__ == "__main__":
    main()
