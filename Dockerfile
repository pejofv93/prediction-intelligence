FROM python:3.11-slim

# Variables de entorno para evitar problemas de encoding en producción
ENV PYTHONIOENCODING=utf-8
ENV PYTHONUTF8=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar ffmpeg y dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    fonts-dejavu \
    fonts-liberation \
    espeak \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright (chromium para tiktok-uploader)
RUN playwright install chromium --with-deps

# Copiar código fuente
COPY . .

# Directorios de salida persistentes (montar como volumen en producción)
RUN mkdir -p output/audio output/video output/thumbnails output/charts assets \
    && chmod -R 777 output/

EXPOSE 8080

CMD ["python", "main.py", "--auto"]
