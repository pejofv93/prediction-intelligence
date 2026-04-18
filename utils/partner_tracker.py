"""
utils/partner_tracker.py
Partner Program Tracker — seguimiento hacia monetización YouTube.

Objetivos del Partner Program (YPP):
  - 1.000 suscriptores
  - 4.000 horas de watch time en los últimos 12 meses

Calcula:
  - Progress actual desde YouTube Analytics (o estimado desde DB)
  - Velocidad actual (horas/semana)
  - Fecha estimada de llegada al objetivo
  - Recomendaciones concretas para acelerar
"""

import os
from datetime import datetime, timedelta
from typing import Optional, Tuple
from utils.logger import get_logger

logger = get_logger("PARTNER_TRACKER")

# Objetivos YPP
TARGET_SUBS = 1_000
TARGET_WATCHTIME_HOURS = 4_000

# Duración mínima para watch time (vídeos <4min no cuentan prácticamente)
MIN_VIDEO_MINUTES = 4.0

# Retención promedio esperada por modo (% del vídeo visto)
_RETENTION_BY_MODE = {
    "urgente":   0.45,  # 45% — noticias tienen drop rápido
    "standard":  0.50,
    "noticia":   0.48,
    "analisis":  0.55,  # análisis retiene mejor (el viewer quiere la conclusión)
    "educativo": 0.60,  # educativo mejor retención (aprendizaje progresivo)
    "opinion":   0.52,
    "semanal":   0.58,
    "evergreen": 0.65,  # evergreen = mejor retención a largo plazo
    "prediccion": 0.53,
}


