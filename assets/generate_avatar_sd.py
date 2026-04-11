"""
Generador de avatar fotorrealista para Carlos (CryptoVerdad NEXUS)
Usa Stable Diffusion 1.5 via diffusers con float16 para RTX 3050.
Guarda en:
  - assets/avatar_carlos_base.png  (reemplaza actual)
  - assets/avatar_carlos_v2.png    (backup)
"""

import sys
import os

# Asegurar que la salida no tiene problemas de encoding en Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_BASE   = os.path.join(ASSETS_DIR, "avatar_carlos_base.png")
OUT_V2     = os.path.join(ASSETS_DIR, "avatar_carlos_v2.png")

POSITIVE_PROMPT = (
    "photorealistic male news anchor, 38 years old, short dark hair, "
    "dark navy suit, white shirt, serious professional expression, "
    "looking directly at camera, studio lighting, sharp focus, 8k portrait, "
    "professional broadcaster, neutral pose, front facing, shoulders visible, "
    "clean background, hyperrealistic skin texture, RAW photo, "
    "intricate details, cinematic lighting"
)

NEGATIVE_PROMPT = (
    "cartoon, anime, ugly, blurry, deformed, watermark, text, "
    "extra limbs, bad anatomy, low quality, disfigured, "
    "painting, illustration, drawing, cgi, render, "
    "overexposed, underexposed, mutated, out of frame"
)

def check_and_install(package_name, import_name=None):
    """Verifica si un paquete esta disponible."""
    if import_name is None:
        import_name = package_name
    try:
        __import__(import_name)
        return True
    except ImportError:
        return False

def main():
    print("[NEXUS] Generador de avatar SD para Carlos - CryptoVerdad")
    print("[NEXUS] Verificando dependencias...")

    # Verificar torch
    if not check_and_install("torch"):
        print("[ERROR] torch no instalado. Ejecutar:")
        print("  C:\\Python311\\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118")
        sys.exit(1)

    import torch
    print(f"[OK] torch {torch.__version__} disponible")

    cuda_ok = torch.cuda.is_available()
    if cuda_ok:
        device_name = torch.cuda.get_device_name(0)
        vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
        print(f"[OK] CUDA disponible: {device_name} ({vram_gb:.1f} GB VRAM)")
    else:
        print("[WARN] CUDA no disponible, usando CPU (sera lento)")

    # Verificar diffusers
    if not check_and_install("diffusers"):
        print("[ERROR] diffusers no instalado. Ejecutar:")
        print("  C:\\Python311\\python.exe -m pip install diffusers accelerate transformers")
        sys.exit(1)

    from diffusers import StableDiffusionPipeline
    print("[OK] diffusers disponible")

    # Cargar pipeline
    MODEL_ID = "runwayml/stable-diffusion-v1-5"
    print(f"[NEXUS] Cargando modelo: {MODEL_ID}")
    print("[NEXUS] (Primera vez puede tardar varios minutos descargando ~4GB)")

    try:
        if cuda_ok:
            pipe = StableDiffusionPipeline.from_pretrained(
                MODEL_ID,
                torch_dtype=torch.float16,
                safety_checker=None,
                requires_safety_checker=False,
            )
            pipe = pipe.to("cuda")
            # Optimizaciones para RTX 3050 (4-8 GB VRAM)
            try:
                pipe.enable_attention_slicing()
                print("[OK] Attention slicing activado (ahorro VRAM)")
            except Exception:
                pass
            try:
                pipe.enable_xformers_memory_efficient_attention()
                print("[OK] xformers memory efficient attention activado")
            except Exception:
                print("[INFO] xformers no disponible, usando atencion estandar")
        else:
            pipe = StableDiffusionPipeline.from_pretrained(
                MODEL_ID,
                safety_checker=None,
                requires_safety_checker=False,
            )
            pipe = pipe.to("cpu")

        print("[OK] Modelo cargado correctamente")

    except Exception as e:
        print(f"[ERROR] Fallo al cargar modelo: {e}")
        sys.exit(1)

    # Generar imagen
    import torch

    # Resolucion objetivo: 512x768 (retrato, compatible con SD 1.5)
    WIDTH  = 512
    HEIGHT = 768
    STEPS  = 45
    CFG    = 8.5
    SEED   = 42

    print(f"\n[NEXUS] Generando imagen {WIDTH}x{HEIGHT} con seed={SEED}...")
    print(f"        Steps: {STEPS}  |  CFG scale: {CFG}")
    print(f"        Device: {'cuda' if cuda_ok else 'cpu'}")

    generator = torch.Generator(device="cuda" if cuda_ok else "cpu").manual_seed(SEED)

    try:
        result = pipe(
            prompt=POSITIVE_PROMPT,
            negative_prompt=NEGATIVE_PROMPT,
            width=WIDTH,
            height=HEIGHT,
            num_inference_steps=STEPS,
            guidance_scale=CFG,
            generator=generator,
            num_images_per_prompt=1,
        )
        image = result.images[0]
        print("[OK] Imagen generada correctamente")

    except torch.cuda.OutOfMemoryError:
        print("[WARN] VRAM insuficiente para 512x768. Reintentando con 512x512...")
        torch.cuda.empty_cache()
        generator = torch.Generator(device="cuda").manual_seed(SEED)
        result = pipe(
            prompt=POSITIVE_PROMPT,
            negative_prompt=NEGATIVE_PROMPT,
            width=512,
            height=512,
            num_inference_steps=STEPS,
            guidance_scale=CFG,
            generator=generator,
        )
        image = result.images[0]
        print("[OK] Imagen 512x512 generada")

    except Exception as e:
        print(f"[ERROR] Fallo durante la generacion: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Guardar archivos
    try:
        image.save(OUT_BASE, "PNG")
        print(f"[OK] Guardado en: {OUT_BASE}")

        image.save(OUT_V2, "PNG")
        print(f"[OK] Copia backup en: {OUT_V2}")

        # Verificar tamaños
        size_base = os.path.getsize(OUT_BASE)
        size_v2   = os.path.getsize(OUT_V2)
        print(f"[OK] Tamaños: base={size_base//1024}KB  v2={size_v2//1024}KB")

        if size_base < 200 * 1024:
            print(f"[WARN] Archivo menor de 200KB ({size_base//1024}KB) - verificar calidad")
        else:
            print(f"[OK] Tamaño adecuado (>{size_base//1024}KB)")

    except Exception as e:
        print(f"[ERROR] Fallo al guardar: {e}")
        sys.exit(1)

    # Escribir archivo .carlos_ready
    output_dir = os.path.join(os.path.dirname(ASSETS_DIR), "output")
    os.makedirs(output_dir, exist_ok=True)
    ready_path = os.path.join(output_dir, ".carlos_ready")
    try:
        with open(ready_path, "w", encoding="utf-8") as f:
            f.write(f"CARLOS GENERADO\n{OUT_BASE}\n")
        print(f"[OK] Marcador escrito en: {ready_path}")
    except Exception as e:
        print(f"[WARN] No se pudo escribir .carlos_ready: {e}")

    print("\nCARLOS GENERADO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
