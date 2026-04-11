import torch
from diffusers import StableDiffusionPipeline
from pathlib import Path

PROMPT = (
    "photorealistic male news anchor, 38 years old, "
    "dark navy suit, white shirt, serious professional expression, "
    "looking directly at camera, studio lighting, sharp focus, 8k, "
    "close up portrait from chest up, shoulders and face visible, "
    "neutral light gray background, front facing, hyperrealistic skin"
)
NEG = (
    "cartoon, anime, ugly, blurry, deformed, watermark, text, "
    "extra limbs, bad anatomy, low quality, disfigured, "
    "full body, legs, feet, hands below waist, wide shot"
)

pipe = StableDiffusionPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5",
    torch_dtype=torch.float16,
    safety_checker=None,
)
pipe = pipe.to("cuda")
pipe.enable_attention_slicing()

generator = torch.Generator("cuda").manual_seed(77)
image = pipe(
    PROMPT,
    negative_prompt=NEG,
    num_inference_steps=50,
    guidance_scale=9.0,
    width=512,
    height=768,
    generator=generator,
).images[0]

out = Path(r"C:\Users\Usuario\nexus\assets\avatar_carlos_base.png")
image.save(out)
print(f"Guardado: {out} ({out.stat().st_size//1024}KB)")
