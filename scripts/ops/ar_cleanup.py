"""Ayudas para ar-cleanup.yml. No llama a GCP: solo lee los JSON que vuelca el workflow.

  verify   <dir> <repo_url> "<revisiones activas esperadas>" <canary>
           Comprueba que las revisiones con tráfico en Cloud Run son exactamente las esperadas
           y que la canary existe con etiqueta de tráfico. Imprime "servicio revisión digest"
           de las 6 protegidas (el digest se valida contra el repo). Sale con 1 si algo no cuadra.
  simulate <images.json> <policy.json>
           Aplica la política al listado de versiones tal como la evalúa Artifact Registry
           (gana cualquier Keep sobre Delete) e imprime lo que se borraría por imagen.
  stages   <images.json> <policy.json> <repo.json> <días...>
           Igual que simulate, una vez por antigüedad (olderThan en las reglas Delete): cuánto
           borraría cada paso de la política escalonada. Solo lectura.
  verify-running <dir> <repo_url> "<revisiones activas esperadas>" <canary>
           Tras activar un paso: revisiones activas, servicios Ready, imágenes de las revisiones
           activas/canary/keep- presentes en el repo. Solo lectura.
"""
import collections
import json
import sys
from datetime import datetime, timezone


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


def _age_seconds(spec):
    """'5184000s' o '60d' → segundos (Artifact Registry usa segundos con sufijo 's')."""
    spec = str(spec).strip()
    if spec.endswith("d"):
        return int(float(spec[:-1]) * 86400)
    return int(float(spec.rstrip("s")))


def _version_time(v):
    """olderThan de Artifact Registry cuenta desde la subida: uploadTime si viene, si no createTime."""
    t = v.get("uploadTime") or v.get("createTime") or ""
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def _simulate(imgs, policy, now):
    """Devuelve ({paquete: (versiones, conservar, borrar, bytes_borrar)}, bytes_conservados)."""
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
        if "olderThan" in c or "newerThan" in c:
            vt = _version_time(v)
            if vt is None:
                return False  # sin fecha: no se borra (prudente)
            age = (now - vt).total_seconds()
            if "olderThan" in c and age <= _age_seconds(c["olderThan"]):
                return False
            if "newerThan" in c and age >= _age_seconds(c["newerThan"]):
                return False
        return True

    out, total_keep = {}, 0
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
        out[pkg] = (len(vers), len(vers) - n_del, n_del, gb_del)
    return out, total_keep


def _print_table(res, total_keep):
    print(f"  {'imagen':18} {'versiones':>9} {'conservar':>9} {'borrar':>7} {'GB borrar':>10}")
    for pkg, (n, k, d, b) in res.items():
        print(f"  {pkg:18} {n:9d} {k:9d} {d:7d} {b / 1024 ** 3:10.2f}")
    total_del = sum(r[3] for r in res.values())
    print(f"  TOTAL: se conserva {total_keep / 1024 ** 3:.2f} GB, se borraría {total_del / 1024 ** 3:.2f} GB")
    return total_del


def simulate(images_path, policy_path):
    _print_table(*_simulate(load(images_path), load(policy_path), datetime.now(timezone.utc)))
    return 0


def stages(images_path, policy_path, repo_path, *days):
    """
    Solo lectura. Para cada antigüedad (días) añade olderThan a las reglas Delete y simula.
    Los GB son lógicos (suma de tamaños de imagen). Las versiones comparten capas, así que se da
    también una estimación real = lógico × (tamaño real del repo / suma lógica total). Es una
    aproximación: las capas compartidas no se reparten por igual entre versiones.
    """
    imgs, policy = load(images_path), load(policy_path)
    now = datetime.now(timezone.utc)
    real = int(load(repo_path).get("sizeBytes", 0) or 0)
    logical_all = sum(int(i.get("metadata", {}).get("imageSizeBytes", 0) or 0) for i in imgs)
    ratio = (real / logical_all) if logical_all else 0.0
    field = "uploadTime" if imgs and imgs[0].get("uploadTime") else "createTime"
    print(f"Fecha usada para la antigüedad: {field}")
    print(f"Repo real {real / 1024 ** 3:.2f} GB · suma lógica {logical_all / 1024 ** 3:.2f} GB · ratio {ratio:.3f}")
    prev = 0
    for d in sorted({int(x) for x in days}, reverse=True):
        staged = json.loads(json.dumps(policy))
        for rule in staged:
            if rule["action"]["type"].lower() == "delete":
                rule.setdefault("condition", {})["olderThan"] = f"{d * 86400}s"
        print(f"\n== Paso: borrar versiones con más de {d} días (keep-3, keep- y market-sentinel intactos) ==")
        total = _print_table(*_simulate(imgs, staged, now))
        print(f"  Este paso añade {(total - prev) / 1024 ** 3:.2f} GB lógicos ~ {(total - prev) * ratio / 1024 ** 3:.2f} GB "
              f"reales · acumulado ~ {total * ratio / 1024 ** 3:.2f} GB reales")
        prev = total
    return 0


