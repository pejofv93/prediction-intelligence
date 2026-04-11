from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
"""
agora.py
AGORA — Community Manager de YouTube para CryptoVerdad.
Monitorea comentarios de videos recientes, genera respuestas con Groq
y las publica via YouTube Data API v3. Aumenta engagement sin spam.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from rich.console import Console
from rich.table import Table

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()

# ── Constantes ─────────────────────────────────────────────────────────────────
MAX_REPLIES_PER_RUN = 10          # Tope de respuestas por ejecución (rate limit)
MIN_COMMENT_WORDS = 5             # Comentarios con menos palabras se ignoran
VIDEOS_LOOKBACK_DAYS = 7          # Vídeos publicados en los últimos N días
COMMENTS_LOOKBACK_HOURS = 24      # Comentarios de las últimas N horas
GROQ_MODEL = "llama-3.3-70b-versatile"

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

# Palabras que indican posible spam o bots
SPAM_PATTERNS = [
    "free bitcoin", "gana dinero", "click aquí", "http://", "https://t.me",
    "whatsapp", "telegram.me", "dm me", "envíame", "ganar dinero rápido",
    "copy trading", "señales gratis", "suscríbete a mi canal",
]

# Prompt base para Groq
GROQ_PROMPT_TEMPLATE = """\
Eres el community manager de CryptoVerdad, canal educativo de crypto en español.
Responde este comentario en 1-2 frases, tono cercano y profesional.
NO das consejos de inversión. Nunca digas cuándo comprar o vender.
Termina siempre con una pregunta abierta para enganchar al usuario.
Usa máximo 1 emoji en toda la respuesta.

