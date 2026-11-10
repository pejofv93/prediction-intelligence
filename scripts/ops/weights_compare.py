"""
Solo lectura: compara model_weights/current con la copia del 9-nov
(model_weights_backup_20261109/<doc>). Imprime campos, versión y, de cada campo numérico o
diccionario de números, lo que ha cambiado. No escribe nada.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from shared.firestore_client import col  # noqa: E402


def flat(d, prefix=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flat(v, key + "."))
        else:
            out[key] = v
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-doc", default="current_20261109T123204Z")
    ap.add_argument("--backup-collection", default="model_weights_backup_20261109")
    a = ap.parse_args()
    cur_snap = col("model_weights").document("current").get()
    old_snap = col(a.backup_collection).document(a.backup_doc).get()
    if not cur_snap.exists or not old_snap.exists:
        sys.exit(f"falta un documento: current={cur_snap.exists} copia={old_snap.exists}")
    cur, old = cur_snap.to_dict(), old_snap.to_dict()
    print(f"current: {len(cur)} campos; última escritura {cur_snap.update_time}")
    print("  " + ", ".join(sorted(cur)))
    print(f"copia {a.backup_doc}: {len(old)} campos")
    print(f"campos nuevos: {sorted(set(cur) - set(old)) or 'ninguno'}; "
          f"desaparecidos: {sorted(set(old) - set(cur)) or 'ninguno'}")
    print(f"versión: {old.get('version')} → {cur.get('version')}")
    for field in sorted(set(cur) | set(old)):
        fo, fc = flat({field: old.get(field)}), flat({field: cur.get(field)})
        changed = [(k, fo.get(k), fc.get(k)) for k in sorted(set(fo) | set(fc)) if fo.get(k) != fc.get(k)]
        if not changed:
            print(f"= {field}: sin cambios")
            continue
        print(f"≠ {field}: {len(changed)} valores cambian")
        for k, o, c in changed[:60]:
            if isinstance(o, (int, float)) and isinstance(c, (int, float)) and not isinstance(o, bool):
                print(f"    {k}: {o} → {c} ({c - o:+.4g})")
            else:
                so, sc = str(o), str(c)
                print(f"    {k}: {so[:80]} → {sc[:80]}")
        if len(changed) > 60:
            print(f"    … y {len(changed) - 60} más")


if __name__ == "__main__":
    main()
