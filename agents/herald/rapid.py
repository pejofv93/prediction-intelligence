from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
rapid.py
RAPID — Publicador TikTok de NEXUS.
Sube el vídeo a TikTok vía tiktok-uploader (Playwright headless).
"""

from pathlib import Path

from rich.console import Console

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

MAX_TIKTOK_TITLE = 150
HASHTAGS_CRYPTO = "#crypto #bitcoin #BTC #ETH #criptomonedas #CryptoVerdad"


class RAPID(BaseAgent):
    """
    Publica el vídeo en TikTok usando tiktok-uploader (Playwright headless).
    OLYMPUS debe haber corrido antes en modo standard para disponer de ctx.youtube_url.
    En modo urgente RAPID corre primero y pone enlace provisional.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("RAPID")

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold cyan]RAPID[/] iniciado")
        try:
            self._validate_inputs(ctx)
            title = self._build_title(ctx)
            description = self._build_description(ctx)
            tiktok_url = self._upload(ctx.video_path, title, description)
            ctx.tiktok_url = tiktok_url
            self.logger.info(
                f"[green]RAPID[/] vídeo publicado en TikTok: {tiktok_url}"
            )
            self._persist(ctx)
        except Exception as exc:
            self.logger.error(f"[red]RAPID error:[/] {exc}")
            ctx.add_error("RAPID", str(exc))
        return ctx

    # ── validaciones ──────────────────────────────────────────────────────────
    def _validate_inputs(self, ctx: Context) -> None:
        if not ctx.video_path or not Path(ctx.video_path).exists():
            raise FileNotFoundError(f"video_path no encontrado: {ctx.video_path!r}")
        if not ctx.seo_title:
            raise ValueError("ctx.seo_title está vacío")

    # ── construcción de textos ────────────────────────────────────────────────
    def _build_title(self, ctx: Context) -> str:
        base = ctx.seo_title
        suffix = f" {HASHTAGS_CRYPTO}"
        max_base = MAX_TIKTOK_TITLE - len(suffix)
        if len(base) > max_base:
            base = base[:max_base - 3] + "..."
        return base + suffix

    def _build_description(self, ctx: Context) -> str:
        if ctx.is_urgent or not ctx.youtube_url:
            yt_line = "📺 Próximamente en YouTube — ¡síguenos!"
        else:
            yt_line = f"📺 Vídeo completo en YouTube → {ctx.youtube_url}"
        return yt_line

    # ── subida con Playwright ─────────────────────────────────────────────────
    def _upload(self, video_path: str, title: str, description: str) -> str:
        try:
            from tiktok_uploader.upload import upload_video  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Instala tiktok-uploader: pip install tiktok-uploader"
            ) from exc

        cookies_path = self._get_cookies_path()

        try:
            console.print(
                "[bold cyan]RAPID[/] Iniciando Playwright headless para TikTok..."
            )
            results = upload_video(
                video=video_path,
                description=f"{title}\n\n{description}",
                cookies=cookies_path,
                headless=True,
            )
            # tiktok-uploader devuelve lista de dicts con 'url'
            if results and isinstance(results, list) and results[0].get("url"):
                return results[0]["url"]
            # Fallback: TikTok no siempre devuelve URL inmediata
            self.logger.warning(
                "[yellow]RAPID[/] TikTok no devolvió URL directa; usando perfil genérico"
            )
            return "https://www.tiktok.com/@CryptoVerdad"
        except Exception as exc:
            # No crash — Playwright puede fallar por sesión caducada
            self.logger.warning(
                f"[yellow]RAPID[/] Playwright falló: {exc}. Se continúa sin TikTok."
            )
            ctx_warning = f"TikTok upload falló (Playwright): {exc}"
            return ""

    def _get_cookies_path(self) -> str:
        import os

        # Si hay TIKTOK_SESSION_ID, generamos cookies en memoria (formato tiktok-uploader)
        session_id = os.getenv("TIKTOK_SESSION_ID")
        if session_id:
            cookies_path = "/tmp/tiktok_cookies.txt"
            with open(cookies_path, "w") as f:
                f.write(
                    "# Netscape HTTP Cookie File\n"
                    f".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\t{session_id}\n"
                )
            return cookies_path

        path = os.getenv("TIKTOK_COOKIES_PATH", "secrets/tiktok_cookies.txt")
        if not Path(path).exists():
            raise FileNotFoundError(
                f"TIKTOK_SESSION_ID ni TIKTOK_COOKIES_PATH encontrados: {path!r}. "
                "Configura TIKTOK_SESSION_ID en el entorno o exporta las cookies."
            )
        return path

    # ── persistencia ──────────────────────────────────────────────────────────
    def _persist(self, ctx: Context) -> None:
        import uuid
        if not ctx.tiktok_url:
            return
        try:
            self.db.save_video({
                "id": str(uuid.uuid4()),
                "pipeline_id": ctx.pipeline_id,
                "platform": "tiktok",
                "video_id": "",
                "title": ctx.seo_title,
                "url": ctx.tiktok_url,
            })
        except Exception as exc:
            self.logger.warning(f"[yellow]RAPID[/] no se pudo persistir en DB: {exc}")

