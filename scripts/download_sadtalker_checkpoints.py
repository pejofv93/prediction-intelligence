"""
Descarga los checkpoints de SadTalker desde GitHub Releases.
Uso: python scripts/download_sadtalker_checkpoints.py
"""
import requests
import sys
from pathlib import Path

SADTALKER_DIR = Path(__file__).parent.parent / "sadtalker"
CHECKPOINTS_DIR = SADTALKER_DIR / "checkpoints"
GFPGAN_DIR = SADTALKER_DIR / "gfpgan" / "weights"

CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
GFPGAN_DIR.mkdir(parents=True, exist_ok=True)

FILES = [
    # Checkpoints principales
    (
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/SadTalker_V0.0.2_256.safetensors",
        CHECKPOINTS_DIR / "SadTalker_V0.0.2_256.safetensors",
    ),
    (
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/SadTalker_V0.0.2_512.safetensors",
        CHECKPOINTS_DIR / "SadTalker_V0.0.2_512.safetensors",
    ),
    (
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/mapping_00109-model.pth.tar",
        CHECKPOINTS_DIR / "mapping_00109-model.pth.tar",
    ),
    (
        "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/mapping_00229-model.pth.tar",
        CHECKPOINTS_DIR / "mapping_00229-model.pth.tar",
    ),
    # GFPGAN / facexlib weights
    (
        "https://github.com/xinntao/facexlib/releases/download/v0.1.0/alignment_WFLW_4HG.pth",
        GFPGAN_DIR / "alignment_WFLW_4HG.pth",
    ),
    (
        "https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth",
        GFPGAN_DIR / "detection_Resnet50_Final.pth",
    ),
    (
        "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth",
        GFPGAN_DIR / "GFPGANv1.4.pth",
    ),
    (
        "https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth",
        GFPGAN_DIR / "parsing_parsenet.pth",
    ),
]


def download(url: str, dest: Path):
    if dest.exists():
        print(f"  [SKIP] {dest.name} ya existe ({dest.stat().st_size // 1024 // 1024} MB)")
        return

    print(f"  [DL] {dest.name} ...", flush=True)
    resp = requests.get(url, stream=True, timeout=60,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    chunk_size = 1024 * 1024  # 1 MB

    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    mb = downloaded // 1024 // 1024
                    total_mb = total // 1024 // 1024
                    print(f"\r      {pct:3d}%  {mb}/{total_mb} MB", end="", flush=True)

    print(f"\r  [OK] {dest.name} ({downloaded // 1024 // 1024} MB)          ")


def main():
    print("=== Descargando checkpoints de SadTalker ===\n")
    errors = []
    for i, (url, dest) in enumerate(FILES, 1):
        print(f"[{i}/{len(FILES)}] {dest.parent.name}/{dest.name}")
        try:
            download(url, dest)
        except Exception as e:
            print(f"\n  [ERROR] {e}")
            errors.append((dest.name, str(e)))

    print("\n=== Resultado ===")
    for url, dest in FILES:
        size = f"{dest.stat().st_size // 1024 // 1024} MB" if dest.exists() else "FALTA"
        status = "OK" if dest.exists() else "ERROR"
        print(f"  [{status}] {dest.name}  ({size})")

    if errors:
        print(f"\n{len(errors)} error(s):")
        for name, err in errors:
            print(f"  - {name}: {err}")
        sys.exit(1)
    else:
        print("\nTodos los checkpoints descargados correctamente.")


if __name__ == "__main__":
    main()
