"""Lee de stdin la salida de `wrangler pages deployment list --json` e imprime el id del
despliegue cuya URL es la indicada (o nada si no está). Uso: ... | python3 deployment_id.py URL

Busca la URL en cualquier campo de cada despliegue, sin depender del nombre exacto de la
columna; el id se toma de la clave "Id"/"id"/"ID"."""
import json
import sys


def find_id(listing, url: str) -> str:
    host = url.rstrip("/").split("://", 1)[-1]
    for dep in listing if isinstance(listing, list) else []:
        if not isinstance(dep, dict):
            continue
        if any(isinstance(v, str) and host in v for v in dep.values()):
            for key in ("Id", "id", "ID"):
                if dep.get(key):
                    return str(dep[key])
    return ""


if __name__ == "__main__":
    try:
        data = json.loads(sys.stdin.read() or "[]")
    except json.JSONDecodeError:
        data = []
    print(find_id(data, sys.argv[1]))
