"""Solo lectura: cuenta los avisos (WARNING) de una pasada por TIPO, sin nombres ni cifras.

Cada aviso del log se empareja con la llamada logger.warning(...) del código que lo escribió
(se lee el fichero .py con ast) y se muestra la PLANTILLA del código — el texto literal con
{…} donde iban los valores —, la línea y cuántas veces salió. Al log público solo va texto
que ya está en el repo; lo que no casa con ninguna plantilla se cuenta como «sin plantilla».

Uso: LOGS_FILE=logs.json python scripts/ops/warning_types.py \
         analyzers.value_bet_engine=services/sports-agent/analyzers/value_bet_engine.py ...
logs.json: salida de gcloud logging read --format json (timestamp, textPayload); textPayload =
«fecha hora módulo WARNING mensaje», con el traceback en las líneas siguientes si lo hay.

25-nov-2026: LOGS_TEXT=fichero lee en su lugar el log completo del runner (el de JOB_LOG_FILE,
mismo formato, una entrada por línea que empieza por fecha); así sports-analyze cuenta los
avisos de su pasada sin depender de Cloud Logging (la cuenta del workflow no puede escribir).
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

_PCT = re.compile(r"%(?:\([^)]*\))?[-+ #0]*(?:\d+|\*)?(?:\.\d+)?[sdifrxXeEgGc%]")
_WARN = {"warning", "warn"}
GAP_MIN = 30          # entradas separadas más de 30 min = pasadas distintas


def _parts(node: ast.AST) -> list[str | None] | None:
    """Trozos de la cadena de formato: texto literal o None (= valor). None si no es cadena."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out: list[str | None] = []
        pos = 0
        for m in _PCT.finditer(node.value):
            if m.group() == "%%":
                continue
            out.append(node.value[pos:m.start()])
            out.append(None)
            pos = m.end()
        out.append(node.value[pos:])
        return out
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
            else:
                out.append(None)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _parts(node.left), _parts(node.right)
        if a is None and b is None:
            return None
        return (a if a is not None else [None]) + (b if b is not None else [None])
    return None


def templates(path: str) -> list[tuple[int, str, re.Pattern]]:
    """(línea, plantilla legible, regex) de cada logger.warning(...) del fichero."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _WARN and node.args):
            continue
        parts = _parts(node.args[0])
        if parts is None:
            continue
        shown = "".join("{…}" if p is None else p for p in parts)
        rx = "".join(".*?" if p is None else re.escape(p) for p in parts)
        out.append((node.lineno, " ".join(shown.split()), re.compile(rx, re.S)))
    return out


def classify(msg: str, tpls: list[tuple[int, str, re.Pattern]]) -> tuple[int, str] | None:
    """La plantilla que casa entera con el mensaje; si casan varias, la de más texto literal."""
    best = None
    for line, shown, rx in tpls:
        if rx.fullmatch(msg):
            score = len(shown.replace("{…}", ""))
            if best is None or score > best[0]:
                best = (score, line, shown)
    return (best[1], best[2]) if best else None


_LINE = re.compile(r"^\S+ \S+ (?P<mod>[\w.]+) WARNING (?P<msg>.*)$", re.S)


_EXC = re.compile(r"^([A-Za-z_][\w.]{0,80}?)(?::|$)")


def exc_class(trace_lines: list[str]) -> str | None:
    """Clase de la excepción de un traceback (exc_info=True), sin su mensaje."""
    lines = [x for x in trace_lines if x.strip()]
    if not lines or not any(x.startswith("Traceback") for x in lines):
        return None
    m = _EXC.match(lines[-1].strip())
    return m.group(1) if m else None


def last_pass(rows: list[tuple[datetime, str]]) -> list[tuple[datetime, str]]:
    """Las entradas de la última pasada (corte donde hay un hueco de más de GAP_MIN)."""
    rows = sorted(rows)
    start = 0
    for i in range(1, len(rows)):
        if (rows[i][0] - rows[i - 1][0]).total_seconds() > GAP_MIN * 60:
            start = i
    return rows[start:]


_ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def read_text(path: str) -> list[tuple[datetime, str]]:
    """Entradas del log del runner: cada una empieza por «AAAA-MM-DD HH:MM:SS»; las líneas que
    no empiezan así (trazas) se pegan a la entrada anterior."""
    rows: list[tuple[datetime, str]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            m = _ENTRY.match(line)
            if m:
                rows.append((datetime.fromisoformat(m.group(1)), line))
            elif rows:
                rows[-1] = (rows[-1][0], rows[-1][1] + "\n" + line)
    return rows


def main(argv: list[str]) -> int:
    modules = dict(a.split("=", 1) for a in argv[1:])
    tpls = {mod: templates(path) for mod, path in modules.items()}
    rows = read_text(os.environ["LOGS_TEXT"]) if os.environ.get("LOGS_TEXT") else []
    for e in ([] if os.environ.get("LOGS_TEXT") else json.load(open(os.environ["LOGS_FILE"], encoding="utf-8"))):
        ts, text = e.get("timestamp", ""), e.get("textPayload", "")
        try:
            when = datetime.fromisoformat(ts[:19])
        except ValueError:
            continue
        rows.append((when, text))
    if not rows:
        print("sin avisos en el periodo")
        return 0
    rows = last_pass(rows)
    print(f"Pasada: {rows[0][0]:%Y-%m-%d %H:%M} – {rows[-1][0]:%H:%M} UTC")
    counts: dict[str, Counter] = {mod: Counter() for mod in modules}
    for _, text in rows:
        m = _LINE.match(text)
        if not m or m["mod"] not in modules:
            continue
        first, *rest = m["msg"].split("\n")
        hit = classify(first, tpls[m["mod"]])
        key = hit if hit else (0, "sin plantilla (no casa con ningún aviso del código)")
        exc = exc_class(rest)
        if exc:
            key = (key[0], f"{key[1]} [excepción {exc}]")
        counts[m["mod"]][key] += 1
    for mod, c in counts.items():
        print(f"== {mod}: {sum(c.values())} avisos, {len(c)} tipos")
        for (line, shown), n in c.most_common():
            where = f"línea {line}" if line else "—"
            print(f"  {n:5d}  {where}: {shown[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
