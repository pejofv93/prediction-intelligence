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
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# ── Constantes ────────────────────────────────────────────────────────────────
YOUTUBE_CATEGORY_SCIENCE_TECH = "28"  # Science & Technology
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

# ── Tabla de afiliados ────────────────────────────────────────────────────────
# Sustituye los códigos de referido por los reales antes de producción
_AFFILIATE_LINKS = {
    "binance":  ("Binance",               "https://www.binance.com/es/register?ref=CRYPTOVERDAD"),
    "coinbase": ("Coinbase",              "https://coinbase.com/join/CRYPTOVERDAD"),
    "ledger":   ("Ledger Hardware Wallet","https://shop.ledger.com/?r=CRYPTOVERDAD"),
    "kraken":   ("Kraken",               "https://www.kraken.com/sign-up?referral=CRYPTOVERDAD"),
    "trezor":   ("Trezor",               "https://trezor.io/?offer_id=CRYPTOVERDAD"),
}


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

            # 4. Resolver playlist automáticamente
            playlist_id = self._get_or_create_playlist(service, ctx)

            # 5. Construir cuerpo de la petición con todos los metadatos
            body = self._build_request_body(ctx, privacy)

            # 6. Subir vídeo
            video_id, video_url = self._upload_video(service, ctx.video_path, body)

            # 7. Añadir a playlist
            if playlist_id:
                self._add_to_playlist(service, video_id, playlist_id)

            # 8. Subir thumbnail A (fallback a B si A no existe)
            self._set_thumbnail(
                service, video_id,
                getattr(ctx, "thumbnail_a_path", ""),
                fallback_path=getattr(ctx, "thumbnail_b_path", ""),
            )

            # 8b. Subir captions SRT si existen
            self._upload_captions(service, video_id, ctx)

            # 8c. Fijar comentario inicial
            self._pin_first_comment(service, video_id, ctx)

            # 9. Actualizar contexto con URL corta
            ctx.youtube_video_id = video_id
            ctx.youtube_url = f"https://youtu.be/{video_id}"

            self.logger.info(
                f"[green]OLYMPUS[/] publicado: [link={ctx.youtube_url}]{ctx.youtube_url}[/link]"
            )

            # 10. Persistir en SQLite (videos + youtube_url en pipelines)
            self._persist(ctx, privacy)
            try:
                self.db.update_pipeline_youtube_url(ctx.pipeline_id, ctx.youtube_url)
            except Exception as exc:
                self.logger.warning(f"[yellow]OLYMPUS[/] no se pudo actualizar youtube_url en pipeline: {exc}")

            # 11. Subir Short a YouTube Shorts (si existe)
            short_path = getattr(ctx, "short_video_path", "") or ""
            if short_path and Path(short_path).exists():
                try:
                    short_title = f"{ctx.seo_title[:47]} #Shorts"
                    short_desc = (
                        f"#Shorts #Bitcoin #Crypto #CryptoVerdad\n\n"
                        f"Versión corta del análisis completo: {ctx.youtube_url}\n\n"
                        f"{ctx.seo_description[:500]}"
                    )
                    short_body = {
                        "snippet": {
                            "title": short_title[:100],
                            "description": short_desc,
                            "tags": ctx.seo_tags[:10] + ["Shorts", "CryptoVerdad"],
                            "categoryId": "25",  # News & Politics
                            "defaultLanguage": "es",
                        },
                        "status": {
                            "privacyStatus": privacy,
                            "selfDeclaredMadeForKids": False,
                        },
                    }
                    short_id, short_url = self._upload_video(service, short_path, short_body)
                    ctx.metadata["youtube_short_url"] = short_url
                    self.logger.info(f"[green]OLYMPUS Short[/] publicado: {short_url}")
                except Exception as exc:
                    self.logger.warning(f"[yellow]OLYMPUS[/] Short upload falló (no crítico): {exc}")

        except Exception as exc:
            self.logger.error(f"[red]OLYMPUS error:[/] {exc}")
            ctx.add_error("OLYMPUS", str(exc))
        return ctx

    # ── playlists automáticas ─────────────────────────────────────────────────

    # Mapa de palabras clave → nombre de playlist
    _PLAYLIST_MAP = [
        (["bitcoin", "btc", "halving"],                     "Análisis Bitcoin"),
        (["ethereum", "eth", "solana", "altcoin", "altcoins", "sol"], "Altcoins"),
        (["urgente", "breaking", "última hora", "alerta"],  "Urgente — Última hora"),
        (["educación", "educativo", "tutorial", "explico", "qué es", "cómo"], "Educación Crypto"),
        (["noticia", "noticias", "sec", "regulación", "blackrock", "etf"], "Noticias Crypto"),
    ]
    _PLAYLIST_DEFAULT = "Análisis Crypto"

    def _detect_playlist_name(self, ctx: Context) -> str:
        """Detecta la playlist adecuada según tema, modo y título."""
        text = " ".join([
            ctx.topic or "",
            getattr(ctx, "seo_title", "") or "",
            getattr(ctx, "mode", "") or "",
        ]).lower()
        for keywords, playlist_name in self._PLAYLIST_MAP:
            if any(kw in text for kw in keywords):
                return playlist_name
        return self._PLAYLIST_DEFAULT

    def _get_or_create_playlist(self, service, ctx: Context) -> str:
        """Devuelve el playlist_id de la playlist adecuada, creándola si no existe."""
        try:
            playlist_name = self._detect_playlist_name(ctx)
            self.logger.info(f"[yellow]OLYMPUS[/] playlist detectada: '{playlist_name}'")

            # Buscar si ya existe
            response = service.playlists().list(
                part="id,snippet",
                mine=True,
                maxResults=50,
            ).execute()

            for item in response.get("items", []):
                if item["snippet"]["title"].strip().lower() == playlist_name.lower():
                    playlist_id = item["id"]
                    self.logger.info(f"[yellow]OLYMPUS[/] playlist existente: {playlist_id}")
                    return playlist_id

            # No existe → crear
            new_playlist = service.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": playlist_name,
                        "description": f"Vídeos de CryptoVerdad — {playlist_name}",
                        "defaultLanguage": "es",
                    },
                    "status": {"privacyStatus": "public"},
                },
            ).execute()
            playlist_id = new_playlist["id"]
            self.logger.info(f"[green]OLYMPUS[/] playlist creada: '{playlist_name}' ({playlist_id})")
            return playlist_id

        except Exception as exc:
            self.logger.warning(f"[yellow]OLYMPUS[/] playlist no disponible (no crítico): {exc}")
            return ""

    def _add_to_playlist(self, service, video_id: str, playlist_id: str) -> None:
        """Añade el vídeo a la playlist indicada."""
        try:
            service.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id,
                        },
                    }
                },
            ).execute()
            self.logger.info(f"[green]OLYMPUS[/] vídeo añadido a playlist {playlist_id}")
        except Exception as exc:
            self.logger.warning(f"[yellow]OLYMPUS[/] no se pudo añadir a playlist: {exc}")

    # ── privacidad adaptativa ─────────────────────────────────────────────────
    def _resolve_privacy(self, ctx: Context) -> str:
        """
        Publica siempre como público.
        """
        return "public"

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
        token_path = None  # FIX-01: inicializar antes de cualquier bloque condicional
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
        token_b64 = os.getenv("YOUTUBE_TOKEN_B64", "").strip()
        if token_b64:
            try:
                token_b64 += "=" * (-len(token_b64) % 4)  # padding por si Railway lo eliminó
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

        # Enriquecer descripción con links de afiliado relevantes
        description = self._enrich_description_with_affiliates(
            description, getattr(ctx, "script", "")
        )

        tags = ctx.seo_tags[:15] if ctx.seo_tags else []

        # FIX-06: Publicación programada a las 15:00 UTC aprox. (ahora + 5h)
        # YouTube requiere privacyStatus="private" cuando se usa publishAt
        if privacy == "public":
            publish_at = datetime.utcnow() + timedelta(hours=5)
            publish_at_str = publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            status_block = {
                "privacyStatus": "private",
                "publishAt": publish_at_str,
                "selfDeclaredMadeForKids": False,
            }
        else:
            status_block = {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            }

        return {
            "snippet": {
                "title": ctx.seo_title[:100],
                "description": description[:5000],
                "tags": tags,
                "categoryId": YOUTUBE_CATEGORY_SCIENCE_TECH,
                "defaultLanguage": "es",
                "defaultAudioLanguage": "es",
            },
            "status": status_block,
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
    def _set_thumbnail(self, service, video_id: str, thumbnail_path: str,
                       fallback_path: str = "") -> None:
        """
        Sube thumbnail a YouTube. Intenta thumbnail_path (A) primero, fallback_path (B) despues.

        Requisitos:
          - Canal verificado con telefono en YouTube Studio (obligatorio para thumbnails custom)
          - Token OAuth2 con scope youtube.upload (suficiente para thumbnails.set)
          - Archivo JPEG/PNG de max 2MB y min 640x360px
        """
        from googleapiclient.http import MediaFileUpload

        # Determinar que archivo usar (A primero, B como fallback)
        thumb_to_use = None
        for candidate, label in [(thumbnail_path, "A"), (fallback_path, "B")]:
            if candidate and Path(candidate).exists():
                size_kb = Path(candidate).stat().st_size // 1024
                self.logger.info(
                    f"[yellow]OLYMPUS[/] thumbnail {label}: {candidate!r} ({size_kb} KB)"
                )
                thumb_to_use = (candidate, label)
                break

        if thumb_to_use is None:
            self.logger.warning(
                f"[yellow]OLYMPUS[/] no hay thumbnail disponible "
                f"(A={thumbnail_path!r} B={fallback_path!r}) — omitido. "
                "Verifica que IRIS genero el archivo en output/thumbnails/."
            )
            return

        path, label = thumb_to_use
        try:
            mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
            media = MediaFileUpload(path, mimetype=mime)
            service.thumbnails().set(videoId=video_id, media_body=media).execute()
            self.logger.info(f"[green]OLYMPUS[/] thumbnail {label} subido correctamente")
        except Exception as exc:
            import traceback as _tb
            self.logger.warning(
                f"[yellow]OLYMPUS[/] error subiendo thumbnail {label}: {exc} | "
                f"Si es 403/forbidden: verifica canal en YouTube Studio -> Personalizacion "
                f"-> Verificacion con telefono (requerido para miniaturas custom) | "
                f"Detalle: {_tb.format_exc()[-400:]}"
            )

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

    # ── afiliados en descripción ──────────────────────────────────────────────
    def _enrich_description_with_affiliates(self, description: str, script: str) -> str:
        """
        Añade sección de recursos/afiliados al final de la descripción
        basándose en menciones en el script. Máximo 3 links para no saturar.
        """
        script_lower = (script or "").lower()
        relevant_links: list[str] = []

        for keyword, (name, url) in _AFFILIATE_LINKS.items():
            if keyword in script_lower:
                relevant_links.append(f"► {name}: {url}")
            if len(relevant_links) >= 3:
                break

        # Siempre incluir Binance si no hay ninguno detectado
        if not relevant_links:
            name, url = _AFFILIATE_LINKS["binance"]
            relevant_links.append(f"► {name}: {url}")

        affiliate_section = (
            "\n\n─────────────────────────────\n"
            "RECURSOS MENCIONADOS\n"
            "─────────────────────────────\n"
            + "\n".join(relevant_links)
            + "\n\n⚠️ Links de afiliado: si te registras, CryptoVerdad recibe una pequeña "
            "comisión sin coste adicional para ti."
        )
        return description + affiliate_section

    # ── captions SRT ─────────────────────────────────────────────────────────
    def _upload_captions(self, service, video_id: str, ctx: Context) -> None:
        """FIX YT-04: Sube el archivo SRT de subtítulos si existe en ctx.srt_path."""
        srt_path = getattr(ctx, "srt_path", "") or ""
        if not srt_path or not Path(srt_path).exists():
            return
        try:
            from googleapiclient.http import MediaFileUpload
            media = MediaFileUpload(srt_path, mimetype="text/plain")
            service.captions().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "language": "es",
                        "name": "Español",
                        "isDraft": False,
                    }
                },
                media_body=media,
            ).execute()
            self.logger.info("[green]OLYMPUS[/] captions SRT subidos")
        except Exception as exc:
            self.logger.warning(f"[yellow]OLYMPUS[/] captions upload falló (no crítico): {exc}")

    # ── comentario fijado ─────────────────────────────────────────────────────
    def _pin_first_comment(self, service, video_id: str, ctx: Context) -> None:
        """FIX YT-08: Publica y fija un comentario de debate en el vídeo."""
        try:
            comment_text = (
                "📌 Debate del día: ¿Alcista o bajista esta semana?\n"
                "¡Responde abajo! 👇\n\n"
                "📱 Alertas en tiempo real → t.me/CryptoVerdad"
            )
            service.commentThreads().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "snippet": {
                                "textOriginal": comment_text,
                            }
                        },
                    }
                },
            ).execute()
            self.logger.info("[green]OLYMPUS[/] comentario fijado publicado")
        except Exception as exc:
            self.logger.warning(f"[yellow]OLYMPUS[/] comentario fijado falló (no crítico): {exc}")
