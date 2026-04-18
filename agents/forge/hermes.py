from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agents/forge/hermes.py
HERMES v2 — Motor SEO de NEXUS.

Mejoras v2:
  - YouTube Autocomplete: keywords reales que buscan usuarios (sin API key)
  - CTR Formula Engine: puntúa títulos con patrones de alto CTR comprobados
  - Scoring v2: 13 criterios vs 8 anteriores (weight-based)
  - Chapter markers integrados con ARES
  - Description v2: estructura emocional + CTA + links
"""

import json
import re
import time
import httpx
from pathlib import Path
from typing import List, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from core.base_agent import BaseAgent
from core.context import Context
from utils.llm_client import LLMClient

console = Console()

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SEO_MIN_SCORE = 70
WPM = 145


# ── CTR Power Words — empiricamente validados en crypto YouTube España ────────
_CTR_POWER_WORDS = {
    "urgencia":    ["AHORA", "HOY", "URGENTE", "INMEDIATO", "YA", "ACABA DE"],
    "exclusivo":   ["NADIE", "SECRETO", "VERDAD", "REVELADO", "EXCLUSIVO"],
    "miedo_fomo":  ["CUIDADO", "PELIGRO", "ALERTA", "ATENCIÓN", "NO PIERDAS"],
    "dato":        ["RECORD", "HISTORICO", "MAXIMO", "MINIMO", "MILLONES"],
    "opinion":     ["MI OPINION", "POR QUE", "LA RAZON", "LO QUE PIENSO"],
}

# Boost por patrón de título (añadir al score base del LLM)
_TITLE_PATTERN_BOOST = {
    r'\?':                          +8,   # pregunta directa
    r'\bHOY\b|\bAHORA\b':          +6,   # urgencia temporal
    r'\b\d+\b':                     +5,   # número concreto
    r'\bNADIE\b|\bSECRETO\b|\bVERDAD\b': +7,  # exclusividad
    r'\bCUIDADO\b|\bALERTA\b|\bPELIGRO\b': +6,  # miedo/FOMO
    r'^\w.{0,50}$':                 +4,   # longitud ≤50 chars (mejor en móvil)
    r'[A-ZÁÉÍÓÚ]{3,}':             +3,   # alguna palabra en mayúscula
}


class HERMES(BaseAgent):
    """
    Motor SEO v2 para CryptoVerdad.

    Genera:
      - Título CTR-optimizado (≤60 chars, keyword en primeras 3 palabras)
      - Descripción SEO (≥300 palabras, chapters, CTA, links)
      - 8-25 tags (primer tag = título exacto, incluye sugerencias YouTube)
      - Score SEO v2 (0-100, 13 criterios)
      - Keywords de YouTube Autocomplete (sin API key)
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config)
        self.db = db
        self.llm = LLMClient(config, db=db)

    # ── YouTube Autocomplete ──────────────────────────────────────────────────

    def _get_youtube_suggestions(self, keyword: str) -> List[str]:
        """
        Obtiene sugerencias de búsqueda reales de YouTube (sin API key).
        Endpoint público de Google Suggest — mismo que usa YouTube search.
        Timeout agresivo para no bloquear el pipeline.
        """
        suggestions = []
        try:
            url = "https://suggestqueries.google.com/complete/search"
            # Evitar duplicar la keyword si ya es "bitcoin"
            crypto_terms = {"bitcoin", "btc", "ethereum", "eth", "crypto", "cripto"}
            query = keyword if keyword.lower() in crypto_terms else f"{keyword} bitcoin"
            params = {
                "client": "youtube",
                "ds": "yt",
                "q": query,
                "hl": "es",
                "gl": "ES",
            }
            with httpx.Client(timeout=4.0) as client:
                resp = client.get(url, params=params)
            if resp.status_code == 200:
                # La respuesta es JSONP: callback([...])
                raw = resp.text
                match = re.search(r'\[.*\]', raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    if isinstance(data, list) and len(data) > 1:
                        items = data[1]
                        suggestions = [
                            item[0] if isinstance(item, list) else str(item)
                            for item in items[:8]
                            if item
                        ]
        except Exception as e:
            self.logger.debug(f"YouTube Autocomplete no disponible: {e}")
        return suggestions

    # ── CTR Formula Engine ────────────────────────────────────────────────────

    def _score_title_ctr(self, title: str) -> int:
        """
        Puntúa el CTR potencial de un título (0-50 adicionales al score SEO base).
        Basado en patrones de alto CTR en canales financieros/crypto en español.
        """
        total = 0
        title_upper = title.upper()
        for pattern, boost in _TITLE_PATTERN_BOOST.items():
            if re.search(pattern, title_upper):
                total += boost
        return min(total, 25)  # cap en 25 pts adicionales

    def _generate_title_variants(self, topic: str, keyword: str, prices: dict,
                                  suggestions: List[str]) -> List[Tuple[str, int]]:
        """
        Genera 8 variantes de título con fórmulas de CTR validadas.
        Complementa las 5 del LLM con 3 generadas por fórmula directa.
        """
        btc_price = ""
        if prices:
            btc = prices.get("BTC", {})
            if isinstance(btc, dict) and btc.get("price"):
                btc_price = f"${btc['price']:,.0f}"

        # Fórmulas de alto CTR — slot para keyword y precio
        kw = keyword.upper()
        price_str = btc_price or ""
        topic_short = topic[:35].rstrip(".,;:") if len(topic) > 35 else topic

        formula_variants = [
            f"{kw} HOY: lo que nadie te cuenta",
            f"¿Por qué {keyword.capitalize()} {price_str} es diferente?",
            f"CUIDADO con {keyword.capitalize()} ahora mismo",
            f"La VERDAD sobre {topic_short}",
            f"{kw}: mi análisis honesto esta semana",
        ]

        # Añadir variante con sugerencia de YouTube si disponible
        if suggestions:
            s = suggestions[0][:50].rstrip(".,;:")
            formula_variants.append(f"{s.capitalize()} — análisis real")

        # Calcular CTR score por fórmula
        scored = []
        for t in formula_variants:
            t_clean = t[:60]  # YouTube trunca a 60 chars
            ctr = self._score_title_ctr(t_clean)
            scored.append((t_clean, ctr))

        return sorted(scored, key=lambda x: x[1], reverse=True)[:3]

    # ── SEO Score v2 (13 criterios) ───────────────────────────────────────────

    def _calculate_seo_score(self, title: str, description: str, tags: list,
                              script: str, keyword: str) -> int:
        """
        Score SEO v2 — 13 criterios con pesos diferenciados.

        Distribución:
          Título (30 pts): keyword posición + longitud + CTR patterns
          Descripción (30 pts): keyword + longitud + timestamps + CTA + links
          Tags (20 pts): cantidad + primer tag + diversidad
          Script (20 pts): longitud + densidad de contenido
        """
        score = 0
        kw = keyword.lower()

        # ── Título (30 pts) ───────────────────────────────────────────────
        title_lower = title.lower()
        title_words = title_lower.split()

        # Keyword en primeras 3 palabras (+12)
        if any(kw in w for w in title_words[:3]):
            score += 12

        # Longitud óptima (+8 si ≤55, +4 si ≤60)
        if len(title) <= 55:
            score += 8
        elif len(title) <= 60:
            score += 4

        # CTR patterns en título (+10)
        ctr = self._score_title_ctr(title)
        score += min(ctr, 10)

        # ── Descripción (30 pts) ──────────────────────────────────────────
        desc_lower = description.lower()
        first_200 = desc_lower[:200]

        # Keyword en primeras 2 líneas (+8)
        first_lines = "\n".join(description.split("\n")[:2]).lower()
        if kw in first_lines:
            score += 8

        # Longitud ≥300 palabras (+8)
        desc_words = len(description.split())
        if desc_words >= 500:
            score += 8
        elif desc_words >= 300:
            score += 5

        # Timestamps / chapters (+6)
        if re.search(r'\b\d{1,2}:\d{2}\b', description):
            score += 6

        # CTA claro en descripción (+4)
        cta_words = ["suscríbete", "activa", "comenta", "síguenos", "like", "subscri"]
        if any(w in desc_lower for w in cta_words):
            score += 4

        # Link a red social o web (+4)
        if re.search(r'https?://', description) or "@" in description:
            score += 4

        # ── Tags (20 pts) ─────────────────────────────────────────────────
        # Cantidad óptima (+8)
        n_tags = len(tags)
        if 12 <= n_tags <= 20:
            score += 8
        elif 8 <= n_tags <= 25:
            score += 5

        # Primer tag = título exacto (+6)
        if tags and tags[0].strip().lower() == title.strip().lower():
            score += 6

        # Tags incluyen keyword exacta (+6)
        tags_lower = [t.lower() for t in tags]
        if kw in tags_lower or any(kw in t for t in tags_lower):
            score += 6

        # ── Script (20 pts) ───────────────────────────────────────────────
        if script:
            script_words = len(script.split())
            if script_words >= 1000:
                score += 20
            elif script_words >= 600:
                score += 12
            elif script_words >= 300:
                score += 6

        return min(score, 100)

    # ── Prompts ───────────────────────────────────────────────────────────────

    def _load_system_prompt(self) -> str:
        path = PROMPTS_DIR / "hermes_seo.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return (
            "Eres el experto SEO de CryptoVerdad, canal de análisis crypto en español. "
            "Genera títulos con alto CTR, descripciones detalladas y tags relevantes. "
            "Responde SOLO con JSON válido, sin markdown."
        )

    def _build_user_prompt(self, ctx: Context, suggestions: List[str]) -> str:
        lines = [
            f"TEMA: {ctx.topic}",
            f"MODO: {ctx.mode}",
            f"KEYWORD PRINCIPAL: {self._extract_keyword(ctx.topic)}",
        ]
        if ctx.script:
            lines.append(f"\nRESUMEN DEL GUION (primeras 600 palabras):\n{' '.join(ctx.script.split()[:600])}")
        if ctx.prices:
            btc = ctx.prices.get("BTC", {})
            if isinstance(btc, dict) and btc.get("price"):
                lines.append(f"\nPRECIO BTC ACTUAL: ${btc['price']:,.0f} ({btc.get('change_24h', 0):+.1f}% 24h)")
        if ctx.retention_score:
            lines.append(f"\nRETENTION SCORE: {ctx.retention_score}/100")
        if suggestions:
            lines.append(f"\nBUSQUEDAS REALES EN YOUTUBE (úsalas en tags y descripción):\n" + "\n".join(f"  - {s}" for s in suggestions[:6]))
        if ctx.chapter_markers:
            lines.append(f"\nCHAPTER MARKERS GENERADOS (incorpóralos en la descripción):\n{ctx.chapter_markers}")

        lines.append(
            "\n\nDevuelve EXACTAMENTE este JSON (sin markdown, sin comentarios):\n"
            '{\n'
            '  "titles": [\n'
            '    {"title": "Titulo A (max 60 chars, keyword en primeras 3 palabras)", "score": 92},\n'
            '    {"title": "Titulo B con pregunta o dato (max 60 chars)", "score": 85},\n'
            '    {"title": "Titulo C con urgencia (max 60 chars)", "score": 79},\n'
            '    {"title": "Titulo D alternativo (max 60 chars)", "score": 74},\n'
            '    {"title": "Titulo E conservador (max 60 chars)", "score": 70}\n'
            '  ],\n'
            '  "description": "descripcion 400+ palabras con chapters, CTA, links a redes...",\n'
            '  "tags": ["titulo exacto", "keyword", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8"]\n'
            '}'
        )
        return "\n".join(lines)

    def _extract_keyword(self, topic: str) -> str:
        stopwords = {"el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "a",
                     "que", "por", "para", "con", "se", "es", "al", "lo", "su", "más"}
        words = topic.lower().split()
        for w in words:
            clean = re.sub(r"[^\w]", "", w, flags=re.UNICODE)
            if clean not in stopwords and len(clean) > 2:
                return clean
        return re.sub(r"[^\w]", "", words[0], flags=re.UNICODE) if words else topic.lower()

    def _parse_llm_response(self, raw: str) -> dict:
        clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            clean_json = re.sub(r"[\x00-\x1f\x7f]", "", match.group())
            return json.loads(clean_json)
        raise ValueError(f"No se encontró JSON válido: {raw[:200]}")

    def _ensure_tag_limit(self, tags: list, title: str, keyword: str,
                           suggestions: List[str]) -> list:
        """Garantiza 12-20 tags con diversidad semántica."""
        # Primer tag siempre = título exacto
        clean = [t for t in tags if t.strip().lower() != title.strip().lower()]
        result = [title] + clean

        # Añadir sugerencias de YouTube que no estén ya
        for s in suggestions:
            s_clean = s.strip()
            if s_clean and s_clean.lower() not in [r.lower() for r in result]:
                result.append(s_clean)

        # Recortar a 20
        result = result[:20]

        # Padding con tags de alta frecuencia crypto España
        fallback = [
            keyword, "Bitcoin", "BTC", "Criptomonedas",
            "CryptoVerdad", "análisis Bitcoin", "Bitcoin hoy",
            "precio Bitcoin", "Ethereum", "mercado crypto",
            "análisis técnico Bitcoin", "crypto España",
            "Bitcoin análisis", "criptomonedas España",
        ]
        for candidate in fallback:
            if len(result) >= 15:
                break
            if candidate.lower() not in [r.lower() for r in result]:
                result.append(candidate)

        return result[:20]

    def _enrich_description(self, description: str, ctx: Context,
                             chapters: str) -> str:
        """
        Enriquece la descripción con estructura profesional:
        chapters → cuerpo → CTA → links → aviso legal
        """
        parts = []

        # 1. Aviso legal primero (regla identidad visual)
        if ctx.legal_warning_added or ctx.script:
            trigger_words = ["comprar", "vender", "invertir", "bullish", "bearish",
                             "acumular", "entrada", "salida", "predicción"]
            script_lower = (ctx.script or "").lower()
            if any(w in script_lower for w in trigger_words):
                parts.append(
                    "⚠️ Este vídeo es solo contenido educativo e informativo. "
                    "No es consejo de inversión. Invierte bajo tu propia responsabilidad.\n"
                )

        # 2. Chapter markers (si no están ya en la descripción)
        if chapters and "📌" not in description:
            parts.append(chapters + "\n")

        # 3. Cuerpo principal
        parts.append(description.strip())

        # 4. CTA y redes
        cta = (
            "\n\n🔔 SUSCRÍBETE para análisis diarios sin humo → @CryptoVerdad\n"
            "💬 Deja tu comentario: ¿cuál es tu escenario esta semana?\n"
            "👍 Si el análisis te ayudó, dale al like — es gratis y nos ayuda mucho.\n"
            "\n📲 SÍGUENOS EN REDES:\n"
            "• Telegram: https://t.me/CryptoVerdad\n"
            "• Twitter/X: https://x.com/CryptoVerdad\n"
        )
        parts.append(cta)

        # 5. Tags de búsqueda al final (mejora indexación)
        if ctx.seo_tags:
            tag_line = " ".join(f"#{t.replace(' ', '')}" for t in ctx.seo_tags[:10])
            parts.append(f"\n{tag_line}")

        return "\n".join(parts)

    # ── run() ─────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("HERMES v2 iniciado")
        console.print(Panel(
            "[bold #F7931A]HERMES v2[/] — Motor SEO\n"
            f"Tema: [italic]{ctx.topic}[/]",
            border_style="#F7931A",
        ))

        try:
            keyword = self._extract_keyword(ctx.topic)
            console.print(f"[dim]Keyword: [bold]{keyword}[/][/]")

            # 1. YouTube Autocomplete (async-tolerant, falla silencioso)
            console.print("[dim]Consultando YouTube Autocomplete...[/]")
            suggestions = self._get_youtube_suggestions(keyword)
            ctx.keyword_suggestions = suggestions
            if suggestions:
                self.logger.info(f"YouTube Autocomplete: {len(suggestions)} sugerencias — {suggestions[:3]}")
                console.print(f"[dim green]✓ {len(suggestions)} búsquedas reales detectadas[/]")
            else:
                console.print("[dim yellow]Autocomplete no disponible (offline o Railway)[/]")

            # 2. Variantes extra por fórmula CTR
            formula_variants = self._generate_title_variants(
                ctx.topic, keyword, ctx.prices, suggestions
            )

            # 3. LLM genera 5 variantes + descripción + tags
            system_prompt = self._load_system_prompt()
            user_prompt = self._build_user_prompt(ctx, suggestions)
            console.print("[dim]Generando metadata SEO con LLM...[/]")

            raw = self.llm.generate(
                prompt=user_prompt,
                system=system_prompt,
                max_tokens=2000,
                temperature=0.6,  # más consistente para SEO que 0.9
            )
            data = self._parse_llm_response(raw)

            # 4. Combinar variantes LLM + fórmula y elegir mejor
            titles_list = data.get("titles", [])
            all_candidates = []

            for t in titles_list:
                if isinstance(t, dict) and t.get("title"):
                    title_clean = str(t["title"])[:60]
                    base_score = int(t.get("score", 70))
                    ctr_boost = self._score_title_ctr(title_clean)
                    all_candidates.append((title_clean, base_score + ctr_boost))

            # Añadir variantes de fórmula
            for title_f, ctr_f in formula_variants:
                all_candidates.append((title_f, 70 + ctr_f))

            # Seleccionar el mejor por score combinado
            if all_candidates:
                best_title, best_combined = max(all_candidates, key=lambda x: x[1])
                title = best_title
                # Log completo para auditoría
                self.logger.info(
                    "Candidatos de título: " +
                    " | ".join(f'"{t[:25]}"({s})' for t, s in sorted(all_candidates, key=lambda x: -x[1])[:5])
                )
            else:
                title = ctx.topic[:60]

            description = data.get("description", "")
            tags = data.get("tags", [])

            # 5. Enriquecer tags con sugerencias YouTube
            tags = self._ensure_tag_limit(tags, title, keyword, suggestions)

            # 6. Enriquecer descripción con estructura profesional
            chapters = ctx.chapter_markers or ""
            description = self._enrich_description(description, ctx, chapters)

            # 7. Calcular score v2
            score = self._calculate_seo_score(title, description, tags, ctx.script, keyword)

            # 8. Persistir en Context
            ctx.seo_title = title
            ctx.seo_description = description
            ctx.seo_tags = tags
            ctx.seo_score = score

            if score < SEO_MIN_SCORE:
                msg = f"SEO score {score}/100 — bajo el mínimo ({SEO_MIN_SCORE})"
                ctx.add_warning("HERMES", msg)
                self.logger.warning(msg)

            # Display
            table = Table(title="HERMES v2 — Resultado SEO", border_style="#F7931A",
                          box=box.ROUNDED, show_header=True)
            table.add_column("Campo", style="bold white", width=18)
            table.add_column("Valor", style="white")
            table.add_row("Título", title)
            table.add_row("Score SEO v2", f"[bold {'green' if score >= 70 else 'red'}]{score}/100[/]")
            table.add_row("Tags", f"{len(tags)} tags")
            table.add_row("Desc. palabras", str(len(description.split())))
            table.add_row("YT Suggestions", str(len(suggestions)))
            console.print(table)

            self.logger.info(
                f"HERMES v2 — título: '{title}' | score: {score}/100 | "
                f"tags: {len(tags)} | suggestions: {len(suggestions)}"
            )

        except Exception as e:
            self.logger.error(f"Error en HERMES: {e}")
            ctx.add_error("HERMES", str(e))

        return ctx
