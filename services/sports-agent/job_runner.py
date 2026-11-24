"""
Ejecuta un trabajo de sports-agent fuera de Cloud Run (GitHub Actions), con el mismo código
que el endpoint: `python job_runner.py collect` = POST /run-collect.

Recorte de la factura de GCP (23-nov-2026): el trabajo corre en el runner de GitHub (gratis
en repo público) en vez de en Cloud Run; lo sigue lanzando el repartidor por workflow_dispatch.
  - collect: probado el 23-nov y devuelto a Cloud Run (Sofascore bloquea las IP del runner).
  - analyze: fase 2 (no usa Sofascore: cuotas por API con clave, Firestore y telegram-bot).

Logs — el log de Actions es PÚBLICO:
  - Todo (equipos, partidos, trazas) va a un fichero temporal del runner, que el workflow
    borra al terminar, y a Cloud Logging con el mismo recurso que el servicio
    (cloud_run_revision / sports-agent, revisión "github-actions"), para que los diagnósticos
    que leen esos logs sigan funcionando. Si la cuenta no puede escribir en Cloud Logging,
    se avisa una vez y se sigue (queda el fichero).
  - Al log público solo salen las líneas de resumen del trabajo que llevan recuentos
    (PUBLIC_RE) y, de los avisos y errores, el nombre del logger, el nivel y la clase de la
    excepción, nunca el mensaje.
Código de salida: 1 si el trabajo registró su error no controlado o lanzó; 0 si no.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from collections import Counter

JOBS = {"collect": "_bg_collect", "analyze": "_bg_analyze"}

# Líneas de resumen con recuentos/códigos (formatos fijos del código de main.py).
PUBLIC_RE = re.compile(
    r"^(collect(\.[a-z_0-9]+)?: (iniciando pipeline|completado( en [0-9.]+s)?|[0-9]+ "
    r"[a-zA-Záéíóúñ ]+|equipos por motivo \{[a-z_0-9': ,]*\}|sin datos para [A-Z0-9]+ \(id=[0-9]+\)"
    r"|upcoming=[0-9]+ expirados, predictions=[0-9]+ expiradas, odds_cache=[0-9]+ eliminados)"
    r"|get_finished_matches: [0-9]+ partidos terminados|RESULTS_UPDATE: [0-9]+ partidos [a-zA-Z ]+"
    # analyze: solo las líneas de recuentos (sin ligas, equipos ni ids de partido)
    r"|analyze: (iniciando pipeline|pre-fetch completado — todas las fuentes en cache"
    r"|pre-fetching [0-9]+ coroutines en paralelo"
    r"|[0-9]+ enriched \(7d\) de [0-9]+ upcoming SCHEDULED \(get_all [0-9]+ refs\)"
    r"|dedup por partido físico — [0-9]+ fixtures con doc duplicado, [0-9]+ señales eliminadas"
    r"|[0-9]+ partidos de (tenis|baloncesto) \(48h\) a analizar"
    r"|tenis — [0-9]+ señales( generadas|, [0-9]+ sin stats \([^()]*\))"
    r"|[0-9]+ bloqueos de filtro registrados"
    r"|[0-9]+ senales generadas de [0-9]+ enriquecidos en [0-9.]+s \([0-9]+ partidos descartados[^()]*\)"
    r"|arbitrage detector lanzado en background|odds-check lanzado en telegram-bot))$"
)
FAIL_MARKS = tuple(f"{job}: error no controlado" for job in JOBS)
PENDING_TASKS_TIMEOUT = 300   # lo que el trabajo deja en segundo plano (analyze → _bg_arb)


class PublicFilter(logging.Filter):
    """Deja pasar al log público solo resúmenes; de avisos/errores, sin el mensaje."""

    def __init__(self) -> None:
        super().__init__()
        self.counts: Counter = Counter()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            self.counts[(record.levelname, record.name)] += 1
            exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
            record.public = f"{record.levelname} en {record.name}" + (f" ({exc})" if exc else "")
            return record.levelno >= logging.ERROR      # avisos: solo en el recuento final
        try:
            msg = record.getMessage()
        except Exception:
            return False
        if PUBLIC_RE.match(msg):
            record.public = msg
            return True
        return False


class PublicFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"{self.formatTime(record, '%H:%M:%S')} {getattr(record, 'public', '')}"


class FailFlag(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.failed = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.getMessage().startswith(FAIL_MARKS):
                self.failed = True
        except Exception:
            pass


_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
# Errores que no se arreglan reintentando (falta permiso, petición inválida, no existe).
_PERMANENT_CODES = {"PERMISSION_DENIED", "UNAUTHENTICATED", "INVALID_ARGUMENT", "NOT_FOUND",
                    "FAILED_PRECONDITION"}


def _error_code(exc: Exception) -> str | None:
    """Código gRPC del error de la API de Google (PERMISSION_DENIED…) o, si no hay, el HTTP."""
    grpc_code = getattr(exc, "grpc_status_code", None)
    if grpc_code is not None:
        return getattr(grpc_code, "name", None) or str(grpc_code)
    http = getattr(exc, "code", None)
    return f"HTTP {http}" if isinstance(http, int) else None


def describe_api_error(exc: Exception) -> str:
    """«clase, código X, motivo Y» SIN el texto del error (podría llevar datos). El motivo es
    la constante de Google de ErrorInfo (p. ej. IAM_PERMISSION_DENIED); si no tiene forma de
    constante, no se muestra."""
    parts = [type(exc).__name__]
    code = _error_code(exc)
    if code:
        parts.append(f"código {code}")
    reason = getattr(exc, "reason", None)
    parts.append(f"motivo {reason}" if isinstance(reason, str) and _REASON_RE.match(reason)
                 else "sin motivo")
    return ", ".join(parts)


class CloudLoggingHandler(logging.Handler):
    """Manda cada línea a Cloud Logging como si la escribiera el servicio en Cloud Run."""

    def __init__(self, project: str, service: str) -> None:
        super().__init__(level=logging.INFO)
        from google.cloud import logging as gcl
        from google.cloud.logging_v2.resource import Resource

        self._logger = gcl.Client(project=project).logger("run.googleapis.com%2Fstderr")
        self._resource = Resource(type="cloud_run_revision", labels={
            "project_id": project, "service_name": service, "location": "europe-west1",
            "configuration_name": service, "revision_name": "github-actions"})
        self._fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        self._buf: list[tuple[str, str]] = []
        self.broken = False
        self.lost = 0            # líneas que no se pudieron escribir tras los reintentos
        self._fails_in_row = 0

    def emit(self, record: logging.LogRecord) -> None:
        if self.broken:
            return
        try:
            self._buf.append((self._fmt.format(record), record.levelname))
        except Exception:
            return
        if len(self._buf) >= self.BATCH:
            self.flush()

    BATCH = 100
    RETRY_WAITS = (2, 5, 15)     # segundos entre reintentos (ResourceExhausted = cuota por minuto)

    def flush(self) -> None:
        """Escribe lo acumulado por lotes de BATCH líneas. Si un lote falla, reintenta con
        espera; si sigue fallando, lo da por perdido (queda en el fichero) y sigue con el
        siguiente. Solo se rinde del todo tras 3 lotes perdidos seguidos."""
        if self.broken or not self._buf:
            return
        buf, self._buf = self._buf, []
        for i in range(0, len(buf), self.BATCH):
            chunk, err = buf[i:i + self.BATCH], None
            for wait in (0, *self.RETRY_WAITS):
                if wait:
                    time.sleep(wait)
                try:
                    batch = self._logger.batch()
                    for text, sev in chunk:
                        batch.log_text(text, resource=self._resource, severity=sev)
                    batch.commit()
                    err = None
                    break
                except Exception as exc:  # sin el mensaje: podría llevar datos
                    err = describe_api_error(exc)
                    if _error_code(exc) in _PERMANENT_CODES:
                        # reintentar no lo arregla (p. ej. falta el permiso de escribir logs)
                        self.lost += len(buf) - i
                        self.broken = True
                        print(f"Cloud Logging: no se puede escribir ({err}); queda el fichero",
                              file=sys.stdout, flush=True)
                        return
            if err is None:
                self._fails_in_row = 0
                continue
            self.lost += len(chunk)
            self._fails_in_row += 1
            if self._fails_in_row >= 3:
                self.broken = True
                print(f"Cloud Logging: no se puede escribir ({err}); queda el fichero",
                      file=sys.stdout, flush=True)
                return


def setup_logging(log_file: str, project: str | None) -> tuple[PublicFilter, FailFlag]:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.addHandler(fh)

    pub_filter = PublicFilter()
    sh = logging.StreamHandler(sys.stdout)
    sh.addFilter(pub_filter)
    sh.setFormatter(PublicFormatter())
    root.addHandler(sh)

    fail = FailFlag()
    root.addHandler(fail)

    if project:
        try:
            root.addHandler(CloudLoggingHandler(project, "sports-agent"))
        except Exception as exc:
            print(f"Cloud Logging: no disponible ({describe_api_error(exc)}); queda el fichero", flush=True)
    for noisy in ("httpx", "httpcore", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return pub_filter, fail


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in JOBS:
        print(f"uso: python job_runner.py {{{'|'.join(JOBS)}}}")
        return 2
    job = argv[1]
    pub_filter, fail = setup_logging(os.environ.get("JOB_LOG_FILE", f"/tmp/{job}.log"),
                                     os.environ.get("GOOGLE_CLOUD_PROJECT"))
    # Tras configurar los handlers: el basicConfig de main.py ya no añade el de consola.
    import main as svc

    async def _run() -> None:
        await getattr(svc, JOBS[job])()
        # Lo que el trabajo deja en segundo plano (en Cloud Run seguía tras la respuesta):
        # aquí se espera, porque asyncio.run() lo cancelaría al salir.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=PENDING_TASKS_TIMEOUT)

    rc = 0
    try:
        asyncio.run(_run())
    except Exception as exc:
        logging.getLogger("job_runner").error("%s lanzó %s", job, type(exc).__name__, exc_info=True)
        rc = 1
    if fail.failed:
        rc = 1
    for h in logging.getLogger().handlers:
        h.flush()
        if getattr(h, "lost", 0):
            print(f"Cloud Logging: {h.lost} líneas sin escribir (quedan en el fichero)", flush=True)
    print("avisos y errores por módulo: "
          + (", ".join(f"{lvl} {name}={n}" for (lvl, name), n in sorted(pub_filter.counts.items()))
             or "ninguno"), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
