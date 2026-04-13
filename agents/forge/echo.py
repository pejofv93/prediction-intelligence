# -*- coding: utf-8 -*-
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/forge/echo.py
ECHO — Sintetizador de Voz de NEXUS.

Convierte ctx.script a audio MP3 usando edge-tts (es-ES-AlvaroNeural).
Fallback a pyttsx3 si edge-tts falla.
Preprocesa el texto con preprocess_script(): elimina markdown, headers,
emoticonos y SSML residual; convierte precios y símbolos a palabras.
edge-tts recibe SOLO texto plano en español — no soporta SSML.
"""

import asyncio
import re
import os
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from core.base_agent import BaseAgent
from core.context import Context

console = Console()

OUTPUT_AUDIO_DIR = Path(__file__).resolve().parents[2] / "output" / "audio"

# Velocidad de lectura estimada en palabras por minuto
WORDS_PER_MINUTE = 150


def _pct_decimal_to_words(m: re.Match) -> str:
    """Convierte porcentajes decimales a palabras naturales en español.
    2.5% → "dos y medio por ciento"
    1.5% → "uno y medio por ciento"
    3.25% → "tres coma veinticinco por ciento"
    """
    try:
        val = float(m.group(1))
        int_part = int(val)
        frac = round(val - int_part, 4)

        if abs(frac - 0.5) < 0.01:
            base = "medio" if int_part == 0 else f"{int_part} y medio"
        elif abs(frac - 0.25) < 0.01:
            base = "un cuarto" if int_part == 0 else f"{int_part} y cuarto"
        elif abs(frac - 0.75) < 0.01:
            base = "tres cuartos" if int_part == 0 else f"{int_part} y tres cuartos"
        elif frac == 0.0:
            base = str(int_part)
        else:
            # Genérico: "X coma Y"
            decimals = str(round(frac * 100)).rstrip('0') or '0'
            base = f"{int_part} coma {decimals}"

        return f"{base} por ciento"
    except Exception:
        return m.group(0).replace('%', ' por ciento')


def _limit_sentences(text: str, max_words: int = 15) -> str:
    """Divide oraciones de más de max_words palabras en dos, cortando en la
    coma o conjunción más cercana al punto medio."""
    _CONJ = {'pero', 'aunque', 'porque', 'cuando', 'donde',
             'mientras', 'además', 'embargo', 'como',
             'sino', 'pues', 'entonces', 'después', 'sin'}

    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    result = []

    for sent in parts:
        words = sent.split()
        if len(words) <= max_words:
            result.append(sent)
            continue

        mid = len(words) // 2
        best_idx = mid      # fallback: cortar en el medio
        best_dist = len(words)

        for i in range(1, len(words) - 1):
            w = words[i].lower().strip('.,;:')
            prev = words[i - 1].lower().strip('.,;:')
            dist = abs(i - mid)
            # Conjunción principal o coma antes
            is_break = (w in _CONJ) or words[i - 1].endswith(',')
            # "y" es break si la palabra anterior no es número (evita "8 y medio")
            if not is_break and w == 'y' and not prev.replace('.', '').isdigit():
                is_break = True
            if is_break and dist < best_dist:
                best_dist = dist
                best_idx = i

        part1 = ' '.join(words[:best_idx]).rstrip(',')
        part2 = ' '.join(words[best_idx:])

        if not part1.rstrip().endswith(('.', '!', '?')):
            part1 += '.'
        if part2 and not part2[0].isupper():
            part2 = part2[0].upper() + part2[1:]

        result.append(part1)
        if len(part2.split()) > max_words:
            result.append(_limit_sentences(part2, max_words))
        else:
            result.append(part2)

    return ' '.join(result)


def preprocess_script(text: str) -> str:
    """
    Preprocesa el guion para síntesis de voz con edge-tts (texto plano).

    edge-tts NO soporta SSML — lo lee como texto literal.
    Esta función produce texto limpio en español, listo para leer en voz alta.

    1.  Elimina headers markdown (## HOOK, ## INTRO, etc.)
    2.  Elimina emoticonos y caracteres unicode especiales.
    3.  Convierte [PAUSA]/[PAUSA_LARGA] a coma/punto; elimina etiquetas de bloque.
    4.  Elimina formato markdown (**, *, guiones de lista).
    5.  Elimina tags XML/SSML residuales.
    6.  Convierte notaciones de miles/millones en inglés (billion/trillion).
    7.  Convierte "X.Y mil millones" en español a forma reducida.
    8.  Convierte porcentajes decimales (2.5% → "dos y medio por ciento").
    9.  Convierte precios $X,XXX.XX a palabras.
    10. Sustituye símbolos (@, *, .com, #, &, %, BTC, ETH, SOL).
    11. Elimina "dólares dólares" duplicados.
    12. Normaliza espacios y saltos de línea.
    13. Limita oraciones a máximo 12 palabras.
    """

    # ── 0. Aplicar pronunciacion espanola (anglicismos y acronimos) ──────────
    try:
        from utils.pronunciation import apply_pronunciation
        text = apply_pronunciation(text)
    except Exception:
        pass  # si falla, continuar sin pronunciacion

    # ── 1. Eliminar headers markdown ─────────────────────────────────────────
    text = re.sub(r'^#+\s+.*$', '', text, flags=re.MULTILINE)

    # ── 2. Eliminar emoticonos y caracteres unicode no ASCII hablados ─────────
    emoji_pattern = re.compile(
        r'[\U00002600-\U000027BF'
        r'\U0001F300-\U0001F64F'
        r'\U0001F680-\U0001F6FF'
        r'\U0001F700-\U0001F77F'
        r'\U0001F780-\U0001F7FF'
        r'\U0001F800-\U0001F8FF'
        r'\U0001F900-\U0001F9FF'
        r'\U0001FA00-\U0001FA6F'
        r'\U0001FA70-\U0001FAFF'
        r'\U00002702-\U000027B0'
        r']+',
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub('', text)

    # ── 3. Eliminar marcadores de sección y convertir pausas ─────────────────
    # Pausas: usar placeholders internos para sobrevivir la limpieza de puntuación.
    # Al final del paso 12 se restauran como comas (pausa natural en edge-tts).
    # \x02 = pausa corta · \x03 = pausa larga
    text = re.sub(r'\[PAUSA_LARGA\]', '\x03', text)
    text = re.sub(r'\[PAUSA\]', '\x02', text)
    # Marcadores gráficos
    text = re.sub(r'\[GRAFICO_GRANDE\]', '.', text)
    # Etiquetas de señala / senala
    text = re.sub(r'\[SE[NÑ]ALA:[^\]]+\]', '', text, flags=re.I)
    # Etiquetas de bloque de CALÍOPE: [PRECIO], [ANÁLISIS], [ANALISIS],
    # [SENTIMIENTO], [DOMINANCIA], [VOLUMEN], [ADOPCION], [ADOPCIÓN],
    # [PREDICCION], [PREDICCIÓN], [NOTICIA], [GENERAL], [DATO:X], etc.
    text = re.sub(
        r'\[(?:PRECIO|AN[AÁ]LISIS|SENTIMIENTO|DOMINANCIA|VOL[ÚU]MEN'
        r'|ADOPCI[OÓ]N|PREDICCI[OÓ]N|NOTICIA|GENERAL'
        r'|DATO(?::[^\]]+)?)\]',
        '', text, flags=re.I,
    )
    # Cualquier otra etiqueta entre corchetes que no sea [PRECIO:ticker]
    text = re.sub(r'\[(?!PRECIO:)[^\]]+\]', '', text)

    # ── 4. Eliminar tags XML/SSML residuales ──────────────────────────────────
    text = re.sub(r'<[^>]+>', '', text)

    # ── 5. Eliminar formato markdown ─────────────────────────────────────────
    text = re.sub(r'\*{2}([^*]+)\*{2}', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'^\s*-\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^-{2,}\s*$', '', text, flags=re.MULTILINE)

    # ── 6. Notación inglesa: billion / trillion ───────────────────────────────
    # "$2.5 billion" → "dos mil quinientos millones de dólares"
    # "1.2B" / "$1.2B" → igual
    def _en_large(m: re.Match) -> str:
        has_dollar = '$' in (m.group(1) or '')
        val = float(m.group(2))
        unit = m.group(3).lower()
        if 'b' in unit:
            millones_total = round(val * 1000)
        else:  # trillion
            millones_total = round(val * 1_000_000)

        text_num = _millones_to_words(millones_total)
        return text_num + (' de dólares' if has_dollar else '')

    text = re.sub(
        r'(\$?)(\d+(?:[.,]\d+)?)\s*(billion|trillion|B|T)\b',
        _en_large, text, flags=re.IGNORECASE,
    )

    # ── 7. "X.Y mil millones" en español ─────────────────────────────────────
    # "2.5 mil millones" → "dos mil quinientos millones"
    def _es_mil_millones(m: re.Match) -> str:
        val = float(m.group(1).replace(',', '.'))
        return _millones_to_words(round(val * 1000))

    text = re.sub(
        r'(\d+[.,]\d+)\s*mil\s*millones',
        _es_mil_millones, text, flags=re.IGNORECASE,
    )

    # ── 8. Porcentajes decimales → palabras ───────────────────────────────────
    # ANTES de reemplazar el símbolo %
    # 2.5% → "dos y medio por ciento"
    text = re.sub(r'(\d+\.\d+)%', _pct_decimal_to_words, text)

    # ── 9. Conversión de precios a palabras ───────────────────────────────────
    # Formato europeo: $83.241,67 (punto = separador miles, coma = decimal)
    def _eu_price(m: re.Match) -> str:
        raw = m.group(1).replace('.', '')
        return _value_to_words(float(raw))

    text = re.sub(r'\$(\d{1,3}(?:\.\d{3})+)(?:,\d+)?', _eu_price, text)

    # Formato anglosajón: $67,432.18 o $67432
    def _us_price(m: re.Match) -> str:
        raw = m.group(1).replace(',', '')
        return _value_to_words(float(raw))

    text = re.sub(r'\$(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', _us_price, text)

    # Números ≥4 dígitos seguidos de "dólares" o "usd"
    text = re.sub(
        r'(\b\d{4,}(?:[.,]\d{3})*\b)(?=\s*(?:dólares?|usd)\b)',
        lambda m: _value_to_words(float(m.group(1).replace('.', '').replace(',', ''))),
        text, flags=re.IGNORECASE,
    )

    # ── 10. Sustitución de símbolos ───────────────────────────────────────────
    # URLs y dominios .com / .net / etc. → eliminar
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\b\w+\.(com|net|org|io|co|es|mx)\b', '', text, flags=re.IGNORECASE)
    # @handles → eliminar
    text = re.sub(r'@\w+', '', text)
    # Asteriscos solos o como marcadores
    text = re.sub(r'\*+', '', text)
    # % entero (los decimales ya fueron convertidos arriba)
    text = re.sub(r'(\d+)\s*%', r'\1 por ciento', text)
    # Resto de símbolos
    replacements = [
        (r'&',         ' y '),
        (r'#',         ''),
        (r'\bBTC\b',   'Bitcoin'),
        (r'\bETH\b',   'Ethereum'),
        (r'\bSOL\b',   'Solana'),
    ]
    for pat, rep in replacements:
        text = re.sub(pat, rep, text)

    # ── 11. Limpiar artefactos de conversión ─────────────────────────────────
    # "X millones dólares" → "X millones de dólares"
    text = re.sub(r'\bmillones\s+dólares\b', 'millones de dólares', text, flags=re.IGNORECASE)
    # "dólares dólares" duplicado
    text = re.sub(r'\bdólares\s+dólares\b', 'dólares', text, flags=re.IGNORECASE)

    # ── 12. Normalizar espacios y saltos de línea ─────────────────────────────
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'^\s+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n(?!\n)', ' ', text)
    text = re.sub(r'([^.!?,])\n\n', r'\1. ', text)
    text = re.sub(r'\s+([.,;:!?])', r'\1', text)
    text = re.sub(r';\s*,', ',', text)
    text = re.sub(r',\s*;', ',', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\.\s*,', '.', text)
    text = re.sub(r',\s*\.', '.', text)
    text = re.sub(r'\.\s*\.', '.', text)
    text = re.sub(r' {2,}', ' ', text)

    # Restaurar pausas (placeholders → texto final para edge-tts)
    # \x02 = [PAUSA] → coma (pausa corta natural en TTS)
    # \x03 = [PAUSA_LARGA] → punto (pausa larga natural en TTS)
    text = text.replace('\x02', ',')
    text = text.replace('\x03', '.')
    # Limpiar colisiones de puntuacion que pueden surgir al restaurar
    # ej: ". ," → "." o ", ," → ","
    text = re.sub(r'\.\s*,', '.', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r' {2,}', ' ', text)

    # Limpieza final de puntuacion incorrecta (doble seguro frente a [PAUSA] del LLM)
    text = re.sub(r'([.?!])\s*,', r'\1', text)   # "fin. ," → "fin."
    text = re.sub(r'\s+([,.])', r'\1', text)       # " ," → ","
    text = re.sub(r',{2,}', ',', text)             # ",," → ","
    text = re.sub(r'^\s*,\s*', '', text, flags=re.MULTILINE)  # coma al inicio de linea
    text = re.sub(r' {2,}', ' ', text)

    # ── 12b. Limpieza de puntuacion degenerada (redundancia explicita) ────────
    # Estos patrones pueden surgir de la composicion de etiquetas del LLM
    text = re.sub(r'\.\s+,', '.', text)   # ". ,"  → "."
    text = re.sub(r'\?\s+,', '?', text)   # "? ,"  → "?"
    text = re.sub(r'!\s+,', '!', text)    # "! ,"  → "!"
    text = re.sub(r' +,', ',', text)      # "  ,"  → ","
    text = re.sub(r',,+', ',', text)      # ",,"   → ","

    text = text.strip()

    # ── 13. Limitar oraciones a máximo 15 palabras ────────────────────────────
    text = _limit_sentences(text, max_words=15)

    return text


def _millones_to_words(millones: int) -> str:
    """Convierte un número de millones a palabras en español.
    1000 millones → "mil millones"
    2500 millones → "dos mil quinientos millones"
    """
    if millones >= 1_000:
        miles = millones // 1000
        resto = millones % 1000
        if resto == 0:
            return f"{miles} mil millones"
        centenas = round(resto / 100) * 100
        if centenas:
            return f"{miles} mil {int(centenas)} millones"
        return f"{miles} mil millones"
    return f"{millones} millones"


def _value_to_words(value: float) -> str:
    """Convierte un valor numérico (precio en dólares) a palabras en español."""
    value = round(value)

    if value >= 1_000_000_000:
        millones_total = round(value / 1_000_000)
        return _millones_to_words(millones_total) + " de dólares"

    if value >= 1_000_000:
        millones = round(value / 1_000_000)
        return f"{millones} millones de dólares"

    if value >= 1_000:
        miles_entero = int(value / 1_000)   # floor, no redondeo
        resto = int(value % 1_000)
        if miles_entero == 0:
            miles_entero = 1
        if miles_entero == 1:
            if resto == 0:
                return "mil dólares"
            centenas = round(resto / 100) * 100
            if centenas == 0:
                return "alrededor de mil dólares"
            return f"mil {int(centenas)} dólares"
        if resto >= 500:
            return f"{miles_entero} mil quinientos dólares"
        return f"{miles_entero} mil dólares"

    return f"{int(value)} dólares"


class ECHO(BaseAgent):
    """
    Sintetizador de voz CryptoVerdad.

    - Voz principal: es-ES-AlvaroNeural (edge-tts)
    - Fallback: pyttsx3 (sin SSML)
    - Rate base: -5% para voz más natural; +5% en modo urgente
    - Guarda MP3 en output/audio/{pipeline_id}.mp3
    - Calcula duracion estimada y la loguea
    - Preprocesa el guion con preprocess_script() antes de sintetizar
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config)
        self.db = db
        OUTPUT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    # ── Voces edge-tts en orden de prioridad ─────────────────────────────────
    _EDGE_VOICES = [
        "es-ES-AlvaroNeural",   # Principal: español de España
        "es-MX-JorgeNeural",    # Fallback 1: español de México
        "es-AR-TomasNeural",    # Fallback 2: español de Argentina
    ]

    # ── edge-tts ──────────────────────────────────────────────────────────────

    async def _synthesize_edge(
        self, clean_text: str, output_path: str,
        rate: str = "-8%", pitch: str = "+0Hz",
        voice: str = "es-ES-AlvaroNeural",
    ) -> None:
        """
        Sintetiza texto plano con edge-tts y guarda en output_path.
        Acepta voice como parámetro para rotar entre voces de fallback.
        """
        import edge_tts

        communicate = edge_tts.Communicate(
            text=clean_text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            volume="+10%",
        )
        await communicate.save(output_path)

    def _synthesize_edge_with_retry(
        self, clean_text: str, output_path: str, rate: str, pitch: str
    ) -> str:
        """
        Intenta edge-tts con las 3 voces españolas, 2 intentos cada una.
        Devuelve el nombre de la voz usada, o lanza excepción si todas fallan.
        """
        import time as _time

        last_exc: Exception = Exception("sin intentos")
        for voice in self._EDGE_VOICES:
            for attempt in range(2):
                try:
                    asyncio.run(
                        self._synthesize_edge(clean_text, output_path, rate, pitch, voice)
                    )
                    return voice
                except Exception as exc:
                    last_exc = exc
                    self.logger.warning(
                        f"edge-tts [{voice}] intento {attempt + 1}/2 fallo: {exc}"
                    )
                    if attempt == 0:
                        _time.sleep(3)  # espera breve antes de reintentar misma voz
        raise last_exc

    # ── Coqui TTS (motor principal offline, voz española natural) ────────────

    # Singleton — el modelo VITS tarda ~3s en cargar, no recargar en cada pipeline
    _coqui_instance = None
    _COQUI_MODEL_NAME = "tts_models/es/css10/vits"
    # Directorio de modelos en volumen persistente Railway
    _COQUI_MODEL_DIR = Path(__file__).resolve().parents[2] / "output" / "models" / "tts"

    # Velocidad por modo (VITS acepta speed: 1.0 = normal)
    _COQUI_SPEED_MAP = {
        "urgente":   1.15,
        "noticia":   1.10,
        "analisis":  0.92,
        "standard":  0.92,
        "educativo": 0.88,
        "tutorial":  0.88,
    }

    @classmethod
    def _get_coqui(cls):
        """Carga Coqui TTS (singleton). Descarga modelo en primer uso (~100MB)."""
        if cls._coqui_instance is None:
            import os
            # Apuntar caché de modelos al volumen persistente Railway
            cls._COQUI_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            os.environ["TTS_HOME"] = str(cls._COQUI_MODEL_DIR)
            from TTS.api import TTS  # type: ignore
            console.print(
                f"[yellow]Coqui TTS: cargando modelo {cls._COQUI_MODEL_NAME} "
                f"(primera vez descarga ~100MB)...[/]"
            )
            cls._coqui_instance = TTS(cls._COQUI_MODEL_NAME, progress_bar=False, gpu=False)
            console.print("[green]Coqui TTS: modelo cargado OK[/]")
        return cls._coqui_instance

    def _synthesize_coqui(self, text: str, output_path: str, mode: str = "") -> str:
        """
        Síntesis local con Coqui TTS (tts_models/es/css10/vits).
        Voz masculina española natural. Sin API, sin red tras primer uso.
        Genera WAV temporal → convierte a MP3 con ffmpeg.
        Devuelve el nombre del modelo usado.
        """
        import subprocess
        import tempfile

        speed = self._COQUI_SPEED_MAP.get(mode, 0.95)
        tts = self._get_coqui()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False,
                                          dir=OUTPUT_AUDIO_DIR) as tmp:
            tmp_wav = tmp.name

        try:
            try:
                tts.tts_to_file(text=text, file_path=tmp_wav, speed=speed)
            except TypeError:
                # Algunos modelos VITS no aceptan speed — usar sin él
                tts.tts_to_file(text=text, file_path=tmp_wav)

            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", tmp_wav,
                    "-acodec", "libmp3lame", "-q:a", "4",
                    output_path,
                ],
                check=True,
                capture_output=True,
            )
        finally:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass

        out = Path(output_path)
        if not out.exists() or out.stat().st_size < 1024:
            raise RuntimeError(
                f"Coqui: MP3 vacío tras conversión "
                f"(size={out.stat().st_size if out.exists() else 0})"
            )

        return self._COQUI_MODEL_NAME

    # ── pyttsx3 + espeak-ng (fallback offline, funciona en Railway) ──────────

    def _synthesize_pyttsx3(self, text: str, output_path: str, mode: str = "") -> str:
        """
        Síntesis offline con pyttsx3 + espeak-ng.
        Funciona en Railway/Debian sin depender de APIs externas.
        Devuelve el id de voz usada.

        Selección de voz:
          - Busca voces cuyo id contenga 'es' o cuyo name contenga 'spanish'
          - Si no hay voz española, usa la primera disponible
        Velocidad ajustada por modo (igual que edge-tts):
          urgente → 175 wpm · noticia → 165 · analisis/standard → 135
          educativo → 125 · default → 145
        """
        import pyttsx3

        _RATE_MAP = {
            "urgente":   175,
            "noticia":   165,
            "analisis":  135,
            "standard":  135,
            "educativo": 125,
            "tutorial":  125,
        }
        wpm = _RATE_MAP.get(mode, 145)

        engine = pyttsx3.init()
        voices = engine.getProperty("voices") or []

        # Buscar voz española
        spanish_voice = next(
            (v for v in voices
             if "spanish" in (v.name or "").lower()
             or "_es" in (v.id or "").lower()
             or "-es" in (v.id or "").lower()),
            voices[0] if voices else None,
        )

        voice_id = ""
        if spanish_voice:
            engine.setProperty("voice", spanish_voice.id)
            voice_id = spanish_voice.id
            self.logger.info(f"pyttsx3 voz seleccionada: {spanish_voice.id} | {spanish_voice.name}")
        else:
            self.logger.warning("pyttsx3: no se encontró voz española — usando voz por defecto")

        engine.setProperty("rate", wpm)
        engine.setProperty("volume", 1.0)

        # pyttsx3.save_to_file acepta ruta .mp3 en Linux con espeak-ng
        engine.save_to_file(text, output_path)
        engine.runAndWait()

        # Verificar que el archivo se generó y tiene contenido
        out = Path(output_path)
        if not out.exists() or out.stat().st_size < 1024:
            raise RuntimeError(
                f"pyttsx3 generó archivo vacío o inexistente: {output_path} "
                f"(size={out.stat().st_size if out.exists() else 0})"
            )

        return voice_id

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _estimate_duration(self, script: str) -> float:
        """Estima duracion en minutos segun numero de palabras."""
        words = len(script.split())
        return round(words / WORDS_PER_MINUTE, 2)

    def _get_rate(self, ctx: Context) -> tuple:
        """
        Devuelve (rate, pitch) segun el modo del pipeline.

        Tabla de modos:
          noticia            → rate=+10%, pitch=+5%   (periodista breaking news)
          urgente            → rate=+15%, pitch=+8%   (alarmante, maxima tension)
          analisis / standard→ rate=-8%,  pitch=+0%   (analista pausado, serio)
          educativo/tutorial → rate=-12%, pitch=+3%   (profesor amigable, didactico)
          default            → rate=-5%,  pitch=+0%   (neutro)

        rate y pitch se expresan en formato +X% / -X% aceptado por edge-tts.
        """
        mode = getattr(ctx, "mode", None) or ""
        is_urgent = getattr(ctx, "is_urgent", False)

        # pitch en formato Hz (edge-tts no acepta % para pitch, solo Hz)
        if mode == "urgente":
            return ("+15%", "+8Hz")
        if mode == "noticia":
            return ("+10%", "+5Hz")
        if mode in ("analisis", "standard"):
            return ("-8%", "+0Hz")
        if mode in ("educativo", "tutorial"):
            return ("-12%", "+3Hz")
        return ("-5%", "+0Hz")

    # ── run() ─────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("ECHO iniciado")
        console.print(
            Panel(
                "[bold #F7931A]ECHO[/] — Sintetizador de Voz\n"
                f"Voz: [italic]es-ES-AlvaroNeural[/] · Pipeline: [dim]{ctx.pipeline_id[:8]}[/]",
                border_style="#F7931A",
            )
        )

        if not ctx.script:
            msg = "ctx.script vacio — no hay guion para sintetizar"
            self.logger.error(msg)
            ctx.add_error("ECHO", msg)
            return ctx

        try:
            output_path = str(OUTPUT_AUDIO_DIR / f"{ctx.pipeline_id}.mp3")
            rate, pitch = self._get_rate(ctx)

            console.print(
                f"[dim]Preprocesando guion para voz natural...[/]"
            )

            # Preprocesar el guion — devuelve texto plano listo para voz
            clean_script = preprocess_script(ctx.script)
            self.logger.info("Guion preprocesado (texto plano, sin SSML)")

            console.print(
                f"[dim]Motor principal: Coqui TTS (es/css10/vits) · "
                f"destino: output/audio/{ctx.pipeline_id[:8]}...mp3[/]"
            )

            # ── Cadena TTS: Coqui → edge-tts → pyttsx3 → silencio-ffmpeg
            mode_str = getattr(ctx, "mode", "") or ""
            try:
                console.print("[dim]Sintetizando con Coqui TTS (voz española natural)...[/]")
                model_used = self._synthesize_coqui(clean_script, output_path, mode=mode_str)
                self.logger.info(f"Audio generado con Coqui TTS [{model_used}]")
                engine_used = f"Coqui TTS ({model_used})"
                console.print(f"[green]Coqui TTS OK:[/] modelo={model_used}")
            except Exception as coqui_err:
                self.logger.warning(
                    f"Coqui TTS fallo ({coqui_err}) — probando edge-tts..."
                )
                console.print("[yellow]Coqui TTS no disponible. Probando edge-tts...[/]")
                try:
                    voice_used = self._synthesize_edge_with_retry(
                        clean_script, output_path, rate, pitch
                    )
                    self.logger.info(f"Audio generado con edge-tts [{voice_used}]")
                    engine_used = f"edge-tts ({voice_used})"
                except Exception as edge_err:
                    self.logger.warning(
                        f"edge-tts (3 voces) fallaron: {edge_err} — probando pyttsx3..."
                    )
                    console.print("[yellow]edge-tts no disponible. Probando pyttsx3 (espeak-ng)...[/]")
                    try:
                        voice_id = self._synthesize_pyttsx3(clean_script, output_path, mode=mode_str)
                        self.logger.info(f"Audio generado con pyttsx3 [{voice_id}]")
                        engine_used = f"pyttsx3 ({voice_id})"
                        console.print(f"[green]pyttsx3 OK:[/] voz={voice_id}")
                    except Exception as pyttsx_err:
                        self.logger.warning(
                            f"pyttsx3 fallo ({pyttsx_err}) — generando audio silencioso de emergencia"
                        )
                        console.print("[yellow]pyttsx3 no disponible. Audio silencioso (emergencia)...[/]")
                        duration_secs = max(30, int(self._estimate_duration(clean_script) * 60))
                        import subprocess
                        subprocess.run(
                            [
                                "ffmpeg", "-y",
                                "-f", "lavfi",
                                "-i", "anullsrc=r=44100:cl=stereo",
                                "-t", str(duration_secs),
                                "-q:a", "9",
                                "-acodec", "libmp3lame",
                                output_path,
                            ],
                            check=True,
                            capture_output=True,
                        )
                        self.logger.warning(
                            f"Audio silencioso generado ({duration_secs}s) — el vídeo se generará sin voz"
                        )
                        engine_used = "silencio-ffmpeg"

            ctx.audio_path = output_path

            # Duracion estimada (basada en el guion original sin etiquetas SSML)
            duration_min = self._estimate_duration(ctx.script)
            duration_str = (
                f"{int(duration_min)}:{int((duration_min % 1) * 60):02d} min"
            )

            console.print(
                f"[green]Audio listo:[/] {output_path}\n"
                f"  Motor: [bold]{engine_used}[/] · "
                f"Duracion estimada: [bold]{duration_str}[/]"
            )
            self.logger.info(
                f"Audio guardado en {output_path} — duracion estimada: {duration_str}"
            )

            # Diagnostico de encoding: muestra muestra del guion procesado
            # para confirmar que tildes y ñ llegan intactas a HEPHAESTUS.
            # Se loguea en DEBUG para no contaminar output normal.
            if clean_script:
                sample = clean_script[:80]
                self.logger.debug(f"Guion muestra (encoding check): {sample!r}")

            # Generar audio para el Short independiente (si CALÍOPE generó short_script)
            if getattr(ctx, "short_script", ""):
                try:
                    short_output = str(OUTPUT_AUDIO_DIR / f"{ctx.pipeline_id}_short.mp3")
                    short_clean = preprocess_script(ctx.short_script)
                    # Short: Coqui → edge-tts → pyttsx3
                    try:
                        self._synthesize_coqui(short_clean, short_output, mode="urgente")
                    except Exception:
                        try:
                            self._synthesize_edge_with_retry(short_clean, short_output, "+5%", "+3Hz")
                        except Exception:
                            self._synthesize_pyttsx3(short_clean, short_output, mode="urgente")
                    ctx.short_audio_path = short_output
                    self.logger.info(f"Audio Short generado: {short_output}")
                    console.print(f"[green]Audio Short:[/] {short_output}")
                except Exception as _se:
                    self.logger.warning(f"Audio Short fallo (no critico): {_se}")

        except Exception as e:
            self.logger.error(f"Error en ECHO: {e}")
            ctx.add_error("ECHO", str(e))

        return ctx
