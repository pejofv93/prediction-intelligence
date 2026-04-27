"""
shared/api_quota_manager.py
Gestor centralizado de cuotas de APIs externas — diarias Y mensuales.

Persiste en Firestore (colección api_quotas) → sobrevive reinicios de Cloud Run.

Inventario de APIs de odds (límites verificados 2026-04):
┌─────────────────┬──────────────┬──────────────────────────────────────────┬────────────────────┐
│ API             │ Límite free  │ Mercados disponibles                     │ Estado             │
├─────────────────┼──────────────┼──────────────────────────────────────────┼────────────────────┤
│ The Odds API    │ 500/mes      │ h2h, totals (football); h2h,spreads,     │ ✓ activa           │
│                 │              │ totals (basketball/tennis)               │                    │
│ OddsPapi v4     │ 250/mes      │ Fixtures c/bookmakerOdds (corners,       │ ⚠ agotada ~May 1  │
│                 │              │ bookings). /v4/odds requiere fixtureId   │                    │
│ AllSports API   │ 100/día      │ Match data + odds (sin verificar)        │ ? sin integrar     │
│ API-Football    │ 100/día      │ /odds requiere plan Pro → 403 en free    │ ✗ no disponible    │
│ (RapidAPI)      │              │ /fixtures y /predictions sí funcionan    │                    │
│ Betfair Exchange│ Ilimitado    │ Exchange prices fútbol, tenis, basket    │ ? requiere cuenta  │
│                 │              │ Requiere registro + AppKey gratis        │                    │
└─────────────────┴──────────────┴──────────────────────────────────────────┴────────────────────┘

Cadena de fuentes para h2h odds (prioridad):
  1. The Odds API  → h2h market, 500/mes
  2. OddsPapi v4   → /v4/odds?fixtureId=X (cuando quota activa)
  3. Poisson own   → fallback estadístico propio, señal flagged "sin validación"

Uso:
    from shared.api_quota_manager import quota

    # Check diario (APIs con límite por día)
    if quota.can_call("api_sports"):
        quota.track_call("api_sports")

    # Check mensual (APIs con límite por mes)
    if quota.can_call_monthly("the_odds_api"):
        resp = ...
        remaining = resp.headers.get("x-requests-remaining")
        quota.track_monthly("the_odds_api", remaining=remaining)

    # ¿Todas las fuentes de odds agotadas?
    if quota.all_monthly_exhausted(["the_odds_api", "oddspapi"]):
        # usar fallback Poisson
        pass
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Presupuestos diarios (APIs con límite por día) ────────────────────────────
_DAILY_LIMITS: dict[str, int] = {
    "api_sports":    100,   # RapidAPI free: 100/día compartido fútbol + basket
    "football_data": 500,   # football-data.org: sin límite duro, tope conservador
    "apifootball":    80,   # api-football-v1 free = 100/día; 20 reservados fixtures
    "allsports":     100,   # allsportsapi.com free: 100/día
}

# ── Presupuestos mensuales (APIs con límite por mes) ─────────────────────────
_MONTHLY_LIMITS: dict[str, int] = {
    "oddsapiio":    72_000, # odds-api.io free: 5000 req/h × 24h × ~15 días activos
    "the_odds_api":    500, # 500/mes — verificado. Header x-requests-remaining fiable.
    "oddspapi":        250, # 250/mes — confirmado. Agotada hasta ~May 1.
}

# ── Umbral de alerta (% del límite) ──────────────────────────────────────────
_ALERT_THRESHOLD = 0.80


class QuotaManager:
    """
    Persiste contadores en Firestore.
    Clave daily:   api_quotas/{api}_{YYYY-MM-DD}
    Clave monthly: api_quotas/{api}_monthly_{YYYY-MM}
    """

    def __init__(self) -> None:
        self._db: Optional[object] = None
        self._col_fn = None
        self._alerted: set[str] = set()

    # ── Firestore ─────────────────────────────────────────────────────────────

    def _col(self):
        if self._col_fn is None:
            from shared.firestore_client import col
            self._col_fn = col
        return self._col_fn("api_quotas")

    def _get_doc(self, key: str) -> dict:
        try:
            snap = self._col().document(key).get()
            if snap.exists:
                return snap.to_dict()
        except Exception:
            logger.warning("QuotaManager: error leyendo Firestore key=%s", key, exc_info=True)
        return {"key": key, "used": 0, "remaining_reported": None}

    def _set_doc(self, key: str, data: dict) -> None:
        try:
            self._col().document(key).set(data, merge=True)
        except Exception:
            logger.warning("QuotaManager: error escribiendo Firestore key=%s", key, exc_info=True)

    # ── Daily ─────────────────────────────────────────────────────────────────

    def can_call(self, api_name: str) -> bool:
        limit = _DAILY_LIMITS.get(api_name)
        if limit is None:
            return True
        today = _today()
        doc = self._get_doc(f"{api_name}_{today}")
        used = doc.get("used", 0)
        if used >= limit:
            logger.warning("QuotaManager: %s cuota diaria agotada (%d/%d)", api_name, used, limit)
            return False
        return True

    def track_call(self, api_name: str, remaining: Optional[str | int] = None) -> None:
        today = _today()
        key = f"{api_name}_{today}"
        doc = self._get_doc(key)
        doc["used"] = doc.get("used", 0) + 1
        doc["last_call"] = datetime.now(timezone.utc).isoformat()
        if remaining is not None:
            try:
                doc["remaining_reported"] = int(remaining)
            except (TypeError, ValueError):
                pass
        self._set_doc(key, doc)
        self._maybe_alert_daily(api_name, doc["used"])

    def _maybe_alert_daily(self, api_name: str, used: int) -> None:
        limit = _DAILY_LIMITS.get(api_name)
        if not limit:
            return
        if used >= int(limit * _ALERT_THRESHOLD):
            key = f"{api_name}_{_today()}_daily"
            if key not in self._alerted:
                self._alerted.add(key)
                pct = int(used / limit * 100)
                try:
                    asyncio.create_task(self._send_alert(api_name, used, limit, pct, "día"))
                except RuntimeError:
                    pass

    # ── Monthly ───────────────────────────────────────────────────────────────

    def can_call_monthly(self, api_name: str) -> bool:
        """True si la API tiene cuota mensual disponible."""
        limit = _MONTHLY_LIMITS.get(api_name)
        if limit is None:
            return True
        month = _this_month()
        doc = self._get_doc(f"{api_name}_monthly_{month}")
        # Si la API reporta remaining via header, es la fuente de verdad
        remaining_rep = doc.get("remaining_reported")
        if remaining_rep is not None:
            if remaining_rep <= 0:
                logger.warning("QuotaManager: %s cuota mensual agotada (header: 0 restantes)", api_name)
                return False
            return True
        used = doc.get("used", 0)
        if used >= limit:
            logger.warning("QuotaManager: %s cuota mensual agotada (%d/%d)", api_name, used, limit)
            return False
        return True

    def track_monthly(self, api_name: str, remaining: Optional[str | int] = None) -> None:
        """Registra una llamada mensual. Llamar junto a track_call() para APIs mensuales."""
        month = _this_month()
        key = f"{api_name}_monthly_{month}"
        doc = self._get_doc(key)
        doc["used"] = doc.get("used", 0) + 1
        doc["last_call"] = datetime.now(timezone.utc).isoformat()
        doc["month"] = month
        doc["api"] = api_name
        if remaining is not None:
            try:
                doc["remaining_reported"] = int(remaining)
            except (TypeError, ValueError):
                pass
        self._set_doc(key, doc)
        self._maybe_alert_monthly(api_name, doc)

    def _maybe_alert_monthly(self, api_name: str, doc: dict) -> None:
        limit = _MONTHLY_LIMITS.get(api_name)
        if not limit:
            return
        used = doc.get("used", 0)
        remaining_rep = doc.get("remaining_reported")
        # Calcular porcentaje consumido
        if remaining_rep is not None:
            used_effective = limit - remaining_rep
        else:
            used_effective = used
        pct = int(used_effective / limit * 100) if limit else 0
        if pct >= int(_ALERT_THRESHOLD * 100):
            key = f"{api_name}_{_this_month()}_monthly"
            if key not in self._alerted:
                self._alerted.add(key)
                try:
                    asyncio.create_task(
                        self._send_alert(api_name, used_effective, limit, pct, "mes")
                    )
                except RuntimeError:
                    pass

    def all_monthly_exhausted(self, api_list: list[str]) -> bool:
        """True si TODAS las APIs de la lista tienen cuota mensual agotada."""
        return all(not self.can_call_monthly(api) for api in api_list)

    def next_rotation_source(self, priority: list[str]) -> str | None:
        """
        Devuelve el primer nombre de API con cuota mensual disponible.
        priority: lista de nombres en orden de preferencia.
        Devuelve None si todas están agotadas.
        """
        for api in priority:
            if self.can_call_monthly(api):
                return api
        return None

    # ── Status ────────────────────────────────────────────────────────────────

    def get_quota_status(self) -> dict[str, dict]:
        """Estado de cuotas diarias para el endpoint /api/quota."""
        today = _today()
        status: dict[str, dict] = {}
        for api_name, limit in _DAILY_LIMITS.items():
            doc = self._get_doc(f"{api_name}_{today}")
            used = doc.get("used", 0)
            remaining_rep = doc.get("remaining_reported")
            remaining = remaining_rep if remaining_rep is not None else max(0, limit - used)
            status[api_name] = {
                "used": used,
                "limit": limit,
                "remaining": remaining,
                "pct_used": round(used / limit * 100, 1) if limit else 0,
                "exhausted": used >= limit,
                "last_call": doc.get("last_call"),
                "period": "daily",
            }
        return status

    def get_monthly_status(self) -> dict[str, dict]:
        """Estado de cuotas mensuales para el endpoint /api/quota."""
        month = _this_month()
        status: dict[str, dict] = {}
        for api_name, limit in _MONTHLY_LIMITS.items():
            doc = self._get_doc(f"{api_name}_monthly_{month}")
            used = doc.get("used", 0)
            remaining_rep = doc.get("remaining_reported")
            if remaining_rep is not None:
                remaining = remaining_rep
                used_effective = limit - remaining_rep
            else:
                remaining = max(0, limit - used)
                used_effective = used
            status[api_name] = {
                "used": used_effective,
                "limit": limit,
                "remaining": remaining,
                "pct_used": round(used_effective / limit * 100, 1) if limit else 0,
                "exhausted": remaining <= 0,
                "last_call": doc.get("last_call"),
                "period": "monthly",
                "month": month,
            }
        return status

    def daily_budget(self) -> dict[str, int]:
        return {**_DAILY_LIMITS, **_MONTHLY_LIMITS}

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def _send_alert(self, api_name: str, used: int, limit: int, pct: int, period: str) -> None:
        from shared.config import TELEGRAM_BOT_URL
        if not TELEGRAM_BOT_URL:
            return
        msg = (
            f"⚠️ *Quota {api_name} al {pct}%*\n"
            f"Periodo: {period} — {used}/{limit} llamadas\n"
            f"Fecha: {_today()}"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    TELEGRAM_BOT_URL + "/send",
                    json={"text": msg, "parse_mode": "Markdown"},
                )
        except Exception:
            logger.warning("QuotaManager: error enviando alerta Telegram para %s", api_name, exc_info=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _this_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


# Singleton global
quota = QuotaManager()
