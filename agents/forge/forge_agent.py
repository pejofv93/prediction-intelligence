from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
forge_agent.py — Orquestador de la capa FORGE
Coordina: CALÍOPE, HERMES, ECHO, HEPHAESTUS, IRIS, DAEDALUS
"""
from core.context import Context
from utils.logger import get_logger

logger = get_logger("FORGE_AGENT")

class ForgeAgent:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self._load_agents()

    def _load_agents(self):
        try:
            from agents.forge.caliope import CALIOPE as Caliope
            self._caliope = Caliope(self.config, self.db)
        except Exception as e:
            logger.warning(f"CALIOPE no disponible: {e}")
            self._caliope = None

        try:
            from agents.forge.hermes import HERMES as Hermes
            self._hermes = Hermes(self.config, self.db)
        except Exception as e:
            logger.warning(f"HERMES no disponible: {e}")
            self._hermes = None

        try:
            from agents.forge.echo import ECHO as Echo
            self._echo = Echo(self.config, self.db)
        except Exception as e:
            logger.warning(f"ECHO no disponible: {e}")
            self._echo = None

        try:
            from agents.forge.daedalus import DAEDALUS as Daedalus
            self._daedalus = Daedalus(self.config, self.db)
        except Exception as e:
            logger.warning(f"DAEDALUS no disponible: {e}")
            self._daedalus = None

        try:
            from agents.forge.helios import HELIOS as Helios
            self._helios = Helios(self.config, self.db)
        except Exception as e:
            logger.warning(f"HELIOS no disponible: {e}")
            self._helios = None

        try:
            from agents.forge.prometheus import PROMETHEUS as Prometheus
            self._prometheus = Prometheus(self.config, self.db)
        except Exception as e:
            logger.warning(f"PROMETHEUS no disponible: {e}")
            self._prometheus = None

        try:
            from agents.forge.hephaestus import HEPHAESTUS as Hephaestus
            self._hephaestus = Hephaestus(self.config, self.db)
        except Exception as e:
            logger.warning(f"HEPHAESTUS no disponible: {e}")
            self._hephaestus = None

        try:
            from agents.forge.iris import IRIS as Iris
            self._iris = Iris(self.config, self.db)
        except Exception as e:
            logger.warning(f"IRIS no disponible: {e}")
            self._iris = None

    # Fallback hardcodeado para precios — último recurso antes de CALÍOPE
    # Actualizado: 2026-04-16 (verificado con Binance spot)
    _PRICE_FALLBACK = {
        "BTC": 74000.0,
        "ETH": 2340.0,
        "SOL": 130.0,
    }

    def _ensure_prices(self, ctx: Context) -> Context:
        """
        Verifica que ctx.prices tiene valores reales para BTC, ETH, SOL.
        Si alguno es 0 o None: intenta CoinGecko directo, luego hardcoded.
        Loguea WARNING si se usa fallback.
        """
        import requests

        needed = {
            "bitcoin":  "BTC",
            "ethereum": "ETH",
            "solana":   "SOL",
        }
        prices = getattr(ctx, "prices", {}) or {}

        missing = [
            (cg_id, sym)
            for cg_id, sym in needed.items()
            if not (prices.get(sym, {}) or {}).get("price", 0)
        ]

        if not missing:
            # Todos los precios están bien — sincronizar attrs individuales por si acaso
            for cg_id, sym in needed.items():
                attr = f"{sym.lower()}_price"
                if not getattr(ctx, attr, 0):
                    setattr(ctx, attr, prices[sym]["price"])
            return ctx

        missing_syms = [sym for _, sym in missing]
        logger.warning(
            f"[FORGE] Precios faltantes en ctx antes de CALÍOPE: {missing_syms} — "
            f"intentando CoinGecko directo"
        )

        # Intento CoinGecko directo
        try:
            ids = ",".join(cg_id for cg_id, _ in missing)
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"[FORGE] CoinGecko directo respuesta: {data}")
            for cg_id, sym in missing:
                usd = data.get(cg_id, {}).get("usd", 0)
                if usd and float(usd) > 0:
                    if sym not in prices:
                        prices[sym] = {"price": 0.0, "change_24h": 0.0, "market_cap": 0.0, "volatility_p90": 0.0}
                    prices[sym]["price"] = float(usd)
                    setattr(ctx, f"{sym.lower()}_price", float(usd))
                    logger.info(f"[FORGE] {sym} actualizado desde CoinGecko directo: ${usd:,.0f}")
        except Exception as e:
            logger.warning(f"[FORGE] CoinGecko directo falló: {e} — usando hardcoded fallback")

        # Para los que sigan en 0: usar hardcoded
        for cg_id, sym in missing:
            current = (prices.get(sym, {}) or {}).get("price", 0)
            if not current:
                fallback_price = self._PRICE_FALLBACK.get(sym, 0)
                if sym not in prices:
                    prices[sym] = {"price": 0.0, "change_24h": 0.0, "market_cap": 0.0, "volatility_p90": 0.0}
                prices[sym]["price"] = fallback_price
                setattr(ctx, f"{sym.lower()}_price", fallback_price)
                ctx.add_warning(
                    "FORGE",
                    f"Precio {sym} no disponible — usando hardcoded fallback ${fallback_price:,.0f} "
                    f"(puede estar desactualizado)"
                )
                logger.warning(
                    f"[FORGE] {sym} usando hardcoded fallback: ${fallback_price:,.0f}"
                )

        ctx.prices = prices
        return ctx

    def run(self, ctx: Context) -> Context:
        logger.info("FORGE_AGENT iniciado")

        # Garantizar precios válidos antes de que CALÍOPE los use en el guión
        ctx = self._ensure_prices(ctx)

        if self._caliope:
            try:
                ctx = self._caliope.run(ctx)
                logger.info("CALIOPE completado")
            except Exception as e:
                ctx.add_error("CALIOPE", str(e))
                logger.error(f"CALIOPE error: {e}")
                return ctx  # Sin guión no tiene sentido continuar

        if self._hermes:
            try:
                ctx = self._hermes.run(ctx)
                logger.info("HERMES completado")
            except Exception as e:
                ctx.add_warning("HERMES", str(e))

        if self._echo:
            try:
                ctx = self._echo.run(ctx)
                logger.info("ECHO completado")
            except Exception as e:
                ctx.add_warning("ECHO", str(e))
                logger.error(f"ECHO error: {e}")

        if self._daedalus:
            try:
                ctx = self._daedalus.run(ctx)
                logger.info("DAEDALUS completado")
            except Exception as e:
                ctx.add_warning("DAEDALUS", str(e))

        # HELIOS y PROMETHEUS desactivados temporalmente — modo sin avatar
        # ctx.avatar_path queda vacío → HEPHAESTUS usa layout FULLSCREEN

        if self._hephaestus:
            try:
                ctx = self._hephaestus.run(ctx)
                logger.info("HEPHAESTUS completado")
            except Exception as e:
                ctx.add_warning("HEPHAESTUS", str(e))
                logger.error(f"HEPHAESTUS error: {e}")

        if self._iris:
            try:
                ctx = self._iris.run(ctx)
                logger.info("IRIS completado")
            except Exception as e:
                ctx.add_warning("IRIS", str(e))

        return ctx

