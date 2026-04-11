from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime
import uuid


@dataclass
class Context:
    # ── Input ──────────────────────────────────────────────────────────────
    topic: str = ""
    mode: str = "standard"         # standard | urgente | short | thread | analisis | tutorial | opinion
    forced_mode: str = ""          # si está definido, THEMIS no puede sobreescribir ctx.mode

    # ── ORACULO outputs ────────────────────────────────────────────────────
    prices: Dict[str, Any] = field(default_factory=dict)
    news: List[Dict] = field(default_factory=list)
    competitors: List[Dict] = field(default_factory=list)
    trends: List[str] = field(default_factory=list)
    strategy_reasoning: str = ""
    is_urgent: bool = False
    urgency_score: float = 0.0

    # ── FORGE outputs ──────────────────────────────────────────────────────
    script: str = ""
    script_mode: str = ""
    short_script: str = ""       # Guion independiente para Short (45-60s, máx 150 palabras)
    short_audio_path: str = ""   # Audio TTS del short_script
    seo_score: int = 0
    seo_title: str = ""
    seo_description: str = ""
    seo_tags: List[str] = field(default_factory=list)
    audio_path: str = ""
    srt_path: str = ""
    video_path: str = ""
    short_video_path: str = ""
    video_format: str = ""
    thumbnail_a_path: str = ""
    thumbnail_b_path: str = ""
    chart_path: str = ""
    fear_greed_chart_path: str = ""
    dominance_chart_path: str = ""
    volume_chart_path: str = ""
    heatmap_chart_path: str = ""
    halving_chart_path: str = ""
    correlation_chart_path: str = ""
    dominance_area_chart_path: str = ""
    chart_90d_path: str = ""
    fear_greed_value: int = 0
    fear_greed_label: str = ""
    btc_price: float = 0.0
    eth_price: float = 0.0
    sol_price: float = 0.0
    btc_dominance: float = 0.0
    support_levels: List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    legal_warning_added: bool = False
    sadtalker_used: bool = False
    # PROMETHEUS: ruta al clip de avatar generado (presentador hablando)
    avatar_path: str = ""
    # PYTHIA: imagen del articulo de noticia principal
    news_image_url: str = ""   # URL de la imagen extraida del feed RSS
    news_image_path: str = ""  # ruta local tras descarga (asignada por HEPHAESTUS)
    # Metadata general: timings, flags de modulos, datos de depuracion
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── HERALD outputs ─────────────────────────────────────────────────────
    youtube_url: str = ""
    youtube_video_id: str = ""
    tiktok_url: str = ""
    telegram_message_id: int = 0

    # ── MIND outputs ───────────────────────────────────────────────────────
    learning_context: Dict[str, Any] = field(default_factory=dict)
    optimal_publish_hour: int = 18

    # ── Meta ───────────────────────────────────────────────────────────────
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pipeline_start: datetime = field(default_factory=datetime.now)
    pipeline_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    approved: bool = False
    review_notes: str = ""
    dry_run: bool = False        # Si True: FORGE genera contenido pero HERALD no publica

    # ── Helpers ────────────────────────────────────────────────────────────
    def add_error(self, agent: str, message: str) -> None:
        entry = f"[{agent}] {message}"
        self.errors.append(entry)

    def add_warning(self, agent: str, message: str) -> None:
        entry = f"[{agent}] {message}"
        self.warnings.append(entry)

    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def summary(self) -> Dict[str, Any]:
        """Return a compact dict suitable for DB storage or logging."""
        return {
            "pipeline_id": self.pipeline_id,
            "topic": self.topic,
            "mode": self.mode,
            "is_urgent": self.is_urgent,
            "urgency_score": self.urgency_score,
            "seo_score": self.seo_score,
            "seo_title": self.seo_title,
            "audio_path": self.audio_path,
            "srt_path": self.srt_path,
            "video_path": self.video_path,
            "short_video_path": self.short_video_path,
            "video_format": self.video_format,
            "youtube_url": self.youtube_url,
            "tiktok_url": self.tiktok_url,
            "telegram_message_id": self.telegram_message_id,
            "approved": self.approved,
            "errors": self.errors,
            "warnings": self.warnings,
            "pipeline_start": self.pipeline_start.isoformat(),
        }
