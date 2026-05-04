#!/usr/bin/env python3
"""
cleanup_volume.py - Limpieza inteligente del volumen persistente Railway
Uso:
  python scripts/cleanup_volume.py           # dry-run (muestra qué borraría)
  python scripts/cleanup_volume.py --confirm # borra realmente
  python scripts/cleanup_volume.py --days 14 # archivos con >14 días

Nunca borra:
  - cryptoverdad.db (memoria del sistema)
  - Archivos .json/.toml/.env de configuración
  - Directorio models/ (modelos TTS, ~100MB, tarda en descargar)
  - Archivos del último pipeline publicado (protección anti-borrado accidental)
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

_REPO_ROOT = Path(__file__).resolve().parents[1]

# En Railway es /app/output; en local es output/ dentro del repo
if Path("/app/output").exists():
    OUTPUT_DIR = Path("/app/output")
else:
    OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(_REPO_ROOT / "output")))

# Nunca tocar estos directorios (modelos TTS, etc.)
PROTECTED_DIRS = {"models", "tts"}

# Nunca tocar estos archivos por nombre
PROTECTED_FILES = {"cryptoverdad.db"}

# Nunca tocar estas extensiones (configuración)
PROTECTED_EXTENSIONS = {".json", ".toml", ".yaml", ".yml", ".env", ".ini", ".cfg"}

# Extensiones de media que sí se pueden purgar
PURGEABLE_EXTENSIONS = {".mp4", ".wav", ".mp3", ".avi", ".jpg", ".jpeg", ".png", ".gif"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(bytes_: int) -> str:
    if bytes_ >= 1_000_000_000:
        return f"{bytes_/1e9:.2f} GB"
    elif bytes_ >= 1_000_000:
        return f"{bytes_/1e6:.1f} MB"
    return f"{bytes_/1e3:.0f} KB"


def _get_db_path() -> Path:
    candidates = [
        OUTPUT_DIR / "cryptoverdad.db",
        _REPO_ROOT / "cryptoverdad.db",
        Path("/app/cryptoverdad.db"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return _REPO_ROOT / "cryptoverdad.db"


def _recent_pipeline_ids(days: int = 7) -> set:
    """Pipeline IDs publicados en los últimos N días — sus archivos quedan protegidos."""
    db_path = _get_db_path()
    if not db_path.exists():
        return set()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id FROM pipelines WHERE created_at >= ? AND youtube_url IS NOT NULL",
            (cutoff,),
        ).fetchall()
        conn.close()
        # Guardamos prefijos de 8 chars del UUID para detectarlos en paths
        return {r[0][:8] for r in rows}
    except Exception:
        return set()


def _is_protected(path: Path, recent_ids: set) -> bool:
    """True si el archivo no debe tocarse bajo ningún concepto."""
    if path.name in PROTECTED_FILES:
        return True
    if path.suffix.lower() in PROTECTED_EXTENSIONS:
        return True
    for part in path.parts:
        if part in PROTECTED_DIRS:
            return True
    path_str = str(path)
    for pid in recent_ids:
        if pid and pid in path_str:
            return True
    return False


def _most_recent_mp4(output_dir: Path, n: int = 3) -> set:
    """Protege los N .mp4 más recientes (el último vídeo publicado)."""
    mp4s = sorted(
        output_dir.rglob("*.mp4"),
        key=lambda f: f.stat().st_mtime if f.exists() else 0,
        reverse=True,
    )[:n]
    return {f for f in mp4s}


def find_purgeable(output_dir: Path, days_old: int, recent_ids: set) -> list:
    """
    Devuelve lista de (Path, size_bytes) candidatos a borrar.
    """
    if not output_dir.exists():
        return []

    cutoff_media = datetime.now() - timedelta(days=days_old)
    cutoff_logs = datetime.now() - timedelta(days=14)
    candidates = []
    protected_mp4s = _most_recent_mp4(output_dir)

    for f in output_dir.rglob("*"):
        if not f.is_file():
            continue
        if _is_protected(f, recent_ids):
            continue
        if f in protected_mp4s:
            continue

        size = f.stat().st_size
        path_str = str(f)

        # Frames temporales de DAEDALUS — siempre purgables
        if "temp_frames" in path_str:
            candidates.append((f, size))
            continue

        # MoviePy temp files
        if f.name.startswith("TEMP_MPY_") or "mpy_temp" in f.name.lower():
            candidates.append((f, size))
            continue

        # Clips Pexels cacheados viejos
        if "pexels" in f.name.lower() and f.suffix == ".mp4":
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff_media:
                candidates.append((f, size))
            continue

        # Archivos de media viejos (>N días)
        if f.suffix.lower() in PURGEABLE_EXTENSIONS:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff_media:
                candidates.append((f, size))
            continue

        # Logs viejos (>14 días)
        if f.suffix.lower() in {".log", ".txt"} and ("logs" in path_str or "log" in f.parent.name):
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff_logs:
                candidates.append((f, size))

    return candidates


def top_consumers(output_dir: Path, n: int = 5) -> list:
    """Retorna los N subdirectorios con más espacio ocupado."""
    if not output_dir.exists():
        return []
    results = []
    try:
        for item in output_dir.iterdir():
            if item.is_dir():
                size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
            else:
                size = item.stat().st_size
            results.append((item, size))
    except Exception:
        pass
    return sorted(results, key=lambda x: x[1], reverse=True)[:n]


# ── Main ──────────────────────────────────────────────────────────────────────

def run_cleanup(output_dir: Path, days_old: int, confirm: bool) -> int:
    """
    Ejecuta la limpieza. Retorna bytes liberados (0 en dry-run).
    """
    console.rule("[bold #F7931A]NEXUS Volume Cleanup[/]")
    console.print(f"[dim]Directorio: {output_dir}[/]")

    # 1. Estado actual del disco
    try:
        usage = shutil.disk_usage(output_dir)
        pct = usage.used / usage.total * 100
        color = "red" if pct > 85 else ("yellow" if pct > 70 else "green")
        console.print(
            f"\n[bold]Disco actual:[/] {_fmt(usage.used)} usados / {_fmt(usage.total)} total "
            f"([{color}]{pct:.1f}%[/] usado · {_fmt(usage.free)} libres)\n"
        )
    except Exception as e:
        console.print(f"[yellow]No se pudo leer disco: {e}[/]")

    # 2. Top consumidores
    tops = top_consumers(output_dir)
    if tops:
        t = Table(title="Top consumidores", box=box.SIMPLE)
        t.add_column("Directorio/archivo")
        t.add_column("Tamaño", justify="right")
        for path, size in tops:
            try:
                label = str(path.relative_to(output_dir))
            except ValueError:
                label = path.name
            t.add_row(label, _fmt(size))
        console.print(t)

    # 3. Encontrar candidatos
    recent_ids = _recent_pipeline_ids(days=days_old)
    console.print(
        f"[dim]Protegiendo {len(recent_ids)} pipeline(s) publicado(s) "
        f"en los últimos {days_old} días + 3 MP4 más recientes[/]\n"
    )

    candidates = find_purgeable(output_dir, days_old, recent_ids)
    total_purgeable = sum(s for _, s in candidates)

    if not candidates:
        console.print("[green]Nada que limpiar. El volumen está ordenado.[/]")
        return 0

    # 4. Mostrar candidatos
    table = Table(
        title=f"Archivos purgables (>{days_old} dias)", box=box.SIMPLE_HEAVY
    )
    table.add_column("Archivo", style="dim", no_wrap=False)
    table.add_column("Tamaño", justify="right")
    table.add_column("Modificado")

    shown = sorted(candidates, key=lambda x: x[1], reverse=True)[:40]
    for f, size in shown:
        try:
            rel = str(f.relative_to(output_dir))
        except ValueError:
            rel = f.name
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        table.add_row(rel, _fmt(size), mtime)

    if len(candidates) > 40:
        table.add_row(f"... y {len(candidates)-40} archivos más", "", "")
    console.print(table)

    console.print(
        f"\n[bold]Total a liberar:[/] [green]{_fmt(total_purgeable)}[/] "
        f"en [yellow]{len(candidates)}[/] archivos"
    )

    if not confirm:
        console.print(
            "\n[dim yellow]Modo DRY-RUN — nada ha sido borrado.[/]\n"
            "Para borrar realmente:\n"
            "  [cyan]python scripts/cleanup_volume.py --confirm[/]\n"
            "En Railway:\n"
            "  [cyan]railway run python scripts/cleanup_volume.py --confirm[/]"
        )
        return 0

    # 5. Borrar
    console.print("\n[bold red]Borrando...[/]")
    deleted_bytes = 0
    errors = 0
    for f, size in candidates:
        try:
            f.unlink()
            deleted_bytes += size
        except Exception as e:
            console.print(f"[red]Error: {f.name}: {e}[/]")
            errors += 1

    # Limpiar directorios vacíos (excepto protegidos)
    for d in sorted(output_dir.rglob("*"), reverse=True):
        if d.is_dir() and not any(part in PROTECTED_DIRS for part in d.parts):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except Exception:
                pass

    # Estado final
    try:
        after = shutil.disk_usage(output_dir)
        pct_after = after.used / after.total * 100
        console.print(
            f"\n[bold green]Limpieza completada.[/]\n"
            f"  Liberados: [green]{_fmt(deleted_bytes)}[/]\n"
            f"  Errores:   {errors}\n"
            f"  Disco ahora: {pct_after:.1f}% usado ({_fmt(after.free)} libres)"
        )
    except Exception:
        pass

    return deleted_bytes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Limpieza del volumen Railway de NEXUS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Borrar realmente (sin este flag es dry-run)"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Purgar archivos con más de N días de antigüedad (default: 7)"
    )
    parser.add_argument(
        "--output-dir", type=str, default=str(OUTPUT_DIR),
        help=f"Directorio a limpiar (default: {OUTPUT_DIR})"
    )
    args = parser.parse_args()

    freed = run_cleanup(Path(args.output_dir), args.days, args.confirm)
    sys.exit(0)
