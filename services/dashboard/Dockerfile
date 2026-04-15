# Stage 1: compilar React
FROM node:20-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build
# Resultado en /frontend/dist

# Stage 2: servidor Python con el build estatico
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# shared/ ya esta dentro del servicio (copiado por CI antes del build)
COPY . .
COPY --from=frontend-builder /frontend/dist /app/static
ENV PYTHONPATH=/app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
