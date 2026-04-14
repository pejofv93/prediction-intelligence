from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/forge/caliope.py
CALIOPE — Guionista Maestra de NEXUS.

Genera guiones estructurados en 7 modos para YouTube y TikTok.
Detecta automaticamente palabras legales e inyecta aviso de inversión.
"""

import os
import re
from pathlib import Path
from typing import List

from rich.console import Console
from rich.panel import Panel

from core.base_agent import BaseAgent
from core.context import Context
from utils.llm_client import LLMClient

console = Console()

# Palabras que activan el aviso legal (sincronizadas con config.yaml)
LEGAL_TRIGGER_WORDS: List[str] = [
    "comprar", "vender", "invertir", "bullish", "bearish",
    "all-in", "acumular", "entrada", "salida",
]

LEGAL_DISCLAIMER = (
    "\n\nAVISO LEGAL: Esto no es consejo de inversión. "
    "CryptoVerdad ofrece análisis e información. "
    "Haz tu propia investigación. Tus decisiones financieras son tuyas."
)

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def clean_script(text: str) -> str:
    """
    Elimina del output del LLM todo lo que no es texto narrable en voz alta.

    Elimina:
    - Headers markdown: ## HOOK (...), ## INTRO (...), ## DESARROLLO, etc.
    - Cualquier línea que empiece con # o ##
    - Emoticonos y caracteres unicode especiales
    - Formato markdown: **negrita**, *cursiva*
    - Marcadores: [PAUSA], [PAUSA_LARGA], [GRAFICO_GRANDE] → puntuación natural
    - Guiones de lista al inicio de línea
    - Líneas vacías múltiples

    Resultado: texto corrido listo para voz, sin markdown ni código.
    """
    import re

    # Headers markdown (## HOOK (0-5s), ## INTRO, # Título, etc.)
    text = re.sub(r'^#+\s+.*$', '', text, flags=re.MULTILINE)

    # Emoticonos y símbolos unicode
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

    # Marcadores de pausa y gráfico → puntuación natural
    text = re.sub(r'\[PAUSA_LARGA\]', '.', text)
    text = re.sub(r'\[PAUSA\]', ',', text)
    text = re.sub(r'\[GRAFICO_GRANDE\]', '.', text)

    # Formato markdown: **negrita** y *cursiva* → texto plano
    text = re.sub(r'\*{2}([^*]+)\*{2}', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)

    # Guiones de lista al inicio de línea
    text = re.sub(r'^\s*-\s+', '', text, flags=re.MULTILINE)

    # Separadores de guiones
    text = re.sub(r'^-{2,}\s*$', '', text, flags=re.MULTILINE)

    # Líneas vacías múltiples → máximo una
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Espacios múltiples
    text = re.sub(r'[ \t]{2,}', ' ', text)

    # Líneas con solo espacios
    text = re.sub(r'^\s+$', '', text, flags=re.MULTILINE)

    return text.strip()


def _fix_english_words(script: str) -> str:
    """
    Reemplaza anglicismos crypto frecuentes por su equivalente en español.
    Solo sustituye palabras aisladas (word boundary) para no romper etiquetas.
    """
    import re

    # (patron_regex, reemplazo) — orden importa: más específico primero
    _REPLACEMENTS = [
        # Sentimiento de mercado
        (r'\bbullish\b',          'alcista'),
        (r'\bbearish\b',          'bajista'),
        (r'\bmarket\b',           'mercado'),
        (r'\btrend(?:ing)?\b',    'tendencia'),
        (r'\brally\b',            'remontada'),
        # Movimientos de precio
        (r'\bpump(?:ed|ing)?\b',  'subida'),
        (r'\bdump(?:ed|ing)?\b',  'caída'),
        (r'\bdip\b',              'corrección'),
        (r'\bcrash(?:ed|ing)?\b', 'colapso'),
        (r'\bmoon(?:ing)?\b',     'subida extrema'),
        # Horizonte temporal
        (r'\bweeks?\b',           'semanas'),
        (r'\bmonths?\b',          'meses'),
        (r'\bdays?\b',            'días'),
        # Acciones de inversor
        (r'\bhold(?:ing|ers?)?\b', 'mantener'),
        (r'\bstake(?:ing|rs?)?\b', 'apostar'),
        (r'\byield\b',            'rendimiento'),
        (r'\bswap\b',             'intercambio'),
        (r'\bhash\s?rate\b',      'tasa de hash'),
        # Participantes
        (r'\bwhale(?:s)?\b',      'ballena'),
        (r'\btoken(?:s)?\b',      'token'),   # "token" se acepta pero si lleva s → tokens
        # Jerga
        (r'\ball[- ]in\b',        'todo adentro'),
        (r'\bFOMO\b',             'miedo a quedarse fuera'),
        (r'\bFUD\b',              'miedo e incertidumbre'),
        (r'\bATH\b',              'máximo histórico'),
        (r'\bdefi\b',             'finanzas descentralizadas'),
        (r'\bweb3?\b',            'web3'),     # se acepta en español
    ]

    for pattern, replacement in _REPLACEMENTS:
        script = re.sub(pattern, replacement, script, flags=re.IGNORECASE)

    return script


def _fix_repetitions(script: str) -> str:
    """
    Detecta y corrige patrones repetitivos comunes en el guion.
    """
    import re

    lines = script.split('\n')
    result = []

    # Patron: "lo que llevaría a X, lo que llevaría a Y" → romper cadena causal
    # Reemplaza segunda/tercera instancia de "lo que" en la misma linea
    causal_pattern = re.compile(r'(lo que [^,\.]{5,40}),\s*(lo que )', re.I)

    for line in lines:
        # Romper cadenas causales: "lo que ... lo que" → ". Ademas,"
        line = causal_pattern.sub(r'\1. Ademas, ', line)

        # Eliminar "lo que ... lo que ... lo que" (triple)
        line = re.sub(r'(lo que [^,\.]{3,30})[,\s]+(lo que [^,\.]{3,30})[,\s]+(lo que )',
                      r'\1. \3', line, flags=re.I)

        result.append(line)

    text = '\n'.join(result)

    # Patron: frases que terminan igual que empiezan la siguiente
    # "...Bitcoin. Bitcoin..." → "...Bitcoin, que..."
    text = re.sub(r'(Bitcoin)\. \1 (es|está|tiene|sigue|cotiza)', r'\1, que \2', text, flags=re.I)
    text = re.sub(r'(mercado)\. El \1 (es|está|tiene|sigue)', r'\1, el cual \2', text, flags=re.I)

    return text


def _fix_punctuation(text: str) -> str:
    """Elimina combinaciones de puntuación imposibles generadas por [PAUSA] o el LLM."""
    import re
    # Punto/interrogación/exclamación seguidos de coma → eliminar la coma
    text = re.sub(r'([.?!])\s*,', r'\1', text)
    # Espacio antes de coma o punto
    text = re.sub(r'\s+([,.])', r'\1', text)
    # Comas dobles o más
    text = re.sub(r',{2,}', ',', text)
    # Puntos dobles (sin tocar "...")
    text = re.sub(r'\.{2}(?!\.)', '.', text)
    # Coma al inicio de línea (con espacios opcionales)
    text = re.sub(r'^\s*,\s*', '', text, flags=re.MULTILINE)
    return text


def _check_block_uniqueness(script: str) -> list:
    """
    Retorna lista de pares de bloques con contenido MUY similar (Jaccard > 0.70).
    Usa Jaccard similarity con stopwords extendidas (incluye términos crypto
    omnipresentes) para evitar falsos positivos masivos.
    """
    import re
    blocks = re.split(r'\[(?:PRECIO|AN[AÁ]LISIS|SENTIMIENTO|DOMINANCIA|ADOPCI[OÓ]N|PREDICCI[OÓ]N)\]',
                      script, flags=re.I)
    blocks = [b.strip() for b in blocks if b.strip()]

    # Stopwords: artículos/preposiciones + términos crypto que aparecen en TODOS los bloques
    stopwords = {
        'el', 'la', 'de', 'en', 'un', 'una', 'y', 'a', 'que', 'es', 'se',
        'no', 'lo', 'los', 'las', 'su', 'por', 'con', 'al', 'del', 'le',
        'si', 'me', 'mi', 'ya', 'o', 'ha', 'he', 'tu', 'te', 'mas', 'más',
        'para', 'este', 'esta', 'esto', 'pero', 'como', 'hay', 'muy', 'también',
        # Términos crypto omnipresentes en cualquier guión
        'bitcoin', 'btc', 'crypto', 'precio', 'mercado', 'ethereum', 'eth',
        'sol', 'criptomoneda', 'criptomonedas', 'blockchain', 'análisis',
        'analisis', 'dólares', 'dolares', 'mil', 'millones',
    }

    warnings = []
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            words_i = set(blocks[i].lower().split()) - stopwords
            words_j = set(blocks[j].lower().split()) - stopwords
            union = words_i | words_j
            if not union:
                continue
            jaccard = len(words_i & words_j) / len(union)
            if jaccard > 0.70:  # 70% de palabras idénticas → realmente repetitivo
                warnings.append(
                    f'Bloques {i+1} y {j+1} muy similares (Jaccard={jaccard:.0%})'
                )
    return warnings


def _has_strong_hook(script: str) -> bool:
    """
    Verifica que el guion tiene un gancho impactante en el primer bloque.
    Criterios (basta con uno):
      - Hay una pregunta (?) en los primeros 400 caracteres.
      - Hay un número concreto en los primeros 400 caracteres (dato impactante).
      - Alguna frase de apertura típica de contenido viral en español.
    """
    first_block = script[:400]
    first_lower = first_block.lower()

    # Criterio A: pregunta directa al espectador
    if '?' in first_block:
        return True

    # Criterio B: dato numérico concreto (precio, porcentaje, año, cantidad)
    if re.search(r'\b\d[\d,.]*\s*%?', first_block):
        return True

    # Criterio C: frases de apertura viral habituales en guiones de CALÍOPE
    hook_phrases = [
        "acaba de", "solo tres", "solo dos", "nunca antes", "primera vez",
        "historia de bitcoin", "historia de cripto", "cambia todo",
        "lo que nadie", "muy poca gente", "fíjate", "mira esto",
        "esto es clave", "antes de que", "presta atención",
        "en los próximos", "va a cambiar", "en toda la historia",
    ]
    return any(ph in first_lower for ph in hook_phrases)


class CALIOPE(BaseAgent):
    """
    Guionista Maestra de NEXUS.

    Lee el modo del pipeline (ctx.script_mode o ctx.mode) y genera un guion
    completo usando el prompt especifico para ese modo.
    Estructura fija: HOOK → INTRO → DESARROLLO → CONCLUSION → DISCLAIMER.
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config)
        self.db = db
        self.llm = LLMClient(config, db=db)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_prompt(self, mode: str) -> str:
        """Carga prompts/caliope_{mode}.txt; fallback a forge_script.txt."""
        specific = PROMPTS_DIR / f"caliope_{mode}.txt"
        fallback = PROMPTS_DIR / "forge_script.txt"

        if specific.exists():
            self.logger.debug(f"Prompt cargado: caliope_{mode}.txt")
            return specific.read_text(encoding="utf-8")

        if fallback.exists():
            self.logger.warning(
                f"Prompt caliope_{mode}.txt no encontrado — usando forge_script.txt"
            )
            return fallback.read_text(encoding="utf-8")

        # Si no hay nada, devolvemos instruccion minima
        self.logger.warning("Sin prompt disponible — usando instruccion mínima")
        return (
            "Eres el guionista de CryptoVerdad. "
            "Escribe un guion profesional sobre el tema proporcionado."
        )

    def _build_user_prompt(self, ctx: Context) -> str:
        """Construye el mensaje de usuario con los datos del Context."""
        lines = [f"TEMA: {ctx.topic}"]

        # Precios relevantes
        if ctx.prices:
            btc = ctx.prices.get("BTC", {})
            eth = ctx.prices.get("ETH", {})
            price_lines = []
            if btc:
                price_lines.append(
                    f"BTC: ${btc.get('price', 'N/A'):,.0f} "
                    f"({btc.get('change_24h', 0):+.2f}% 24h)"
                )
            if eth:
                price_lines.append(
                    f"ETH: ${eth.get('price', 'N/A'):,.0f} "
                    f"({eth.get('change_24h', 0):+.2f}% 24h)"
                )
            for sym, data in ctx.prices.items():
                if sym not in ("BTC", "ETH") and isinstance(data, dict):
                    price_lines.append(
                        f"{sym}: ${data.get('price', 'N/A'):,.4f}"
                    )
            if price_lines:
                lines.append("\nPRECIOS EN TIEMPO REAL:\n" + "\n".join(price_lines))

        # Noticias recientes
        if ctx.news:
            lines.append("\nNOTICIAS RECIENTES:")
            for item in ctx.news[:5]:
                title = item.get("title", "")
                source = item.get("source", "")
                lines.append(f"  - {title} [{source}]")

        # Razonamiento estratégico de THEMIS
        if ctx.strategy_reasoning:
            lines.append(f"\nANÁLISIS ESTRATÉGICO:\n{ctx.strategy_reasoning}")

        # Contexto de aprendizaje de MNEME
        if ctx.learning_context:
            top_formats = ctx.learning_context.get("top_formats", [])
            if top_formats:
                lines.append(f"\nFORMATOS CON MEJOR RENDIMIENTO: {', '.join(top_formats)}")
            avoid = ctx.learning_context.get("avoid_patterns", [])
            if avoid:
                lines.append(f"PATRONES A EVITAR: {', '.join(avoid)}")

        lines.append(
            "\n\nEstructura del guion (sigue este orden internamente, "
            "pero NO escribas los nombres de sección en el texto):\n"
            "1. Gancho inicial impactante (primeras 5 segundos)\n"
            "2. Introduccion y contexto (siguientes 10-15 segundos)\n"
            "3. Desarrollo con datos y análisis\n"
            "4. Conclusion con puntos clave y llamada a la accion\n"
            "(El aviso legal se añade automáticamente si aplica.)\n\n"
            "FORMATO OBLIGATORIO: escribe SOLO texto corrido para leer en voz alta. "
            "Sin headers, sin markdown, sin emoticonos."
        )

        return "\n".join(lines)

    def _pad_script_to_minimum(self, script: str, ctx: "Context", words_needed: int) -> str:
        """
        Añade secciones de contexto automáticas al guion hasta alcanzar el mínimo.
        Usa los datos disponibles en ctx (precios, Fear&Greed, dominancia) para
        generar párrafos informativos que no parezcan relleno.
        """
        sections = []

        btc = getattr(ctx, "btc_price", 0) or 0
        eth = getattr(ctx, "eth_price", 0) or 0
        sol = getattr(ctx, "sol_price", 0) or 0
        fg_val = getattr(ctx, "fear_greed_value", 0) or 0
        fg_lbl = getattr(ctx, "fear_greed_label", "") or ""
        dom = getattr(ctx, "btc_dominance", 0) or 0
        supports = getattr(ctx, "support_levels", []) or []
        resistances = getattr(ctx, "resistance_levels", []) or []

        if btc:
            sections.append(
                f"[PRECIO] En este momento Bitcoin cotiza en torno a los {btc:,.0f} dólares. "
                f"Este nivel es clave para determinar la dirección del mercado en las próximas horas. "
                f"Ethereum se mueve alrededor de {eth:,.0f} dólares y Solana en los {sol:,.0f} dólares. "
                f"La correlación entre estas tres monedas sigue siendo alta, lo que indica que el "
                f"mercado actúa de forma coordinada. Cualquier movimiento fuerte en Bitcoin "
                f"arrastrará al resto del mercado en la misma dirección."
            )

        if fg_val:
            sentiment_desc = (
                "pánico extremo, una señal históricamente asociada a oportunidades de compra para "
                "los inversores más pacientes" if fg_val < 25 else
                "miedo en el mercado, lo que sugiere que muchos inversores están dubitativos" if fg_val < 45 else
                "neutralidad, un estado que suele preceder a movimientos importantes" if fg_val < 55 else
                "codicia moderada, señal de que el optimismo está volviendo al mercado" if fg_val < 75 else
                "codicia extrema, un nivel que históricamente ha precedido a correcciones"
            )
            sections.append(
                f"[SENTIMIENTO] El índice de Miedo y Codicia se sitúa en {fg_val} puntos, "
                f"lo que indica {sentiment_desc}. "
                f"Este indicador mide la emoción predominante entre los participantes del mercado "
                f"y es una herramienta clave para entender si el mercado está sobrecomprado o sobrevendido. "
                f"Históricamente, los mejores momentos para acumular han sido cuando el índice "
                f"marcaba pánico extremo, y los mejores momentos para ser prudentes han sido "
                f"cuando marcaba codicia extrema. El nivel actual de {fg_lbl} nos dice mucho "
                f"sobre el estado psicológico del mercado en este momento."
            )

        if dom:
            alt_sentiment = (
                "podría favorecer a las altcoins en el corto plazo" if dom < 50 else
                "sugiere que el dinero sigue refugiándose en Bitcoin como activo más seguro"
            )
            sections.append(
                f"[DOMINANCIA] La dominancia de Bitcoin en el mercado se sitúa en el {dom:.1f}%. "
                f"Este porcentaje representa la cuota de Bitcoin dentro del mercado cripto total "
                f"y es un indicador clave para entender el ciclo del mercado. "
                f"Un nivel de {dom:.1f}% {alt_sentiment}. "
                f"Los traders más experimentados monitorean este indicador de cerca porque "
                f"los cambios en la dominancia suelen anticipar rotaciones importantes entre "
                f"Bitcoin y el resto de criptomonedas."
            )

        if supports or resistances:
            lvl_text = ""
            if resistances:
                lvl_text += f"La resistencia más cercana se encuentra en los {resistances[0]:,.0f} dólares. "
            if supports:
                lvl_text += f"El soporte más próximo está en torno a los {supports[0]:,.0f} dólares. "
            sections.append(
                f"[ANALISIS] Desde el punto de vista técnico, el gráfico de Bitcoin muestra "
                f"niveles críticos que los traders están vigilando de cerca. "
                f"{lvl_text}"
                f"La ruptura de cualquiera de estos niveles desencadenará liquidaciones "
                f"y podría acelerar el movimiento en la dirección que rompa. "
                f"Es fundamental no operar con apalancamiento en estas zonas de incertidumbre."
            )

        # Si no tenemos datos suficientes, añadir sección educativa genérica
        if not sections:
            sections.append(
                "[ADOPCION] El ecosistema cripto continúa su proceso de maduración. "
                "Cada día más instituciones y gobiernos estudian cómo integrar la tecnología "
                "blockchain en sus sistemas financieros. Esta adopción progresiva es lo que "
                "diferencia este ciclo de los anteriores: ya no hablamos solo de especulación "
                "minorista, sino de una transformación estructural del sistema financiero global. "
                "Los inversores que entienden esta diferencia están mejor posicionados para "
                "tomar decisiones informadas en este mercado."
            )

        addition = "\n\n".join(sections)
        padded = script.rstrip() + "\n\n" + addition
        self.logger.info(
            f"Script ampliado de {len(script.split())} a {len(padded.split())} palabras "
            f"con secciones de contexto automáticas."
        )
        return padded

    def _detect_legal_trigger(self, script: str) -> bool:
        """Devuelve True si el guion contiene alguna palabra legal."""
        lower = script.lower()
        return any(word in lower for word in LEGAL_TRIGGER_WORDS)

    def _generate_short_script(self, ctx: Context) -> str:
        """
        Genera un guion Short de 45-60s (máx 150 palabras) independiente del largo.
        Devuelve el texto o "" si falla.
        """
        try:
            short_prompt_path = PROMPTS_DIR / "caliope_short.txt"
            system_short = (
                short_prompt_path.read_text(encoding="utf-8")
                if short_prompt_path.exists()
                else "Escribe un Short de 45-60s para TikTok sobre el tema en español. Máximo 150 palabras."
            )

            # Construir user prompt simplificado para el Short
            lines = [
                f"TEMA: {ctx.topic}",
                f"Precio actual de Bitcoin: ${getattr(ctx, 'btc_price', 0):,.0f}",
            ]
            if getattr(ctx, "fear_greed_value", 0):
                lines.append(f"Fear & Greed: {ctx.fear_greed_value} ({ctx.fear_greed_label})")
            if ctx.news:
                top_news = ctx.news[0].get("title", "") if ctx.news else ""
                if top_news:
                    lines.append(f"Noticia principal: {top_news}")
            lines.append(
                "\n\nEscribe el guion SHORT. Máximo 150 palabras. "
                "Estructura: hook (5s) → desarrollo (40s) → CTA (10s). "
                "Solo texto corrido. Sin títulos ni markdown."
            )

            short_script = self.llm.generate(
                prompt="\n".join(lines),
                system=system_short,
                max_tokens=400,
                temperature=0.9,
            )
            short_script = clean_script(short_script)
            short_script = _fix_english_words(short_script)
            # Truncar si supera 160 palabras (margen de seguridad)
            words = short_script.split()
            if len(words) > 160:
                short_script = " ".join(words[:150])
            self.logger.info(f"Short script generado — {len(short_script.split())} palabras")
            return short_script
        except Exception as e:
            self.logger.warning(f"Short script fallo (no critico): {e}")
            return ""

    # ── run() ─────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("CALIOPE iniciada")
        console.print(
            Panel(
                f"[bold #F7931A]CALÍOPE[/] — Guionista Maestra\n"
                f"Modo: [bold]{ctx.script_mode or ctx.mode}[/] · Tema: [italic]{ctx.topic}[/]",
                border_style="#F7931A",
            )
        )

        try:
            mode = ctx.script_mode or ctx.mode or "standard"
            system_prompt = self._load_prompt(mode)
            user_prompt = self._build_user_prompt(ctx)

            # Ajustar max_tokens segun modo
            # Regla: 1 token ≈ 0.75 palabras en español → para 1.200 palabras necesitamos ≥1.600 tokens
            max_tokens_map = {
                "urgente":   2500,   # objetivo 600-800 palabras
                "short":      600,   # máximo 150 palabras
                "standard":  3500,   # objetivo 1.000-1.200 palabras
                "analisis":  4096,   # objetivo 1.200-1.500 palabras
                "opinion":   3000,   # objetivo 1.000 palabras
                "tutorial":  4096,   # objetivo 1.500-2.000 palabras
                "noticia":   2500,   # objetivo 800-1.000 palabras
                "educativo": 4096,   # objetivo 1.500-2.000 palabras
                "thread":    1500,
            }
            max_tokens = max_tokens_map.get(mode, 3500)

            # Construir contexto de datos reales para el prompt
            data_context = []
            if getattr(ctx, 'btc_price', 0):
                data_context.append(f"BTC precio actual: ${ctx.btc_price:,.0f}")
            if getattr(ctx, 'eth_price', 0):
                data_context.append(f"ETH precio actual: ${ctx.eth_price:,.0f}")
            if getattr(ctx, 'fear_greed_value', None):
                data_context.append(
                    f"Fear & Greed Index: {ctx.fear_greed_value} ({ctx.fear_greed_label})"
                )
            if getattr(ctx, 'btc_dominance', None):
                data_context.append(f"Dominancia BTC: {ctx.btc_dominance:.1f}%")
            if getattr(ctx, 'support_levels', []):
                for i, s in enumerate(ctx.support_levels[:2], 1):
                    data_context.append(f"Soporte S{i}: ${s:,.0f}")
            if getattr(ctx, 'resistance_levels', []):
                for i, r in enumerate(ctx.resistance_levels[:2], 1):
                    data_context.append(f"Resistencia R{i}: ${r:,.0f}")

            if data_context:
                data_section = (
                    "\n\nDATOS DE MERCADO ACTUALES (usa estos números exactos en el guión):\n"
                )
                data_section += "\n".join(f"- {d}" for d in data_context)
                user_prompt = user_prompt + data_section

            # Instrucción de sincronización con ChartZoomEngine
            # HEPHAESTUS hará zoom en estos niveles exactos durante el vídeo;
            # CALÍOPE debe mencionar los mismos precios para que audio y gráfico coincidan.
            sync_instruction = []
            if getattr(ctx, 'resistance_levels', []):
                r_levels = ctx.resistance_levels[:3]
                sync_instruction.append(
                    "INSTRUCCION DE SINCRONIZACION: El grafico mostrara zoom en estos niveles "
                    "de resistencia: "
                    + ", ".join(f"${r:,.0f}" for r in r_levels)
                    + ". Menciona EXACTAMENTE estos precios en el bloque [ANALISIS]. "
                    + f"Ejemplo: 'Fijate en este nivel de ${r_levels[0]:,.0f}. "
                    + "Es la resistencia clave.'"
                )
            if getattr(ctx, 'support_levels', []):
                s_levels = ctx.support_levels[:2]
                sync_instruction.append(
                    "El grafico tambien mostrara zoom en soportes: "
                    + ", ".join(f"${s:,.0f}" for s in s_levels)
                    + f". Menciona: 'El soporte critico esta en ${s_levels[0]:,.0f}.'"
                )

            if sync_instruction:
                user_prompt += "\n\n" + "\n".join(sync_instruction)
                self.logger.info(
                    f"Sincronizacion grafico-guion: "
                    f"R={getattr(ctx, 'resistance_levels', [])[:3]} "
                    f"S={getattr(ctx, 'support_levels', [])[:2]}"
                )

            console.print(
                f"[dim]Generando guion con LLM ({max_tokens} tokens max)...[/]"
            )
            script = self.llm.generate(
                prompt=user_prompt,
                system=system_prompt,
                max_tokens=max_tokens,
                temperature=0.9,
            )

            # Limpiar output del LLM: eliminar markdown, headers, emoticonos
            script = clean_script(script)
            script = _fix_english_words(script)  # sustituir anglicismos por español
            script = _fix_repetitions(script)     # eliminar patrones repetitivos
            self.logger.info("Guion limpiado (sin markdown, sin anglicismos)")

            # Validación de gancho potente al inicio (solo log, no warning en ctx)
            if not _has_strong_hook(script):
                self.logger.debug("[CALIOPE] Gancho podría ser más directo")

            # Umbrales mínimos de palabras por modo (basados en objetivos de duración)
            _min_words = {
                "analisis":  1200,   # 8-10 min
                "standard":  1000,   # 7-8 min
                "noticia":    800,   # 5-7 min
                "opinion":    800,   # 5-7 min
                "urgente":    600,   # 4-5 min
                "educativo": 1500,   # 10-13 min
                "tutorial":  1500,   # 10-13 min
                "short":      80,    # 45-60s (máximo 150 palabras)
                "thread":     200,
            }
            min_words = _min_words.get(mode, 800)

            # Forzar mínimo de palabras — hasta 3 intentos
            for _attempt in range(3):
                word_count_now = len(script.split())
                if word_count_now >= min_words:
                    break
                self.logger.warning(
                    f"Guion corto ({word_count_now} palabras, mínimo {min_words}), "
                    f"regenerando intento {_attempt + 1}/3..."
                )
                console.print(
                    f"[yellow]Guion corto ({word_count_now}/{min_words} palabras). "
                    f"Regenerando (intento {_attempt + 1}/3)...[/]"
                )
                if mode == "analisis":
                    extra_instruction = (
                        f"\n\nCRITICO: El guion DEBE tener MINIMO {min_words} palabras. "
                        f"Tienes {word_count_now} palabras — faltan {min_words - word_count_now}. "
                        "OBLIGATORIO: 7 secciones con estos mínimos de palabras:\n"
                        "[PRECIO] HOOK: mínimo 70 palabras\n"
                        "[ANALISIS] CONTEXTO HISTÓRICO: mínimo 200 palabras (2 comparativas históricas)\n"
                        "[ANALISIS] ANÁLISIS TÉCNICO: mínimo 260 palabras (R1, S1, estructura técnica)\n"
                        "[SENTIMIENTO] SENTIMIENTO: mínimo 180 palabras (Fear&Greed + contexto histórico)\n"
                        "[ADOPCION] FACTORES FUNDAMENTALES: mínimo 200 palabras (dato institucional + impacto)\n"
                        "[DOMINANCIA] DOMINANCIA: mínimo 130 palabras (BTC dominance + implicaciones)\n"
                        "[PREDICCION] ESCENARIOS+OPINIÓN+CTA: mínimo 200 palabras\n"
                        "Desarrolla CADA sección hasta su mínimo. Añade tensión narrativa y preguntas retóricas."
                    )
                else:
                    extra_instruction = (
                        f"\n\nCRITICO: El guion DEBE tener MINIMO {min_words} palabras. "
                        f"Tienes {word_count_now} palabras — necesitas {min_words - word_count_now} más. "
                        "Desarrolla cada bloque con más profundidad, más análisis, más contexto histórico. "
                        "Añade tensión narrativa entre datos. Usa más preguntas retóricas. "
                        "Cada bloque debe tener mínimo 200 palabras para modos largos. "
                        "OBLIGATORIO: Usa las etiquetas de bloque [PRECIO], [ANALISIS], "
                        "[SENTIMIENTO], [DOMINANCIA], [ADOPCION], [PREDICCION] al inicio de cada párrafo."
                    )
                try:
                    script = self.llm.generate(
                        prompt=user_prompt + extra_instruction,
                        system=system_prompt,
                        max_tokens=max_tokens,
                        temperature=0.85,
                    )
                    script = clean_script(script)
                    script = _fix_english_words(script)
                    script = _fix_repetitions(script)
                    self.logger.info(
                        f"Guion regenerado — {len(script.split())} palabras"
                    )
                except Exception:
                    break  # si falla la regeneración, usar lo que tenemos

            # Validar estructura de bloques para modo analisis
            if mode == "analisis":
                required_tags = ["[PRECIO]", "[ANALISIS]", "[SENTIMIENTO]",
                                 "[DOMINANCIA]", "[ADOPCION]", "[PREDICCION]"]
                missing = [t for t in required_tags if t.lower() not in script.lower()]
                if missing:
                    self.logger.warning(
                        f"Guion analisis falta etiquetas: {missing}. Regenerando..."
                    )
                    console.print(
                        f"[yellow]Etiquetas de bloque faltantes: {missing}. Regenerando...[/]"
                    )
                    tag_instruction = (
                        "\n\nCRITICO: El guion DEBE contener EXACTAMENTE estas 6 etiquetas, "
                        "una al inicio de cada bloque:\n"
                        "[PRECIO] ... [ANALISIS] ... [SENTIMIENTO] ... "
                        "[DOMINANCIA] ... [ADOPCION] ... [PREDICCION]\n"
                        "Cada etiqueta debe estar al inicio de su parrafo, sin texto antes."
                    )
                    try:
                        script2 = self.llm.generate(
                            prompt=user_prompt + tag_instruction,
                            system=system_prompt,
                            max_tokens=max_tokens,
                            temperature=0.9,
                        )
                        script2 = clean_script(script2)
                        script2 = _fix_repetitions(script2)  # eliminar patrones repetitivos
                        missing2 = [t for t in required_tags if t.lower() not in script2.lower()]
                        if len(missing2) < len(missing):
                            script = script2
                            self.logger.info(
                                f"Guion regenerado con etiquetas — {len(script.split())} palabras"
                            )
                    except Exception:
                        pass  # usar el guion sin etiquetas

            # ── Garantía de mínimo absoluto 600 palabras ──────────────────────
            ABSOLUTE_MIN = 600
            word_count_final = len(script.split())
            if word_count_final < ABSOLUTE_MIN:
                needed = ABSOLUTE_MIN - word_count_final
                self.logger.warning(
                    f"Guion sigue corto ({word_count_final} palabras) tras reintentos. "
                    f"Añadiendo secciones de contexto automáticas ({needed} palabras más)..."
                )
                console.print(
                    f"[yellow]Script corto ({word_count_final}/{ABSOLUTE_MIN} palabras). "
                    f"Añadiendo contexto automático...[/]"
                )
                script = self._pad_script_to_minimum(
                    script, ctx, ABSOLUTE_MIN - word_count_final
                )

            # Validacion de unicidad de bloques
            uniqueness_warnings = _check_block_uniqueness(script)
            for w in uniqueness_warnings:
                self.logger.warning(f'Guion repetitivo: {w}')

            # Aviso legal automatico
            if self._detect_legal_trigger(script):
                script += LEGAL_DISCLAIMER
                ctx.legal_warning_added = True
                self.logger.info(
                    "Aviso legal añadido automaticamente (palabras de inversión detectadas)"
                )
                console.print(
                    "[bold yellow]⚠ Aviso legal añadido al guion[/]"
                )

            # Limpiar puntuacion incorrecta generada por [PAUSA] o LLM
            script = _fix_punctuation(script)

            ctx.script = script
            ctx.script_mode = mode

            word_count = len(script.split())
            console.print(
                f"[green]Guion generado:[/] {word_count} palabras · "
                f"~{word_count // 130} min de audio estimado"
            )
            self.logger.info(
                f"Guion generado exitosamente — {word_count} palabras, modo {mode}"
            )

            # Generar guion Short independiente (no para modo short/thread)
            if mode not in ("short", "thread"):
                console.print("[dim]Generando guion Short independiente (45-60s)...[/]")
                ctx.short_script = self._generate_short_script(ctx)
                if ctx.short_script:
                    console.print(
                        f"[green]Short script:[/] {len(ctx.short_script.split())} palabras"
                    )

        except Exception as e:
            self.logger.error(f"Error en CALIOPE: {e}")
            ctx.add_error("CALIOPE", str(e))

        return ctx

