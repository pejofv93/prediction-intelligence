from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
aletheia.py
ALETHEIA — Verificador de veracidad de NEXUS.
Cruza afirmaciones del guión con precios y noticias reales.
NO bloquea el pipeline; solo advierte y anota.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# Tolerancia para discrepancias de precio (20 %)
PRICE_TOLERANCE = 0.20

# Umbrales de inconsistencia para nota en el guión
GRAVE_THRESHOLD = 3  # si hay >= 3 inconsistencias, se añade nota al guión

# Precio mínimo razonable por moneda (evita falsos positivos con horas/porcentajes)
MIN_COIN_PRICE: Dict[str, float] = {
    "bitcoin":      1_000,
    "ethereum":     50,
    "binancecoin":  10,
    "solana":       1,
    "ripple":       0.05,
    "cardano":      0.05,
    "dogecoin":     0.001,
    "avalanche-2":  1,
    "polkadot":     1,
    "matic-network":0.1,
}

# Ticker → clave en ctx.prices
COIN_ALIASES: Dict[str, str] = {
    "BTC": "bitcoin",
    "BITCOIN": "bitcoin",
    "ETH": "ethereum",
    "ETHEREUM": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "MATIC": "matic-network",
}

NOTA_INCONSISTENCIAS = (
    "\n\n---\n"
    "⚠️ NOTA DE VERIFICACIÓN: Este guión fue revisado automáticamente por ALETHEIA. "
    "Se detectaron posibles inexactitudes en algunas cifras. "
    "Verifica los datos antes de publicar.\n"
    "---"
)


