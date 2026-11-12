"""Plan de borrado de despliegues antiguos de Pages (trends-web-prune).

Lee de stdin la salida de `wrangler pages deployment list --json` (más reciente primero) y
escribe en el fichero indicado una línea "ID URL" por despliegue a borrar: todos menos el de
producción más reciente, que es el que sirve la web (Cloudflare no deja borrar el activo).
Por la salida estándar solo saca recuentos: el repositorio y sus logs son públicos y las
direcciones de los despliegues antiguos no deben aparecer.

Uso: ... | python3 prune_plan.py FICHERO_SALIDA
"""
import json
import sys


def _get(dep: dict, *keys: str) -> str:
    for k in keys:
        v = dep.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def plan(listing) -> tuple[str, list[tuple[str, str]]]:
    """(id que se conserva, [(id, url) a borrar]). Sin producción reconocible → no borra nada."""
    deps = [d for d in (listing if isinstance(listing, list) else []) if isinstance(d, dict)]
    rows = []
    for d in deps:
        dep_id = _get(d, "Id", "id", "ID")
        url = next((v for v in d.values() if isinstance(v, str) and v.startswith("https://")
                    and v.rstrip("/").endswith(".pages.dev")), "")
        env = _get(d, "Environment", "environment").lower()
        if dep_id and url:
            rows.append((dep_id, url.rstrip("/"), env))
    keep = next((r for r in rows if r[2] == "production"), None)
    if keep is None:
        return "", []
    return keep[0], [(i, u) for i, u, _ in rows if i != keep[0]]


if __name__ == "__main__":
    try:
        data = json.loads(sys.stdin.read() or "[]")
    except json.JSONDecodeError:
        data = []
    keep_id, to_delete = plan(data)
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        for dep_id, url in to_delete:
            f.write(f"{dep_id} {url}\n")
    total = len(data) if isinstance(data, list) else 0
    print(f"Listados: {total} · se conserva el de producción más reciente: {'sí' if keep_id else 'NO ENCONTRADO'}"
          f" · a borrar: {len(to_delete)}")
    if not keep_id:
        sys.exit(1)
