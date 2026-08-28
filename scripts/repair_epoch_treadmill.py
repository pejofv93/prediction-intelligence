"""
repair_epoch_treadmill.py — repara el daño de la "cinta de correr" del rebuild de ELO.

Contexto
--------
rebuild-elo.yml corría cada noche `rebuild_elo.py --confirm` completo:
  - reset_weights() ponía model_weights/current.total_predictions / .correct_predictions
    a 0 y limpiaba accuracy_by_*;
  - se reescribía model_weights/elo_rebuild.rebuilt_at = ahora.
El learning engine (02:00) usa rebuilt_at como frontera de época: toda predicción con
created_at < rebuilt_at queda como model_epoch=1 (legacy) y NO cuenta para pesos,
accuracy ni los contadores. Al mover rebuilt_at al presente cada noche, TODA señal de
más de ~1 día quedaba marcada legacy indebidamente y los contadores no volvían a subir.

Las noches del 26 y 27 de agosto (y la siembra del 25) marcaron como legacy predicciones
creadas DESPUÉS del último rebuild estructural real (2026-08-25). No son legacy: son
víctimas del treadmill.

Este script (idempotente, dry-run por defecto):
  1. Congela model_weights/elo_rebuild.rebuilt_at en 2026-08-25T00:00:00Z.
  2. Congela accuracy_log/*.elo_rebuild_at en la misma fecha.
  3. Rescata las predicciones marcadas legacy indebidamente (created_at >= 2026-08-25,
     model_epoch=1 / excluded_from_learning, SIN regrade_note): model_epoch=2 y se borra
     excluded_from_learning.
  4. Recalcula model_weights/current.total_predictions / .correct_predictions desde un
     barrido completo de predictions, CIEGO A LA ÉPOCA (una apuesta graduada es un
     resultado real). Coherente con el nuevo learning_engine.
  5. Recalcula accuracy_log de las semanas afectadas (--weeks, por defecto W34..W36)
     desde predictions.created_at, ciego a la época.
  6. Repara closed_at / regraded_at que quedaron como string en shadow_trades (rompían
     calculate_metrics → bankroll 50 €, ROI 0 %).

Uso:
    python scripts/repair_epoch_treadmill.py                 # dry-run (no escribe)
    python scripts/repair_epoch_treadmill.py --apply         # escribe
    python scripts/repair_epoch_treadmill.py --apply --weeks 2026-W33,2026-W34,2026-W35,2026-W36

Requiere:
    gcloud auth print-access-token  (cuenta con acceso a Firestore del proyecto)
    env GOOGLE_CLOUD_PROJECT (default prediction-intelligence)
    env FIRESTORE_COLLECTION_PREFIX (default prod)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

_BASE = "https://firestore.googleapis.com/v1"
_TOKEN_TTL = 45 * 60

# Frontera de época fija — DEBE coincidir con
# services/sports-agent/learner/learning_engine.py :: ELO_STRUCTURAL_REBUILD_AT
STRUCTURAL_REBUILD_AT = datetime(2026, 8, 25, tzinfo=timezone.utc)
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


# ── Firestore REST mínimo ────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    """Contexto TLS. En el puesto con Norton, urllib no confía en la CA del proxy:
    se usa SSL_CERT_FILE / REQUESTS_CA_BUNDLE o el custom_ca_certs_file de gcloud.
    En CI no hay ninguno → contexto por defecto."""
    for env in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        p = os.environ.get(env)
        if p and os.path.exists(p):
            return ssl.create_default_context(cafile=p)
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if exe:
        try:
            out = subprocess.run([exe, "config", "get-value", "core/custom_ca_certs_file"],
                                 capture_output=True, text=True, timeout=15)
            p = out.stdout.strip()
            if p and p not in ("", "(unset)") and os.path.exists(p):
                return ssl.create_default_context(cafile=p)
        except Exception:
            pass
    return ssl.create_default_context()


class Fs:
    def __init__(self, project: str, prefix: str, account: str | None):
        self.project, self.prefix, self.account = project, prefix, account
        self._tok, self._tok_at = "", 0.0
        self._ctx = _ssl_ctx()

    def _token(self) -> str:
        if self._tok and (time.time() - self._tok_at) < _TOKEN_TTL:
            return self._tok
        exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not exe:
            sys.exit("gcloud no está en el PATH")
        cmd = [exe, "auth", "print-access-token"]
        if self.account:
            cmd += ["--account", self.account]
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            sys.exit(f"gcloud auth print-access-token falló: {out.stderr[:300]}")
        self._tok, self._tok_at = out.stdout.strip(), time.time()
        return self._tok

    def _req(self, url: str, method: str = "GET", body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", f"Bearer {self._token()}")
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=90, context=self._ctx) as resp:
            return json.loads(resp.read().decode() or "{}")

    def _docs_url(self, path: str) -> str:
        return f"{_BASE}/projects/{self.project}/databases/(default)/documents{path}"

    def get(self, coll: str, doc_id: str) -> dict | None:
        url = self._docs_url(f"/{self.prefix}{coll}/{urllib.parse.quote(str(doc_id), safe='')}")
        try:
            return self._req(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def scan(self, coll: str):
        token = ""
        while True:
            q = "?pageSize=300" + (f"&pageToken={token}" if token else "")
            page = self._req(self._docs_url(f"/{self.prefix}{coll}{q}"))
            for d in page.get("documents", []):
                yield d["name"].split("/")[-1], d.get("fields", {})
            token = page.get("nextPageToken", "")
            if not token:
                return

    def patch(self, coll: str, doc_id: str, set_fields: dict, delete_fields: list[str] | None = None):
        """PATCH parcial: solo toca los campos nombrados (updateMask). delete_fields se van
        en la máscara pero no en el body → Firestore los borra."""
        delete_fields = delete_fields or []
        paths = list(set_fields) + delete_fields
        mask = "&".join(f"updateMask.fieldPaths={urllib.parse.quote(p)}" for p in paths)
        url = self._docs_url(
            f"/{self.prefix}{coll}/{urllib.parse.quote(str(doc_id), safe='')}?{mask}"
        )
        self._req(url, "PATCH", {"fields": {k: _enc(v) for k, v in set_fields.items()}})


def _enc(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return {"timestampValue": dt.astimezone(timezone.utc).strftime(_TS_FMT)}
    return {"stringValue": str(v)}


def _dec(v):
    kind, raw = next(iter(v.items()))
    if kind == "integerValue":
        return int(raw)
    if kind == "doubleValue":
        return float(raw)
    if kind == "booleanValue":
        return bool(raw)
    if kind == "nullValue":
        return None
    return raw  # timestampValue / stringValue → str


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_week_range(label: str) -> tuple[datetime, datetime]:
    year, wk = label.split("-W")
    monday = datetime.fromisocalendar(int(year), int(wk), 1).replace(tzinfo=timezone.utc)
    return monday, monday + timedelta(days=7)


# ── Pasos ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Repara el daño de la cinta de correr del rebuild de ELO "
                    "(ver docstring del módulo).")
    ap.add_argument("--apply", action="store_true", help="escribir (por defecto: dry-run)")
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", "prediction-intelligence"))
    ap.add_argument("--prefix", default=os.environ.get("FIRESTORE_COLLECTION_PREFIX", "prod"))
    ap.add_argument("--account", default=os.environ.get("GCLOUD_ACCOUNT"))
    ap.add_argument("--weeks", default="2026-W34,2026-W35,2026-W36",
                    help="semanas ISO de accuracy_log a recomputar (coma-separadas)")
    args = ap.parse_args()

    fs = Fs(args.project, args.prefix, args.account)
    now = datetime.now(timezone.utc)
    W = "APLICANDO" if args.apply else "DRY-RUN (no escribe)"
    print(f"=== repair_epoch_treadmill — {W} ===")
    print(f"    proyecto {args.project} · prefijo {args.prefix} · frontera {STRUCTURAL_REBUILD_AT.isoformat()}\n")

    # ── 1. elo_rebuild.rebuilt_at ────────────────────────────────────────────
    er = fs.get("model_weights", "elo_rebuild")
    cur_rb = _parse_dt(_dec(er["fields"]["rebuilt_at"])) if er and "rebuilt_at" in er.get("fields", {}) else None
    print(f"1. model_weights/elo_rebuild.rebuilt_at actual: {cur_rb.isoformat() if cur_rb else '—'}")
    if cur_rb != STRUCTURAL_REBUILD_AT:
        print(f"   → congelar en {STRUCTURAL_REBUILD_AT.isoformat()}")
        if args.apply:
            fs.patch("model_weights", "elo_rebuild", {
                "rebuilt_at": STRUCTURAL_REBUILD_AT,
                "frozen_note": "congelado por repair_epoch_treadmill.py — la frontera de época "
                               "ahora es constante en learning_engine.ELO_STRUCTURAL_REBUILD_AT",
                "frozen_at": now,
            })
    else:
        print("   ya correcto.")

    # ── 2. accuracy_log/*.elo_rebuild_at ────────────────────────────────────
    print("\n2. accuracy_log/*.elo_rebuild_at")
    fixed = 0
    for doc_id, f in fs.scan("accuracy_log"):
        if "elo_rebuild_at" not in f:
            continue
        val = _parse_dt(_dec(f["elo_rebuild_at"]))
        if val != STRUCTURAL_REBUILD_AT:
            print(f"   {doc_id}: {val.isoformat() if val else '—'} → {STRUCTURAL_REBUILD_AT.date()}")
            if args.apply:
                fs.patch("accuracy_log", doc_id, {"elo_rebuild_at": STRUCTURAL_REBUILD_AT})
            fixed += 1
    print(f"   {fixed} fila(s) a corregir.")

    # ── barrido único de predictions (reutilizado por pasos 3-5) ─────────────
    print("\n   barriendo predictions...")
    preds: list[tuple[str, dict]] = list(fs.scan("predictions"))
    print(f"   {len(preds)} predicciones leídas.")

    # ── 3. rescatar víctimas del treadmill ─────────────────────────────────
    print("\n3. predicciones marcadas legacy indebidamente (created_at >= frontera)")
    victims = []
    for doc_id, f in preds:
        model_epoch = _dec(f["model_epoch"]) if "model_epoch" in f else None
        excluded = _dec(f["excluded_from_learning"]) if "excluded_from_learning" in f else None
        if model_epoch != 1 and excluded is not True:
            continue
        if "regrade_note" in f:  # backfill legítimo de alt-markets (mayo/junio)
            continue
        created = _parse_dt(_dec(f["created_at"])) if "created_at" in f else None
        if created is None or created < STRUCTURAL_REBUILD_AT:
            continue
        victims.append((doc_id, created, f))

    for doc_id, created, f in sorted(victims, key=lambda x: x[1]):
        mkt = _dec(f.get("market_type", {"stringValue": "?"}))
        corr = _dec(f["correct"]) if "correct" in f else None
        print(f"   {created.isoformat()}  {doc_id[:34]:<34}  {str(mkt):<16}  correct={corr}")
    print(f"   {len(victims)} víctima(s) → model_epoch=2, se borra excluded_from_learning.")
    if args.apply:
        for doc_id, _created, _f in victims:
            fs.patch("predictions", doc_id,
                     {"model_epoch": 2, "epoch_restored_at": now,
                      "epoch_restored_note": "repair_epoch_treadmill.py — creada tras el rebuild "
                                             "estructural del 2026-08-25, no es legacy"},
                     delete_fields=["excluded_from_learning"])

    # ── 4. model_weights/current.total_predictions / .correct_predictions ───
    resolved = [(i, f) for i, f in preds
                if "correct" in f and _dec(f["correct"]) is not None
                and not (("push" in f) and _dec(f["push"]) is True)]
    total = len(resolved)
    correct = sum(1 for _i, f in resolved if _dec(f["correct"]) is True)
    cw = fs.get("model_weights", "current")
    cf = cw.get("fields", {}) if cw else {}
    old_total = _dec(cf["total_predictions"]) if "total_predictions" in cf else 0
    old_correct = _dec(cf["correct_predictions"]) if "correct_predictions" in cf else 0
    print("\n4. model_weights/current — contadores de por vida (ciegos a época)")
    print(f"   total_predictions   {old_total} → {total}")
    _acc = f"  ({correct / total * 100:.1f}% acc)" if total else ""
    print(f"   correct_predictions {old_correct} → {correct}{_acc}")
    if args.apply:
        fs.patch("model_weights", "current", {
            "total_predictions": total,
            "correct_predictions": correct,
            "recounted_at": now,
            "recount_note": "repair_epoch_treadmill.py — recuento ciego a época desde predictions",
        })

    # ── 5. accuracy_log semanas afectadas ─────────────────────────────────
    print("\n5. accuracy_log — recuento por semana (created_at, ciego a época)")
    for wk in [w.strip() for w in args.weeks.split(",") if w.strip()]:
        start, end = _iso_week_range(wk)
        wk_res = [f for _i, f in resolved
                  if (c := _parse_dt(_dec(f["created_at"]) if "created_at" in f else None))
                  and start <= c < end]
        wk_total = len(wk_res)
        wk_correct = sum(1 for f in wk_res if _dec(f["correct"]) is True)
        wk_acc = round(wk_correct / wk_total, 4) if wk_total else 0.0
        doc = fs.get("accuracy_log", wk)
        if not doc:
            print(f"   {wk}: doc no existe — se omite (recuento sería {wk_correct}/{wk_total})")
            continue
        df = doc.get("fields", {})
        o_t = _dec(df["predictions_total"]) if "predictions_total" in df else 0
        o_c = _dec(df["predictions_correct"]) if "predictions_correct" in df else 0
        print(f"   {wk}: total {o_t}→{wk_total}  correct {o_c}→{wk_correct}  acc={wk_acc:.1%}")
        if args.apply:
            fs.patch("accuracy_log", wk, {
                "predictions_total": wk_total,
                "predictions_correct": wk_correct,
                "accuracy": wk_acc,
                "recount_note": "repair_epoch_treadmill.py — recuento ciego a época desde predictions.created_at",
                "recounted_at": now,
            })

    # ── 6. shadow_trades closed_at / regraded_at string → timestamp ────────
    print("\n6. shadow_trades — closed_at / regraded_at guardados como string")
    bad = 0
    for doc_id, f in fs.scan("shadow_trades"):
        if doc_id == "retroactive_done":
            continue
        fix: dict = {}
        for k in ("closed_at", "regraded_at", "opened_at"):
            if k in f and "stringValue" in f[k]:
                dt = _parse_dt(f[k]["stringValue"])
                if dt:
                    fix[k] = dt
        if fix:
            bad += 1
            print(f"   {doc_id[:38]:<38}  {', '.join(fix)}")
            if args.apply:
                fs.patch("shadow_trades", doc_id, fix)
    print(f"   {bad} shadow_trade(s) con timestamp string.")

    print(f"\n=== {'HECHO' if args.apply else 'DRY-RUN — re-ejecutar con --apply'} ===")


if __name__ == "__main__":
    main()
