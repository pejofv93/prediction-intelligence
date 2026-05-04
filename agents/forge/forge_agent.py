from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
forge_agent.py — Orquestador de la capa FORGE
Coordina: CALÍOPE → HERMES → ARES → [ECHO ∥ DAEDALUS] → HEPHAESTUS → IRIS

Pipeline paralelo:
  ECHO y DAEDALUS se ejecutan en threads independientes ya que no comparten
  estado de escritura. Esto reduce el tiempo de FORGE en ~40%.
  HEPHAESTUS espera a que ambos terminen antes de componer el vídeo.
"""
import copy
import time
import concurrent.futures
from core.context import Context
from utils.logger import get_logger

logger = get_logger("FORGE_AGENT")


class ForgeAgent:
    def __init__(self, config: dict, db=None):
        self.config = config
        self.db = db
        self._load_agents()

    def _load_agents(self):
        for attr, module, cls in [
            ("_caliope",    "agents.forge.caliope",    "CALIOPE"),
            ("_hermes",     "agents.forge.hermes",     "HERMES"),
            ("_ares",       "agents.forge.ares",       "ARES"),
            ("_echo",       "agents.forge.echo",       "ECHO"),
            ("_daedalus",   "agents.forge.daedalus",   "DAEDALUS"),
            ("_hephaestus", "agents.forge.hephaestus", "HEPHAESTUS"),
            ("_iris",       "agents.forge.iris",       "IRIS"),
        ]:
            try:
                mod = __import__(module, fromlist=[cls])
                setattr(self, attr, getattr(mod, cls)(self.config, self.db))
            except Exception as e:
                setattr(self, attr, None)
                logger.warning(f"{cls} no disponible: {e}")

        # HELIOS y PROMETHEUS desactivados — modo FULLSCREEN sin avatar
        self._helios = None
        self._prometheus = None

    # ── Fallback de precios ───────────────────────────────────────────────────
    # Actualizado: 2026-04-16 (verificado con Binance spot)
    _PRICE_FALLBACK = {"BTC": 74000.0, "ETH": 2340.0, "SOL": 130.0}

    def _ensure_prices(self, ctx: Context) -> Context:
        """Garantiza que BTC/ETH/SOL tienen precio antes de CALÍOPE."""
        import requests

        needed = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
        prices = getattr(ctx, "prices", {}) or {}
        missing = [
            (cg_id, sym) for cg_id, sym in needed.items()
            if not (prices.get(sym, {}) or {}).get("price", 0)
        ]
        if not missing:
            for _, sym in needed.items():
                attr = f"{sym.lower()}_price"
                if not getattr(ctx, attr, 0) and prices.get(sym, {}).get("price"):
                    setattr(ctx, attr, prices[sym]["price"])
            return ctx

        missing_syms = [s for _, s in missing]
        logger.warning(f"[FORGE] Precios faltantes: {missing_syms} — consultando CoinGecko")
        try:
            ids = ",".join(cg_id for cg_id, _ in missing)
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd"}, timeout=8,
            )
            resp.raise_for_status()
            for cg_id, sym in missing:
                usd = resp.json().get(cg_id, {}).get("usd", 0)
                if usd:
                    prices.setdefault(sym, {"price": 0.0, "change_24h": 0.0,
                                            "market_cap": 0.0, "volatility_p90": 0.0})
                    prices[sym]["price"] = float(usd)
                    setattr(ctx, f"{sym.lower()}_price", float(usd))
        except Exception as e:
            logger.warning(f"[FORGE] CoinGecko falló: {e} — usando fallback")

        for _, sym in missing:
            if not (prices.get(sym, {}) or {}).get("price", 0):
                fb = self._PRICE_FALLBACK.get(sym, 0)
                prices.setdefault(sym, {"price": 0.0, "change_24h": 0.0,
                                        "market_cap": 0.0, "volatility_p90": 0.0})
                prices[sym]["price"] = fb
                setattr(ctx, f"{sym.lower()}_price", fb)
                ctx.add_warning("FORGE", f"Precio {sym} hardcoded: ${fb:,.0f}")

        ctx.prices = prices
        return ctx

    # ── Ejecución paralela ECHO ∥ DAEDALUS ───────────────────────────────────

    def _run_echo_thread(self, ctx_snapshot: Context) -> Context:
        """Ejecuta ECHO en un thread independiente sobre copia de ctx."""
        try:
            return self._echo.run(ctx_snapshot)
        except Exception as e:
            ctx_snapshot.add_error("ECHO", str(e))
            logger.error(f"ECHO thread error: {e}")
            return ctx_snapshot

    def _run_daedalus_thread(self, ctx_snapshot: Context) -> Context:
        """Ejecuta DAEDALUS en un thread independiente sobre copia de ctx."""
        try:
            return self._daedalus.run(ctx_snapshot)
        except Exception as e:
            ctx_snapshot.add_warning("DAEDALUS", str(e))
            logger.warning(f"DAEDALUS thread error: {e}")
            return ctx_snapshot

    def _merge_echo_fields(self, ctx: Context, ctx_echo: Context) -> None:
        """Copia campos de audio de ctx_echo al ctx principal."""
        for field in ("audio_path", "short_audio_path", "tts_engine", "srt_path"):
            val = getattr(ctx_echo, field, "")
            if val:
                setattr(ctx, field, val)

    def _merge_daedalus_fields(self, ctx: Context, ctx_daedalus: Context) -> None:
        """Copia campos de gráficos de ctx_daedalus al ctx principal."""
        for field in (
            "chart_path", "fear_greed_chart_path", "dominance_chart_path",
            "volume_chart_path", "heatmap_chart_path", "halving_chart_path",
            "correlation_chart_path", "dominance_area_chart_path",
            "chart_90d_path", "chart_animated_path",
            "fear_greed_value", "fear_greed_label",
            "support_levels", "resistance_levels",
            "btc_dominance",
        ):
            val = getattr(ctx_daedalus, field, None)
            if val is not None and val != getattr(ctx, field, None):
                setattr(ctx, field, val)

    def _merge_errors_warnings(self, ctx: Context, *others: Context) -> None:
        for other in others:
            for e in other.errors:
                if e not in ctx.errors:
                    ctx.errors.append(e)
            for w in other.warnings:
                if w not in ctx.warnings:
                    ctx.warnings.append(w)

    # ── Pipeline principal ────────────────────────────────────────────────────

    def run(self, ctx: Context) -> Context:
        logger.info("FORGE_AGENT iniciado")
        t_start = time.time()

        # ── 1. Precios garantizados ────────────────────────────────────────
        ctx = self._ensure_prices(ctx)

        # ── 2. CALÍOPE — guionista ────────────────────────────────────────
        if self._caliope:
            try:
                ctx = self._caliope.run(ctx)
                logger.info("CALIOPE completado")
            except Exception as e:
                ctx.add_error("CALIOPE", str(e))
                logger.error(f"CALIOPE error: {e}")
                return ctx  # sin guión no tiene sentido continuar

        # ── 3. HERMES v2 — SEO ────────────────────────────────────────────
        if self._hermes:
            try:
                ctx = self._hermes.run(ctx)
                logger.info("HERMES completado")
            except Exception as e:
                ctx.add_warning("HERMES", str(e))

        # ── 4. ARES — Retention Engine ────────────────────────────────────
        if self._ares:
            try:
                ctx = self._ares.run(ctx)
                logger.info(f"ARES completado — retention_score={ctx.retention_score}/100")
            except Exception as e:
                ctx.add_warning("ARES", str(e))
                logger.warning(f"ARES error (no crítico): {e}")

        # ── 5. ECHO ∥ DAEDALUS (paralelo) ────────────────────────────────
        if self._echo or self._daedalus:
            t_parallel = time.time()
            echo_future = daedalus_future = None
            _echo_timed_out = False

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                if self._echo:
                    ctx_echo_in = copy.deepcopy(ctx)
                    echo_future = executor.submit(self._run_echo_thread, ctx_echo_in)
                if self._daedalus:
                    ctx_dae_in = copy.deepcopy(ctx)
                    daedalus_future = executor.submit(self._run_daedalus_thread, ctx_dae_in)

                if echo_future:
                    try:
                        ctx_echo_out = echo_future.result(timeout=600)
                        self._merge_echo_fields(ctx, ctx_echo_out)
                        self._merge_errors_warnings(ctx, ctx_echo_out)
                        logger.info("ECHO completado (paralelo)")
                    except concurrent.futures.TimeoutError:
                        ctx.add_warning("ECHO", "Timeout en paralelo — audio puede faltar")
                        logger.warning("ECHO timeout en thread paralelo")
                        _echo_timed_out = True

                if daedalus_future:
                    try:
                        ctx_dae_out = daedalus_future.result(timeout=300)
                        self._merge_daedalus_fields(ctx, ctx_dae_out)
                        self._merge_errors_warnings(ctx, ctx_dae_out)
                        logger.info("DAEDALUS completado (paralelo)")
                    except concurrent.futures.TimeoutError:
                        ctx.add_warning("DAEDALUS", "Timeout en paralelo — gráficos pueden faltar")
                        logger.warning("DAEDALUS timeout en thread paralelo")

            elapsed_parallel = time.time() - t_parallel
            logger.info(f"ECHO ∥ DAEDALUS completados en {elapsed_parallel:.1f}s")

            # Recuperar audio si ECHO tuvo timeout pero terminó mientras esperábamos.
            # ThreadPoolExecutor.shutdown(wait=True) garantiza que ECHO completó
            # antes de salir del bloque with — sólo falta mergear su resultado.
            if _echo_timed_out and echo_future is not None and not getattr(ctx, "audio_path", ""):
                try:
                    ctx_echo_late = echo_future.result(timeout=5)
                    if getattr(ctx_echo_late, "audio_path", ""):
                        self._merge_echo_fields(ctx, ctx_echo_late)
                        self._merge_errors_warnings(ctx, ctx_echo_late)
                        logger.info("ECHO recuperado tras timeout — audio_path actualizado")
                    else:
                        logger.warning("ECHO recuperado pero sin audio_path — HEPHAESTUS usará silencio")
                except Exception as _er:
                    logger.warning(f"ECHO recuperación post-timeout falló: {_er}")

        # ── 6. HEPHAESTUS — composición de vídeo ─────────────────────────
        if self._hephaestus:
            try:
                ctx = self._hephaestus.run(ctx)
                logger.info("HEPHAESTUS completado")
            except Exception as e:
                ctx.add_warning("HEPHAESTUS", str(e))
                logger.error(f"HEPHAESTUS error: {e}")

        # ── 7. IRIS — thumbnails A/B ──────────────────────────────────────
        if self._iris:
            try:
                ctx = self._iris.run(ctx)
                logger.info("IRIS completado")
            except Exception as e:
                ctx.add_warning("IRIS", str(e))

        elapsed = time.time() - t_start
        logger.info(f"FORGE completado en {elapsed:.1f}s")
        return ctx
