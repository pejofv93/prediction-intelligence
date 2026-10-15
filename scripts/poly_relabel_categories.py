"""
scripts/poly_relabel_categories.py

Reetiqueta la categoría de las señales Polymarket antiguas con el categorizador
nuevo (services/polymarket-agent/categories.py: palabra completa + formatos de
partido). Sin esto el aprendizaje mezclaría etiquetas viejas (por subcadena) con
las nuevas: by_category y llm_bias_by_category se calculan por categoría.

Qué toca (solo los campos de categoría, con updateMask; el resto del doc no se reescribe):
  - shadow_trades (source polymarket*): category y signal_data.category
  - alerts_sent (type polymarket) que tengan category
  - poly_predictions: category (solo con --predictions)
y guarda el valor anterior en category_v1 para poder deshacerlo (--revert).

Orden: aplicar DESPUÉS de pasar el aprendizaje a alerts_sent y de desplegar el
categorizador, y antes del siguiente /run-learn.

Uso:
    python scripts/poly_relabel_categories.py                 # dry-run: cuenta cambios
    python scripts/poly_relabel_categories.py --apply         # escribe
    python scripts/poly_relabel_categories.py --apply --predictions
    python scripts/poly_relabel_categories.py --revert        # vuelve a category_v1

Requiere: gcloud auth print-access-token --account pejocanal@gmail.com activo.
"""
import argparse
import collections
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "polymarket-agent"))
sys.path.insert(0, os.path.dirname(__file__))

from categories import categorize_market  # noqa: E402
import poly_real_roi as p  # noqa: E402  (token, lectura paginada, decodificación)

_ROOT = f"projects/{p.PROJECT}/databases/(default)/documents"
_PREFIX = "prod"


def _post_batch(writes: list[dict]) -> None:
    url = p.BASE.rsplit("/documents", 1)[0] + "/documents:batchWrite"
    for i in range(0, len(writes), 200):
        rq = urllib.request.Request(
            url, data=json.dumps({"writes": writes[i:i + 200]}).encode(), method="POST",
            headers={"Authorization": f"Bearer {p._tok()}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(rq, timeout=120, context=p._ctx) as r:
            res = json.loads(r.read())
        bad = [s for s in res.get("status", []) if s.get("code")]
        if bad:
            raise RuntimeError(f"batchWrite con errores: {bad[:3]}")


def _write(coll: str, doc_id: str, fields: dict, mask: list[str]) -> dict:
    return {
        "update": {"name": f"{_ROOT}/{_PREFIX}{coll}/{urllib.parse.quote(doc_id, safe='')}", "fields": fields},
        "updateMask": {"fieldPaths": mask},
        "currentDocument": {"exists": True},
    }


def _s(v) -> dict:
    return {"stringValue": v} if v is not None else {"nullValue": None}


def _plan(include_predictions: bool) -> list[tuple]:
    """(colección, doc_id, cat_vieja, cat_nueva, pregunta, tiene_signal_data)"""
    plan = []
    for d in p._list(_PREFIX + "shadow_trades"):
        t = p._fields(d)
        if not str(t.get("source", "")).startswith("polymarket"):
            continue
        sd = t.get("signal_data") or {}
        q = sd.get("question") or t.get("market") or ""
        plan.append(("shadow_trades", d["name"].split("/")[-1], t.get("category"), categorize_market(q), q, bool(sd)))
    for d in p._list(_PREFIX + "alerts_sent"):
        a = p._fields(d)
        if a.get("type") != "polymarket" or not a.get("question"):
            continue
        plan.append(("alerts_sent", d["name"].split("/")[-1], a.get("category"),
                     categorize_market(a["question"]), a["question"], False))
    if include_predictions:
        for d in p._list(_PREFIX + "poly_predictions"):
            f = p._fields(d)
            q = f.get("question") or ""
            plan.append(("poly_predictions", d["name"].split("/")[-1], f.get("category"),
                         categorize_market(q), q, False))
    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--predictions", action="store_true")
    args = ap.parse_args()

    if args.revert:
        writes = []
        for coll in ("shadow_trades", "alerts_sent", "poly_predictions"):
            for d in p._list(_PREFIX + coll):
                f = p._fields(d)
                if "category_v1" not in f:
                    continue
                fields = {"category": _s(f["category_v1"])}
                mask = ["category", "category_v1"]
                if coll == "shadow_trades" and f.get("signal_data"):
                    fields["signal_data"] = {"mapValue": {"fields": {"category": _s(f["category_v1"])}}}
                    mask.append("signal_data.category")
                writes.append(_write(coll, d["name"].split("/")[-1], fields, mask))
        print(f"revert: {len(writes)} docs")
        if writes:
            _post_batch(writes)
        return

    plan = _plan(args.predictions)
    changes = [x for x in plan if (x[2] or "other") != x[3]]
    by_coll = collections.Counter(x[0] for x in plan)
    ch_coll = collections.Counter(x[0] for x in changes)
    print("docs revisados:", dict(by_coll))
    print("cambian:", dict(ch_coll))
    for coll in ch_coll:
        print(f"\n[{coll}]")
        for (o, n), c in collections.Counter((x[2], x[3]) for x in changes if x[0] == coll).most_common(15):
            print(f"  {str(o):12s} → {n:12s} {c}")

    if not args.apply:
        print("\n(dry-run: no se ha escrito nada; --apply para escribir)")
        return

    writes = []
    for coll, doc_id, old, new, _q, has_sd in changes:
        fields = {"category": _s(new), "category_v1": _s(old)}
        mask = ["category", "category_v1"]
        if has_sd:
            fields["signal_data"] = {"mapValue": {"fields": {"category": _s(new)}}}
            mask.append("signal_data.category")
        writes.append(_write(coll, doc_id, fields, mask))
    _post_batch(writes)
    print(f"\nescritos {len(writes)} docs (valor anterior en category_v1)")


if __name__ == "__main__":
    main()
