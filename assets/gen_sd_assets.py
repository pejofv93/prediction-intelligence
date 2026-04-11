"""
assets/gen_sd_assets.py
Genera studio_background.png y avatar_carlos_base.png con Stable Diffusion.

Uso:
    python assets/gen_sd_assets.py

Requisitos:
    pip install diffusers transformers accelerate
    GPU con CUDA (RTX 3050 con 6GB VRAM es suficiente con float16)

Outputs:
    assets/studio_background.png  — fondo de estudio 1920x1080
    assets/avatar_carlos_base.png — avatar presentador 512x768
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

STUDIO_OUT = ASSETS / "studio_background.png"
AVATAR_OUT = ASSETS / "avatar_carlos_base.png"


def _load_pipe():
    from diffusers import StableDiffusionPipeline
    import torch

    print("[SD] Cargando runwayml/stable-diffusion-v1-5 ...")
    pipe = StableDiffusionPipeline.from_pretrained(
        "runwayml/stable-diffusion-v1-5",
        torch_dtype=torch.float16,
        safety_checker=None,
    )
    pipe = pipe.to("cuda")
    pipe.enable_attention_slicing()
    print("[SD] Modelo cargado en GPU.")
    return pipe


def gen_studio_bg(pipe) -> bool:
    """Genera fondo de estudio profesional. Devuelve True si ok."""
    from PIL import Image

    prompt = (
        "professional TV news studio, crypto financial news set, "
        "large LED screens on wall showing orange bitcoin price charts, "
        "dark background with blue and orange accent lighting, "
        "anchor desk in foreground, cinematic, photorealistic, 8k, "
        "broadcast quality, CNN Bloomberg style"
    )
    negative = (
        "cartoon, anime, ugly, blurry, low quality, text, watermark, "
        "people, person, human"
    )

    try:
        print("[SD] Generando studio_background.png (768x512) ...")
        result = pipe(
            prompt=prompt,
            negative_prompt=negative,
            width=768,
            height=512,
            num_inference_steps=30,
            guidance_scale=7.5,
        )
        img = result.images[0]

        # Upscale a 1920x1080 con Lanczos
        img_large = img.resize((1920, 1080), Image.LANCZOS)
        img_large.save(str(STUDIO_OUT))
        print(f"[SD] studio_background.png guardado -> {STUDIO_OUT} ({img_large.size})")
        return True

    except Exception as e:
        print(f"[SD] ERROR studio_background: {e}")
        return False


def gen_avatar_carlos(pipe) -> bool:
    """Genera avatar Carlos presentador. Devuelve True si ok."""
    from PIL import Image

    prompt = (
        "photorealistic male TV news anchor, 38 years old, short dark hair, "
        "dark navy suit, white shirt, serious professional expression, "
        "looking directly at camera, broadcasting, studio lighting, "
        "sharp focus, 8k portrait, professional broadcaster, "
        "crypto financial analyst, half body shot"
    )
    negative = (
        "cartoon, anime, ugly, blurry, deformed, extra limbs, bad anatomy, "
        "drawing, illustration, painting, watermark, text, logo, "
        "disfigured, mutated, extra fingers"
    )

    try:
        print("[SD] Generando avatar_carlos_base.png (512x768) ...")
        result = pipe(
            prompt=prompt,
            negative_prompt=negative,
            width=512,
            height=768,
            num_inference_steps=35,
            guidance_scale=8.0,
        )
        img = result.images[0]
        img.save(str(AVATAR_OUT))
        print(f"[SD] avatar_carlos_base.png guardado -> {AVATAR_OUT} ({img.size})")
        return True

    except Exception as e:
        print(f"[SD] ERROR avatar_carlos: {e}")
        return False


def main():
    import torch

    if not torch.cuda.is_available():
        print("[SD] CUDA no disponible. Ejecuta este script en un entorno con GPU.")
        sys.exit(1)

    try:
        pipe = _load_pipe()
    except Exception as e:
        print(f"[SD] No se pudo cargar el modelo: {e}")
        print("[SD] Asegurate de tener conexion a internet para descargar el modelo (~4GB).")
        sys.exit(1)

    ok_studio = gen_studio_bg(pipe)
    ok_avatar = gen_avatar_carlos(pipe)

    if ok_studio and ok_avatar:
        print("\n[SD] Assets generados correctamente.")
    else:
        print("\n[SD] Algunos assets fallaron. Revisa los errores anteriores.")
        sys.exit(1)


if __name__ == "__main__":
    main()
