from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
themis.py
THEMIS — Juez Estratégico
Capa ORÁCULO · NEXUS v1.0 · CryptoVerdad

Consolida todos los datos de ARGOS, PYTHIA, RECON y VECTOR,
llama al LLM para decidir la estrategia editorial y persiste
la decisión en SQLite.
"""

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from core.base_agent import BaseAgent
from core.context import Context
from utils.llm_client import LLMClient
from utils.logger import get_logger

console = Console()

STRATEGY_TABLE = "oracle_strategy"
PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "themis_strategy.txt"

VALID_MODES = {"standard", "urgente", "short", "analisis", "opinion", "tutorial", "thread"}

# Fallback si el LLM no responde
FALLBACK_STRATEGY = {
    "mode":             "standard",
    "angle":            "Análisis de mercado con perspectiva española",
    "hook":             "El mercado cripto nunca duerme — aquí va lo que necesitas saber hoy.",
    "reasoning":        "Fallback automático: LLM no disponible. Modo estándar por defecto.",
    "urgent":           False,
    "estimated_views":  "medio",
}


class THEMIS(BaseAgent):
    """Juez estratégico — decide modo, ángulo y hooks del vídeo."""

    def __init__(self, config: dict, db):
        self.config = config
        self.db = db
        self.logger = get_logger("THEMIS")
        self.llm = LLMClient(config, db=db)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _load_prompt_template(self) -> str:
        try:
            return PROMPT_PATH.read_text(encoding="utf-8")
        except Exception as exc:
            self.logger.warning(f"No se pudo leer prompt THEMIS: {exc}. Usando inline.")
            return (
                "Eres el estratega de CryptoVerdad. Analiza los datos y decide la estrategia.\n\n"
                "DATOS:\n{data}\n\n"
                'Responde en JSON: {{"mode":"standard","angle":"...","hook":"...","reasoning":"...","urgent":false,"estimated_views":"medio"}}'
            )

    def _build_data_payload(self, ctx: Context) -> str:
        """Construye el bloque de datos para el prompt del LLM."""
        # Precios — top 3 por cambio absoluto
        prices_summary = []
        for sym, d in ctx.prices.items():
            # ctx.prices puede ser plano {coin: float} o anidado {coin: {price: float, ...}}
            if isinstance(d, (int, float)):
                prices_summary.append(f"  {sym}: ${float(d):,.0f}")
            else:
                prices_summary.append(
                    f"  {sym}: ${d.get('price', 0):,.0f} ({d.get('change_24h', 0):+.2f}% 24h) "
                    f"Vol_P90={d.get('volatility_p90', 0):.1f}%"
                )

        # Noticias top 3
        news_summary = []
        for n in ctx.news[:3]:
            news_summary.append(
                f"  [{n.get('source', '')}] {n.get('title', '')} (relevancia {n.get('relevance', 0)}/100)"
            )

        # Tendencias top 10
        trends_summary = ctx.trends[:10]

        # Gap de competidores
        gap_info = "Sin datos de competidores."
        if ctx.competitors:
            titles = [c.get("title", "") for c in ctx.competitors[:3]]
            gap_info = f"Competidores recientes: {'; '.join(titles)}"

        payload = {
            "topic":           ctx.topic,
            "is_urgent":       ctx.is_urgent,
            "urgency_score":   ctx.urgency_score,
            "precios": "\n".join(prices_summary) if prices_summary else "No disponible",
            "noticias_top3": "\n".join(news_summary) if news_summary else "No disponible",
            "tendencias": ", ".join(trends_summary) if trends_summary else "No disponible",
            "competidores_gap": gap_info,
            "errores_previos": len(ctx.errors),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _parse_llm_response(self, raw: str) -> Dict[str, Any]:
        """
        Extrae el JSON de la respuesta del LLM.
        Tolerante a texto extra antes/después del JSON.
        """
        # Intentar extraer bloque JSON
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise ValueError("No se encontró JSON en la respuesta del LLM.")
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON inválido en respuesta LLM: {exc}")

        # Validar y normalizar campos
        mode = data.get("mode", "standard").lower()
        if mode not in VALID_MODES:
            mode = "standard"

        return {
            "topic":           str(data.get("topic", "")).strip(),
            "mode":            mode,
            "angle":           str(data.get("angle", FALLBACK_STRATEGY["angle"])),
            "hook":            str(data.get("hook", FALLBACK_STRATEGY["hook"])),
            "reasoning":       str(data.get("reasoning", FALLBACK_STRATEGY["reasoning"])),
            "urgent":          bool(data.get("urgent", False)),
            "estimated_views": str(data.get("estimated_views", "medio")),
        }

    # ── DB ────────────────────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {STRATEGY_TABLE} (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        pipeline_id     TEXT UNIQUE,
                        topic           TEXT,
                        mode            TEXT,
                        angle           TEXT,
                        hook            TEXT,
                        reasoning       TEXT,
                        urgent          INTEGER DEFAULT 0,
                        estimated_views TEXT,
                        urgency_score   REAL,
                        llm_used        INTEGER DEFAULT 1,
                        recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
        except Exception as exc:
            self.logger.warning(f"No se pudo crear tabla {STRATEGY_TABLE}: {exc}")

    def _persist(self, ctx: Context, strategy: Dict, llm_used: bool) -> None:
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                conn.execute(
                    f"""
                    INSERT INTO {STRATEGY_TABLE}
                        (pipeline_id, topic, mode, angle, hook, reasoning,
                         urgent, estimated_views, urgency_score, llm_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pipeline_id) DO UPDATE SET
                        mode            = excluded.mode,
                        angle           = excluded.angle,
                        hook            = excluded.hook,
                        reasoning       = excluded.reasoning,
                        urgent          = excluded.urgent,
                        estimated_views = excluded.estimated_views,
                        urgency_score   = excluded.urgency_score,
                        llm_used        = excluded.llm_used
                    """,
                    (
                        ctx.pipeline_id,
                        ctx.topic,
                        strategy["mode"],
                        strategy["angle"],
                        strategy["hook"],
                        strategy["reasoning"],
                        1 if strategy["urgent"] else 0,
                        strategy["estimated_views"],
                        ctx.urgency_score,
                        1 if llm_used else 0,
                    ),
                )
        except Exception as exc:
            self.logger.warning(f"Persistencia de estrategia fallida: {exc}")

    def _print_strategy(self, strategy: Dict, llm_used: bool) -> None:
        views_colors = {
            "viral": "#F7931A",
            "alto":  "#4CAF50",
            "medio": "#FFFFFF",
            "bajo":  "#888888",
        }
        ev = strategy.get("estimated_views", "medio")
        ev_color = views_colors.get(ev, "#FFFFFF")
        llm_tag = "[bold #4CAF50]LLM[/]" if llm_used else "[bold #888888]FALLBACK[/]"

        text = Text()
        text.append("Modo:      ", style="bold #F7931A")
        text.append(f"{strategy['mode'].upper()}\n", style="bold white")
        text.append("Ángulo:    ", style="bold #F7931A")
        text.append(f"{strategy['angle']}\n", style="white")
        text.append("Hook:      ", style="bold #F7931A")
        text.append(f"{strategy['hook']}\n", style="italic white")
        text.append("Views est: ", style="bold #F7931A")
        text.append(f"{ev.upper()}\n", style=f"bold {ev_color}")
        text.append("Urgente:   ", style="bold #F7931A")
        urgente_val = strategy.get("urgent", False)
        text.append(
            "SI\n" if urgente_val else "NO\n",
            style="bold #F44336" if urgente_val else "dim white",
        )
        text.append("Fuente:    ", style="bold #F7931A")
        text.append(llm_tag + "\n")

        console.print(
            Panel(
                text,
                title="[bold #F7931A]THEMIS — Estrategia Editorial[/]",
                border_style="#F7931A",
                padding=(1, 2),
            )
        )

        # Reasoning completo
        console.print(
            Panel(
                f"[dim white]{strategy['reasoning']}[/]",
                title="[dim]Razonamiento[/]",
                border_style="dim #888888",
            )
        )

    # ── run ────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("THEMIS iniciado — elaborando estrategia editorial...")
        try:
            self._ensure_table()

            template = self._load_prompt_template()
            data_payload = self._build_data_payload(ctx)
            # Usar replace en vez de format() para evitar que las {} del JSON
            # del prompt file sean interpretadas como format placeholders
            prompt = template.replace("{data}", data_payload)

            strategy: Dict
            llm_used = True

            try:
                self.logger.debug("Llamando al LLM...")
                raw_response = self.llm.generate(
                    prompt=prompt,
                    system=(
                        "Eres el estratega editorial de CryptoVerdad. "
                        "Responde SIEMPRE en JSON válido, sin texto adicional fuera del JSON."
                    ),
                    max_tokens=1200,
                )
                strategy = self._parse_llm_response(raw_response)
                self.logger.info("Estrategia obtenida del LLM correctamente.")
            except Exception as llm_exc:
                self.logger.error(
                    f"LLM falló: {llm_exc} — usando estrategia fallback."
                )
                ctx.add_warning("THEMIS", f"LLM falló, usando fallback: {llm_exc}")
                strategy = FALLBACK_STRATEGY.copy()
                llm_used = False

            # Garantía: strategy_reasoning nunca vacío
            reasoning = strategy.get("reasoning", "").strip()
            if not reasoning:
                reasoning = (
                    f"Estrategia {'automática' if llm_used else 'fallback'} para topic: {ctx.topic}. "
                    f"Urgency score: {ctx.urgency_score}. "
                    f"Modo seleccionado: {strategy.get('mode', 'standard')}."
                )
                strategy["reasoning"] = reasoning

            # Aplicar al Context
            ctx.strategy_reasoning = strategy["reasoning"]

            # Actualizar topic si THEMIS generó uno mejor (modo auto)
            # El topic genérico "análisis crypto diario" se reemplaza siempre.
            _generic_topics = {"análisis crypto diario", "analisis crypto diario", ""}
            new_topic = strategy.get("topic", "").strip()
            if new_topic and ctx.topic.strip().lower() in _generic_topics:
                ctx.topic = new_topic
                self.logger.info(f"THEMIS: topic actualizado desde noticias: '{new_topic}'")

            # Respetar modo forzado por CLI — THEMIS solo sugiere, no impone
            if ctx.forced_mode:
                ctx.script_mode = ctx.forced_mode
                ctx.mode = ctx.forced_mode
                self.logger.info(
                    f"THEMIS: modo forzado por CLI '{ctx.forced_mode}' — ignorando sugerencia '{strategy['mode']}'"
                )
            else:
                ctx.script_mode = strategy["mode"]
                ctx.mode = strategy["mode"]
            ctx.is_urgent = strategy.get("urgent", False) or ctx.is_urgent

            self._print_strategy(strategy, llm_used)
            self._persist(ctx, strategy, llm_used)

            self.logger.info(
                f"THEMIS completado. Modo={ctx.script_mode}. Urgente={ctx.is_urgent}."
            )

        except Exception as e:
            self.logger.error(f"THEMIS error crítico: {e}")
            ctx.add_error("THEMIS", str(e))
            # Fallback garantizado
            if not ctx.strategy_reasoning:
                ctx.strategy_reasoning = (
                    f"Error en THEMIS: {e}. "
                    f"Topic: {ctx.topic}. Modo por defecto: standard."
                )
            if not ctx.script_mode:
                ctx.script_mode = "standard"

        return ctx

