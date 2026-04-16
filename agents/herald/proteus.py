"""
agents/herald/proteus.py
PROTEUS — Channel Manager y Repurposing Engine de NEXUS.
Convierte un vídeo largo en contenido para múltiples plataformas.
"""

import re
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.panel import Panel

from core.context import Context
from core.base_agent import BaseAgent
from database.db import DBManager
from utils.logger import get_logger

console = Console()


class PROTEUS(BaseAgent):
    """
    Repurposing Engine: 1 vídeo largo -> múltiples formatos.

    Genera:
    - Hilo Twitter/X (5 tweets desde los insights más poderosos)
    - Post LinkedIn (versión profesional del análisis)
    - Artículo HTML de blog (transcript limpiado + SEO)

    Output guardado en output/repurposed/{pipeline_id}/
    Paths guardados en ctx.twitter_thread_path, ctx.linkedin_post_path,
    ctx.blog_article_path.
    """

    def __init__(self, config: dict, db: DBManager):
        super().__init__(config)
        self.db = db
        self.logger = get_logger("PROTEUS")
        self._output_dir = Path(__file__).resolve().parents[2] / "output" / "repurposed"
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ── extracción de insights ────────────────────────────────────────────────
    def _extract_key_insights(self, script: str, n: int = 5) -> list:
        """
        Extrae los N insights más poderosos del script.
        Busca frases con datos concretos (precios, porcentajes, fechas).
        """
        # Limpiar marcadores del script
        clean = re.sub(
            r'\[(?:PRECIO|ANÁLISIS|NOTICIA|PAUSA|PAUSA_LARGA'
            r'|SEÑALA:[^\]]+|GENERAL|DATO:[^\]]+)\]',
            '',
            script,
        )

        # Dividir en frases/párrafos
        sentences = [
            s.strip()
            for s in re.split(r'[.!?]+', clean)
            if len(s.strip()) > 30
        ]

        def _score(s: str) -> int:
            pts = 0
            if re.search(r'\$[\d,]+|\d+\s*(?:mil|millones|%)', s):
                pts += 3
            if re.search(r'\b\d{4}\b', s):
                pts += 1
            if any(w in s.lower() for w in ['importante', 'crítico', 'clave',
                                              'nunca', 'siempre', 'jamás']):
                pts += 2
            if len(s) > 80:
                pts += 1
            return pts

        scored = sorted(sentences, key=_score, reverse=True)
        return scored[:n]

    # ── Twitter/X thread ─────────────────────────────────────────────────────
    def _generate_twitter_thread(self, ctx: Context) -> str:
        """Genera hilo de 5 tweets desde el script."""
        insights = self._extract_key_insights(ctx.script or "", n=5)
        if not insights:
            return ""

        btc_price = getattr(ctx, 'btc_price', 0)
        title = getattr(ctx, 'seo_title', ctx.topic or "")

        tweets = []

        # Tweet 1: Hook con dato impactante
        tweets.append(
            f"Hilo: {title}\n\n"
            f"BTC: ${btc_price:,.0f}\n\n"
            f"Lo que el 95% no sabe sobre esto"
        )

        # Tweets 2-4: insights
        for i, insight in enumerate(insights[:3], 2):
            text = insight[:220] + "..." if len(insight) > 220 else insight
            tweets.append(f"{i}/ {text}")

        # Tweet 5: CTA
        yt = ctx.youtube_url or "https://youtube.com/@CryptoVerdad"
        tweets.append(
            f"5/ Análisis completo en YouTube\n\n"
            f"{yt}\n\n"
            f"¿Qué opinas? #Bitcoin #BTC #Crypto #CryptoVerdad"
        )

        return "\n\n---\n\n".join(tweets)

    # ── LinkedIn post ─────────────────────────────────────────────────────────
    def _generate_linkedin_post(self, ctx: Context) -> str:
        """Genera post de LinkedIn en tono formal."""
        insights = self._extract_key_insights(ctx.script or "", n=3)
        btc = getattr(ctx, 'btc_price', 0)
        title = getattr(ctx, 'seo_title', ctx.topic or "")

        lines = [
            f"{title}",
            "",
            f"Bitcoin cotiza hoy en ${btc:,.0f}.",
            "",
            "Los 3 puntos clave que todo inversor debe conocer:",
            "",
        ]

        for i, insight in enumerate(insights[:3], 1):
            lines.append(f"{i}. {insight[:200]}")
            lines.append("")

        yt = ctx.youtube_url or "https://youtube.com/@CryptoVerdad"
        lines += [
            "Análisis completo disponible en YouTube:",
            yt,
            "",
            "#Bitcoin #Criptomonedas #AnalisisTecnico #CryptoVerdad #Inversion",
        ]

        return "\n".join(lines)

    # ── artículo HTML de blog ─────────────────────────────────────────────────
    def _generate_blog_article(self, ctx: Context) -> str:
        """Genera artículo HTML completo desde el script."""
        script_clean = re.sub(
            r'\[(?:PRECIO|ANÁLISIS|NOTICIA|PAUSA[^\]]*'
            r'|SEÑALA:[^\]]+|GENERAL|DATO:[^\]]+)\]',
            '',
            ctx.script or "",
        ).strip()

        title       = getattr(ctx, 'seo_title', ctx.topic or "Análisis Bitcoin")
        description = (getattr(ctx, 'seo_description', '') or '')[:300]
        youtube_url = ctx.youtube_url or ''
        btc         = getattr(ctx, 'btc_price', 0)
        date_str    = datetime.now().strftime("%d de %B de %Y")

        # Convertir párrafos a HTML (máx 20 párrafos)
        paragraphs = [
            f"<p>{p.strip()}</p>"
            for p in script_clean.split('\n\n')
            if p.strip()
        ][:20]
        body_html = "\n".join(paragraphs)

        # Embed de YouTube si disponible
        youtube_embed = ""
        if youtube_url and "youtu.be/" in youtube_url:
            vid_id = youtube_url.split("youtu.be/")[-1].split("?")[0]
            youtube_embed = (
                '<div class="video-container" '
                'style="position:relative;padding-bottom:56.25%;height:0;">\n'
                f'  <iframe src="https://www.youtube.com/embed/{vid_id}" '
                'style="position:absolute;top:0;left:0;width:100%;height:100%;" '
                'frameborder="0" allowfullscreen></iframe>\n</div>'
            )

        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} | CryptoVerdad</title>
  <meta name="description" content="{description}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{description}">
  <meta name="robots" content="index, follow">
  <style>
    body  {{ font-family: 'Segoe UI', sans-serif; background: #0A0A0A; color: #fff;
             max-width: 800px; margin: 0 auto; padding: 20px; }}
    h1   {{ color: #F7931A; }}
    h2   {{ color: #F7931A; border-bottom: 1px solid #333; }}
    p    {{ line-height: 1.7; color: #ccc; }}
    a    {{ color: #F7931A; }}
    .price      {{ background: #1a1a1a; padding: 10px 20px;
                   border-left: 4px solid #F7931A; margin: 20px 0; }}
    .disclaimer {{ background: #1a1a1a; padding: 15px; border: 1px solid #333;
                   font-size: 0.85em; color: #888; }}
  </style>
</head>
<body>
  <header>
    <p><a href="/">← CryptoVerdad</a></p>
  </header>
  <article>
    <h1>{title}</h1>
    <p style="color:#888;">Por Carlos · CryptoVerdad · {date_str}</p>
    <div class="price">
      <strong>Bitcoin:</strong> ${btc:,.0f} USD ·
      <a href="{youtube_url}">Ver análisis en YouTube →</a>
    </div>
    {youtube_embed}
    <div class="content">
      {body_html}
    </div>
    <div class="disclaimer">
      Este contenido es exclusivamente educativo e informativo.
      No constituye asesoramiento financiero.
      Invierte solo lo que puedas permitirte perder.
    </div>
  </article>
</body>
</html>"""
        return html

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self, ctx: Context) -> Context:
        self.logger.info("PROTEUS iniciado — repurposing multi-plataforma")
        console.print(Panel(
            "[bold cyan]PROTEUS[/] — Repurposing Engine",
            border_style="cyan",
        ))

        try:
            if not ctx.script:
                self.logger.warning("PROTEUS: sin script, omitiendo repurposing")
                return ctx

            pid     = ctx.pipeline_id[:8]
            out_dir = self._output_dir / ctx.pipeline_id
            out_dir.mkdir(parents=True, exist_ok=True)

            # 1. Twitter/X thread
            try:
                thread = self._generate_twitter_thread(ctx)
                if thread:
                    path = out_dir / "twitter_thread.txt"
                    path.write_text(thread, encoding="utf-8")
                    ctx.twitter_thread_path = str(path)
                    self.logger.info(f"Twitter thread: {path}")
            except Exception as exc:
                self.logger.warning(f"Twitter thread falló: {exc}")

            # 2. LinkedIn post
            try:
                linkedin = self._generate_linkedin_post(ctx)
                if linkedin:
                    path = out_dir / "linkedin_post.txt"
                    path.write_text(linkedin, encoding="utf-8")
                    ctx.linkedin_post_path = str(path)
                    self.logger.info(f"LinkedIn post: {path}")
            except Exception as exc:
                self.logger.warning(f"LinkedIn post falló: {exc}")

            # 3. Artículo HTML de blog
            try:
                article = self._generate_blog_article(ctx)
                if article:
                    path = out_dir / f"{pid}_article.html"
                    path.write_text(article, encoding="utf-8")
                    ctx.blog_article_path = str(path)
                    self.logger.info(f"Blog article: {path}")
            except Exception as exc:
                self.logger.warning(f"Blog article falló: {exc}")

            twitter_ok  = "ok" if ctx.twitter_thread_path  else "sin datos"
            linkedin_ok = "ok" if ctx.linkedin_post_path   else "sin datos"
            blog_ok     = "ok" if ctx.blog_article_path    else "sin datos"

            console.print(
                f"[green]PROTEUS completado[/] — "
                f"Twitter: {twitter_ok} | "
                f"LinkedIn: {linkedin_ok} | "
                f"Blog: {blog_ok}"
            )

        except Exception as exc:
            self.logger.error(f"PROTEUS error: {exc}")
            ctx.add_error("PROTEUS", str(exc))

        return ctx
