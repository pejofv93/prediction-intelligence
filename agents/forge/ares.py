"""
agents/forge/ares.py
ARES — Audience Retention Engine System.

Maximiza el watch time de YouTube analizando y mejorando scripts.
Ejecutar DESPUÉS de HERMES, ANTES de ECHO.

Ciencia detrás:
  - Los primeros 30s determinan si YouTube recomienda el video
  - El viewer promedio abandona si no hay "patrón de interrupción" cada 90s
  - Re-engagement explícito a 30%/50%/70% replica la estructura de los
    videos con >70% de retención promedio en el nicho financiero
  - Frases de 8-14 palabras optimizan el ritmo para narración oral española
"""

import re
import random
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from core.base_agent import BaseAgent
from core.context import Context

console = Console()

WPM = 145          # palabras/min de es-ES-AlvaroNeural con rate+5%
INTERRUPT_EVERY = 200  # palabras entre pattern interrupts (~82 segundos)


class ARES(BaseAgent):
    """
    Analiza y mejora scripts para maximizar watch time en YouTube.

    Métricas calculadas:
      hook_score      — fuerza del gancho (0-25)
      interrupt_score — densidad de pattern interrupts (0-25)
      reengage_score  — re-enganche en puntos críticos (0-25)
      pacing_score    — variedad rítmica del guión (0-25)
      retention_score — suma total (0-100)
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config)
        self.db = db

    # Pattern interrupts — varían para no sonar repetitivo
    _INTERRUPTS = [
        "Pero espera. Porque aquí viene lo que la mayoría no sabe.",
        "Fíjate en esto. Es el dato que lo cambia todo.",
        "Un momento. Necesito que proceses lo que acabo de decir.",
        "Y aquí está el giro que no te esperabas.",
        "Esto es clave. Quédate conmigo un segundo.",
        "Antes de continuar. Hay algo que no te he dicho todavía.",
        "Para. Porque esto es exactamente donde la gente se equivoca.",
    ]

    # Re-enganche por posición — tono diferente en cada momento
    _REENGAGE_30 = [
        "Ahora que tienes el contexto, lo que sigue va a cambiar tu lectura del mercado.",
        "Hasta aquí el setup. Ahora viene el análisis que realmente importa.",
        "Bien. Con eso claro, la siguiente parte lo conecta todo.",
    ]
    _REENGAGE_50 = [
        "Si llegas hasta aquí, lo que viene ahora es exactamente para lo que merece quedarse.",
        "Esto es la mitad del vídeo. Y lo más importante viene ahora.",
        "Quédate. Porque ahora es cuando esto se pone realmente interesante.",
    ]
    _REENGAGE_70 = [
        "Ya casi está. Y lo que cierra este análisis, nadie más lo está diciendo.",
        "Tercer y último bloque. Y es el que más importa para esta semana.",
        "Esto lo conecta todo. El escenario completo, en los próximos dos minutos.",
    ]

    # ── run() ─────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("ARES iniciado — analizando retención del script")
        try:
            if not ctx.script or len(ctx.script.split()) < 50:
                ctx.add_warning("ARES", "Script demasiado corto — saltando análisis")
                return ctx

            # 1. Puntuar script actual
            score, analysis = self._score_retention(ctx.script)
            ctx.retention_score = score

            # 2. Mejorar si score < 80
            if score < 80:
                enhanced = self._enhance_script(ctx.script, analysis)
                word_count_ok = len(enhanced.split()) >= len(ctx.script.split()) * 0.95
                if enhanced and word_count_ok:
                    ctx.script = enhanced
                    ctx.retention_score, _ = self._score_retention(enhanced)
                    self.logger.info(
                        f"ARES: script mejorado — retención {score}→{ctx.retention_score}/100"
                    )

            # 3. Generar chapter markers con timestamps precisos
            chapters = self._generate_chapters(ctx.script, ctx.mode)
            ctx.chapter_markers = chapters

            # 4. Inyectar chapters en descripción SEO si falta
            if ctx.seo_description and chapters and "📌" not in ctx.seo_description:
                ctx.seo_description = chapters + "\n\n" + ctx.seo_description
                self.logger.info("ARES: chapter markers inyectados en descripción SEO")

            self._print_report(ctx.retention_score, analysis)

        except Exception as e:
            self.logger.error(f"Error en ARES: {e}")
            ctx.add_warning("ARES", str(e))

        return ctx

    # ── Puntuación ────────────────────────────────────────────────────────────

    def _score_retention(self, script: str) -> tuple:
        """Puntúa el script de 0 a 100 según cuatro dimensiones de retención."""
        words = script.split()
        total = len(words)
        if total == 0:
            return 0, {}

        analysis = {
            "total_words": total,
            "estimated_minutes": round(total / WPM, 1),
        }
        score = 0

        # ── Hook (25 pts) ──────────────────────────────────────────────────
        hook_text = " ".join(words[:80]).lower()
        h = 0
        if "?" in hook_text:
            h += 8   # pregunta directa crea curiosidad
        if re.search(r'\b\d[\d.,]*\s*(?:mil|millón|%|dólar|euro|punto)\b', hook_text):
            h += 7   # dato numérico = credibilidad + atención
        if any(p in hook_text for p in ["acaba de", "nunca antes", "histórico", "récord", "máximo"]):
            h += 5   # urgencia temporal
        if any(p in hook_text for p in ["quédate", "te explico", "voy a mostrarte", "en este vídeo"]):
            h += 5   # promesa explícita
        hook_score = min(h, 25)
        score += hook_score
        analysis["hook_score"] = hook_score
        analysis["hook_weak"] = hook_score < 15

        # ── Pattern Interrupts (25 pts) ────────────────────────────────────
        interrupt_kw = [
            "pero espera", "fíjate", "un momento", "espera un",
            "antes de continuar", "esto es clave", "aquí está el giro",
            "lo que nadie", "lo que pocos", "lo que la mayoría",
            "atención", "importante:", "stop", "para.",
        ]
        n_blocks = max(1, total // INTERRUPT_EVERY)
        blocks_with_interrupt = 0
        for i in range(0, total, INTERRUPT_EVERY):
            block = " ".join(words[i:i + INTERRUPT_EVERY]).lower()
            if any(kw in block for kw in interrupt_kw):
                blocks_with_interrupt += 1

        interrupt_score = min(blocks_with_interrupt * 5, 25)
        score += interrupt_score
        analysis["interrupts_found"] = blocks_with_interrupt
        analysis["interrupt_score"] = interrupt_score

        # ── Re-engagement por posición (25 pts) ────────────────────────────
        reengage_kw = [
            "lo que sigue", "ahora viene", "quédate", "si llegas hasta",
            "esto lo conecta", "ya casi", "tercer", "último bloque",
            "la parte que", "lo más importante viene", "para lo que merece",
            "ahora que tienes", "bien. con eso",
        ]
        re_score = 0

        def block_at(pct_start, pct_end):
            s = int(total * pct_start)
            e = int(total * pct_end)
            return " ".join(words[s:e]).lower()

        if any(kw in block_at(0.25, 0.38) for kw in reengage_kw):
            re_score += 9
            analysis["reengage_30"] = True
        if any(kw in block_at(0.45, 0.58) for kw in reengage_kw):
            re_score += 9
            analysis["reengage_50"] = True
        if any(kw in block_at(0.65, 0.78) for kw in reengage_kw):
            re_score += 7
            analysis["reengage_70"] = True
        score += re_score
        analysis["reengage_score"] = re_score

        # ── Pacing (25 pts) ────────────────────────────────────────────────
        sentences = [s.strip() for s in re.split(r'[.!?]', script) if s.strip()]
        pacing_score = 0
        if sentences:
            lengths = [len(s.split()) for s in sentences]
            avg = sum(lengths) / len(lengths)
            variance = max(lengths) - min(lengths) if len(lengths) > 1 else 0
            short_punches = sum(1 for l in lengths if l <= 4)

            if 6 <= avg <= 14:
                pacing_score += 10
            if variance >= 8:
                pacing_score += 10
            if short_punches >= 3:
                pacing_score += 5
            analysis["avg_sentence_len"] = round(avg, 1)
            analysis["pacing_score"] = pacing_score
        score += pacing_score

        return min(score, 100), analysis

    # ── Mejora del script ─────────────────────────────────────────────────────

    def _enhance_script(self, script: str, analysis: dict) -> str:
        """Inyecta elementos de retención donde faltan sin alterar el contenido."""
        words = script.split()
        total = len(words)

        def inject_at_pct(word_list: list, pct: float, phrase: str) -> list:
            pos = int(len(word_list) * pct)
            boundary = self._sentence_boundary(word_list, pos)
            return word_list[:boundary] + phrase.split() + word_list[boundary:]

        # Re-engagement a 30%
        if not analysis.get("reengage_30") and total >= 300:
            words = inject_at_pct(words, 0.30, random.choice(self._REENGAGE_30))
            total = len(words)

        # Re-engagement a 50%
        if not analysis.get("reengage_50") and total >= 500:
            words = inject_at_pct(words, 0.50, random.choice(self._REENGAGE_50))
            total = len(words)

        # Re-engagement a 70%
        if not analysis.get("reengage_70") and total >= 700:
            words = inject_at_pct(words, 0.70, random.choice(self._REENGAGE_70))
            total = len(words)

        # Pattern interrupts en bloques sin ellos (excepto primero y último)
        interrupt_kw = ["pero espera", "fíjate", "un momento", "esto es clave"]
        result = []
        for i in range(0, total, INTERRUPT_EVERY):
            block = words[i:i + INTERRUPT_EVERY]
            block_text = " ".join(block).lower()
            is_first = i == 0
            is_last = i + INTERRUPT_EVERY >= total
            if not is_first and not is_last and not any(kw in block_text for kw in interrupt_kw):
                result.extend(random.choice(self._INTERRUPTS).split())
            result.extend(block)

        return " ".join(result)

    def _sentence_boundary(self, words: list, pos: int) -> int:
        """Encuentra el límite de oración más cercano a la posición."""
        for offset in range(min(25, len(words) - pos)):
            for direction, idx in [(1, pos + offset), (-1, pos - offset)]:
                if 0 <= idx < len(words) and words[idx].rstrip().endswith(('.', '!', '?')):
                    return idx + 1
        return pos

    # ── Chapter markers ───────────────────────────────────────────────────────

    def _generate_chapters(self, script: str, mode: str) -> str:
        """
        Genera capítulos para la descripción de YouTube con timestamps precisos.
        Los emojis en los capítulos aumentan el CTR en el panel de descripción.
        """
        words_total = len(script.split())

        # Detectar secciones por marcadores CALIOPE
        marker_map = {
            "[PRECIO]":    "💰 Precio y movimiento",
            "[ANALISIS]":  "📊 Análisis técnico",
            "[IMPACTO]":   "🌍 Contexto e impacto",
            "[SENTIMIENTO]": "😨 Sentimiento del mercado",
            "[DOMINANCIA]": "👑 Altcoins y dominancia",
            "[ADOPCION]":  "🏢 Adopción y fundamentales",
            "[PREDICCION]": "🎯 Mis escenarios para esta semana",
            "[CONCLUSION]": "✅ Conclusión y CTA",
        }

        chapters = [("0:00", "🎬 Intro — El dato que cambia el análisis")]
        words_seen = 0

        for line in script.split("\n"):
            for marker, label in marker_map.items():
                if marker in line:
                    secs = int((words_seen / WPM) * 60)
                    if secs > 0:  # evitar duplicar 0:00
                        m, s = divmod(secs, 60)
                        ts = f"{m}:{s:02d}"
                        if not any(t == ts for t, _ in chapters):
                            chapters.append((ts, label))
            words_seen += len(line.split())

        # Fallback por modo si no se detectaron marcadores
        if len(chapters) < 3:
            structure = {
                "urgente":   ["💰 Precio ahora", "📊 El gráfico no miente", "🌍 Lo que los medios no cuentan", "🎯 Mis escenarios"],
                "noticia":   ["📰 La noticia completa", "📊 Impacto en el precio", "🌍 Contexto histórico", "🎯 Conclusión"],
                "analisis":  ["💰 Precio actual", "📊 Análisis técnico", "😨 Sentimiento", "👑 Altcoins", "🏢 Fundamentales", "🎯 Predicción"],
                "standard":  ["💰 Precio y contexto", "📊 Análisis", "🌍 Factores clave", "🎯 Conclusión"],
                "educativo": ["🎓 Qué es y por qué importa", "⚙️ Cómo funciona", "📈 En la práctica", "✅ Lo que debes recordar"],
                "opinion":   ["🔍 El argumento principal", "📊 Datos que lo respaldan", "⚔️ El contraargumento", "💡 Mi posición"],
            }.get(mode, ["💰 Contexto", "📊 Análisis", "🎯 Conclusión"])

            chapters = [("0:00", "🎬 Intro")]
            n = len(structure)
            for i, label in enumerate(structure, 1):
                words_at = int((i / (n + 1)) * words_total)
                secs = int((words_at / WPM) * 60)
                m, s = divmod(secs, 60)
                chapters.append((f"{m}:{s:02d}", label))

        lines = ["📌 CAPÍTULOS:"] + [f"{ts} {label}" for ts, label in chapters]
        return "\n".join(lines)

    # ── Display ───────────────────────────────────────────────────────────────

    def _print_report(self, score: int, analysis: dict) -> None:
        color = "green" if score >= 75 else "yellow" if score >= 60 else "red"
        emoji = "🔥" if score >= 80 else "⚡" if score >= 65 else "⚠️"

        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Métrica", style="dim")
        table.add_column("Valor", style="bold white")

        table.add_row("Hook",         f"{analysis.get('hook_score', 0)}/25 {'✓' if not analysis.get('hook_weak') else '⚠'}")
        table.add_row("Interrupts",   f"{analysis.get('interrupt_score', 0)}/25 ({analysis.get('interrupts_found', 0)} bloques)")
        table.add_row("Re-engage",    f"{analysis.get('reengage_score', 0)}/25")
        table.add_row("Pacing",       f"{analysis.get('pacing_score', 0)}/25 (avg {analysis.get('avg_sentence_len', '?')} palabras/frase)")
        table.add_row("Duración est.", f"~{analysis.get('estimated_minutes', '?')} min")

        console.print(Panel(
            f"[bold {color}]{emoji} Retention Score: {score}/100[/]\n",
            title="[bold white]ARES — Audience Retention Engine[/]",
            border_style=color,
            subtitle=f"[dim]{analysis.get('total_words', 0)} palabras[/]",
        ))
        console.print(table)
