"""Inventario de Artifact Registry frente a Cloud Run + simulación de limpieza. SOLO LECTURA.

Entrada: un directorio con los JSON que vuelca ar-inventory.yml:
  repos.json             gcloud artifacts repositories list
  images_<repo>.json     gcloud artifacts docker images list ... --include-tags
  revisions.json         gcloud run revisions list
  services.json          gcloud run services list

Salida (log público): por imagen, digest corto, tamaño, fecha, etiquetas y revisiones que la usan.
Después simula la política propuesta (no borra nada):
  conservar las KEEP_RECENT más recientes de cada imagen
  + las protegidas: digest de una revisión con tráfico, con etiqueta de tráfico (canary) o la última lista
  el resto: candidata a borrar.
"""
import collections
import json
import os
import sys

KEEP_RECENT = int(os.environ.get("KEEP_RECENT", "3"))
CANARY = os.environ.get("CANARY_REVISION_SUFFIX", "00370-biq")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        return json.loads(t) if t else default
    except FileNotFoundError:
        return default


def short(digest):
    return digest.split("sha256:")[-1][:12] if digest else "?"


def gb(n):
    return n / 1024 ** 3


def main(d):
    revisions = load(os.path.join(d, "revisions.json"), [])
    services = load(os.path.join(d, "services.json"), [])

    # digest -> [(revisión, servicio)]
    used_by = collections.defaultdict(list)
    rev_digest = {}
    for r in revisions:
        name = r["metadata"]["name"]
        svc = r["metadata"].get("labels", {}).get("serving.knative.dev/service", "?")
        dig = (r.get("status", {}).get("imageDigest") or "").split("@")[-1]
        if not dig:
            img = r["spec"]["containers"][0]["image"]
            dig = img.split("@")[-1] if "@" in img else ""
        rev_digest[name] = dig
        if dig:
            used_by[dig].append((name, svc))

    # revisiones protegidas: tráfico > 0, etiqueta de tráfico, última lista, o la canary
    protected_rev = {}
    for s in services:
        st = s.get("status", {})
        latest = st.get("latestReadyRevisionName")
        if latest:
            protected_rev.setdefault(latest, set()).add("última")
        for t in st.get("traffic", []):
            rn = t.get("revisionName") or (latest if t.get("latestRevision") else None)
            if not rn:
                continue
            if t.get("percent"):
                protected_rev.setdefault(rn, set()).add(f"tráfico {t['percent']}%")
            if t.get("tag"):
                protected_rev.setdefault(rn, set()).add(f"etiqueta {t['tag']}")
    canary = [n for n in rev_digest if n.endswith(CANARY)]
    for n in canary:
        protected_rev.setdefault(n, set()).add("canary pedida")
    print(f"Revisiones de Cloud Run: {len(revisions)}; protegidas: {len(protected_rev)}; "
          f"canary '{CANARY}': {', '.join(canary) or 'NO ENCONTRADA'}")
    for n in sorted(protected_rev):
        print(f"  protegida {n}: {', '.join(sorted(protected_rev[n]))} -> digest {short(rev_digest.get(n))}")
    missing = [n for n in protected_rev if not rev_digest.get(n)]
    if missing:
        print(f"  AVISO: sin digest conocido para {missing}")
    protected_dig = {rev_digest[n] for n in protected_rev if rev_digest.get(n)}

    total = keep_total = del_total = 0
    n_del = 0
    lost_rollback = []
    for repo in load(os.path.join(d, "repos.json"), []):
        rid = repo["name"].split("/")[-1]
        loc = repo["name"].split("/")[3]
        fmt = repo.get("format")
        imgs = load(os.path.join(d, f"images_{rid}.json"), None)
        if imgs is None:
            print(f"\n## repo {rid} ({loc}, {fmt}): sin listar")
            continue
        by_pkg = collections.defaultdict(list)
        for i in imgs:
            by_pkg[i["package"].split("/")[-1]].append(i)
        size_repo = sum(int(i.get("metadata", {}).get("imageSizeBytes", 0) or 0) for i in imgs)
        print(f"\n## repo {rid} ({loc}, {fmt}): {len(by_pkg)} imágenes, {len(imgs)} versiones, {gb(size_repo):.2f} GB")
        for pkg in sorted(by_pkg):
            vers = sorted(by_pkg[pkg], key=lambda i: i.get("createTime", ""), reverse=True)
            print(f"\n### {pkg}: {len(vers)} versiones")
            print(f"  {'#':>3} {'digest':12} {'MB':>7} {'creada':20} {'acción':9} etiquetas | revisiones")
            for k, v in enumerate(vers):
                dig = v["version"]
                size = int(v.get("metadata", {}).get("imageSizeBytes", 0) or 0)
                tags = v.get("tags") or []
                if isinstance(tags, str):
                    tags = [t for t in tags.split(",") if t]
                revs = used_by.get(dig, [])
                prot = dig in protected_dig
                keep = k < KEEP_RECENT or prot
                why = "conservar" if keep else "BORRAR"
                if prot and k >= KEEP_RECENT:
                    why = "PROTEGIDA"
                total += size
                if keep:
                    keep_total += size
                else:
                    del_total += size
                    n_del += 1
                    lost_rollback += [r for r, _ in revs]
                rtxt = ", ".join(r + ("*" if r in protected_rev else "") for r, _ in revs) or "-"
                print(f"  {k + 1:>3} {short(dig):12} {size / 1024 ** 2:7.1f} {v.get('createTime', '')[:19]:20} "
                      f"{why:9} {','.join(tags) or '-'} | {rtxt}")

    print("\n## Simulación (no borra nada)")
    print(f"  política: {KEEP_RECENT} más recientes por imagen + digests de revisiones protegidas (*)")
    print(f"  total {gb(total):.2f} GB | se conserva {gb(keep_total):.2f} GB | se borraría {gb(del_total):.2f} GB "
          f"en {n_del} versiones")
    print(f"  revisiones SIN tráfico que perderían su imagen (ya no se podría volver a ellas sin reconstruir): "
          f"{len(lost_rollback)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
