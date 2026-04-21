"""
shared/api_quota_manager.py
Gestor centralizado de cuotas de APIs externas.

Persiste en Firestore (colección api_quotas) → sobrevive reinicios de Cloud Run.
Alerta a Telegram cuando cualquier API supera el 80% de su presupuesto diario.

APIs gestionadas:
  the_odds_api    — 500 req/mes (~17/día free tier). Header x-requests-remaining.
  api_sports      — 100 req/día (compartido fútbol + otros deportes, FOOTBALL_RAPID_API_KEY)
  football_data   — sin límite duro, rate limit 10 req/min (FOOTBALL_API_KEY)
  oddspapi        — desconocido; presupuesto conservador 50/día

Uso:
    from shared.api_quota_manager import quota

    if quota.can_call("the_odds_api"):
        resp = await client.get(url, ...)
        remaining = resp.headers.get("x-requests-remaining")
        quota.track_call("the_odds_api", remaining=remaining)
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Presupuestos diarios por API (llamadas/día)
_DAILY_LIMITS: dict[str, int] = {
    "the_odds_api":  17,   # 500/mes ÷ 30 días
    "api_sports":   100,   # plan gratuito explícito
    "football_data": 500,  # sin límite real, tope conservador
    "oddspapi":       50,  # desconocido — presupuesto conservador
}

# Umbral para alerta Telegram (fracción del límite diario)
_ALERT_THRESHOLD = 0.80


class QuotaManager:
    """
    Persiste contadores diarios en Firestore.
    Thread-safe para asyncio: todas las operaciones son sync (Firestore Admin SDK es sync).
    """

    def __init__(self) -> None:
        self._db: Optional[object] = None          # lazy Firestore client
        self._telegram_url: Optional[str] = None
        self._alerted: set[str] = set()            # evita spam de alertas en la misma sesión

    # ── Firestore lazy init ───────────────────────────────────────────────────

    def _col(self):
        """Devuelve la colección Firestore api_quotas (inicializa cliente si hace falta)."""
        if self._db is None:
            from shared.firestore_client import col
            self._col_fn = col
        return self._col_fn("api_quotas")

    def _get_doc(self, api_name: str, today: str) -> dict:
        """Lee el doc de Firestore para api_name en la fecha today."""
        try:
            doc_ref = self._col().document(f"{api_name}_{today}")
            snap = doc_ref.get()
            if snap.exists:
                return snap.to_dict()
        except Exception:
            logger.warning("QuotaManager: error leyendo Firestore para %s", api_name, exc_info=True)
        return {"api": api_name, "date": today, "used": 0, "remaining_reported": None}

    def _set_doc(self, api_name: str, today: str, data: dict) -> None:
        """Escribe/actualiza el doc en Firestore."""
        try:
            doc_ref = self._col().document(f"{api_name}_{today}")
            doc_ref.set(data, merge=True)
        except Exception:
            logger.warning("QuotaManager: error escribiendo Firestore para %s", api_name, exc_info=True)

    # ── API pública ───────────────────────────────────────────────────────────

    def can_call(self, api_name: str) -> bool:
        """
        Devuelve True si la API tiene cuota disponible para hoy.
        Si no hay límite definido, siempre devuelve True.
        """
        limit = _DAILY_LIMITS.get(api_name)
        if limit is None:
            return True
        today = _today()
        doc = self._get_doc(api_name, today)
        used = doc.get("used", 0)
        if used >= limit:
            logger.warning("QuotaManager: %s agotada (%d/%d para %s)", api_name, used, limit, today)
            return False
        return True

    def track_call(self, api_name: str, remaining: Optional[str | int] = None) -> None:
        """
        Registra una llamada realizada.
        Si la API devuelve el nº de requests restantes, úsalo para calibrar el contador.
        Alerta a Telegram si se supera el 80%.
        """
        today = _today()
        doc = self._get_doc(api_name, today)
        doc["used"] = doc.get("used", 0) + 1
        doc["date"] = today
        doc["api"] = api_name
        doc["last_call"] = datetime.now(timezone.utc).isoformat()

        # Calibrar con el header x-requests-remaining si está disponible
        if remaining is not None:
            try:
                doc["remaining_reported"] = int(remaining)
            except (TypeError, ValueError):
                pass

        self._set_doc(api_name, today, doc)
        logger.debug("QuotaManager: %s → %d llamadas hoy", api_name, doc["used"])

        # Alerta Telegram al 80%
        limit = _DAILY_LIMITS.get(api_name)
        if limit and doc["used"] >= int(limit * _ALERT_THRESHOLD):
            alert_key = f"{api_name}_{today}"
            if alert_key not in self._alerted:
                self._alerted.add(alert_key)
                pct = int(doc["used"] / limit * 100)
                asyncio.create_task(
                    self._send_telegram_alert(api_name, doc["used"], limit, pct)
                )

    def get_quota_status(self) -> dict[str, dict]:
        """
        Devuelve el estado de todas las APIs para hoy.
        Formato: {api_name: {used, limit, remaining, pct_used, exhausted}}
        """
        today = _today()
        status: dict[str, dict] = {}
        for api_name, limit in _DAILY_LIMITS.items():
            doc = self._get_doc(api_name, today)
            used = doc.get("used", 0)
            remaining_rep = doc.get("remaining_reported")
            # Si tenemos el dato reportado por la API, usarlo; sino calcular
            if remaining_rep is not None:
                remaining = remaining_rep
            else:
                remaining = max(0, limit - used)
            status[api_name] = {
                "used": used,
                "limit": limit,
                "remaining": remaining,
                "pct_used": round(used / limit * 100, 1) if limit else 0,
                "exhausted": used >= limit,
                "last_call": doc.get("last_call"),
            }
        return status

    def daily_budget(self) -> dict[str, int]:
        """Devuelve el presupuesto diario configurado por API."""
        return dict(_DAILY_LIMITS)

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def _send_telegram_alert(self, api_name: str, used: int, limit: int, pct: int) -> None:
        """Envía alerta al bot de Telegram (usa TELEGRAM_BOT_URL del env)."""
        from shared.config import TELEGRAM_BOT_URL
        if not TELEGRAM_BOT_URL:
            logger.warning("QuotaManager: TELEGRAM_BOT_URL no configurada, alerta descartada")
            return

        msg = (
            f"⚠️ *Quota al {pct}%*\n"
            f"API: `{api_name}`\n"
            f"Usadas: {used}/{limit} llamadas hoy\n"
            f"Fecha: {_today()}"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    TELEGRAM_BOT_URL + "/send",
                    json={"text": msg, "parse_mode": "Markdown"},
                )
            logger.info("QuotaManager: alerta Telegram enviada para %s (%d%%)", api_name, pct)
        except Exception:
            logger.warning("QuotaManager: error enviando alerta Telegram para %s", api_name, exc_info=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Singleton global — importar este objeto en los módulos que llaman APIs
quota = QuotaManager()
