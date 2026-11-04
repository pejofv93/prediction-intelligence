"""Ayudas para ar-cleanup.yml. No llama a GCP: solo lee los JSON que vuelca el workflow.

  verify   <dir> <repo_url> "<revisiones activas esperadas>" <canary>
           Comprueba que las revisiones con tráfico en Cloud Run son exactamente las esperadas
           y que la canary existe con etiqueta de tráfico. Imprime "servicio revisión digest"
           de las 6 protegidas (el digest se valida contra el repo). Sale con 1 si algo no cuadra.
  simulate <images.json> <policy.json>
           Aplica la política al listado de versiones tal como la evalúa Artifact Registry
           (gana cualquier Keep sobre Delete) e imprime lo que se borraría por imagen.
"""
import collections
import json
import sys


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def verify(d, repo_url, expected, canary):
    revisions = load(f"{d}/revisions.json")
    services = load(f"{d}/services.json")
    by_name = {r["metadata"]["name"]: r for r in revisions}
    active = set()
    for s in services:
        st = s.get("status", {})
        for t in st.get("traffic", []):
            rn = t.get("revisionName") or (st.get("latestReadyRevisionName") if t.get("latestRevision") else None)
            if rn and t.get("percent"):
                active.add(rn)
    expected = set(expected.split())
    ok = True
    if active != expected:
        print(f"ERROR: revisiones activas en Cloud Run {sorted(active)} != esperadas {sorted(expected)}")
        ok = False
    else:
        print(f"Revisiones activas coinciden con las esperadas ({len(active)}).")
    canary_tagged = any(t.get("revisionName") == canary and t.get("tag")
                        for s in services for t in s.get("status", {}).get("traffic", []))
    if canary not in by_name:
        print(f"ERROR: la canary {canary} no existe en Cloud Run")
        ok = False
    elif not canary_tagged:
        print(f"AVISO: {canary} existe pero ya no tiene etiqueta de tráfico (se protege igualmente)")
    rows = []
    for rn in sorted(expected | {canary}):
        r = by_name.get(rn)
        if not r:
            print(f"ERROR: revisión {rn} no encontrada")
            ok = False
            continue
        svc = r["metadata"].get("labels", {}).get("serving.knative.dev/service", "")
        img = r.get("status", {}).get("imageDigest", "")
        pkg, _, dig = img.partition("@")
        if pkg != f"{repo_url}/{svc}" or not dig.startswith("sha256:") or not rn.startswith(svc + "-"):
            print(f"ERROR: {rn}: imagen fuera del repo esperado o sin digest")
            ok = False
            continue
        rows.append((svc, rn[len(svc) + 1:], dig))
    if not ok:
        return 1
    with open(f"{d}/protected.txt", "w", encoding="utf-8") as f:
        for svc, rev, dig in rows:
            f.write(f"{svc} {rev} {dig}\n")
            print(f"  protegida {svc} {rev} -> {dig.split(':')[1][:12]}")
    return 0


def simulate(images_path, policy_path):
    imgs = load(images_path)
    policy = load(policy_path)
    by_pkg = collections.defaultdict(list)
    for i in imgs:
        tags = i.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in tags.split(",") if t]
        i["_tags"] = tags
        by_pkg[i["package"].split("/")[-1]].append(i)

    def cond_match(c, pkg, v):
        if not c:
            return True
        pp = c.get("packageNamePrefixes")
        if pp and not any(pkg.startswith(p) for p in pp):
            return False
        ts = c.get("tagState", "any").lower()
        if ts == "tagged" and not v["_tags"]:
            return False
        if ts == "untagged" and v["_tags"]:
            return False
        tp = c.get("tagPrefixes")
        if tp and not any(t.startswith(p) for t in v["_tags"] for p in tp):
            return False
        return True

    total_del = total_keep = 0
    print(f"  {'imagen':18} {'versiones':>9} {'conservar':>9} {'borrar':>7} {'GB borrar':>10}")
    for pkg in sorted(by_pkg):
        vers = sorted(by_pkg[pkg], key=lambda v: v.get("createTime", ""), reverse=True)
        n_del = gb_del = 0
        for k, v in enumerate(vers):
            keep = delete = False
            for rule in policy:
                act = rule["action"]["type"].lower()
                if "mostRecentVersions" in rule:
                    m = rule["mostRecentVersions"]
                    pp = m.get("packageNamePrefixes")
                    hit = (not pp or any(pkg.startswith(p) for p in pp)) and k < int(m["keepCount"])
                else:
                    hit = cond_match(rule.get("condition"), pkg, v)
                if hit and act == "keep":
                    keep = True
                elif hit and act == "delete":
                    delete = True
            size = int(v.get("metadata", {}).get("imageSizeBytes", 0) or 0)
            if delete and not keep:
                n_del += 1
                gb_del += size
            else:
                total_keep += size
        total_del += gb_del
        print(f"  {pkg:18} {len(vers):9d} {len(vers) - n_del:9d} {n_del:7d} {gb_del / 1024 ** 3:10.2f}")
    print(f"  TOTAL: se conserva {total_keep / 1024 ** 3:.2f} GB, se borraría {total_del / 1024 ** 3:.2f} GB")
    return 0


if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    sys.exit(verify(*a) if cmd == "verify" else simulate(*a))
