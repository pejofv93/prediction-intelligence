"""CLV real al cierre de lo enviado (fútbol 1X2), por versión del Poisson, para el informe
semanal. Lee los campos que escribe sports-agent learner/closing_clv.py en alerts_sent."""
import math
from datetime import datetime, timedelta, timezone

FIT_OLD = "nelder-mead-2000"           # docs sin poisson_fit (antes del 16-nov-2026)
WINDOW_DAYS = 28                       # el CSV llega con días de retraso: ventana de 4 semanas
Z90 = 1.645


def _dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _stats(v: list[float]) -> dict:
    n = len(v)
    mean = sum(v) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
    half = Z90 * sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": mean, "lo": mean - half, "hi": mean + half,
            "pos": sum(1 for x in v if x > 0) / n}


def summarize(docs: list[dict], now: datetime, window_days: int = WINDOW_DAYS) -> dict:
    """{"groups": {fit: {n, mean, lo, hi, pos}} frente a la media sin margen,
        "groups_max": lo mismo frente a la cuota máxima al cierre,
        "pending": n, "source": str|None}."""
    since = now - timedelta(days=window_days)
    vals: dict[str, list[float]] = {}
    vals_max: dict[str, list[float]] = {}
    pending, sources = 0, set()
    for d in docs:
        if str(d.get("sport") or "football").lower() not in ("football", "soccer"):
            continue
        if (d.get("market_type") or "h2h") != "h2h":
            continue
        t = _dt(d.get("sent_at"))
        if t is None or t < since:
            continue
        clv = d.get("clv_close")
        if clv is None:
            if d.get("close_status") != "sin_cierre":
                pending += 1
            continue
        fit = d.get("poisson_fit") or FIT_OLD
        vals.setdefault(fit, []).append(float(clv))
        if d.get("clv_close_max") is not None:
            vals_max.setdefault(fit, []).append(float(d["clv_close_max"]))
        if d.get("close_source"):
            sources.add(d["close_source"])
    return {"groups": {f: _stats(v) for f, v in vals.items()},
            "groups_max": {f: _stats(v) for f, v in vals_max.items()},
            "pending": pending,
            "source": ", ".join(sorted(sources)) if sources else None}


def report_lines(s: dict) -> list[str]:
    """Líneas del informe semanal (vacío si no hay nada en la ventana)."""
    if not s or (not s.get("groups") and not s.get("pending")):
        return []
    ref = "Pinnacle sin margen" if "pinnacle" in (s.get("source") or "") else "media sin margen"
    lines = ["CLV real al cierre (enviadas 1X2, 4 semanas):"]
    names = {FIT_OLD: "Poisson anterior", "lbfgs-l2-v1": "Poisson nuevo"}

    def _fmt(g: dict) -> str:
        ci = f" [{g['lo']:+.1%}, {g['hi']:+.1%}]" if g["n"] > 1 else ""
        return f"{g['mean']:+.1%}{ci} · n={g['n']} · {g['pos']:.0%} por encima"

    for label, key in ((ref, "groups"), ("cuota máxima", "groups_max")):
        groups = s.get(key) or {}
        if not groups:
            continue
        lines.append(f" Frente a {label}:")
        for fit in sorted(groups, key=lambda f: (f != FIT_OLD, f)):
            lines.append(f"  {names.get(fit, fit)}: {_fmt(groups[fit])}")
    if s.get("pending"):
        lines.append(f"  Pendientes de cierre: {s['pending']}")
    return lines
