"""
Horario del repartidor (/dispatch en telegram-bot). Un reloj externo gratuito (Worker de
Cloudflare reloj-dispatch cada 5 min, con respaldo horario en GitHub) llama al repartidor; este lanza por
workflow_dispatch los workflows a los que les toca. Motivo: los cron de GitHub del repo
llegaban 5-8 h tarde y perdían la mitad de las pasadas (auditoría 16-nov-2026), mientras
que workflow_dispatch arranca al momento.

Cada tarea: workflow, expresión cron (UTC, 5 campos), retraso máximo y parámetros.
  - Una franja se lanza una sola vez (lo garantiza el repartidor con un documento por franja).
  - Si una franja lleva más de `max_late_min` vencida sin lanzarse, se salta: mejor perder
    una pasada que lanzar pasadas viejas en cadena.
Frecuencias (decididas por el usuario el 16-nov-2026):
  - A su hora: Sports (recogida, enriquecimiento, análisis, fdco, tendencias, ELO, matched),
    fichas, aprendizaje, informes y mantenimiento de Highlightly.
  - Recogida y enriquecimiento de Sports, 2 al día (17-nov-2026): con las 4 pasadas a su hora
    sports-agent doblaría su tiempo de Cloud Run; así queda como con el cron de GitHub.
    Análisis, 4 al día.
  - Polymarket: al ritmo real que tenía con el cron de GitHub (no al nominal).
  - hl-load-auto no está: se apagó sola al terminar la carga (15-nov-2026) y conserva su cron.
  - oracle-a1-retry: cada 15 minutos.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Modo del repartidor: "dry_run" anota lo que lanzaría sin llamar a GitHub; "live" lanza.
# Pasar a "live" va en el mismo commit que quita los `schedule:` de estos workflows.
DISPATCH_MODE = "live"


@dataclass(frozen=True)
class Task:
    workflow: str
    cron: str
    max_late_min: int = 60
    inputs: dict = field(default_factory=dict)


SCHEDULED = {"programado": "true"}   # para los workflows que distinguen el lanzamiento programado

TASKS: dict[str, Task] = {
    # ── Sports, a su hora ──────────────────────────────────────────────────────────
    "sports-collect":       Task("sports-collect.yml",        "30 5,17 * * *"),
    "sports-enrich":        Task("sports-enrich.yml",         "20 6,18 * * *"),
    "sports-analyze":       Task("sports-analyze.yml",        "0 1,7,13,19 * * *"),
    "sports-fdco-collect":  Task("sports-fdco-collect.yml",   "0 6 * * *", 120),
    "sports-trend-grade":   Task("sports-trend-grade.yml",    "20 6 * * *", 120),
    "sports-trends":        Task("sports-trends.yml",         "30 7 * * *", 120),
    "rebuild-elo":          Task("rebuild-elo.yml",           "0 3 * * *", 120, SCHEDULED),
    "matched-scan":         Task("matched-scan.yml",          "30 6 * * *", 120),
    "learning-engine":      Task("learning-engine.yml",       "0 2 * * *", 180),
    "match-previews":       Task("sports-match-previews.yml", "0 2,16 * * *", 120, SCHEDULED),
    "eur-tennis-daily":     Task("sports-eur-tennis-daily.yml", "30 6 * * *", 120),  # Euroliga + tenis (fase 2)
    # ── Informes ───────────────────────────────────────────────────────────────────
    "daily-report":         Task("daily-report.yml",          "0 7 * * *", 180),
    "trends-summary":       Task("trends-daily-summary.yml",  "0 8 * * *", 120),   # fichas del día por Telegram
    "weekly-report":        Task("weekly-report.yml",         "0 8 * * 1", 360),
    "tier-review":          Task("sports-tier-review.yml",    "0 8 1 * *", 360, SCHEDULED),
    "run-backtest-sports":  Task("run-backtest-sports.yml",   "0 2 1 * *", 360),
    # ── Highlightly (mantenimiento), a su hora ─────────────────────────────────────
    "hl-load-daily":        Task("hl-load-daily.yml",         "40 5,17 * * *", 120),
    # ── Polymarket, al ritmo real medido (≈ 3-4 al día, no el nominal) ──────────────
    "polymarket-scan":      Task("polymarket-scan.yml",       "0 1,7,13,19 * * *"),
    "poly-news-trigger":    Task("poly-news-trigger.yml",     "10 1,7,13,19 * * *"),
    "poly-price-monitor":   Task("poly-price-monitor.yml",    "20 1,7,13,19 * * *"),
    "poly-ws-keepalive":    Task("poly-websocket-keepalive.yml", "5 1,7,13,19 * * *"),
    "polymarket-enrich":    Task("polymarket-enrich.yml",     "30 3,11,19 * * *"),
    "polymarket-analyze":   Task("polymarket-analyze.yml",    "0 4,12,20 * * *"),
    "polymarket-resolve":   Task("polymarket-resolve.yml",    "0 3 * * *", 180),
    "polymarket-learn":     Task("polymarket-learn.yml",      "30 3 * * *", 180),
    "poly-backtest":        Task("poly-backtest.yml",         "0 3 1 * *", 360),
    # ── Oracle ─────────────────────────────────────────────────────────────────────
    "oracle-a1-retry":      Task("oracle-a1-retry.yml",       "*/15 * * * *", 14),
}


def _expand(spec: str, lo: int, hi: int) -> frozenset[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return frozenset(out)


def matches(cron: str, t: datetime) -> bool:
    """¿Cae `t` (UTC, al minuto) en la expresión cron? Día del mes y de la semana como en cron."""
    m, h, dom, mon, dow = cron.split()
    if t.minute not in _expand(m, 0, 59) or t.hour not in _expand(h, 0, 23):
        return False
    if t.month not in _expand(mon, 1, 12):
        return False
    dom_ok = t.day in _expand(dom, 1, 31)
    dow_ok = (t.isoweekday() % 7) in {d % 7 for d in _expand(dow, 0, 7)}
    if dom == "*" or dow == "*":
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def latest_slot(task: Task, now: datetime) -> datetime | None:
    """Última franja programada en (now − max_late_min, now], o None."""
    t = now.replace(second=0, microsecond=0)
    for _ in range(task.max_late_min + 1):
        if matches(task.cron, t):
            return t
        t -= timedelta(minutes=1)
    return None


def due(now: datetime, tasks: dict[str, Task] | None = None) -> list[tuple[str, datetime]]:
    """Tareas con franja vigente ahora (el repartidor descarta las ya lanzadas)."""
    out = []
    for name, task in (TASKS if tasks is None else tasks).items():
        slot = latest_slot(task, now)
        if slot is not None:
            out.append((name, slot))
    return out
