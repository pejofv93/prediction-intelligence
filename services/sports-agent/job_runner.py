"""
Ejecuta un trabajo de sports-agent fuera de Cloud Run (GitHub Actions), con el mismo código
que el endpoint: `python job_runner.py collect` = POST /run-collect.

Fase 1 del recorte de la factura de GCP (23-nov-2026): el collect corre en el runner de
GitHub (gratis en repo público) en vez de en Cloud Run; lo sigue lanzando el repartidor por
workflow_dispatch, como antes.

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
from collections import Counter

JOBS = {"collect": "_bg_collect"}

# Líneas de resumen con recuentos/códigos (formatos fijos del código de main.py).
PUBLIC_RE = re.compile(
    r"^(collect(\.[a-z_0-9]+)?: (iniciando pipeline|completado( en [0-9.]+s)?|[0-9]+ "
    r"[a-zA-Záéíóúñ ]+|equipos por motivo \{[a-z_0-9': ,]*\}|sin datos para [A-Z0-9]+ \(id=[0-9]+\)"
    r"|upcoming=[0-9]+ expirados, predictions=[0-9]+ expiradas, odds_cache=[0-9]+ eliminados)"
    r"|get_finished_matches: [0-9]+ partidos terminados|RESULTS_UPDATE: [0-9]+ partidos [a-zA-Z ]+)$"
)
FAIL_MARK = "collect: error no controlado"


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
            if record.getMessage().startswith(FAIL_MARK):
                self.failed = True
        except Exception:
            pass


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

    def emit(self, record: logging.LogRecord) -> None:
        if self.broken:
            return
        try:
            self._buf.append((self._fmt.format(record), record.levelname))
        except Exception:
            return
        if len(self._buf) >= 200:
            self.flush()

    def flush(self) -> None:
        """Escribe lo acumulado en una sola llamada (por lotes de 200 líneas)."""
        if self.broken or not self._buf:
            return
        buf, self._buf = self._buf, []
        try:
            batch = self._logger.batch()
            for text, sev in buf:
                batch.log_text(text, resource=self._resource, severity=sev)
            batch.commit()
        except Exception as exc:          # sin el mensaje: podría llevar datos
            self.broken = True
            print(f"Cloud Logging: no se puede escribir ({type(exc).__name__}); queda el fichero",
                  file=sys.stdout, flush=True)


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
            print(f"Cloud Logging: no disponible ({type(exc).__name__}); queda el fichero", flush=True)
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

    rc = 0
    try:
        asyncio.run(getattr(svc, JOBS[job])())
    except Exception as exc:
        logging.getLogger("job_runner").error("%s lanzó %s", job, type(exc).__name__, exc_info=True)
        rc = 1
    if fail.failed:
        rc = 1
    for h in logging.getLogger().handlers:
        h.flush()
    print("avisos y errores por módulo: "
          + (", ".join(f"{lvl} {name}={n}" for (lvl, name), n in sorted(pub_filter.counts.items()))
             or "ninguno"), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
