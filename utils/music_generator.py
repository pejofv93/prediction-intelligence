# -*- coding: utf-8 -*-
"""
Generador de musica ambiental para NEXUS.
Usa numpy para generar tonos sintetizados sin dependencias externas.
"""
import numpy as np
from pathlib import Path


def _sine(freq: float, duration: float, sr: int = 44100, amp: float = 0.3) -> np.ndarray:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _fade(arr: np.ndarray, sr: int = 44100, fade_s: float = 2.0) -> np.ndarray:
    """Fade-in y fade-out suave."""
    n_fade = int(sr * fade_s)
    n_fade = min(n_fade, len(arr) // 4)
    fade_in  = np.linspace(0, 1, n_fade)
    fade_out = np.linspace(1, 0, n_fade)
    result = arr.copy()
    result[:n_fade]  *= fade_in
    result[-n_fade:] *= fade_out
    return result


def generate_music(mode: str, duration: float, output_path: str, sr: int = 44100) -> str:
    """
    Genera musica ambiental para el modo dado y la guarda como WAV.

    Args:
        mode: 'analisis', 'educativo', 'noticia', 'urgente', 'standard'
        duration: duracion en segundos
        output_path: ruta donde guardar el .wav
        sr: sample rate

    Returns:
        output_path si OK, "" si falla
    """
    try:
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)

        if mode in ("analisis", "standard"):
            # Drone en Do menor: C3(130Hz) + G3(196Hz) + Eb3(155Hz)
            # Tension armonica con ligero vibrato
            vibrato = 1 + 0.003 * np.sin(2 * np.pi * 5.5 * t)
            wave = (
                0.45 * np.sin(2 * np.pi * 130.81 * t * vibrato) +  # C3
                0.25 * np.sin(2 * np.pi * 196.00 * t) +             # G3
                0.20 * np.sin(2 * np.pi * 155.56 * t * vibrato) +   # Eb3
                0.10 * np.sin(2 * np.pi * 261.63 * t)               # C4
            ).astype(np.float32)
            # Pulso sutil cada 2 segundos para dar ritmo
            pulse_env = 0.85 + 0.15 * np.sin(2 * np.pi * 0.5 * t)
            wave *= pulse_env

        elif mode in ("educativo", "tutorial"):
            # Arpegios en Do mayor: C4, E4, G4, C5
            # Patron que se repite cada 2 segundos
            freqs = [261.63, 329.63, 392.00, 523.25]
            period = 2.0
            wave = np.zeros(len(t), dtype=np.float32)
            for i, freq in enumerate(freqs):
                offset = i * period / 4
                env = np.maximum(0, np.sin(np.pi * ((t - offset) % period) / (period / 2)))
                wave += 0.22 * env * np.sin(2 * np.pi * freq * t).astype(np.float32)
            # Anadir fondo suave
            wave += 0.08 * np.sin(2 * np.pi * 130.81 * t).astype(np.float32)

        elif mode == "noticia":
            # Pulso urgente 120bpm con tension
            bpm = 120
            beat_freq = bpm / 60
            beat_env = 0.5 + 0.5 * np.abs(np.sin(np.pi * beat_freq * t))
            wave = (
                0.40 * beat_env * np.sin(2 * np.pi * 220.00 * t) +  # A3
                0.30 * np.sin(2 * np.pi * 164.81 * t) +              # E3
                0.20 * np.sin(2 * np.pi * 329.63 * t)                # E4
            ).astype(np.float32)

        elif mode == "urgente":
            # Alarma musical: tritono (el intervalo mas tenso en musica)
            # F3 + B3 = tritono clasico de tension
            # Con modulacion rapida de amplitud
            mod_env = 0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t)  # 3Hz = tension
            wave = (
                0.45 * mod_env * np.sin(2 * np.pi * 174.61 * t) +  # F3
                0.35 * mod_env * np.sin(2 * np.pi * 246.94 * t) +  # B3 (tritono de F)
                0.20 * np.sin(2 * np.pi * 110.00 * t)               # A2 bajo
            ).astype(np.float32)

        else:
            # Default: tono neutro suave
            wave = (0.3 * np.sin(2 * np.pi * 196.0 * t)).astype(np.float32)

        # Normalizar y aplicar fade
        max_val = np.max(np.abs(wave))
        if max_val > 0:
            wave = wave / max_val * 0.28  # nivel seguro
        wave = _fade(wave, sr=sr, fade_s=min(3.0, duration * 0.1))

        # Convertir a stereo (duplicar canal)
        stereo = np.stack([wave, wave], axis=1)

        # Guardar como WAV
        _save_wav(output_path, stereo, sr)
        return output_path

    except Exception as e:
        import logging
        logging.getLogger("MUSIC").warning(f"generate_music error: {e}")
        return ""


def _save_wav(path: str, data: np.ndarray, sr: int) -> None:
    """Guarda array float32 como WAV sin scipy (solo struct y modulo wave)."""
    import wave as wav_module
    # Convertir float32 [-1,1] a int16
    data_int16 = (data * 32767).astype(np.int16)
    n_channels = data_int16.shape[1] if data_int16.ndim > 1 else 1
    n_frames = data_int16.shape[0]

    with wav_module.open(path, 'w') as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(2)  # 16 bits
        wf.setframerate(sr)
        if n_channels == 2:
            # Intercalar canales: L R L R ...
            interleaved = data_int16.flatten()
        else:
            interleaved = data_int16.flatten()
        wf.writeframes(interleaved.tobytes())


def generate_transition_click(output_path: str, sr: int = 44100) -> str:
    """Click metalico sutil para transiciones entre escenas."""
    try:
        duration = 0.08  # 80ms
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        # Tono alto decayendo rapido
        decay = np.exp(-t * 80)
        wave = (0.4 * decay * np.sin(2 * np.pi * 1200 * t)).astype(np.float32)
        stereo = np.stack([wave, wave], axis=1)
        _save_wav(output_path, stereo, sr)
        return output_path
    except Exception:
        return ""
