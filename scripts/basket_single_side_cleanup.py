"""
Migra las señales de baloncesto a UN doc por partido y mercado y siembra los slots de
dedup nuevos del bot. Acompaña al cambio de basketball_analyzer / alert_manager.

Contexto (2026-10-02): cada lado tenía doc propio (`{id}_ml_home` / `{id}_ml_away`,
`{id}_tot_over` / `{id}_tot_under`, igual en H1/Q1) y los dos sobrevivían entre
ejecuciones. Besiktas–Barça quedó con los dos lados alertados; Hapoel–Real Madrid y
Paris–ASVEL con ambos lados guardados. Y el grader (_dedup_pending) copia el `correct`
del primario a los duplicados del mismo partido+mercado: con lados contrarios, el lado
perdedor se gradúa como acierto.

Fases (todo dry-run salvo --apply):

  --phase locks   ANTES de desplegar. Para cada partido de baloncesto aún por jugar,
                  crea en alerts_sent el slot nuevo (bball_{home}_vs_{away}_{día}_{mercado})
                  a partir de la última alerta enviada con la clave antigua. Sin esto el
                  primer analyze tras el deploy reenviaría todas las señales vivas (la
                  clave nueva no existiría). Los slots nuevos no los lee el código viejo.

  --phase docs    DESPUÉS de desplegar (el código viejo volvería a crear _ml_home/_ml_away).
                  Por partido y familia de mercado pendiente (result=None):
                    - se queda UN doc con el id nuevo ({id}_ml, {id}_tot, {id}_h1_tot,
                      {id}_q1_tot): el lado alertado más reciente si lo hubo (marcado
                      alerted=True); si no, el de mayor EV;
                    - el resto se borra (son los "ambos lados" que contaminan el grader);
                      los lados contrarios se copian antes a `predictions_superseded`
                      (auditoría: ningún servicio la lee).
                  Con --historical, además, en partidos YA graduados con ambos lados se
                  borra el lado que nunca se alertó (si ninguno o los dos se alertaron,
                  solo se informa).

Uso:
  python scripts/basket_single_side_cleanup.py --phase locks              # dry-run
  python scripts/basket_single_side_cleanup.py --phase locks --apply
  python scripts/basket_single_side_cleanup.py --phase docs [--historical] [--apply]

Auth: REST con token de gcloud (ver scripts/_fsrest.py). En local: Python 3.11 y
SSL_CERT_FILE apuntando a la CA de Norton.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fsrest import RestDB, decode, encode  # noqa: E402

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "prediction-intelligence")
PREFIX = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "prod")
ACCOUNT = os.environ.get("GCLOUD_ACCOUNT", "pejocanal@gmail.com")

# familia → (sufijos antiguos por lado, sufijo nuevo)
_FAMILIES = {
    "ml":     (("ml_home", "ml_away"), "ml"),
    "tot":    (("tot_over", "tot_under"), "tot"),
    "h1_tot": (("h1_tot_over", "h1_tot_under"), "h1_tot"),
    "q1_tot": (("q1_tot_over", "q1_tot_under"), "q1_tot"),
}
_SFX_TO_FAMILY = {s: fam for fam, (old, new) in _FAMILIES.items() for s in (*old, new)}
_DOC_RE = re.compile(r"^(?P<base>.+?)_(?P<sfx>" + "|".join(
    sorted(_SFX_TO_FAMILY, key=len, reverse=True)) + r")$")
_LINE_RE = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*(H1|Q1)?\s*$", re.IGNORECASE)
_MARKETS = ("h2h", "spread", "totals", "basketball_h1_spread",
            "basketball_h1_totals", "basketball_q1_totals")
_ALERT_LOOKBACK = timedelta(days=10)   # alertas antiguas que cuentan para un partido


# ── helpers (espejo de alert_manager para que las claves coincidan) ──────────

def side_and_line(selection: str) -> tuple[str, float | None]:
    sel = str(selection or "").strip()
    m = _LINE_RE.search(sel)
    if not m:
        return sel.lower(), None
    return sel[:m.start()].strip().lower(), float(m.group(1))


def safe_doc_id(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(key))[:500]


def parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    if len(s) == 10:
        s += "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def run_query(db: RestDB, col: str, field: str, value: str) -> list[tuple[str, dict]]:
    """[(doc_id, campos_crudos)] — crudos para reescribir sin cambiar tipos."""
    sq = {"from": [{"collectionId": f"{PREFIX}{col}"}],
          "where": {"fieldFilter": {"field": {"fieldPath": field}, "op": "EQUAL",
                                    "value": {"stringValue": value}}}}
    out = []
    for r in db._call(":runQuery", "POST", {"structuredQuery": sq}):
        d = r.get("document")
        if d:
            out.append((d["name"].split("/")[-1], d.get("fields", {})))
    return out


def raw_write(db: RestDB, col: str, docs: dict[str, dict]) -> None:
    items = list(docs.items())
    for i in range(0, len(items), 200):
        writes = [{"update": {"name": db._doc_name(col, did), "fields": fields}}
                  for did, fields in items[i:i + 200]]
        db._call(":batchWrite", "POST", {"writes": writes})


def doc_exists(db: RestDB, col: str, doc_id: str) -> bool:
    import urllib.error
    import urllib.parse
    try:
        db._call(f"/{PREFIX}{col}/{urllib.parse.quote(doc_id, safe='')}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


# ── carga ────────────────────────────────────────────────────────────────────

def load(db: RestDB):
    preds = []
    for sport in ("basketball", "nba"):
        for did, raw in run_query(db, "predictions", "sport", sport):
            rec = {k: decode(v) for k, v in raw.items()}
            rec["_id"], rec["_raw"] = did, raw
            preds.append(rec)
    legacy = []   # alertas enviadas con la clave antigua
    for did, raw in run_query(db, "alerts_sent", "type", "sports"):
        rec = {k: decode(v) for k, v in raw.items()}
        if "_vs_" in str(rec.get("alert_key", "")):
            rec["_raw_sent_at"] = raw.get("sent_at")
            legacy.append(rec)
    return preds, legacy


def alerts_for(legacy: list[dict], p: dict, market: str) -> list[dict]:
    """Alertas antiguas de este partido+mercado, más reciente primero, con side/line."""
    home = str(p.get("home_team") or "").lower().strip()
    away = str(p.get("away_team") or "").lower().strip()
    prefix = f"{home}_vs_{away}_{market}_"
    kickoff = parse_ts(p.get("match_date"))
    out = []
    for a in legacy:
        key = str(a.get("alert_key", ""))
        if not key.startswith(prefix):
            continue
        sent = parse_ts(a.get("sent_at"))
        if kickoff and sent and not (kickoff - _ALERT_LOOKBACK <= sent <= kickoff + timedelta(hours=6)):
            continue   # alerta de otro cruce de los mismos equipos
        selection = key[len(prefix):]
        side, line = side_and_line(selection)
        out.append({**a, "selection": selection, "side": side, "line": line, "_sent": sent})
    out.sort(key=lambda a: a["_sent"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


# ── fase locks ───────────────────────────────────────────────────────────────

def phase_locks(db: RestDB, preds: list[dict], legacy: list[dict], apply: bool) -> None:
    now = datetime.now(timezone.utc)
    matches: dict[tuple, list[dict]] = defaultdict(list)
    for p in preds:
        ko = parse_ts(p.get("match_date"))
        if p.get("result") is None and ko and ko > now:
            day = str(p.get("match_date"))[:10]
            matches[(str(p["home_team"]).lower().strip(), str(p["away_team"]).lower().strip(), day)].append(p)

    to_write: dict[str, dict] = {}
    for (home, away, day), docs in sorted(matches.items(), key=lambda x: x[0][2]):
        for market in _MARKETS:
            alerts = alerts_for(legacy, docs[0], market)
            if not alerts:
                continue
            last = alerts[0]
            key = f"bball_{home}_vs_{away}_{day}_{market}"
            slot = safe_doc_id(key)
            if doc_exists(db, "alerts_sent", slot):
                print(f"  = {key}: slot ya existe")
                continue
            # cuota/EV de referencia solo si el doc guardado es exactamente lo enviado: misma
            # selección y escrito en la misma pasada que la alerta (el doc se reescribe en
            # cada analyze; Maccabi se envió @5.75 y el doc ya dice 5.25). Si no, el slot
            # queda sin referencia y no admite reenvío.
            def _same_run(d: dict) -> bool:
                c = parse_ts(d.get("created_at"))
                return bool(c and last["_sent"] and abs((last["_sent"] - c).total_seconds()) <= 120)
            same = next((d for d in docs if (d.get("market_type") or "h2h") == market
                         and d.get("selection") == last["selection"] and _same_run(d)), None)
            odds = float(same["odds"]) if same and same.get("odds") is not None else None
            ev = float(same["ev"]) if same and same.get("ev") is not None else None
            fields = {k: encode(v) for k, v in {
                "alert_key": key, "type": "sports_bball", "side": last["side"],
                "line": last["line"], "odds": odds, "ev": ev, "resends": 0,
                "selection": last["selection"], "match_date": str(docs[0].get("match_date")),
                "seeded_from": last["alert_key"],
            }.items()}
            fields["sent_at"] = last["_raw_sent_at"]
            to_write[slot] = fields
            print(f"  + {key}: '{last['selection']}' enviada {str(last.get('sent_at'))[:16]}"
                  f" | cuota ref {odds if odds is not None else '—'}")
    print(f"\nlocks: {len(to_write)} slots a crear")
    if apply and to_write:
        raw_write(db, "alerts_sent", to_write)
        print("  escritos.")


# ── fase docs ────────────────────────────────────────────────────────────────

def phase_docs(db: RestDB, preds: list[dict], legacy: list[dict], apply: bool, historical: bool) -> None:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in preds:
        m = _DOC_RE.match(p["_id"])
        if m:
            groups[(m.group("base"), _SFX_TO_FAMILY[m.group("sfx")])].append(p)

    to_write: dict[str, dict] = {}
    to_delete: list[str] = []
    to_archive: dict[str, dict] = {}
    for (base, fam), docs in sorted(groups.items()):
        new_id = f"{base}_{_FAMILIES[fam][1]}"
        pending = [d for d in docs if d.get("result") is None]
        graded = [d for d in docs if d.get("result") is not None]
        market = docs[0].get("market_type") or "h2h"
        alerts = alerts_for(legacy, docs[0], market)
        alerted_sides = [a["side"] for a in alerts]
        alerted_sides += [side_and_line(d.get("selection"))[0] for d in docs if d.get("alerted")]
        label = f"{base} [{fam}] {docs[0].get('home_team')} vs {docs[0].get('away_team')}"

        if pending:
            if alerts:
                want = alerts[0]["side"]
                keeper = next((d for d in pending if side_and_line(d.get("selection"))[0] == want), None)
            else:
                keeper = None
            if keeper is None:
                keeper = max(pending, key=lambda d: (float(d.get("ev") or 0), d["_id"] == new_id))
            k_side = side_and_line(keeper.get("selection"))[0]
            is_alerted = k_side in alerted_sides
            others = [d for d in pending if d is not keeper]
            if keeper["_id"] == new_id and not others and bool(keeper.get("alerted")) == is_alerted:
                continue   # ya migrado
            fields = dict(keeper["_raw"])
            fields["match_id"] = encode(new_id)
            if is_alerted:
                fields["alerted"] = encode(True)
            to_write[new_id] = fields
            dels = [d["_id"] for d in pending if d["_id"] != new_id]
            to_delete += dels
            # Lado contrario retirado → copia íntegra en la colección de auditoría antes de
            # borrarlo. No se marca en `predictions`: shadow_engine cuenta como pérdida
            # cualquier doc con result != None y correct falso.
            for d in others:
                if side_and_line(d.get("selection"))[0] == k_side:
                    continue
                arch = dict(d["_raw"])
                arch.update({k: encode(v) for k, v in {
                    "superseded_by": new_id,
                    "superseded_at": datetime.now(timezone.utc),
                    "superseded_reason": "lado contrario del mismo partido (migración a doc único)",
                    "was_alerted": side_and_line(d.get("selection"))[0] in alerted_sides,
                    "original_doc_id": d["_id"],
                }.items()})
                to_archive[d["_id"]] = arch
            print(f"  {label}\n      se queda '{keeper.get('selection')}' @{keeper.get('odds')} "
                  f"ev={keeper.get('ev')} ({keeper['_id']} → {new_id}"
                  f"{', alertada' if is_alerted else ''})")
            for d in others:
                print(f"      BORRA '{d.get('selection')}' @{d.get('odds')} ev={d.get('ev')} ({d['_id']})"
                      f"{' — lado contrario → ARCHIVO predictions_superseded' if side_and_line(d.get('selection'))[0] != k_side else ''}")

        sides = {side_and_line(d.get("selection"))[0] for d in graded}
        if len(graded) > 1 and len(sides) > 1:
            never = [d for d in graded if side_and_line(d.get("selection"))[0] not in alerted_sides]
            if never and len(never) < len(graded):
                print(f"  [graduado] {label}: lado nunca alertado → "
                      + ", ".join(f"'{d.get('selection')}' ({d['_id']}, {d.get('result')}, correct={d.get('correct')})" for d in never)
                      + ("" if historical else "  (usa --historical para borrarlo)"))
                if historical:
                    to_delete += [d["_id"] for d in never]
            else:
                print(f"  [graduado] {label}: ambos lados {'alertados' if not never else 'sin alertar'} "
                      f"— ambiguo, no se toca: " + ", ".join(f"'{d.get('selection')}'={d.get('result')}" for d in graded))

    print(f"\ndocs: {len(to_write)} a escribir con id nuevo, {len(to_delete)} a borrar, "
          f"{len(to_archive)} lados contrarios archivados en predictions_superseded")
    if apply:
        if to_archive:   # primero el archivo: si algo falla después, no se pierde nada
            raw_write(db, "predictions_superseded", to_archive)
        if to_write:
            raw_write(db, "predictions", to_write)
        if to_delete:
            db.delete_docs("predictions", to_delete)
        print("  aplicado.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=("locks", "docs"), required=True)
    ap.add_argument("--apply", action="store_true", help="escribe/borra (por defecto: dry-run)")
    ap.add_argument("--historical", action="store_true",
                    help="docs: borra también el lado nunca alertado en partidos ya graduados")
    args = ap.parse_args()
    db = RestDB(PROJECT, PREFIX, ACCOUNT)
    preds, legacy = load(db)
    print(f"{len(preds)} predicciones de baloncesto · {len(legacy)} alertas con clave antigua"
          f" · {'APPLY' if args.apply else 'dry-run'}\n")
    if args.phase == "locks":
        phase_locks(db, preds, legacy, args.apply)
    else:
        phase_docs(db, preds, legacy, args.apply, args.historical)


if __name__ == "__main__":
    main()
