"""SOLO LECTURA. Resumen del aprendizaje nocturno desde el registro de Cloud Run de sports-agent.

El repositorio es público: NO imprime líneas del registro. Solo, por cada ejecución (desde
"learning: iniciando pipeline"): hora de inicio, versión de model_weights escrita (o que no se
escribió), recuentos de los mensajes que importan y el NOMBRE de la excepción que acompañe a un
error (p. ej. FailedPrecondition), sin su texto.

Uso: python learning_log_summary.py entradas.json   (salida de `gcloud logging read --format=json`)
"""
import json
import re
import sys
from collections import Counter

# mensaje (subcadena exacta del logger) → etiqueta que se imprime
EVENTS = {
    "model_weights actualizado → version": "model_weights escrito",
    "sin época actual — solo contadores de por vida": "solo contadores de por vida escritos",
    "error guardando model_weights": "ERROR guardando model_weights (precondición u otro)",
    "model_weights no se pudo leer al empezar": "ERROR leyendo model_weights al empezar (no se escribe)",
    "error leyendo model_weights": "ERROR leyendo model_weights al empezar (no se escribe)",
    "model_weights/current no existe — nada que actualizar": "ERROR model_weights no existe",
    "sin model_weights guardado — no se actualiza accuracy_log": "predicciones SIN marcar (quedan pendientes)",
    "error actualizando prediction": "error marcando una predicción",
    "error guardando accuracy_log": "error guardando accuracy_log",
    "nada resuelto hoy": "nada resuelto",
}
_VERSION = re.compile(r"model_weights actualizado → version (\d+)")
_PROCESSED = re.compile(r"run_daily_learning: completado — (\d+) procesadas")
# Última línea de una traza: "google.api_core.exceptions.FailedPrecondition: 400 ..." → nombre
_EXC = re.compile(r"^\s*([A-Za-z_][\w.]*?(?:Error|Exception|FailedPrecondition|Aborted|"
                  r"AlreadyExists|NotFound|Conflict|DeadlineExceeded|ServiceUnavailable))(?::|$)")


def text(e: dict) -> str:
    return str(e.get("textPayload") or (e.get("jsonPayload") or {}).get("message") or "")


def main(path: str) -> int:
    entries = sorted(json.load(open(path, encoding="utf-8")), key=lambda e: e.get("timestamp", ""))
    runs = []
    cur = None
    for e in entries:
        t, ts = text(e), e.get("timestamp", "")[:19]
        if "learning: iniciando pipeline" in t:
            cur = {"inicio": ts, "version": None, "procesadas": None, "eventos": Counter(),
                   "excepciones": Counter()}
            runs.append(cur)
            continue
        if cur is None:
            continue
        for needle, label in EVENTS.items():
            if needle in t:
                cur["eventos"][label] += 1
        if m := _VERSION.search(t):
            cur["version"] = int(m.group(1))
        if m := _PROCESSED.search(t):
            cur["procesadas"] = int(m.group(1))
        if m := _EXC.match(t):
            cur["excepciones"][m.group(1).rsplit(".", 1)[-1]] += 1

    if not runs:
        print("Ninguna ejecución del aprendizaje en la ventana pedida.")
        return 0
    for r in runs:
        print(f"== Ejecución iniciada {r['inicio']} UTC")
        print(f"  model_weights escrito: {'sí, versión ' + str(r['version']) if r['version'] else 'NO'}")
        print(f"  predicciones procesadas: {r['procesadas'] if r['procesadas'] is not None else '— (sin línea de fin)'}")
        for label, n in sorted(r["eventos"].items()):
            print(f"  {label}: {n}")
        if r["excepciones"]:
            print("  excepciones (solo el nombre): "
                  + ", ".join(f"{k}×{v}" for k, v in sorted(r["excepciones"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