class ALETHEIA(BaseAgent):
    """
    Verifica que las afirmaciones numéricas del guión sean consistentes
    con los datos reales del pipeline (precios, noticias, porcentajes).
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("ALETHEIA")

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold white]ALETHEIA[/] iniciado")
        try:
            if not ctx.script:
                self.logger.info("[yellow]ALETHEIA[/] guión vacío, nada que verificar")
                return ctx

            inconsistencies: List[str] = []

            price_issues = self._check_prices(ctx)
            inconsistencies.extend(price_issues)

            date_issues = self._check_dates(ctx)
            inconsistencies.extend(date_issues)

            pct_issues = self._check_percentages(ctx)
            inconsistencies.extend(pct_issues)

            for issue in inconsistencies:
                ctx.add_warning("ALETHEIA", issue)

            self._log_results(inconsistencies)

            if len(inconsistencies) >= GRAVE_THRESHOLD:
                ctx.script += NOTA_INCONSISTENCIAS
                self.logger.warning(
                    f"[yellow]ALETHEIA[/] {len(inconsistencies)} inconsistencias graves — "
                    "nota añadida al guión"
                )
        except Exception as exc:
            self.logger.error(f"[red]ALETHEIA error:[/] {exc}")
            ctx.add_error("ALETHEIA", str(exc))
        return ctx

    # ── normalización números españoles ──────────────────────────────────────
    @staticmethod
    def _normalize_spanish_numbers(text: str) -> str:
        """
        Convierte formas verbales de precio al equivalente numérico para comparación.
        Ejemplos:
          "67 mil dólares"         → "67000 dólares"
          "67 mil quinientos"      → "67500"
          "3 mil quinientos dólares" → "3500 dólares"
          "100 mil millones"       se deja igual (evita falsos positivos de market-cap)
        """
        _CENTENAS = {
            "cien": 100, "ciento": 100,
            "doscientos": 200, "doscientas": 200,
            "trescientos": 300, "trescientas": 300,
            "cuatrocientos": 400, "cuatrocientas": 400,
            "quinientos": 500, "quinientas": 500,
            "seiscientos": 600, "seiscientas": 600,
            "setecientos": 700, "setecientas": 700,
            "ochocientos": 800, "ochocientas": 800,
            "novecientos": 900, "novecientas": 900,
        }
        centenas_pat = "|".join(_CENTENAS.keys())

        def _replace(m: re.Match) -> str:
            base = float(m.group(1).replace(",", "."))
            centenas_word = (m.group(2) or "").strip().lower()
            if "millones" in centenas_word or "billones" in centenas_word:
                return m.group(0)  # no tocar market-cap
            extra = _CENTENAS.get(centenas_word, 0)
            total = int(base * 1_000 + extra)
            return str(total)

        pattern = re.compile(
            rf"(\d+(?:[.,]\d+)?)\s+mil(?:\s+({centenas_pat}|millones|billones))?",
            re.IGNORECASE,
        )
        return pattern.sub(_replace, text)

    def _check_prices(self, ctx: Context) -> List[str]:
        """Detecta menciones de precio en el guión y las compara con ctx.prices."""
        issues = []
        if not ctx.prices:
            return issues

        # Normaliza "67 mil dólares" → "67000 dólares" antes de buscar
        script_normalized = self._normalize_spanish_numbers(ctx.script)

        # Busca patrones: BTC a 100k, BTC en $95,000, precio de ETH: 3.500
        # Excluye:
        #   - porcentajes: número seguido de %, "por ciento"
        #   - períodos de tiempo: número seguido de h, hs, horas, días, semanas
        pattern = re.compile(
            r"\b(BTC|ETH|BITCOIN|ETHEREUM|BNB|SOL|XRP|ADA|DOGE|AVAX|DOT|MATIC)\b"
            r"[^.!?\n]{0,40}?"
            r"\$?\s*([\d][,\d]*(?:\.\d+)?)\s*([kKmM]?)\b"
            r"(?!\s*(?:%|por ciento|hs?\b|horas?\b|días?\b|semanas?\b|meses?\b|minutos?\b))",
            re.IGNORECASE,
        )

        for match in pattern.finditer(script_normalized):
            ticker = match.group(1).upper()
            raw_num = match.group(2).replace(",", "")
            multiplier = match.group(3).lower()

            try:
                mentioned_price = float(raw_num)
                if multiplier == "k":
                    mentioned_price *= 1_000
                elif multiplier == "m":
                    mentioned_price *= 1_000_000
            except ValueError:
                continue

            # Excluir años históricos citados como contexto temporal.
            # Detecta patrones: "en 2020", "desde 2021", "año 2022", "durante 2020".
            # Un precio real de ETH en ~$2000 nunca aparece precedido de "en" o "año".
            if 1999 <= mentioned_price <= 2030:
                num_start = match.start(2)
                pre_ctx = script_normalized[max(0, num_start - 25):num_start]
                if re.search(
                    r'\b(en|año|desde|hasta|durante|antes de|después de|en el)\s*$',
                    pre_ctx.strip(), re.I
                ):
                    continue  # es un año histórico, no un precio

            # Descarta valores por debajo del mínimo razonable por moneda
            coin_key_check = COIN_ALIASES.get(ticker)
            min_price = MIN_COIN_PRICE.get(coin_key_check, 0.001) if coin_key_check else 0.001
            if mentioned_price < min_price:
                continue

            coin_key = COIN_ALIASES.get(ticker)
            if not coin_key:
                continue

            coin_data = ctx.prices.get(coin_key)
            if coin_data is None:
                continue
            # ctx.prices puede ser plano {coin: float} o anidado {coin: {"usd": float}}
            if isinstance(coin_data, (int, float)):
                real_price = float(coin_data)
            else:
                real_price = coin_data.get("usd")
            if real_price is None:
                continue

            try:
                real_price = float(real_price)
            except (ValueError, TypeError):
                continue

            if real_price == 0:
                continue

            discrepancy = abs(mentioned_price - real_price) / real_price
            if discrepancy > PRICE_TOLERANCE:
                issues.append(
                    f"Precio mencionado de {ticker} (${mentioned_price:,.0f}) "
                    f"difiere del real (${real_price:,.0f}) "
                    f"en {discrepancy*100:.1f}% — fragmento: «{match.group(0)[:60]}»"
                )

        return issues

    # ── verificación de fechas ────────────────────────────────────────────────
    def _check_dates(self, ctx: Context) -> List[str]:
        """
        Verifica que las fechas mencionadas en el guión coincidan con
        las noticias reales (ctx.news).
        Detecta menciones de 'ayer', 'hoy', 'el lunes' y las contrasta.
        """
        issues = []
        if not ctx.news:
            return issues

        # Extrae títulos de noticias para búsqueda de fechas implícitas
        # Por ahora solo verificamos que no haya fechas futuras presentadas como pasadas
        from datetime import datetime

        # Busca años mencionados en el guión
        year_pattern = re.compile(r"\b(20\d{2})\b")
        current_year = datetime.now().year

        for match in year_pattern.finditer(ctx.script):
            year = int(match.group(1))
            if year > current_year:
                # Año futuro presentado en contexto narrativo puede ser OK (predicción)
                # Solo advertimos si está en un contexto de hecho consumado
                context_snippet = ctx.script[max(0, match.start()-30):match.end()+30]
                past_verbs = re.search(
                    r"\b(ocurrió|pasó|sucedió|fue|llegó|subió|bajó|crasheó|cayó)\b",
                    context_snippet, re.IGNORECASE
                )
                if past_verbs:
                    issues.append(
                        f"Posible fecha futura ({year}) usada en contexto pasado: "
                        f"«{context_snippet.strip()}»"
                    )

        return issues

    # ── verificación de porcentajes ───────────────────────────────────────────
    def _check_percentages(self, ctx: Context) -> List[str]:
        """
        Contrasta porcentajes de variación mencionados con los de ctx.prices.
        """
        issues = []
        if not ctx.prices:
            return issues

        # Pattern: +X%, -X%, subió un X%, cayó un X%
        pct_pattern = re.compile(
            r"([+\-]?\s*\d+(?:\.\d+)?)\s*%",
            re.IGNORECASE,
        )

        # Extraemos los cambios 24h reales disponibles
        real_changes: List[float] = []
        for coin_data in ctx.prices.values():
            if isinstance(coin_data, (int, float)):
                continue  # precio plano, sin datos de cambio 24h
            chg = coin_data.get("usd_24h_change")
            if chg is not None:
                try:
                    real_changes.append(float(chg))
                except (ValueError, TypeError):
                    pass

        if not real_changes:
            return issues

        max_real = max(abs(c) for c in real_changes)

        for match in pct_pattern.finditer(ctx.script):
            raw = match.group(1).replace(" ", "")
            try:
                mentioned_pct = abs(float(raw))
            except ValueError:
                continue

            # Alerta si el porcentaje mencionado es >3x el movimiento real máximo
            # (indicador de exageración significativa)
            if mentioned_pct > 0 and max_real > 0 and mentioned_pct > max_real * 3:
                context_snippet = ctx.script[max(0, match.start()-40):match.end()+40]
                issues.append(
                    f"Porcentaje mencionado ({mentioned_pct:.1f}%) es muy superior "
                    f"al movimiento real máximo 24h ({max_real:.1f}%): "
                    f"«{context_snippet.strip()[:80]}»"
                )

        return issues

    # ── log de resultados ─────────────────────────────────────────────────────
    def _log_results(self, issues: List[str]) -> None:
        if not issues:
            console.print(
                "[bold white]ALETHEIA[/] [green]✓ Sin inconsistencias detectadas[/]"
            )
            return

        table = Table(
            title=f"[bold white]ALETHEIA[/] [yellow]{len(issues)} inconsistencia(s) detectada(s)[/]",
            show_header=True,
            header_style="bold white on #0A0A0A",
            show_lines=True,
        )
        table.add_column("#", style="yellow", width=3)
        table.add_column("Inconsistencia", style="white")

        for i, issue in enumerate(issues, 1):
            table.add_row(str(i), issue)

        console.print(table)