class PartnerTracker:
    """
    Calcula el progreso hacia el YouTube Partner Program.
    Funciona con o sin YouTube Analytics (fallback a estimación local).
    """

    def __init__(self, db=None):
        self.db = db

    def get_progress(self) -> dict:
        """
        Devuelve el estado actual hacia el YPP.
        Prioriza datos reales de YouTube Analytics.
        """
        # Intentar datos reales de YouTube Analytics
        yt_data = self._fetch_youtube_analytics()

        if yt_data:
            watchtime_hours = yt_data.get("watchtime_hours", 0)
            subs = yt_data.get("subs", 0)
            source = "YouTube Analytics"
        else:
            watchtime_hours, subs = self._estimate_from_db()
            source = "estimación DB"

        # Velocidad y proyección
        weekly_hours = self._estimate_weekly_velocity(watchtime_hours)
        eta_watchtime = self._estimate_eta(watchtime_hours, TARGET_WATCHTIME_HOURS, weekly_hours)
        eta_subs = self._estimate_subs_eta(subs)

        # Bottleneck: el más lejano de los dos
        eta = max(eta_watchtime, eta_subs) if eta_watchtime and eta_subs else (eta_watchtime or eta_subs)

        return {
            "watchtime_hours":     round(watchtime_hours, 1),
            "watchtime_pct":       round(watchtime_hours / TARGET_WATCHTIME_HOURS * 100, 1),
            "subs":                subs,
            "subs_pct":            round(subs / TARGET_SUBS * 100, 1),
            "weekly_hours":        round(weekly_hours, 2),
            "eta_date":            eta,
            "bottleneck":          "watch_time" if watchtime_hours / TARGET_WATCHTIME_HOURS < subs / TARGET_SUBS else "subs",
            "source":              source,
            "recommendations":     self._get_recommendations(watchtime_hours, subs, weekly_hours),
        }

    def _fetch_youtube_analytics(self) -> Optional[dict]:
        """Intenta leer Watch Time y Subs desde YouTube Analytics API."""
        try:
            import base64, json, tempfile
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_b64 = os.getenv("YOUTUBE_TOKEN_B64", "")
            if not token_b64:
                return None

            token_json = base64.b64decode(token_b64).decode("utf-8")
            token_data = json.loads(token_json)
            creds = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=token_data.get("client_id"),
                client_secret=token_data.get("client_secret"),
            )

            yt = build("youtube", "v3", credentials=creds)
            analytics = build("youtubeAnalytics", "v2", credentials=creds)

            # Suscriptores actuales
            ch = yt.channels().list(part="statistics", mine=True).execute()
            subs = int(ch["items"][0]["statistics"].get("subscriberCount", 0))

            # Watch time últimos 365 días
            end = datetime.utcnow().date()
            start = end - timedelta(days=365)
            report = analytics.reports().query(
                ids="channel==MINE",
                startDate=str(start),
                endDate=str(end),
                metrics="estimatedMinutesWatched",
                dimensions="",
            ).execute()

            mins = 0
            if report.get("rows"):
                mins = int(report["rows"][0][0])

            return {"watchtime_hours": mins / 60, "subs": subs}

        except Exception as e:
            logger.debug(f"YouTube Analytics no disponible: {e}")
            return None

    def _estimate_from_db(self) -> Tuple[float, int]:
        """Estima watch time y subs desde la base de datos local."""
        if not self.db:
            return 0.0, 0

        try:
            conn = self.db._get_conn()
            cur = conn.cursor()

            # Watch time desde tabla videos
            cur.execute("""
                SELECT SUM(watch_time_minutes), SUM(views)
                FROM videos
                WHERE platform = 'youtube'
                  AND created_at >= date('now', '-365 days')
            """)
            row = cur.fetchone()
            watch_minutes = float(row[0] or 0)
            total_views = int(row[1] or 0)

            # Si no hay watch_time_minutes en DB, estimar desde views + duración promedio
            if watch_minutes == 0 and total_views > 0:
                # Estimar: duración promedio 5 min × retención 50% = 2.5 min efectivos por view
                watch_minutes = total_views * 2.5

            watchtime_hours = watch_minutes / 60

            # Subs: aproximar con fórmula (1 sub por cada ~100 views en canales nuevos crypto)
            estimated_subs = max(1, total_views // 80)

            return watchtime_hours, estimated_subs

        except Exception as e:
            logger.debug(f"Estimación DB falló: {e}")
            return 0.0, 0

    def _estimate_weekly_velocity(self, current_hours: float) -> float:
        """Estima horas/semana basado en el histórico del canal."""
        if not self.db:
            return 0.0
        try:
            conn = self.db._get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT SUM(watch_time_minutes) / 60.0
                FROM videos
                WHERE platform = 'youtube'
                  AND created_at >= date('now', '-28 days')
            """)
            row = cur.fetchone()
            monthly = float(row[0] or 0)
            return monthly / 4  # horas/semana
        except Exception:
            return 0.0

    def _estimate_eta(self, current: float, target: float, weekly_rate: float) -> Optional[str]:
        """Calcula la fecha estimada de llegada al objetivo."""
        if current >= target:
            return "¡YA CUMPLIDO!"
        if weekly_rate <= 0:
            return "Sin datos suficientes"
        weeks_needed = (target - current) / weekly_rate
        eta = datetime.now() + timedelta(weeks=weeks_needed)
        return eta.strftime("%B %Y")

    def _estimate_subs_eta(self, current_subs: int) -> Optional[str]:
        """Estima cuándo llegaremos a 1000 subs."""
        if current_subs >= TARGET_SUBS:
            return "¡YA CUMPLIDO!"
        if not self.db:
            return None
        try:
            conn = self.db._get_conn()
            cur = conn.cursor()
            # Aproximar subs ganados últimas 4 semanas desde views
            cur.execute("""
                SELECT SUM(views)
                FROM videos
                WHERE platform = 'youtube'
                  AND created_at >= date('now', '-28 days')
            """)
            row = cur.fetchone()
            views_4w = int(row[0] or 0)
            subs_4w = max(1, views_4w // 80)
            weeks_needed = (TARGET_SUBS - current_subs) / max(subs_4w / 4, 0.1)
            eta = datetime.now() + timedelta(weeks=weeks_needed)
            return eta.strftime("%B %Y")
        except Exception:
            return None

    def _get_recommendations(self, watchtime_hours: float, subs: int,
                              weekly_hours: float) -> list:
        """Recomendaciones concretas y accionables según el estado actual."""
        recs = []
        watchtime_pct = watchtime_hours / TARGET_WATCHTIME_HOURS

        if watchtime_pct < 0.05:
            recs.append("🎯 Prioridad: publica vídeos de 8-12 min (analisis/educativo) — máximo watch time por vídeo")
            recs.append("📅 Publica mínimo 5 días a la semana para acumular watch time rápido")

        if watchtime_pct < 0.20:
            recs.append("🔁 Activa el modo 'educativo' una vez por semana — retención 60%+ vs 45% urgente")
            recs.append("📌 Añade playlists temáticas — YouTube encadena vídeos de la misma playlist (más watch time)")

        if weekly_hours < 1:
            recs.append("⚡ Pipeline diario: 1 vídeo/día × 5 min × 50% retención × 10 views = 0.4h/día = 2.8h/semana")

        if subs / TARGET_SUBS < watchtime_pct:
            recs.append("💬 Pide suscripción en el minuto 1 Y en el minuto final — doble CTA aumenta subs +30%")
            recs.append("📢 Comparte en Telegram y Twitter cada vídeo — tráfico externo señal positiva al algoritmo")

        if not recs:
            recs.append("✅ Buen ritmo — mantén consistencia y el algoritmo empezará a recomendar")

        return recs


def render_partner_panel(tracker_data: dict) -> str:
    """Genera el panel visual para rich.Panel."""
    wt = tracker_data["watchtime_hours"]
    wt_pct = tracker_data["watchtime_pct"]
    subs = tracker_data["subs"]
    subs_pct = tracker_data["subs_pct"]
    eta = tracker_data.get("eta_date", "?")
    bottleneck = tracker_data.get("bottleneck", "watch_time")
    source = tracker_data.get("source", "DB")

    # Barra de progreso ASCII
    def bar(pct, width=20):
        filled = int(width * min(pct, 100) / 100)
        return f"[{'█' * filled}{'░' * (width - filled)}] {pct:.0f}%"

    lines = [
        f"[bold #F7931A]⚡ PARTNER PROGRAM PROGRESS[/] [dim]({source})[/]",
        "",
        f"[bold]Watch Time:[/] {wt:,.1f}h / {TARGET_WATCHTIME_HOURS:,}h",
        f"  {bar(wt_pct)}",
        "",
        f"[bold]Suscriptores:[/] {subs:,} / {TARGET_SUBS:,}",
        f"  {bar(subs_pct)}",
        "",
        f"[bold yellow]Cuello de botella:[/] {'⏱ Watch Time' if bottleneck == 'watch_time' else '👥 Suscriptores'}",
        f"[bold]ETA estimado:[/] [green]{eta}[/]",
    ]
    return "\n".join(lines)