def verify_running(d, repo_url, expected, canary):
    """
    Solo lectura, tras activar un paso. Comprueba: revisiones con tráfico == esperadas; cada
    servicio Ready; la imagen de cada revisión activa, de la canary y de cada keep- sigue en el
    repo y esas revisiones siguen Ready. Escribe health_urls.txt ("servicio url") para que el
    workflow llame a /health — sin market-sentinel, que no se toca.
    """
    revisions = load(f"{d}/revisions.json")
    services = load(f"{d}/services.json")
    imgs = load(f"{d}/images.json")
    by_name = {r["metadata"]["name"]: r for r in revisions}
    in_repo = {f"{i['package']}@{i['version']}" for i in imgs}

    def ready(obj):
        conds = obj.get("status", {}).get("conditions", [])
        return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)

    ok = verify(d, repo_url, expected, canary) == 0
    urls = []
    for s in services:
        name = s["metadata"]["name"]
        print(f"  servicio {name}: Ready={'sí' if ready(s) else 'NO'}")
        ok = ok and ready(s)
        if not name.startswith("market-sentinel") and s.get("status", {}).get("url"):
            urls.append((name, s["status"]["url"]))
    revs = set(expected.split()) | {canary}
    for i in imgs:
        tags = i.get("tags") or []
        tags = [t for t in tags.split(",") if t] if isinstance(tags, str) else tags
        revs.update(t[len("keep-"):] for t in tags if t.startswith("keep-"))
    for rn in sorted(revs):
        r = by_name.get(rn)
        if not r:
            print(f"  AVISO: {rn} ya no existe como revisión de Cloud Run")
            continue
        present = r.get("status", {}).get("imageDigest", "") in in_repo
        print(f"  {rn}: Ready={'sí' if ready(r) else 'NO'} · imagen en el repo={'sí' if present else 'NO'}")
        ok = ok and present and ready(r)
    with open(f"{d}/health_urls.txt", "w", encoding="utf-8") as f:
        for name, url in urls:
            f.write(f"{name} {url}\n")
    return 0 if ok else 1


MIN_OLDER_THAN_DAYS = 14


def preflight_activate(d, policy_path, expected):
    """
    Solo lectura, justo antes de quitar el dry-run. Para si:
      - alguna regla Delete no tiene olderThan, o lo tiene por debajo de 14 días (la política
        sin antigüedad borraría todo salvo keep-3/keep-);
      - alguna revisión con tráfico esperada no tiene su imagen etiquetada keep-.
    """
    ok = True
    for rule in load(policy_path):
        if rule["action"]["type"].lower() != "delete":
            continue
        ot = (rule.get("condition") or {}).get("olderThan")
        if not ot:
            print(f"ERROR: la regla Delete {rule.get('name')} no tiene olderThan")
            ok = False
        elif _age_seconds(ot) < MIN_OLDER_THAN_DAYS * 86400:
            print(f"ERROR: {rule.get('name')}: olderThan {ot} < {MIN_OLDER_THAN_DAYS} días")
            ok = False
        else:
            print(f"  {rule.get('name')}: olderThan = {_age_seconds(ot) // 86400} días")
    revisions = {r["metadata"]["name"]: r for r in load(f"{d}/revisions.json")}
    tags_by_img = {}
    for i in load(f"{d}/images.json"):
        tags = i.get("tags") or []
        tags_by_img[f"{i['package']}@{i['version']}"] = [t for t in tags.split(",") if t] if isinstance(tags, str) else tags
    for rn in expected.split():
        img = revisions.get(rn, {}).get("status", {}).get("imageDigest", "")
        keep = [t for t in tags_by_img.get(img, []) if t.startswith("keep-")]
        print(f"  {rn}: etiquetas keep- = {keep or 'NINGUNA'}")
        ok = ok and bool(keep)
    return 0 if ok else 1


def tag_target(d, repo_url, revision):
    """Solo lectura: imprime 'servicio digest' de una revisión, validando que su imagen es del repo."""
    r = {x["metadata"]["name"]: x for x in load(f"{d}/revisions.json")}.get(revision)
    if not r:
        print(f"ERROR: la revisión {revision} no existe")
        return 1
    svc = r["metadata"].get("labels", {}).get("serving.knative.dev/service", "")
    pkg, _, dig = r.get("status", {}).get("imageDigest", "").partition("@")
    if pkg != f"{repo_url}/{svc}" or not dig.startswith("sha256:") or not revision.startswith(svc + "-"):
        print(f"ERROR: {revision}: imagen fuera del repo esperado o sin digest")
        return 1
    print(f"{svc} {dig}")
    return 0


if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    cmds = {"verify": verify, "simulate": simulate, "stages": stages, "verify-running": verify_running,
            "preflight-activate": preflight_activate, "tag-target": tag_target}
    sys.exit(cmds[cmd](*a))
