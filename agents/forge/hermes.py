from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/forge/hermes.py
HERMES — Motor SEO de NEXUS.

Genera titulo, descripcion y tags optimizados para YouTube.
Calcula un score SEO 0-100 y emite warnings si es bajo.
El BLOQUEO real antes de publicar lo ejecuta NexusCore.
"""

import json
import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.base_agent import BaseAgent
from core.context import Context
from utils.llm_client import LLMClient

console = Console()

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SEO_MIN_SCORE = 70


class HERMES(BaseAgent):
    """
    Motor SEO completo para CryptoVerdad.

    Genera:
        - Titulo SEO (<= 60 chars, keyword en primeras 3 palabras)
        - Descripcion SEO (>= 300 palabras, keyword en primeras 2 lineas)
        - 5-7 tags (primer tag = titulo exacto)
        - Score SEO 0-100

    No bloquea el pipeline; emite ctx.warnings si score < 70.
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config)
        self.db = db
        self.llm = LLMClient(config, db=db)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_system_prompt(self) -> str:
        path = PROMPTS_DIR / "hermes_seo.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return (
            "Eres el experto en SEO de CryptoVerdad. "
            "Genera titulos, descripciones y tags optimizados para YouTube. "
            "Responde SOLO con JSON valido."
        )

    def _build_user_prompt(self, ctx: Context) -> str:
        lines = [
            f"TEMA: {ctx.topic}",
            f"MODO: {ctx.mode}",
        ]
        if ctx.script:
            lines.append(f"\nRESUMEN DEL GUION:\n{ctx.script[:800]}...")
        if ctx.prices:
            btc = ctx.prices.get("BTC", {})
            if isinstance(btc, dict) and btc.get("price"):
                lines.append(f"\nPRECIO BTC: ${btc['price']:,.0f}")

        lines.append(
            "\n\nDevuelve EXACTAMENTE este JSON (sin markdown, sin explicaciones):\n"
            '{\n'
            '  "titles": [\n'
            '    {"title": "Opcion 1 (max 60 chars)", "score": 88},\n'
            '    {"title": "Opcion 2 (max 60 chars)", "score": 82},\n'
            '    {"title": "Opcion 3 (max 60 chars)", "score": 79},\n'
            '    {"title": "Opcion 4 (max 60 chars)", "score": 75},\n'
            '    {"title": "Opcion 5 (max 60 chars)", "score": 71}\n'
            '  ],\n'
            '  "description": "descripcion 300+ palabras aqui...",\n'
            '  "tags": ["titulo exacto", "tag2", "tag3", "tag4", "tag5"]\n'
            '}'
        )
        return "\n".join(lines)

    def _extract_keyword(self, topic: str) -> str:
        """Extrae la keyword principal del tema (primera palabra significativa)."""
        import re as _re
        stopwords = {"el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "a"}
        words = topic.lower().split()
        for w in words:
            clean = _re.sub(r"[^\w]", "", w, flags=_re.UNICODE)
            if clean not in stopwords and len(clean) > 2:
                return clean
        return _re.sub(r"[^\w]", "", words[0], flags=_re.UNICODE) if words else topic.lower()

    def _calculate_seo_score(
        self,
        title: str,
        description: str,
        tags: list,
        script: str,
        keyword: str,
    ) -> int:
        """Calcula score SEO de 0 a 100."""
        import re as _re
        score = 0

        # Titulo: keyword en primeras 3 palabras (+20)
        title_words = title.lower().split()[:3]
        if any(keyword in w for w in title_words):
            score += 20

        # Titulo: longitud <= 60 chars (+10)
        if len(title) <= 60:
            score += 10

        # Descripcion: keyword en primeras 2 lineas (+15)
        first_lines = "\n".join(description.split("\n")[:2]).lower()
        if keyword in first_lines:
            score += 15

        # Descripcion: >= 300 palabras (+15)
        if len(description.split()) >= 300:
            score += 15

        # Tags: entre 8 y 25 (+15)
        if 8 <= len(tags) <= 25:
            score += 15

        # Primer tag = titulo exacto (+10)
        if tags and tags[0].strip().lower() == title.strip().lower():
            score += 10

        # Descripcion: contiene timestamps formato 0:00 o 00:00 (+10)
        if _re.search(r'\b\d{1,2}:\d{2}\b', description):
            score += 10

        # Legibilidad del guion: longitud razonable (+5)
        if script and len(script.split()) >= 300:
            score += 5

        return min(score, 100)

    def _parse_llm_response(self, raw: str) -> dict:
        """Extrae JSON de la respuesta del LLM, incluso si viene con markdown."""
        # Eliminar bloques de markdown si existen
        clean = re.sub(r"```(?:json)?", "", raw).strip()
        # Buscar el primer { ... }
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            import re as _re
            clean_json = _re.sub(r"[\x00-\x1f\x7f]", "", match.group())
            return json.loads(clean_json)
        raise ValueError(f"No se encontro JSON valido en la respuesta LLM: {raw[:200]}")

    def _ensure_tag_limit(self, tags: list, title: str) -> list:
        """Garantiza 8-25 tags con el titulo como primer elemento."""
        # Asegurar titulo como primer tag
        clean_tags = [t for t in tags if t.strip().lower() != title.strip().lower()]
        result = [title] + clean_tags

        # Recortar a 25 maximos
        result = result[:25]

        # Padding si hay menos de 8
        fallback = [
            "Bitcoin", "Crypto", "CryptoVerdad", "BTC", "Criptomonedas",
            "Ethereum", "ETH", "análisis crypto", "precio bitcoin",
            "bitcoin hoy", "ethereum hoy", "criptomonedas España",
            "bitcoin análisis", "crypto España", "inversión crypto",
        ]
        while len(result) < 8:
            candidate = fallback.pop(0) if fallback else f"crypto{len(result)}"
            if candidate not in result:
                result.append(candidate)

        return result

    def _generate_timestamps(self, script: str, wpm: int) -> str:
        """Genera timestamps a partir de las secciones del guion."""
        section_markers = [
            ("[PRECIO]", "Precio actual del mercado"),
            ("[ANALISIS]", "Análisis técnico"),
            ("[SENTIMIENTO]", "Sentimiento del mercado"),
            ("[ADOPCION]", "Factores fundamentales y adopción"),
            ("[DOMINANCIA]", "Dominancia y altcoins"),
            ("[PREDICCION]", "Escenarios y predicción"),
        ]

        lines = script.split("\n")
        section_times = []
        words_so_far = 0

        for marker, label in section_markers:
            for i, line in enumerate(lines):
                if marker in line:
                    total_seconds = int((words_so_far / wpm) * 60)
                    minutes = total_seconds // 60
                    seconds = total_seconds % 60
                    section_times.append(f"{minutes}:{seconds:02d} {label}")
                    # Count words from start to this point
                    words_so_far = len(" ".join(lines[:i]).split())
                    break

        if not section_times:
            # Distribucion uniforme en 6 partes si no hay marcadores de seccion
            total_words = len(script.split())
            generic_sections = [
                "Introducción y precio actual",
                "Contexto histórico",
                "Análisis técnico",
                "Sentimiento del mercado",
                "Factores fundamentales",
                "Escenarios y predicción",
            ]
            for idx, label in enumerate(generic_sections):
                words_at = int((idx / len(generic_sections)) * total_words)
                total_seconds = int((words_at / wpm) * 60)
                minutes = total_seconds // 60
                seconds = total_seconds % 60
                section_times.append(f"{minutes}:{seconds:02d} {label}")

        return "📌 CAPÍTULOS:\n" + "\n".join(section_times)

    # ── run() ─────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("HERMES iniciado")
        console.print(
            Panel(
                "[bold #F7931A]HERMES[/] — Motor SEO\n"
                f"Tema: [italic]{ctx.topic}[/]",
                border_style="#F7931A",
            )
        )

        try:
            system_prompt = self._load_system_prompt()
            user_prompt = self._build_user_prompt(ctx)
            keyword = self._extract_keyword(ctx.topic)

            console.print(f"[dim]Keyword principal detectada: [bold]{keyword}[/][/]")
            console.print("[dim]Generando metadata SEO con LLM...[/]")

            raw = self.llm.generate(
                prompt=user_prompt,
                system=system_prompt,
                max_tokens=1500,
            )

            data = self._parse_llm_response(raw)

            # Seleccionar el mejor título del array de 5 variantes
            titles_list = data.get("titles", [])
            if titles_list and isinstance(titles_list, list):
                # Elegir el de mayor score; si hay empate, el primero
                best = max(titles_list, key=lambda t: t.get("score", 0) if isinstance(t, dict) else 0)
                title: str = str(best.get("title", ctx.topic))[:60]
                # Log de variantes para auditoría
                self.logger.info(
                    f"Títulos generados: "
                    + " | ".join(
                        f'{t.get("title","?")[:30]} ({t.get("score","?")})'
                        for t in titles_list[:5]
                        if isinstance(t, dict)
                    )
                )
            else:
                # Fallback: campo "title" legacy
                title = str(data.get("title", ctx.topic))[:60]

            description: str = data.get("description", "")
            tags: list = data.get("tags", [])

            # Normalizar tags
            tags = self._ensure_tag_limit(tags, title)

            # Calcular score
            score = self._calculate_seo_score(
                title, description, tags, ctx.script, keyword
            )

            # Guardar en Context
            ctx.seo_title = title
            ctx.seo_description = description
            ctx.seo_tags = tags
            ctx.seo_score = score

            # Warning si score bajo (el bloqueo real es en NexusCore)
            if score < SEO_MIN_SCORE:
                msg = f"SEO bajo: {score}/100 (minimo recomendado: {SEO_MIN_SCORE})"
                ctx.add_warning("HERMES", msg)
                self.logger.warning(msg)
                console.print(f"[bold yellow]⚠ {msg}[/]")

            # Mostrar tabla resumen
            table = Table(title="Resultado SEO", border_style="#F7931A", show_header=True)
            table.add_column("Campo", style="bold white")
            table.add_column("Valor", style="white")
            table.add_row("Titulo", title)
            table.add_row("Score", f"[bold {'green' if score >= 70 else 'red'}]{score}/100[/]")
            table.add_row("Tags", ", ".join(tags))
            table.add_row("Desc. palabras", str(len(description.split())))
            console.print(table)

            self.logger.info(
                f"SEO generado — titulo: '{title}' | score: {score}/100 | tags: {len(tags)}"
            )

            # Garantizar timestamps en descripcion
            import re as _re
            if not _re.search(r'\b\d{1,2}:\d{2}\b', ctx.seo_description):
                wpm = 145  # velocidad media del narrador
                timestamps = self._generate_timestamps(ctx.script or "", wpm)
                ctx.seo_description = timestamps + "\n\n" + ctx.seo_description
                self.logger.info("HERMES: timestamps generados automaticamente e inyectados en descripcion")

        except Exception as e:
            self.logger.error(f"Error en HERMES: {e}")
            ctx.add_error("HERMES", str(e))

        return ctx


