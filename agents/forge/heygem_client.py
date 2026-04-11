# coding: utf-8
"""
HeyGem API Client — wrapper para Duix.Avatar (puerto 8383)
Documentación: https://github.com/duixcom/Duix.Heygem

Endpoints usados:
  POST http://127.0.0.1:8383/easy/submit  → envía job de lip-sync
  GET  http://127.0.0.1:8383/easy/query?code=<uuid> → polling estado
"""

import time
import uuid
import shutil
import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger("HEYGEM_CLIENT")

_HEYGEM_URL  = "http://127.0.0.1:8383"
_DATA_DIR    = Path("C:/duix_avatar_data/face2face")  # montado en /code/data


def _is_available() -> bool:
    """Devuelve True si el contenedor Docker de HeyGem está respondiendo."""
    try:
        import urllib.request
        urllib.request.urlopen(f"{_HEYGEM_URL}/", timeout=3)
        return True
    except Exception:
        return False


def _copy_to_data_dir(src: str, name: str) -> str:
    """
    Copia un fichero al directorio montado en el contenedor Docker.
    Devuelve la ruta interna del contenedor (/code/data/<name>).
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    dst = _DATA_DIR / name
    shutil.copy2(src, dst)
    # Ruta tal como la ve el contenedor
    return f"/code/data/{name}"


def submit_lipsync(
    video_path: str,
    audio_path: str,
    timeout: int = 600,
) -> Optional[str]:
    """
    Envía un job de lip-sync a HeyGem.

    Args:
        video_path: ruta local al vídeo del avatar (MP4, ≥8s)
        audio_path: ruta local al audio generado por ECHO (MP3/WAV)
        timeout:    segundos máximos de espera

    Returns:
        Ruta local al vídeo resultante, o None si falla.
    """
    import urllib.request, urllib.parse, json

    try:
        # ── 1. Copiar ficheros al volumen compartido con el contenedor ────
        job_id = str(uuid.uuid4())
        video_name = f"{job_id}_avatar.mp4"
        audio_name = f"{job_id}_audio.mp3"

        container_video = _copy_to_data_dir(video_path, video_name)
        container_audio = _copy_to_data_dir(audio_path, audio_name)

        # ── 2. Enviar job ─────────────────────────────────────────────────
        payload = json.dumps({
            "audio_url":       container_audio,
            "video_url":       container_video,
            "code":            job_id,
            "chaofen":         0,
            "watermark_switch": 0,
            "pn":              1,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{_HEYGEM_URL}/easy/submit",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        _log.info(f"HeyGem submit OK — job_id={job_id}, resp={result}")

        # ── 3. Polling hasta completar ────────────────────────────────────
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            query_url = f"{_HEYGEM_URL}/easy/query?code={job_id}"
            with urllib.request.urlopen(query_url, timeout=15) as resp:
                status = json.loads(resp.read())

            progress  = status.get("progress", 0)
            state     = status.get("status", "")
            _log.debug(f"HeyGem poll — {job_id}: {state} {progress}%")

            if state in ("completed", "success", "2"):  # algunos builds usan "2"
                # Buscar el MP4 generado en el volumen
                out_path = _find_output(job_id)
                if out_path:
                    _log.info(f"HeyGem completado: {out_path}")
                    return out_path
                _log.warning("HeyGem: estado completado pero no se encontró MP4")
                return None

            if state in ("failed", "error", "-1"):
                _log.warning(f"HeyGem: job fallido — {status}")
                return None

        _log.warning(f"HeyGem: timeout ({timeout}s) — job {job_id}")
        return None

    except Exception as e:
        _log.warning(f"HeyGem: excepción — {e}")
        return None


def _find_output(job_id: str) -> Optional[str]:
    """Busca el MP4 generado por HeyGem en el volumen de datos."""
    # HeyGem guarda el resultado como <job_id>.mp4 o en subdirectorio result/
    candidates = list(_DATA_DIR.glob(f"{job_id}*.mp4"))
    candidates += list((_DATA_DIR / "result").glob(f"{job_id}*.mp4")) \
        if (_DATA_DIR / "result").exists() else []
    # También buscar el más reciente generado durante el job
    if not candidates:
        all_mp4 = sorted(_DATA_DIR.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if all_mp4:
            candidates = [all_mp4[0]]
    return str(candidates[0]) if candidates else None
