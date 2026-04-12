FROM python:3.11-slim

# Variables de entorno para evitar problemas de encoding en producción
ENV PYTHONIOENCODING=utf-8
ENV PYTHONUTF8=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar ffmpeg y dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    fonts-dejavu \
    fonts-liberation \
    espeak-ng \
    libespeak-ng1 \
    espeak \
    libespeak1 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# tiktok-uploader (Playwright headless) + gTTS fallback TTS + ElevenLabs TTS
RUN pip install tiktok-uploader gTTS==2.5.1 google-generativeai elevenlabs

# Kokoro TTS — motor de voz local, sin API, funciona offline en Railway
RUN pip install kokoro-onnx soundfile

# Descargar modelos Kokoro durante el build (evita latencia en producción)
# hexgrad/Kokoro-82M es público en HuggingFace (~300MB onnx + ~10MB voices)
RUN mkdir -p /app/models && \
    wget -q -O /app/models/kokoro-v0_19.onnx \
      "https://huggingface.co/hexgrad/Kokoro-82M/resolve/main/kokoro-v0_19.onnx" && \
    wget -q -O /app/models/voices.bin \
      "https://huggingface.co/hexgrad/Kokoro-82M/resolve/main/voices.bin" && \
    echo "Kokoro models downloaded: $(du -sh /app/models/)"

# Playwright (chromium para tiktok-uploader) — deps instalados manualmente arriba
RUN playwright install chromium

# Copiar código fuente
COPY . .

# Directorios de salida persistentes (montar como volumen en producción)
RUN mkdir -p output/audio output/video output/thumbnails output/charts assets \
    && chmod -R 777 output/

EXPOSE 8080

CMD ["python", "main.py", "--auto"]
