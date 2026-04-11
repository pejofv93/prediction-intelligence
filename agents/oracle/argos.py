from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
argos.py
ARGOS — Vigilante de Precios
Capa ORÁCULO · NEXUS v1.0 · CryptoVerdad

Obtiene precios en tiempo real de CoinGecko, calcula volatilidad P90
a 30 días y detecta movimientos urgentes (>10% en 24h).
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
from rich.console import Console
from rich.table import Table

from core.base_agent import BaseAgent
from core.context import Context
from utils.logger import get_logger

console = Console()

# Mapeo CoinGecko id → símbolo
COIN_MAP = {
    "bitcoin":      "BTC",
    "ethereum":     "ETH",
    "solana":       "SOL",
    "binancecoin":  "BNB",
}

PRICE_TABLE  = "oracle_prices"
CACHE_TTL    = 300   # segundos — reutilizar caché si tiene <5 min
MAX_RETRIES  = 3     # intentos ante 429 / error de red


class ARGOS(BaseAgent):
    """Vigilante de precios cripto en tiempo real."""

    def __init__(self, config: dict, db):
        self.config = config
        self.db = db
        self.logger = get_logger("ARGOS")
        # Endpoint y headers según API key disponible
        api_key = os.getenv("COINGECKO_API_KEY", "")
        if api_key:
            self.base_url = "https://pro-api.coingecko.com/api/v3"
            self._headers = {"Accept": "application/json", "x-cg-pro-api-key": api_key}
            self.logger.info("CoinGecko PRO endpoint activo")
        else:
            self.base_url = "https://api.coingecko.com/api/v3"
            self._headers = {"Accept": "application/json"}

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get(self, url: str, timeout: float = 15.0) -> httpx.Response:
        """GET con retry y exponential backoff ante 429 / errores de red."""
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.get(url, headers=self._headers)
                if resp.status_code == 429:
                    wait = 2 ** attempt          # 1s, 2s, 4s
                    self.logger.warning(
                        f"CoinGecko 429 — intento {attempt+1}/{MAX_RETRIES}, "
                        f"esperando {wait}s..."
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError:
                raise
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                self.logger.warning(
                    f"CoinGecko error de red ({exc}) — "
                    f"intento {attempt+1}/{MAX_RETRIES}, esperando {wait}s..."
                )
                time.sleep(wait)
        raise last_exc or RuntimeError("CoinGecko: máximo de reintentos alcanzado")

    def _get_cached_prices(self) -> Optional[Dict[str, Any]]:
        """
        Lee oracle_prices en SQLite.
        Devuelve el snapshot más reciente si tiene < CACHE_TTL segundos.
        Devuelve None si el caché está vacío o expirado.
        """
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                row = conn.execute(
                    f"""
                    SELECT coin, price_usd, change_24h, market_cap, volatility,
                           recorded_at
                    FROM {PRICE_TABLE}
                    WHERE recorded_at = (
                        SELECT MAX(recorded_at) FROM {PRICE_TABLE}
                    )
                    ORDER BY coin
                    """
                ).fetchall()
            if not row:
                return None
            # Verificar antigüedad del snapshot
            ts_str = row[0][5]
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > CACHE_TTL:
                self.logger.debug(f"Caché de precios expirado ({age:.0f}s > {CACHE_TTL}s)")
                return None
            # Reconstruir dict en formato interno
            cached: Dict[str, Any] = {}
            sym_map = {v: v for v in COIN_MAP.values()}  # BTC→BTC, etc.
            coin_to_sym = {
                "bitcoin": "BTC", "ethereum": "ETH",
                "solana": "SOL", "binancecoin": "BNB",
            }
            for r in row:
                sym = coin_to_sym.get(r[0], r[0].upper())
                cached[sym] = {
                    "price":          r[1],
                    "change_24h":     r[2],
                    "market_cap":     r[3],
                    "volatility_p90": r[4],
                }
            self.logger.info(
                f"Caché de precios válido ({age:.0f}s < {CACHE_TTL}s) — "
                f"evitando llamada a CoinGecko"
            )
            return cached if cached else None
        except Exception as exc:
            self.logger.debug(f"Error leyendo caché de precios: {exc}")
            return None

    # Precios de último recurso — usados cuando CoinGecko y SQLite fallan
    _HARDCODED_FALLBACK = {
        "bitcoin":     {"usd": 72000.0, "usd_24h_change": 0.0, "usd_market_cap": 1_400_000_000_000.0},
        "ethereum":    {"usd": 2200.0,  "usd_24h_change": 0.0, "usd_market_cap":  260_000_000_000.0},
        "solana":      {"usd": 84.0,    "usd_24h_change": 0.0, "usd_market_cap":   37_000_000_000.0},
        "binancecoin": {"usd": 580.0,   "usd_24h_change": 0.0, "usd_market_cap":   80_000_000_000.0},
    }

    def _fetch_prices(self) -> Dict[str, Any]:
        """
        Obtiene precios de CoinGecko con retry.
        NUNCA lanza excepción — devuelve fallback hardcodeado si todo falla.
        """
        url = (
            f"{self.base_url}/simple/price"
            "?ids=bitcoin,ethereum,solana,binancecoin"
            "&vs_currencies=usd"
            "&include_24hr_change=true"
            "&include_market_cap=true"
        )
        try:
            resp = self._get(url, timeout=15.0)
            return resp.json()
        except Exception as exc:
            self.logger.warning(
                f"CoinGecko no disponible ({exc}) — "
                f"usando precios hardcodeados de último recurso"
            )
            return self._HARDCODED_FALLBACK

    def _fetch_volatility(self, coin_id: str) -> float:
        """
        Descarga precios de cierre de los últimos 30 días para un coin
        y devuelve el percentil 90 de los cambios diarios absolutos (%).
        """
        url = (
            f"{self.base_url}/coins/{coin_id}/market_chart"
            "?vs_currency=usd&days=30&interval=daily"
        )
        try:
            resp = self._get(url, timeout=20.0)
            data = resp.json()
            prices_raw: List[List[float]] = data.get("prices", [])
            if len(prices_raw) < 2:
                return 0.0
            closes = [p[1] for p in prices_raw]
            pct_changes = [
                abs((closes[i] - closes[i - 1]) / closes[i - 1]) * 100
                for i in range(1, len(closes))
            ]
            if _HAS_NUMPY:
                return float(np.percentile(pct_changes, 90))
            else:
                sorted_vals = sorted(pct_changes)
                idx = int(len(sorted_vals) * 0.9)
                return float(sorted_vals[min(idx, len(sorted_vals) - 1)])
        except Exception as exc:
            self.logger.warning(f"Volatilidad {coin_id} no disponible: {exc}")
            return 0.0

    def _ensure_table(self) -> None:
        """Crea la tabla oracle_prices si no existe."""
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {PRICE_TABLE} (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        pipeline_id  TEXT,
                        coin         TEXT,
                        price_usd    REAL,
                        change_24h   REAL,
                        market_cap   REAL,
                        volatility   REAL,
                        recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
        except Exception as exc:
            self.logger.warning(f"No se pudo crear tabla {PRICE_TABLE}: {exc}")

    def _persist(self, pipeline_id: str, prices: Dict[str, Any]) -> None:
        """Guarda snapshot de precios en SQLite."""
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                for symbol, data in prices.items():
                    conn.execute(
                        f"""
                        INSERT INTO {PRICE_TABLE}
                            (pipeline_id, coin, price_usd, change_24h, market_cap, volatility)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pipeline_id,
                            symbol,
                            data.get("price", 0.0),
                            data.get("change_24h", 0.0),
                            data.get("market_cap", 0.0),
                            data.get("volatility_p90", 0.0),
                        ),
                    )
        except Exception as exc:
            self.logger.warning(f"Persistencia de precios fallida: {exc}")

    def _print_table(self, prices: Dict[str, Any]) -> None:
        """Imprime tabla rich con los precios."""
        table = Table(
            title="[bold #F7931A]ARGOS — Precios en tiempo real[/]",
            style="bold white",
            border_style="#F7931A",
            show_header=True,
            header_style="bold #F7931A",
        )
        table.add_column("Moneda", justify="center", style="bold white", width=8)
        table.add_column("Precio USD", justify="right", style="bold white", width=14)
        table.add_column("24h %", justify="right", width=10)
        table.add_column("Market Cap", justify="right", style="dim white", width=16)
        table.add_column("Vol P90 30d", justify="right", style="dim white", width=12)

        for symbol, data in prices.items():
            change = data.get("change_24h", 0.0)
            change_color = "#4CAF50" if change >= 0 else "#F44336"
            change_str = f"[{change_color}]{change:+.2f}%[/]"
            mc = data.get("market_cap", 0)
            mc_str = f"${mc / 1e9:.1f}B" if mc >= 1e9 else f"${mc / 1e6:.0f}M"
            table.add_row(
                symbol,
                f"${data.get('price', 0):,.2f}",
                change_str,
                mc_str,
                f"{data.get('volatility_p90', 0):.2f}%",
            )

        console.print(table)

    # ── run ────────────────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        self.logger.info("ARGOS iniciado — obteniendo precios...")
        try:
            self._ensure_table()

            # 1. Caché primero — evita 429 si ya hay datos frescos (<5 min)
            prices = self._get_cached_prices()

            if prices is None:
                # 2. Caché vacío o expirado → llamar a CoinGecko con retry
                raw = self._fetch_prices()

                prices = {}
                for coin_id, symbol in COIN_MAP.items():
                    coin_data = raw.get(coin_id, {})
                    prices[symbol] = {
                        "price":          coin_data.get("usd", 0.0),
                        "change_24h":     coin_data.get("usd_24h_change", 0.0),
                        "market_cap":     coin_data.get("usd_market_cap", 0.0),
                        "volatility_p90": 0.0,
                    }

                # 3. Volatilidad P90 a 30 días (con delay entre llamadas)
                for coin_id, symbol in COIN_MAP.items():
                    self.logger.debug(f"Calculando volatilidad P90 para {symbol}...")
                    prices[symbol]["volatility_p90"] = self._fetch_volatility(coin_id)
                    time.sleep(1)  # 1s entre llamadas para no acumular 429

            # 3. Guardar en ctx
            ctx.prices = prices

            # 3b. Poblar ctx.btc_price, ctx.eth_price, ctx.sol_price con precios en tiempo real
            btc_data = prices.get("BTC", {})
            eth_data = prices.get("ETH", {})
            sol_data = prices.get("SOL", {})
            ctx.btc_price = float(btc_data.get("price", 0.0) or 0.0)
            ctx.eth_price = float(eth_data.get("price", 0.0) or 0.0)
            ctx.sol_price = float(sol_data.get("price", 0.0) or 0.0)
            self.logger.info(
                f"Precios tiempo real: BTC=${ctx.btc_price:,.2f} "
                f"ETH=${ctx.eth_price:,.2f} SOL=${ctx.sol_price:,.2f}"
            )

            # 3c. Verificar Fear & Greed Index (alternative.me)
            if getattr(ctx, 'fear_greed_value', 0):
                self.logger.info(
                    f"Fear & Greed verificado: {ctx.fear_greed_value} "
                    f"({ctx.fear_greed_label}) — fuente: alternative.me"
                )
            else:
                self.logger.warning(
                    "Fear & Greed no disponible en ctx — "
                    "DAEDALUS deberia obtenerlo de https://api.alternative.me/fng/?limit=1&format=json"
                )
                if not getattr(ctx, 'fear_greed_value', 0):
                    ctx.fear_greed_value = 50
                    ctx.fear_greed_label = "Neutral"
                    self.logger.info("Fear & Greed: fallback aplicado → 50 (Neutral)")

            # 3d. Verificar dominancia BTC (CoinGecko /global)
            if getattr(ctx, 'btc_dominance', 0.0):
                self.logger.info(
                    f"Dominancia BTC verificada: {ctx.btc_dominance:.1f}% — fuente: CoinGecko"
                )
            else:
                self.logger.warning(
                    "Dominancia BTC no disponible en ctx — "
                    "intentando obtener de CoinGecko /global..."
                )
                try:
                    global_url = f"{self.base_url}/global"
                    with httpx.Client(timeout=10.0) as client:
                        g_resp = client.get(global_url, headers={"Accept": "application/json"})
                        g_resp.raise_for_status()
                    g_data = g_resp.json().get("data", {})
                    btc_dom = g_data.get("market_cap_percentage", {}).get("btc", 0.0)
                    if btc_dom > 0:
                        ctx.btc_dominance = float(btc_dom)
                        self.logger.info(
                            f"Dominancia BTC obtenida por ARGOS: {ctx.btc_dominance:.1f}%"
                        )
                    else:
                        self.logger.warning("Dominancia BTC: respuesta vacía de CoinGecko /global")
                except Exception as dom_exc:
                    self.logger.warning(f"Dominancia BTC fallback error: {dom_exc}")

            # 4. Detectar urgencia
            for symbol, data in prices.items():
                if abs(data.get("change_24h", 0.0)) > 10.0:
                    ctx.is_urgent = True
                    ctx.urgency_score += 30.0
                    ctx.add_warning(
                        "ARGOS",
                        f"{symbol} cambio 24h: {data['change_24h']:+.2f}% — URGENTE",
                    )
                    self.logger.warning(
                        f"[bold #F44336]URGENTE:[/] {symbol} {data['change_24h']:+.2f}% en 24h"
                    )

            # 5. Loguear tabla
            self._print_table(prices)

            # 6. Persistir snapshot en oracle_prices
            self._persist(ctx.pipeline_id, prices)

            # 7. Guardar precios válidos en market_prices para fallback futuro
            _coin_map_db = {
                "BTC": "bitcoin",
                "ETH": "ethereum",
                "SOL": "solana",
            }
            for sym, cg_id in _coin_map_db.items():
                _p = prices.get(sym, {}).get("price", 0.0) or 0.0
                if _p > 0:
                    try:
                        self.db.save_coin_price(cg_id, _p)
                    except Exception as _sv:
                        self.logger.warning(f"save_coin_price({cg_id}): {_sv}")

            self.logger.info(
                f"ARGOS completado. {len(prices)} monedas. "
                f"BTC=${ctx.btc_price:,.2f}. Urgente={ctx.is_urgent}"
            )

        except Exception as e:
            self.logger.error(f"ARGOS error: {e}")
            # No registrar error todavía — intentar fallback SQLite primero.
            # Si hay precios en SQLite, el pipeline puede continuar con warning.
            _fetch_error = str(e)

            # ── Fallback SQLite ────────────────────────────────────────────────
            # Reconstruir ctx.prices desde market_prices para que CALÍOPE y
            # HEPHAESTUS tengan los datos aunque CoinGecko esté caído.
            _fallback_map_db = {
                "bitcoin":     "BTC",
                "ethereum":    "ETH",
                "solana":      "SOL",
                "binancecoin": "BNB",
            }
            fallback_prices: Dict[str, Any] = {}
            for cg_id, sym in _fallback_map_db.items():
                try:
                    last = self.db.get_last_coin_price(cg_id)
                    if last > 0:
                        fallback_prices[sym] = {
                            "price":          last,
                            "change_24h":     0.0,
                            "market_cap":     0.0,
                            "volatility_p90": 0.0,
                        }
                except Exception:
                    pass

            if fallback_prices:
                ctx.prices   = fallback_prices
                ctx.btc_price = fallback_prices.get("BTC", {}).get("price", 0.0)
                ctx.eth_price = fallback_prices.get("ETH", {}).get("price", 0.0)
                ctx.sol_price = fallback_prices.get("SOL", {}).get("price", 0.0)
                ctx.add_warning(
                    "ARGOS",
                    f"CoinGecko no disponible ({_fetch_error}) — "
                    f"usando precios SQLite: BTC=${ctx.btc_price:,.0f}",
                )
                self.logger.warning(
                    f"[yellow]ARGOS fallback SQLite:[/] "
                    f"BTC=${ctx.btc_price:,.0f} ETH=${ctx.eth_price:,.0f} "
                    f"SOL=${ctx.sol_price:,.0f} — pipeline continúa"
                )
            else:
                # Sin caché y sin SQLite → error fatal (pipeline debe detenerse)
                ctx.add_error("ARGOS", _fetch_error)
                self.logger.error(
                    "[red]ARGOS: sin precios en CoinGecko ni en SQLite — "
                    "pipeline detenido[/]"
                )

            return ctx

        # ── Fallback SQLite para ctx_attrs individuales (flujo normal) ─────────
        # Solo cubre el caso en que el try tuvo éxito parcial (precio = 0).
        _fallback_map = [
            ("btc_price", "bitcoin"),
            ("eth_price", "ethereum"),
            ("sol_price", "solana"),
        ]
        for ctx_attr, cg_id in _fallback_map:
            current = getattr(ctx, ctx_attr, 0.0) or 0.0
            if current <= 0:
                try:
                    last = self.db.get_last_coin_price(cg_id)
                    if last > 0:
                        setattr(ctx, ctx_attr, last)
                        self.logger.info(
                            f"{ctx_attr} desde SQLite fallback: ${last:,.2f}"
                        )
                except Exception as _fb:
                    self.logger.warning(f"SQLite fallback {cg_id}: {_fb}")

        return ctx