Comentario: {comment_text}
"""

# Respuestas de fallback cuando Groq no está disponible
FALLBACK_PRECIO = (
    "Los precios en tiempo real están en el vídeo 📊 "
    "¿Qué otras criptos te gustaría que analizáramos?"
)
FALLBACK_CRITICA = (
    "Gracias por compartir tu perspectiva, siempre se aprende de puntos de vista distintos. "
    "¿Qué argumentos te llevaron a esa conclusión?"
)
FALLBACK_GENERICO = (
    "Gracias por tu comentario, nos alegra que formes parte de la comunidad. "
    "¿Hay algún tema de crypto que te gustaría que exploráramos en profundidad?"
)


class AGORA(BaseAgent):
    """
    Monitorea y responde comentarios de YouTube para mantener la comunidad
    activa de CryptoVerdad. Máximo MAX_REPLIES_PER_RUN respuestas por llamada.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("AGORA")
        self._ensure_table()

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("[bold cyan]AGORA[/] iniciado")
        try:
            # 1. Construir servicio YouTube OAuth2
            service = self._build_youtube_service(ctx)
            if service is None:
                ctx.metadata["agora_skipped"] = "youtube_not_authenticated"
                return ctx

            # 2. Obtener canal del propietario de las credenciales
            channel_id = self._get_channel_id(service, ctx)
            if not channel_id:
                ctx.metadata["agora_skipped"] = "channel_not_found"
                return ctx

            # 3. Obtener vídeos recientes (últimos VIDEOS_LOOKBACK_DAYS días)
            recent_videos = self._get_recent_videos(service, channel_id, ctx)
            if not recent_videos:
                self.logger.info("[cyan]AGORA[/] sin vídeos recientes — skip")
                ctx.metadata["agora_skipped"] = "no_videos"
                return ctx

            # 4. Procesar comentarios de cada vídeo
            replies_count = 0
            summary_rows = []

            for video in recent_videos:
                if replies_count >= MAX_REPLIES_PER_RUN:
                    break

                video_id = video["id"]
                video_title = video.get("title", video_id)
                comments = self._get_new_comments(service, video_id, ctx)

                for comment in comments:
                    if replies_count >= MAX_REPLIES_PER_RUN:
                        break

                    comment_id = comment["id"]
                    author = comment.get("author", "usuario")
                    text = comment.get("text", "")

                    # 4a. Filtro: min palabras y no spam
                    if not self._is_valid_comment(text):
                        self.logger.info(
                            f"[cyan]AGORA[/] comentario ignorado (filtro): {comment_id}"
                        )
                        continue

                    # 4b. Ya respondido previamente
                    if self._already_replied(comment_id):
                        continue

                    # 4c. Generar respuesta con Groq
                    reply_text = self._generate_reply(text)

                    # 4d. Publicar respuesta en YouTube
                    published = self._post_reply(service, comment_id, reply_text, ctx)
                    if not published:
                        continue

                    # 4e. Persistir en SQLite
                    self._save_comment(
                        video_id=video_id,
                        comment_id=comment_id,
                        author=author,
                        text=text,
                        reply_text=reply_text,
                        pipeline_id=ctx.pipeline_id,
                    )

                    replies_count += 1
                    summary_rows.append((video_title[:40], author[:20], text[:60]))
                    self.logger.info(
                        f"[green]AGORA[/] respondido: {comment_id} en vídeo {video_id}"
                    )

            # 5. Actualizar contexto y mostrar resumen
            ctx.metadata["agora_comments_replied"] = replies_count
            self._print_summary(summary_rows, replies_count)

        except Exception as exc:
            self.logger.error(f"[red]AGORA error:[/] {exc}")
            ctx.add_error("AGORA", str(exc))

        return ctx

    # ── YouTube OAuth2 ────────────────────────────────────────────────────────
    def _build_youtube_service(self, ctx: Context):
        """
        Construye el servicio YouTube con OAuth2.
        Reutiliza la misma lógica de token que OLYMPUS.
        Devuelve None si no hay credenciales disponibles.
        """
        try:
            from googleapiclient.discovery import build
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
        except ImportError:
            msg = "Instala: google-api-python-client google-auth-oauthlib"
            self.logger.warning(f"[yellow]AGORA:[/] {msg}")
            ctx.add_warning("AGORA", msg)
            return None

        client_secret_path = os.getenv("YOUTUBE_CLIENT_SECRET_PATH", "")
        if not client_secret_path or not Path(client_secret_path).exists():
            self.logger.warning(
                f"[yellow]AGORA:[/] YOUTUBE_CLIENT_SECRET_PATH no válido: {client_secret_path!r}"
            )
            return None

        token_path = self._find_token_path()
        creds = None

        if token_path:
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)
            except Exception as exc:
                self.logger.warning(f"[yellow]AGORA[/] error leyendo token.json: {exc}")
                creds = None

        if not creds:
            self.logger.warning("[yellow]AGORA[/] token.json no encontrado — skip YouTube")
            return None

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    # Persistir token refrescado
                    save_path = token_path or (
                        Path(__file__).resolve().parent.parent.parent / "token.json"
                    )
                    try:
                        Path(save_path).write_text(creds.to_json(), encoding="utf-8")
                    except Exception:
                        pass
                except Exception as exc:
                    self.logger.warning(f"[yellow]AGORA[/] no se pudo refrescar token: {exc}")
                    return None
            else:
                self.logger.warning("[yellow]AGORA[/] credenciales inválidas — skip YouTube")
                return None

        try:
            return build("youtube", "v3", credentials=creds)
        except Exception as exc:
            self.logger.warning(f"[yellow]AGORA[/] no se pudo construir servicio: {exc}")
            return None

    def _find_token_path(self) -> Optional[Path]:
        """Busca token.json con la misma prioridad que OLYMPUS."""
        env_path = os.getenv("YOUTUBE_TOKEN_PATH", "")
        if env_path:
            p = Path(env_path)
            if p.exists():
                return p

        project_root = Path(__file__).resolve().parent.parent.parent
        candidate = project_root / "token.json"
        if candidate.exists():
            return candidate

        candidate = Path.cwd() / "token.json"
        if candidate.exists():
            return candidate

        return None

    # ── Canal ─────────────────────────────────────────────────────────────────
    def _get_channel_id(self, service, ctx: Context) -> Optional[str]:
        """Devuelve el channel_id del propietario de las credenciales OAuth2."""
        try:
            response = service.channels().list(
                part="id,snippet",
                mine=True,
            ).execute()
            items = response.get("items", [])
            if not items:
                self.logger.warning("[yellow]AGORA[/] no se encontró canal propio")
                return None
            channel_id = items[0]["id"]
            channel_name = items[0]["snippet"].get("title", channel_id)
            self.logger.info(f"[cyan]AGORA[/] canal: {channel_name} ({channel_id})")
            return channel_id
        except Exception as exc:
            self.logger.error(f"[red]AGORA[/] error obteniendo channel_id: {exc}")
            ctx.add_error("AGORA", f"get_channel_id: {exc}")
            return None

    # ── Vídeos recientes ──────────────────────────────────────────────────────
    def _get_recent_videos(self, service, channel_id: str, ctx: Context) -> list:
        """
        Devuelve lista de vídeos publicados en los últimos VIDEOS_LOOKBACK_DAYS días.
        Cada elemento: {"id": str, "title": str}
        """
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=VIDEOS_LOOKBACK_DAYS)
            published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

            response = service.search().list(
                part="id,snippet",
                channelId=channel_id,
                type="video",
                publishedAfter=published_after,
                order="date",
                maxResults=10,
            ).execute()

            videos = []
            for item in response.get("items", []):
                vid_id = item.get("id", {}).get("videoId")
                if vid_id:
                    videos.append({
                        "id": vid_id,
                        "title": item["snippet"].get("title", vid_id),
                    })

            self.logger.info(f"[cyan]AGORA[/] vídeos recientes encontrados: {len(videos)}")
            return videos

        except Exception as exc:
            self.logger.error(f"[red]AGORA[/] error obteniendo vídeos recientes: {exc}")
            ctx.add_error("AGORA", f"get_recent_videos: {exc}")
            return []

    # ── Comentarios nuevos ────────────────────────────────────────────────────
    def _get_new_comments(self, service, video_id: str, ctx: Context) -> list:
        """
        Devuelve comentarios de las últimas COMMENTS_LOOKBACK_HOURS horas
        que NO sean replies (topLevelComments) del vídeo dado.
        Cada elemento: {"id": str, "author": str, "text": str, "published_at": str}
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=COMMENTS_LOOKBACK_HOURS)
        comments = []

        try:
            page_token = None
            while True:
                kwargs = {
                    "part": "id,snippet",
                    "videoId": video_id,
                    "order": "time",
                    "maxResults": 50,
                    "textFormat": "plainText",
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                response = service.commentThreads().list(**kwargs).execute()

                for item in response.get("items", []):
                    top = item["snippet"]["topLevelComment"]
                    snippet = top["snippet"]
                    published_str = snippet.get("publishedAt", "")

                    # Parsear fecha y filtrar por antigüedad
                    try:
                        published_dt = datetime.fromisoformat(
                            published_str.replace("Z", "+00:00")
                        )
                    except Exception:
                        continue

                    if published_dt < cutoff:
                        # Los comentarios vienen en orden descendente;
                        # si este ya es demasiado antiguo, los siguientes también lo serán
                        return comments

                    comments.append({
                        "id": top["id"],
                        "author": snippet.get("authorDisplayName", "usuario"),
                        "text": snippet.get("textDisplay", ""),
                        "published_at": published_str,
                    })

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        except Exception as exc:
            # commentThreads.list puede dar 403 si los comentarios están desactivados
            self.logger.warning(
                f"[yellow]AGORA[/] error obteniendo comentarios de {video_id}: {exc}"
            )

        return comments

    # ── Filtros ───────────────────────────────────────────────────────────────
    def _is_valid_comment(self, text: str) -> bool:
        """Devuelve True si el comentario supera el filtro de calidad."""
        if not text or not text.strip():
            return False

        # Mínimo de palabras
        words = text.strip().split()
        if len(words) < MIN_COMMENT_WORDS:
            return False

        # Detección de spam
        text_lower = text.lower()
        for pattern in SPAM_PATTERNS:
            if pattern in text_lower:
                return False

        return True

    def _already_replied(self, comment_id: str) -> bool:
        """Devuelve True si ya existe una respuesta registrada para este comment_id."""
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM youtube_comments WHERE comment_id = ?",
                    (comment_id,),
                ).fetchone()
            return row is not None
        except Exception as exc:
            self.logger.warning(f"[yellow]AGORA[/] error verificando respuesta previa: {exc}")
            return False

    # ── Generación de respuesta con Groq ──────────────────────────────────────
    def _generate_reply(self, comment_text: str) -> str:
        """
        Genera una respuesta con Groq llama-3.3-70b-versatile.
        Aplica reglas de negocio antes de llamar al LLM.
        En caso de error devuelve un fallback.
        """
        comment_lower = comment_text.lower()

        # Reglas directas sin LLM para casos específicos
        precio_keywords = ["precio", "cuánto vale", "cuanto vale", "cuánto cuesta", "a cuanto"]
        critica_keywords = ["basura", "mentira", "fraude", "estafa", "no sirve", "malo"]
        timing_keywords = ["cuándo sube", "cuando sube", "cuándo baja", "cuando baja",
                           "va a subir", "va a bajar", "va subir", "va bajar"]

        if any(kw in comment_lower for kw in precio_keywords):
            return FALLBACK_PRECIO

        if any(kw in comment_lower for kw in critica_keywords):
            return FALLBACK_CRITICA

        if any(kw in comment_lower for kw in timing_keywords):
            return (
                "El análisis técnico nos da pistas sobre tendencias, "
                "pero el mercado crypto siempre guarda sorpresas 📊 "
                "¿Qué indicadores utilizas tú para tomar decisiones?"
            )

        # Llamada a Groq
        groq_api_key = os.getenv("GROQ_API_KEY", "")
        if not groq_api_key:
            self.logger.warning("[yellow]AGORA[/] GROQ_API_KEY no configurada — usando fallback")
            return FALLBACK_GENERICO

        try:
            from groq import Groq
            client = Groq(api_key=groq_api_key)
            prompt = GROQ_PROMPT_TEMPLATE.format(comment_text=comment_text[:500])

            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=120,
            )
            reply = completion.choices[0].message.content.strip()

            # Limitar longitud: YouTube acepta hasta 10.000 chars, pero 1-2 frases es suficiente
            if len(reply) > 500:
                reply = reply[:497] + "..."

            return reply

        except Exception as exc:
            self.logger.warning(f"[yellow]AGORA[/] Groq error: {exc} — usando fallback")
            return FALLBACK_GENERICO

    # ── Publicar respuesta en YouTube ─────────────────────────────────────────
    def _post_reply(
        self, service, parent_comment_id: str, reply_text: str, ctx: Context
    ) -> bool:
        """
        Publica reply_text como respuesta al comentario parent_comment_id.
        Devuelve True si se publicó correctamente.
        """
        if ctx.dry_run:
            self.logger.info(
                f"[yellow]AGORA[/] [DRY-RUN] respuesta simulada para {parent_comment_id}"
            )
            return True

        try:
            service.comments().insert(
                part="snippet",
                body={
                    "snippet": {
                        "parentId": parent_comment_id,
                        "textOriginal": reply_text,
                    }
                },
            ).execute()
            return True

        except Exception as exc:
            self.logger.warning(
                f"[yellow]AGORA[/] error publicando respuesta en {parent_comment_id}: {exc}"
            )
            return False

    # ── Persistencia ──────────────────────────────────────────────────────────
    def _ensure_table(self) -> None:
        """Crea la tabla youtube_comments si no existe."""
        try:
            with self.db._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS youtube_comments (
                        id          INTEGER   PRIMARY KEY AUTOINCREMENT,
                        video_id    TEXT,
                        comment_id  TEXT      UNIQUE,
                        author      TEXT,
                        text        TEXT,
                        reply_text  TEXT,
                        replied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        pipeline_id TEXT
                    )
                    """
                )
        except Exception as exc:
            self.logger.error(f"[red]AGORA[/] no se pudo crear tabla youtube_comments: {exc}")
            raise

    def _save_comment(
        self,
        video_id: str,
        comment_id: str,
        author: str,
        text: str,
        reply_text: str,
        pipeline_id: str,
    ) -> None:
        """Persiste un comentario respondido en youtube_comments."""
        try:
            with self.db._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO youtube_comments
                        (video_id, comment_id, author, text, reply_text, pipeline_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (video_id, comment_id, author, text, reply_text, pipeline_id),
                )
        except Exception as exc:
            self.logger.error(f"[red]AGORA[/] error guardando comentario {comment_id}: {exc}")

    # ── Resumen rich ──────────────────────────────────────────────────────────
    def _print_summary(self, rows: list, total: int) -> None:
        """Muestra una tabla rich con el resumen de respuestas publicadas."""
        if not rows:
            console.print(
                "[bold cyan]AGORA[/] Sin comentarios nuevos que responder en esta ejecución."
            )
            return

        table = Table(
            title=f"[bold cyan]AGORA[/] Comentarios respondidos ({total})",
            show_header=True,
            header_style="bold white on #0A0A0A",
        )
        table.add_column("Video", style="yellow", min_width=20)
        table.add_column("Autor", style="cyan", min_width=15)
        table.add_column("Comentario (extracto)", style="white", min_width=40)

        for video_title, author, comment_excerpt in rows:
            table.add_row(video_title, author, comment_excerpt)

        console.print(table)
