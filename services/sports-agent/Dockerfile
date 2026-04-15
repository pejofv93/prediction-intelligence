FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# shared/ ya esta dentro del servicio (copiado por CI/Makefile antes del build)
# COPY . . lo incluye — NO añadir COPY shared/ explicito
COPY . .
ENV PYTHONPATH=/app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
