#!/bin/bash
# Setup SadTalker para NEXUS CryptoVerdad
# Ejecutar desde la raiz del proyecto: bash scripts/setup_sadtalker.sh

set -e

NEXUS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$NEXUS_DIR"

echo "=== SadTalker setup para NEXUS ==="
echo "Directorio: $NEXUS_DIR"

# 1. Clonar SadTalker si no existe
if [ -d "$NEXUS_DIR/sadtalker" ]; then
    echo "sadtalker/ ya existe — omitiendo clone."
else
    git clone https://github.com/OpenTalker/SadTalker.git sadtalker
    echo "SadTalker clonado en sadtalker/"
fi

# 2. PyTorch con CUDA 12.6 (cu126 soporta Python 3.14+; RTX 3050 compatible)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126

# 3. Dependencias de SadTalker (sin pins estrictos — incompatibles con Python 3.14)
# basicsr requiere parche en setup.py (bug KeyError __version__ con Python 3 exec scope)
pip install numpy scipy scikit-image librosa kornia face_alignment \
    imageio imageio-ffmpeg numba resampy pydub tqdm yacs pyyaml \
    joblib facexlib gradio av safetensors

# basicsr: clonar, parchear get_version() y compilar
TMP_BASICSR=$(mktemp -d)
git clone https://github.com/XPixelGroup/BasicSR.git "$TMP_BASICSR"
sed -i 's/exec(compile(f.read(), version_file, .exec.))\n.*return locals\(\)\[.__version__.\]//' "$TMP_BASICSR/setup.py" || true
python - <<'PYEOF'
import re, pathlib
f = pathlib.Path("$TMP_BASICSR/setup.py")
code = f.read_text()
code = code.replace(
    "    with open(version_file, 'r') as f:\n        exec(compile(f.read(), version_file, 'exec'))\n    return locals()['__version__']",
    "    _vars = {}\n    with open(version_file, 'r') as f:\n        exec(compile(f.read(), version_file, 'exec'), _vars)\n    return _vars['__version__']"
)
f.write_text(code)
PYEOF
pip install "$TMP_BASICSR"
pip install gfpgan

# 4. Recordatorio de checkpoints
echo ""
echo "=== IMPORTANTE ==="
echo "Descarga los checkpoints de SadTalker y colocalos en:"
echo "  $NEXUS_DIR/sadtalker/checkpoints/"
echo ""
echo "Puedes descargarlos desde:"
echo "  https://github.com/OpenTalker/SadTalker#pretrained-weights-and-other-resources"
echo ""
echo "SadTalker instalado correctamente. HEPHAESTUS lo usara automaticamente."
