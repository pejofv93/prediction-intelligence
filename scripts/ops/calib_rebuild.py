"""Recalcula model_weights/current.accuracy_by_confidence SIN el resultado exacto.

Mismo universo que calib_breakdown.py (y que learning_engine): graduadas (correct True/False),
sin push ni stale_odds, creadas desde el reset v63 (2026-08-27 15:51 UTC), baloncesto solo hasta
2026-11-01; además, fuera market_type=correct_score (sin distinguir mayúsculas).

Por defecto, PASADA EN SECO: imprime los tramos antes y después. No escribe nada.
Con --escribir, en este orden y parando al primer fallo:
  1. copia el documento completo a model_weights_backup_20261109/current_<hora UTC> (create: falla
     si ya existe) y comprueba que la copia coincide con lo leído;
  2. actualiza SOLO accuracy_by_confidence de model_weights/current, con precondición sobre la
     hora de última escritura (si el aprendizaje nocturno lo cambió entre medias, falla sin escribir);
  3. relee y comprueba que el campo coincide y que ningún otro campo cambió.
Nunca imprime la copia; la colección no la lee ningún servicio.
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from calib_breakdown import BASKET_OUT, BUCKETS, RESET, as_dt, bucket, is_basket  # noqa: E402
from shared.firestore_client import col, get_client, iter_all  # noqa: E402

BACKUP_COLLECTION = "model_weights_backup_20261109"


def is_correct_score(p: dict) -> bool:
    return str(p.get("market_type") or "h2h").strip().lower() == "correct_score"


def rebuild() -> tuple[dict, int]:
    counts = defaultdict(lambda: [0, 0])
    excluded_cs = 0
    for d in iter_all("predictions"):
        p = d.to_dict() or {}
        if p.get("correct") not in (True, False) or p.get("push") or p.get("stale_odds"):
            continue
        created = as_dt(p.get("created_at"))
        b = bucket(float(p.get("confidence") or 0))
        if not created or created < RESET or not b:
            continue
        if is_basket(p) and created >= BASKET_OUT:
            continue
        if is_correct_score(p):
            excluded_cs += 1
            continue
        counts[b][0] += 1
        counts[b][1] += p["correct"] is True
    out = {b: {"count": n, "correct": k, "rate": round(k / n, 4) if n else None}
           for b, (n, k) in ((b, counts[b]) for b in BUCKETS)}
    return out, excluded_cs


def show(title: str, acc: dict) -> None:
    print(title)
    for b in BUCKETS:
        v = (acc or {}).get(b) or {}
        rate = v.get("rate")
        print(f"  {b}: n={v.get('count', 0):4d} aciertos={v.get('correct', 0):4d} "
              f"tasa={'—' if rate is None else f'{rate:.1%}'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--escribir", action="store_true")
    args = ap.parse_args()

    ref = col("model_weights").document("current")
    snap = ref.get()
    if not snap.exists:
        print("ERROR: model_weights/current no existe")
        return 1
    data = snap.to_dict() or {}
    print(f"model_weights/current: última escritura {snap.update_time}; {len(data)} campos\n")

    show("ANTES (contador actual):", data.get("accuracy_by_confidence"))
    new, excluded = rebuild()
    show(f"\nDESPUÉS (recalculado sin resultado exacto; {excluded} de resultado exacto fuera):", new)

    if not args.escribir:
        print(f"\nPasada en seco: no se ha escrito nada (la copia iría a {BACKUP_COLLECTION}).")
        return 0

    # 1. Copia antes de tocar nada
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bref = col(BACKUP_COLLECTION).document(f"current_{stamp}")
    bref.create(data)
    if (bref.get().to_dict() or {}) != data:
        print("ERROR: la copia no coincide con model_weights/current; no se escribe nada más")
        return 1
    print(f"\nCopia guardada en {BACKUP_COLLECTION}/current_{stamp} y comprobada.")

    # 2. Solo accuracy_by_confidence, si nadie ha escrito el documento desde la lectura
    ref.update({"accuracy_by_confidence": new},
               option=get_client().write_option(last_update_time=snap.update_time))

    # 3. Comprobación
    after = ref.get().to_dict() or {}
    ok = after.get("accuracy_by_confidence") == new
    otros = sorted(k for k in set(data) | set(after)
                   if k != "accuracy_by_confidence" and data.get(k) != after.get(k))
    print(f"Escrito accuracy_by_confidence: {'OK' if ok else 'NO COINCIDE'}; "
          f"otros campos cambiados: {otros or 'ninguno'}")
    return 0 if ok and not otros else 1


if __name__ == "__main__":
    sys.exit(main())
