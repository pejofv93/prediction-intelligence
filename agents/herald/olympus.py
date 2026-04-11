from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
olympus.py
OLYMPUS — Publicador YouTube de NEXUS.
Sube el vídeo generado a YouTube via Data API v3 con OAuth2.
Gestiona privacidad adaptativa, thumbnail A/B y notifica a MERCURY.
"""

import os
import time
import random
import uuid
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# ── Constantes ────────────────────────────────────────────────────────────────
YOUTUBE_CATEGORY_ENTERTAINMENT = "24"
CHUNK_SIZE = 1024 * 1024 * 4  # 4 MB por chunk
MAX_RETRIES = 5
RETRY_BASE = 2  # segundos base para backoff exponencial

AVISO_LEGAL = (
    "\n\n⚠️ AVISO LEGAL: Este vídeo tiene carácter exclusivamente educativo e informativo. "
    "Nada de lo aquí expuesto constituye consejo de inversión. "
    "Invierte siempre bajo tu propia responsabilidad."
)

PALABRAS_INVERSION = {
    "comprar", "vender", "invertir", "precio objetivo", "predicción",
    "bullish", "bearish", "all-in", "acumular", "entrada", "salida",
}

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class OLYMPUS(BaseAgent):
    """
    Publica el vídeo en YouTube usando Data API v3 + OAuth2.
    Requiere en .env: YOUTUBE_CLIENT_SECRET_PATH
    Opcional en .env: YOUTUBE_TOKEN_PATH
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("OLYMPUS")

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold yellow]OLYMPUS[/] iniciado")
        try:
            # 1. Validar entradas mínimas
            if not self._validate_inputs(ctx):
                return ctx

            # 2. Construir servicio OAuth2 (con fallback si no hay token)
            service = self._build_service(ctx)
            if service is None:
                return ctx

            # 3. Determinar privacyStatus según urgencia y modo
            privacy = self._resolve_privacy(ctx)

            # 4. Construir cuerpo de la petición con todos los metadatos
            body = self._build_request_body(ctx, privacy)

            # 5. Subir vídeo
            video_id, video_url = self._upload_video(service, ctx.video_path, body)

            # 6. Subir thumbnail A si existe
            self._set_thumbnail(service, video_id, getattr(ctx, "thumbnail_a_path", ""))

            # 7. Actualizar contexto con URL corta
            ctx.youtube_video_id = video_id
            ctx.youtube_url = f"https://youtu.be/{video_id}"

            self.logger.info(
                f"[green]OLYMPUS[/] publicado: [link={ctx.youtube_url}]{ctx.youtube_url}[/link]"
            )

            # 8. Persistir en SQLite
            self._persist(ctx, privacy)

            # 9. Notificar a MERCURY
            self._notify_telegram(ctx)

        except Exception as exc:
            self.logger.error(f"[red]OLYMPUS error:[/] {exc}")
            ctx.add_error("OLYMPUS", str(exc))
        return ctx

    # ── privacidad adaptativa ─────────────────────────────────────────────────
    def _resolve_privacy(self, ctx: Context) -> str:
        """
        SIEMPRE private hasta nuevo aviso.
        No publicar nada público de forma automática.
        """
        return "private"

    # ── validaciones ──────────────────────────────────────────────────────────
    def _validate_inputs(self, ctx: Context) -> bool:
        if not ctx.video_path or not Path(ctx.video_path).exists():
            msg = f"video_path no encontrado: {ctx.video_path!r}"
            self.logger.error(f"[red]OLYMPUS:[/] {msg}")
            ctx.add_error("OLYMPUS", msg)
            return False
        if not ctx.seo_title:
            msg = "ctx.seo_title está vacío"
            self.logger.error(f"[red]OLYMPUS:[/] {msg}")
            ctx.add_error("OLYMPUS", msg)
            return False
        if not ctx.seo_description:
            msg = "ctx.seo_description está vacío"
            self.logger.error(f"[red]OLYMPUS:[/] {msg}")
            ctx.add_error("OLYMPUS", msg)
            return False
        return True

    # ── búsqueda de token.json ────────────────────────────────────────────────
    def _find_token_path(self) -> Path | None:
        """
        Busca token.json en este orden:
        1. Ruta indicada en YOUTUBE_TOKEN_PATH
        2. Raíz del proyecto (tres niveles sobre este archivo)
        3. Directorio de trabajo actual
        """
        # 0. Token JSON inline en env var (Railway/Docker sin volumen persistente)
        token_json = os.getenv("YOUTUBE_TOKEN", "")
        if token_json:
            import tempfile
            try:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, encoding="utf-8"
                )
                tmp.write(token_json)
                tmp.close()
                return Path(tmp.name)
            except Exception:
                pass

        # 1. Variable de entorno explícita
        env_path = os.getenv("YOUTUBE_TOKEN_PATH", "")
        if env_path:
            p = Path(env_path)
            if p.exists():
                return p

        # 2. Raíz del proyecto
        project_root = Path(__file__).resolve().parent.parent.parent
        candidate = project_root / "token.json"
        if candidate.exists():
            return candidate

        # 3. Directorio actual
        candidate = Path.cwd() / "token.json"
        if candidate.exists():
            return candidate

        return None

    # ── OAuth2 + service ──────────────────────────────────────────────────────
    def _build_service(self, ctx: Context):
        """
        Construye el servicio de YouTube con credenciales OAuth2.

        En Railway/producción NUNCA abre navegador — usa refresh_token directamente.
        Orden de búsqueda del token:
          1. YOUTUBE_TOKEN_B64 (env var, base64)
          2. YOUTUBE_TOKEN    (env var, JSON string)
          3. token.json en disco (desarrollo local)
        Si no hay credenciales válidas → warning (no error) y retorna None.
        """
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
        except ImportError as exc:
            msg = "Instala: google-api-python-client google-auth-oauthlib"
            self.logger.error(f"[red]OLYMPUS:[/] {msg}")
            ctx.add_error("OLYMPUS", msg)
            return None

        # ── Obtener token data ────────────────────────────────────────────────
        import json as _json
        import base64 as _b64
        token_data: dict = {}

        # 1. YOUTUBE_TOKEN_B64 — base64 del JSON (más seguro en env vars)
        token_b64 = os.getenv("YOUTUBE_TOKEN_B64", "")
        if token_b64:
            try:
                token_data = _json.loads(_b64.b64decode(token_b64).decode("utf-8"))
                self.logger.info("[yellow]OLYMPUS[/] token desde YOUTUBE_TOKEN_B64")
            except Exception as exc:
                self.logger.warning(f"[yellow]OLYMPUS[/] error decodificando YOUTUBE_TOKEN_B64: {exc}")

        # 2. YOUTUBE_TOKEN — JSON string directo
        if not token_data:
            token_json = os.getenv("YOUTUBE_TOKEN", "")
            if token_json:
                try:
                    token_data = _json.loads(token_json)
                    self.logger.info("[yellow]OLYMPUS[/] token desde YOUTUBE_TOKEN")
                except Exception as exc:
                    self.logger.warning(f"[yellow]OLYMPUS[/] error parseando YOUTUBE_TOKEN: {exc}")

        # 3. token.json en disco (desarrollo local)
        if not token_data:
            token_path = self._find_token_path()
            if token_path:
                try:
                    token_data = _json.loads(token_path.read_text(encoding="utf-8"))
                    self.logger.info(f"[yellow]OLYMPUS[/] token desde disco: {token_path}")
                except Exception as exc:
                    self.logger.warning(f"[yellow]OLYMPUS[/] error leyendo token.json: {exc}")

        if not token_data:
            msg = (
                "YouTube: no hay token disponible. "
                "Configura YOUTUBE_TOKEN o YOUTUBE_TOKEN_B64 en Railway."
            )
            self.logger.warning(f"[yellow]OLYMPUS[/] {msg}")
            ctx.add_warning("OLYMPUS", msg)
            return None

        # ── Construir Credentials directamente desde el token data ────────────
        # NUNCA se llama a flow.run_local_server() — incompatible con Railway.
        try:
            creds = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=token_data.get("client_id"),
                client_secret=token_data.get("client_secret"),
                scopes=token_data.get("scopes", YOUTUBE_SCOPES),
            )
        except Exception as exc:
            msg = f"Error construyendo Credentials: {exc}"
            self.logger.error(f"[red]OLYMPUS:[/] {msg}")
            ctx.add_warning("OLYMPUS", msg)
            return None

        # ── Refrescar si ha expirado ──────────────────────────────────────────
        if not creds.valid:
            if creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self.logger.info("[yellow]OLYMPUS[/] token refrescado correctamente")
                except Exception as exc:
                    msg = f"No se pudo refrescar el token OAuth2: {exc}"
                    self.logger.warning(f"[yellow]OLYMPUS[/] {msg}")
                    ctx.add_warning("OLYMPUS", msg)
                    return None
            else:
                msg = "Token expirado y sin refresh_token — re-autoriza desde local."
                self.logger.warning(f"[yellow]OLYMPUS[/] {msg}")
                ctx.add_warning("OLYMPUS", msg)
                return None

            # Guardar token actualizado en la ruta encontrada (o raíz del proyecto)
            save_path = token_path or (
                Path(__file__).resolve().parent.parent.parent / "token.json"
            )
            try:
                save_path.write_text(creds.to_json(), encoding="utf-8")
                self.logger.info(f"[yellow]OLYMPUS[/] token.json guardado en: {save_path}")
            except Exception as exc:
                self.logger.warning(f"[yellow]OLYMPUS[/] no se pudo guardar token: {exc}")

        return build("youtube", "v3", credentials=creds)

    # ── cuerpo de la petición ────────────────────────────────────────────────
    def _build_request_body(self, ctx: Context, privacy: str) -> dict:
        description = ctx.seo_description

        # Aviso legal si el contenido menciona palabras de inversión
        contenido = (getattr(ctx, "script", "") + " " + description).lower()
        if any(p in contenido for p in PALABRAS_INVERSION):
            description += AVISO_LEGAL

        tags = ctx.seo_tags[:15] if ctx.seo_tags else []

        return {
            "snippet": {
                "title": ctx.seo_title[:100],
                "description": description[:5000],
                "tags": tags,
                "categoryId": YOUTUBE_CATEGORY_ENTERTAINMENT,
                "defaultLanguage": "es",
                "defaultAudioLanguage": "es",
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

    # ── subida con retry exponencial ─────────────────────────────────────────
    def _upload_video(self, service, video_path: str, body: dict) -> tuple:
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise ImportError("Instala google-api-python-client") from exc

        media = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
        request = service.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media,
        )

        video_id = None
        attempt = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold yellow]OLYMPUS[/] subiendo a YouTube..."),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("upload", total=100)

            while video_id is None:
                try:
                    status, response = request.next_chunk()
                    if status:
                        pct = int(status.progress() * 100)
                        progress.update(task, completed=pct)
                    if response:
                        video_id = response["id"]
                        progress.update(task, completed=100)
                except Exception as exc:
                    attempt += 1
                    if attempt > MAX_RETRIES:
                        raise RuntimeError(
                            f"YouTube upload falló tras {MAX_RETRIES} intentos: {exc}"
                        ) from exc
                    wait = RETRY_BASE ** attempt + random.uniform(0, 1)
                    self.logger.warning(
                        f"[yellow]OLYMPUS[/] retry {attempt}/{MAX_RETRIES} en {wait:.1f}s — {exc}"
                    )
                    time.sleep(wait)

        video_url = f"https://youtu.be/{video_id}"
        return video_id, video_url

    # ── thumbnail ─────────────────────────────────────────────────────────────
    def _set_thumbnail(self, service, video_id: str, thumbnail_path: str) -> None:
        if not thumbnail_path or not Path(thumbnail_path).exists():
            self.logger.warning(
                f"[yellow]OLYMPUS[/] thumbnail_a no disponible: {thumbnail_path!r}, se omite"
            )
            return
        try:
            from googleapiclient.http import MediaFileUpload
            media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
            service.thumbnails().set(videoId=video_id, media_body=media).execute()
            self.logger.info("[green]OLYMPUS[/] thumbnail A subido correctamente")
        except Exception as exc:
            self.logger.warning(f"[yellow]OLYMPUS[/] no se pudo subir thumbnail: {exc}")

    # ── persistencia ──────────────────────────────────────────────────────────
    def _persist(self, ctx: Context, privacy: str) -> None:
        # Tabla videos (existente)
        try:
            self.db.save_video({
                "id": str(uuid.uuid4()),
                "pipeline_id": ctx.pipeline_id,
                "platform": "youtube",
                "video_id": ctx.youtube_video_id,
                "title": ctx.seo_title,
                "url": ctx.youtube_url,
            })
        except Exception as exc:
            self.logger.warning(f"[yellow]OLYMPUS[/] no se pudo persistir en videos: {exc}")

        # Tabla memoria_videos
        try:
            self.db.save_memoria_video(
                video_id=ctx.youtube_video_id,
                title=ctx.seo_title,
                url=ctx.youtube_url,
                seo_score=getattr(ctx, "seo_score", 0),
                privacy_status=privacy,
            )
        except Exception as exc:
            self.logger.warning(
                f"[yellow]OLYMPUS[/] no se pudo persistir en memoria_videos: {exc}"
            )

    # ── notificación a MERCURY ────────────────────────────────────────────────
    def _notify_telegram(self, ctx: Context) -> None:
        try:
            from agents.herald.mercury import MERCURY
            mercury = MERCURY(self.config, self.db)
            ctx.telegram_message = (
                f"Publicado en YouTube\n"
                f"📺 {ctx.seo_title}\n"
                f"🔗 {ctx.youtube_url}\n"
                f"📊 SEO: {getattr(ctx, 'seo_score', 'N/A')}/100"
            )
            mercury.run(ctx)
        except Exception as exc:
            self.logger.error(f"[red]OLYMPUS[/] error notificando Telegram: {exc}")
